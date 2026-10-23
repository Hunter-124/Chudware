"""Artifact registry (contract §3) — JSON records persisted under the project dir.

Each ``make_*`` appends one record; ``describe_project``, the preview pane and
telemetry read them. The record shape is the frozen §3 seam — do not change it without a
version bump. Records live in ``<project>/artifacts/registry.json`` (a JSON list); reads return
them newest-first.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import project
from .errors import DeliverableError


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def new_artifact_id(kind: str) -> str:
    return f"art_{kind}_{uuid.uuid4().hex[:12].upper()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_record(artifact_id: str, kind: str, out_abs: Path, run_id: str,
                 source_refs: list, spec_abs: Path) -> dict:
    """Assemble the frozen §3 artifact record; ``path`` / ``spec_ref`` are project-relative."""
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "kind": kind,
        "path": project.to_project_rel(out_abs),
        "created_ts": _now_iso(),
        "run_id": run_id,
        "source_refs": source_refs,
        "spec_ref": project.to_project_rel(spec_abs),
    }


def _read_all(path: Path | None = None) -> list[dict]:
    p = path or project.registry_path()
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliverableError(
            "registry_corrupt",
            f"{p.name} is malformed; repair or restore it before changing artifacts",
        ) from exc
    if not isinstance(data, list) or any(not isinstance(record, dict) for record in data):
        raise DeliverableError(
            "registry_corrupt",
            f"{p.name} must contain a JSON list of artifact records",
        )
    return data


def append_record(record: dict) -> None:
    p = project.registry_path()
    records = _read_all(p)
    records.append(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        temp.replace(p)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_records(run_id: str | None = None) -> list[dict]:
    """All records newest-first (contract §5.5). ``run_id`` filters to one run; unknown → []."""
    records = list(reversed(_read_all()))
    if run_id is not None:
        records = [r for r in records if r.get("run_id") == run_id]
    return records
