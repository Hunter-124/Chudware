"""Build the chudware.geomlabel/1 manifest (contracts/geom_labels.md) from fetched sources.

Each fetched part (``python -m training.viewpoint.datasets.fetch``), each engine-generated GSL
sample and each part of our MFCAD++ generator port (``mfcadpp_gen``, generated on demand in
worker processes) is imported with the real engine, its source labels are matched onto the
engine's faces by geometry (importers.label_part), and one JSONL record per kept part is
written, sorted by part_id, with a deterministic sha1 split. Beside the manifest:

    geomlabel.summary.json   parts, per-source keep/drop counts and reasons, match rate,
                             CANON face/feature histograms, split counts
    geomlabel.reports.jsonl  one match report per attempted part (unmatched faces both sides)

A run that dies midway resumes from ``<manifest>.partial`` (already-labelled part_ids are
skipped); ``--fresh`` discards it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import fetch, importers
from . import mfcadpp_gen

MANIFEST_NAME = "geomlabel.jsonl"
SUMMARY_SCHEMA = "chudware.geomlabel.summary/1"
KNOWN_SOURCES = ("mfcad", "mfcadpp", "mfinstseg", "fusion360_seg", "gsl", "mfcadpp_gen")
DEFAULT_SOURCES = ("mfcad", "gsl", "mfcadpp_gen")
GENERATED_SOURCES = ("gsl", "mfcadpp_gen")      # made here, not fetched: no fetch.SOURCES row
GSL_LICENSE_ROW = {"license": importers.GSL_LICENSE, "commercial_use": "yes"}
MFCADPP_GEN_LICENSE_ROW = {"license": mfcadpp_gen.LICENSE, "commercial_use": "yes"}
MFCADPP_GEN_COUNT = 3000


def default_manifest_path() -> Path:
    """$CHUDWARE_VIEWNET_DATA/manifests/geomlabel.jsonl — the consumers' default."""
    return fetch.data_root() / "manifests" / MANIFEST_NAME


def summary_path(manifest: Path) -> Path:
    return manifest.with_name(manifest.stem + ".summary.json")


def reports_path(manifest: Path) -> Path:
    return manifest.with_name(manifest.stem + ".reports.jsonl")


def _default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


def iter_parts(source: str, *, limit: Optional[int], gsl_families: list, gsl_count: int,
               gsl_seed: int, gsl_dir: Path, gen_count: int = MFCADPP_GEN_COUNT,
               gen_seed: int = 0, gen_dir: Optional[Path] = None,
               gen_workers: Optional[int] = None,
               log: Callable[[str], None] = print) -> Iterator[tuple]:
    """(part_id, loader) pairs; the loader reads/generates the SourcePart lazily so one bad
    part is reported without stopping the run."""
    if source == "gsl":
        for fam in gsl_families:
            for seed in range(gsl_seed, gsl_seed + gsl_count):
                step = gsl_dir / fam / f"{seed}.step"

                def load(fam=fam, seed=seed, step=step):
                    if step.exists() and importers.gsl_sidecar(step).exists():
                        return importers.load_gsl_part(step)
                    return importers.gsl_source_part(fam, seed, gsl_dir)
                yield f"gsl/{fam}/{seed}", load
        return
    if source == "mfcadpp_gen":
        from .mfcadpp_gen.runner import generate  # noqa: PLC0415
        out_dir = gen_dir or mfcadpp_gen.default_dir()
        seeds = range(gen_seed, gen_seed + gen_count)
        status = generate(seeds, out_dir, workers=gen_workers or _default_workers(), log=log)
        for seed in seeds:
            st = status[seed]

            def load(seed=seed, st=st):
                if not st["ok"]:
                    raise importers.LabelError(f"generation failed: {st['reason']}")
                return importers.read_mfcadpp_gen(mfcadpp_gen.step_path(out_dir, seed))
            yield f"mfcadpp_gen/{seed}", load
        return
    parts = fetch.local_parts(source)
    if limit is not None:
        parts = parts[:limit]
    for lp in parts:
        if source == "mfcad":
            def load(lp=lp):
                return importers.read_mfcad(lp.step, lp.companions[0], lp.original_id)
        elif source == "mfcadpp":
            def load(lp=lp):
                return importers.read_mfcadpp(lp.step, lp.original_id)
        elif source == "mfinstseg":
            def load(lp=lp):
                return importers.read_mfinstseg(lp.step, lp.companions[0], lp.original_id)
        elif source == "fusion360_seg":
            def load(lp=lp):
                seg = next(c for c in lp.companions if c.suffix == ".seg")
                return importers.read_fusion_seg(lp.step, seg, lp.shared.get("segment_names.json"),
                                                 lp.original_id)
        else:
            raise ValueError(f"unknown source {source!r}")
        yield f"{source}/{lp.original_id}", load


def _read_partial(path: Path) -> dict:
    done = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["report"]["part_id"]] = row
    return done


def summarize(rows: list, manifest: Path, sources: list) -> dict:
    per = defaultdict(lambda: {"attempted": 0, "kept": 0, "dropped": 0,
                               "drop_reasons": Counter(), "match_rates": []})
    face_hist, feat_hist, splits = Counter(), Counter(), Counter()
    unmatched = 0
    for row in rows:
        rep, rec = row["report"], row["record"]
        src = rep["part_id"].split("/", 1)[0]
        s = per[src]
        s["attempted"] += 1
        if rep.get("match_rate") is not None:
            s["match_rates"].append(rep["match_rate"])
        if rec is None:
            s["dropped"] += 1
            s["drop_reasons"][str(rep.get("dropped") or "unknown").split(" ", 1)[0]] += 1
            continue
        s["kept"] += 1
        splits[rec["split"]] += 1
        face_hist.update(f["class"] for f in rec["faces"])
        feat_hist.update(f["class"] for f in rec["features"])
        unmatched += rec["stats"].get("n_unmatched", 0)
    all_rates = [r for s in per.values() for r in s["match_rates"]]
    out_sources = {}
    for src in sources:
        s = per[src]
        rates = s.pop("match_rates")
        if src == "gsl":
            lic = GSL_LICENSE_ROW
        elif src == "mfcadpp_gen":
            lic = MFCADPP_GEN_LICENSE_ROW
        else:
            lic = {"license": fetch.SOURCES[src].license,
                   "commercial_use": fetch.SOURCES[src].commercial_use}
        out_sources[src] = dict(s, drop_reasons=dict(s["drop_reasons"]), **lic,
                                match_rate_mean=round(sum(rates) / len(rates), 6) if rates else None,
                                match_rate_min=min(rates) if rates else None)
    return {
        "schema": SUMMARY_SCHEMA,
        "manifest": str(manifest),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "parts": sum(s["kept"] for s in out_sources.values()),
        "sources": out_sources,
        "splits": dict(splits),
        "match_rate": {"mean": round(sum(all_rates) / len(all_rates), 6) if all_rates else None,
                       "min": min(all_rates) if all_rates else None},
        "min_match_rate": importers.MIN_MATCH_RATE,
        "face_class_histogram": {c: face_hist[c] for c in importers.CANON if face_hist[c]},
        "feature_class_histogram": {c: feat_hist[c] for c in importers.CANON if feat_hist[c]},
        "unmatched_engine_faces_in_kept_parts": unmatched,
    }


def build(sources: list, out: Path, *, limit: Optional[int], gsl_families: list, gsl_count: int,
          gsl_seed: int, gsl_dir: Path, min_match: float, fresh: bool,
          gen_count: int = MFCADPP_GEN_COUNT, gen_seed: int = 0,
          gen_dir: Optional[Path] = None, gen_workers: Optional[int] = None,
          log: Callable[[str], None] = print) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    if fresh and partial.exists():
        partial.unlink()
    done = _read_partial(partial)
    rows = []
    with open(partial, "a") as sink:
        for src in sources:
            n = 0
            for part_id, load in iter_parts(src, limit=limit, gsl_families=gsl_families,
                                            gsl_count=gsl_count, gsl_seed=gsl_seed,
                                            gsl_dir=gsl_dir, gen_count=gen_count,
                                            gen_seed=gen_seed, gen_dir=gen_dir,
                                            gen_workers=gen_workers, log=log):
                n += 1
                if part_id in done:
                    rows.append(done[part_id])
                    continue
                try:
                    outcome = importers.label_part(load(), min_match=min_match)
                    row = {"record": outcome.record, "report": outcome.report}
                except Exception as e:  # noqa: BLE001 — one bad part is reported, not fatal
                    row = {"record": None,
                           "report": {"part_id": part_id, "match_rate": None,
                                      "dropped": f"{type(e).__name__}: {e}"}}
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                rows.append(row)
                if n % 100 == 0:
                    log(f"  {src}: {n} parts")
            log(f"{src}: {n} parts attempted" if n or src in GENERATED_SOURCES else
                f"{src}: no fetched parts — run `python -m training.viewpoint.datasets.fetch "
                f"{src} --limit N` first")
    rows.sort(key=lambda r: r["report"]["part_id"])
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            if r["record"] is not None:
                f.write(json.dumps(r["record"], sort_keys=True) + "\n")
    os.replace(tmp, out)
    with open(reports_path(out), "w") as f:
        for r in rows:
            f.write(json.dumps(r["report"], sort_keys=True) + "\n")
    summary = summarize(rows, out, sources)
    summary_path(out).write_text(json.dumps(summary, indent=2))
    partial.unlink()
    return summary


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help=f"comma-separated subset of {', '.join(KNOWN_SOURCES)} "
                         f"(default {','.join(DEFAULT_SOURCES)})")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"manifest path (default $CHUDWARE_VIEWNET_DATA/manifests/{MANIFEST_NAME})")
    ap.add_argument("--limit", type=int, default=None,
                    help="label at most N fetched parts per dataset source (fetch order)")
    ap.add_argument("--gsl-families", default=None,
                    help="comma-separated GSL families (default: all in tools/gsl_dataset_gen.py)")
    ap.add_argument("--gsl-count", type=int, default=100, help="GSL samples per family")
    ap.add_argument("--gsl-seed", type=int, default=0, help="first GSL seed")
    ap.add_argument("--gsl-dir", type=Path, default=None,
                    help="GSL STEP + sidecar cache (default $CHUDWARE_VIEWNET_DATA/raw/gsl)")
    ap.add_argument("--mfcadpp-gen-count", type=int, default=MFCADPP_GEN_COUNT,
                    help=f"mfcadpp_gen seeds to generate + label (default {MFCADPP_GEN_COUNT})")
    ap.add_argument("--mfcadpp-gen-seed", type=int, default=0, help="first mfcadpp_gen seed")
    ap.add_argument("--mfcadpp-gen-dir", type=Path, default=None,
                    help="mfcadpp_gen STEP + sidecar cache "
                         "(default $CHUDWARE_VIEWNET_DATA/raw/mfcadpp_gen)")
    ap.add_argument("--mfcadpp-gen-workers", type=int, default=None,
                    help="generator worker processes (default: CPU count - 2)")
    ap.add_argument("--min-match", type=float, default=importers.MIN_MATCH_RATE,
                    help="drop parts with a lower geometric face-match rate (default 0.98)")
    ap.add_argument("--fresh", action="store_true", help="ignore a previous partial run")
    ap.add_argument("--allow-noncommercial", action="store_true",
                    help="permit non-commercial sources (research only; never ship the result)")
    ap.add_argument("--allow-unverified-license", action="store_true",
                    help="permit sources whose data licence is unstated")
    args = ap.parse_args(argv)

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in KNOWN_SOURCES]
    if unknown:
        print(f"FATAL: unknown source(s) {unknown} (known: {', '.join(KNOWN_SOURCES)})",
              file=sys.stderr)
        return 1
    try:
        for s in sources:
            if s not in GENERATED_SOURCES:
                fetch.check_license(fetch.SOURCES[s], args.allow_noncommercial,
                                    args.allow_unverified_license)
    except fetch.LicenseRefused as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    # A batch labeller must not mint hundreds of documents inside the user's running hostd
    # (each one fans out to the open GUI); the in-process engine keeps them private.
    os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    from services.mcp import core_bridge as cb  # noqa: PLC0415
    if not cb.available():
        print("FATAL: libchudware could not be loaded — build the core (./build.sh core) first.",
              file=sys.stderr)
        return 1
    gsl_families: list = []
    if "gsl" in sources:
        from tools.gsl_dataset_gen import FAMILIES  # noqa: PLC0415
        gsl_families = ([f.strip() for f in args.gsl_families.split(",") if f.strip()]
                        if args.gsl_families else list(FAMILIES))
        bad = [f for f in gsl_families if f not in FAMILIES]
        if bad:
            print(f"FATAL: unknown GSL families {bad} (known: {sorted(FAMILIES)})",
                  file=sys.stderr)
            return 1

    out = args.out or default_manifest_path()
    summary = build(sources, out, limit=args.limit, gsl_families=gsl_families,
                    gsl_count=args.gsl_count, gsl_seed=args.gsl_seed,
                    gsl_dir=args.gsl_dir or fetch.data_root() / "raw" / "gsl",
                    min_match=args.min_match, fresh=args.fresh,
                    gen_count=args.mfcadpp_gen_count, gen_seed=args.mfcadpp_gen_seed,
                    gen_dir=args.mfcadpp_gen_dir, gen_workers=args.mfcadpp_gen_workers)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
