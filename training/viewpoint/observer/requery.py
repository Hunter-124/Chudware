"""Rebuild query identities and set-to-query mappings without touching rendered images."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from .queries import QUERY_SCHEMA, build_query_bank, queries_path


def _temp_jsonl(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return Path(name)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def requery_store(root, records, *, seed: int = 0) -> dict:
    """Rebuild all balanced queries; reassign qids by (part, mode, target instance).

    Old labels/checkpoints are moved to a dated archive on schema change. Existing set keys,
    cameras, image paths, face tokens and renders remain unchanged. Safe to run again with the
    same manifest/seed: if qids are identical, already-trained checkpoints are not invalidated.
    Caller must keep other store writers stopped during this migration.
    """
    root = Path(root).expanduser()
    qpath = queries_path(root)
    records = list(records)
    bank = build_query_bank(records, seed=seed)
    qids = {q.qid for q in bank}
    part_ids = {r["part_id"] for r in records}
    keyed = defaultdict(list)
    for q in bank:
        keyed[(q.part_id, q.mode, q.target_instance)].append(q.qid)
    keyed = {key: sorted(ids) for key, ids in keyed.items()}
    existing = []
    old_schema = None
    if qpath.exists():
        with qpath.open(encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                old_schema = row.get("schema") if old_schema is None else old_schema
                existing.append(row["qid"])
    changed = old_schema != QUERY_SCHEMA or set(existing) != qids
    paths = [qpath, root / "observer" / "sets.jsonl", root / "sets.jsonl",
             root / "sets_ab.jsonl"]
    staged = {}
    counts = {}
    missing = set()
    try:
        staged[qpath] = _temp_jsonl(qpath, (q.to_dict() for q in bank))
        for path in paths[1:]:
            if not path.exists():
                continue
            def updated():
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        rec = json.loads(line)
                        if rec["part_id"] not in part_ids:
                            missing.add(rec["part_id"])
                        key = (rec["part_id"], rec["mode"],
                               int(rec.get("target_instance", -1)) if rec["mode"] == "target"
                               else -1)
                        rec["qids"] = keyed.get(key, [])
                        name = str(path.relative_to(root))
                        counts[name] = counts.get(name, 0) + 1
                        yield rec
            staged[path] = _temp_jsonl(path, updated())
        # Check the manifest covers every stored part before changing *any* file.
        if missing:
            raise ValueError(f"manifest missing {len(missing)} stored parts: {sorted(missing)[:5]}")
        archived = []
        if changed:
            archive = root / "query_archive" / time.strftime("%Y%m%d-%H%M%S")
            suffix = 0
            while archive.exists():
                suffix += 1
                archive = root / "query_archive" / f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}"
            stale = [qpath, *root.glob("labels_*.jsonl"),
                     *root.glob("observer/labels_*.jsonl"), root / "observer" / "report.json"]
            for folder in (root / "observer").iterdir():
                if folder.is_dir():
                    stale.extend(folder.glob("fold*.pt"))
                    stale.extend(folder.glob("folds.json"))
            for path in stale:
                if path.exists():
                    dst = archive / path.relative_to(root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dst))
                    archived.append(str(dst.relative_to(root)))
        for path, tmp in staged.items():
            os.replace(tmp, path)
        return {"queries": len(bank), "sets": counts, "changed": changed,
                "archived": archived}
    finally:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)
