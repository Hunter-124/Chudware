#!/usr/bin/env python3
"""GSL reverse-engineering residual eval — the GNN go/no-go input (HANDOFF §TODO-3 EVAL).

For every `corpora/gsl_dataset` sample (and every `corpora/sw_fixtures/*.step`, if present):

  1. rebuild the sample's source part on the live engine (deterministic: family template +
     the recorded seed from meta.json — the same path gsl_dataset_gen.py used),
  2. export it to STEP,
  3. re-import the STEP (the parametric feature tree is now STRIPPED — one `import` feature),
  4. run the deterministic recon pipeline (mcp_server/recon_tools.py `reverse_engineer`),
  5. score {match_pct, pct_faces_claimed, features_recovered}.

Then render `docs/RECOGNITION_RESIDUAL.md` with a per-family verdict under the binding gate
(docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md §3.4 / tools/gsl_gnn/README.md):

    adapter (GNN) training is justified ONLY for a family whose
    median match_pct < 90  OR  median pct_faces_claimed < 70.

Honest numbers over pretty ones: a degraded boolean compare (match_pct null) scores 0.0 in
the medians and is counted separately; unclaimed faces and rebuild failures are never hidden.

If the full set would exceed the ~40-minute budget, run a deterministic stratified subset
(`--per-family N`: the FIRST N samples per family sorted by directory name, seed-free) —
the report header states exactly which mode produced the numbers.

Usage (from the repo root — the `mcp_server` symlink -> services/mcp must resolve):

    python3 tools/gsl_eval.py
    python3 tools/gsl_eval.py --per-family 5 --out docs/RECOGNITION_RESIDUAL.md
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_server import core_bridge as cb  # noqa: E402

# Gate thresholds (binding: tools/gsl_gnn/README.md + HANDOFF_AI_EXPERIENCE.md §TODO-3 — doc pruned 2026-07-23; rule survives in docs/review/RECOGNITION_RESIDUAL.md).
GATE_MATCH_PCT = 90.0
GATE_FACES_CLAIMED_PCT = 70.0
TIME_BUDGET_S = 40 * 60


# ---------------------------------------------------------------- recon tool plumbing
# reverse_engineer is an MCP tool (a closure registered on a FastMCP instance), so drive it
# exactly the way the MCP tests do: a private FastMCP + call_tool. Only the recon tools are
# registered — no server side effects (fd redirects, warmup).
_MCP = None


def _recon_mcp():
    global _MCP
    if _MCP is None:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
        from mcp_server.recon_tools import register_recon_tools  # noqa: PLC0415
        _MCP = FastMCP("gsl-eval")
        register_recon_tools(_MCP)
    return _MCP


def _reverse_engineer(source: str) -> dict:
    content, structured = asyncio.run(
        _recon_mcp().call_tool("reverse_engineer", {"source": source}))
    text = structured.get("result") if isinstance(structured, dict) else None
    if not isinstance(text, str):  # mcp 1.28.1 flattens list returns; recon returns one str
        text = content[0].text if content else "{}"
    return json.loads(text)


def _close(doc_id: Optional[str]) -> None:
    if not doc_id:
        return
    try:
        cb.call("doc_close", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- one sample, end to end


def _clip(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "\u2026"


def _rebuild_source(family: str, seed: str) -> str:
    """Recreate the sample's source part from its family template + recorded seed (the
    generator is deterministic: sample dir name == seed). Returns the open doc_id."""
    from tools.gsl_dataset_gen import FAMILIES  # noqa: PLC0415
    if family not in FAMILIES:
        raise RuntimeError(f"unknown family {family!r} (not in gsl_dataset_gen.FAMILIES)")
    ops = FAMILIES[family](random.Random(int(seed)))
    doc_id = cb.call("doc_new", {"kind": "part", "name": f"{family}_{seed}_src"})["doc_id"]
    try:
        r = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        bad = [s for s in (r.get("rebuild") or {}).get("feature_status", [])
               if not s.get("ok", True)]
        if bad:
            raise RuntimeError(f"source rebuild failed: {bad}")
    except Exception:
        _close(doc_id)
        raise
    return doc_id


def _score(res: dict) -> dict:
    rec = res.get("recognition") or {}
    face_count = rec.get("face_count") or 0
    unclaimed = len(rec.get("unclaimed") or [])
    claimed_pct = (100.0 * (face_count - unclaimed) / face_count) if face_count else 0.0
    return {
        "match_pct": res.get("match_pct"),  # None == degraded compare
        "pct_faces_claimed": round(claimed_pct, 1),
        "features_recovered": len(res.get("features") or []),
        "kind": rec.get("kind"),
        "rejected": len(res.get("rejected") or []),
        "candidates": len(res.get("candidates") or []),
        "face_count": face_count,
    }


def _eval_gsl_sample(family: str, sample_dir: Path, step_dir: Path) -> dict:
    meta = json.loads((sample_dir / "meta.json").read_text())
    seed = str(meta.get("seed", sample_dir.name))
    row: dict[str, Any] = {"family": family, "sample": sample_dir.name}
    t0 = time.monotonic()
    src_doc = ref_doc = new_doc = None
    try:
        src_doc = _rebuild_source(family, seed)
        # sanity: the regenerated source must be the recorded sample (volume within 0.1%)
        want = meta.get("volume_mm3")
        if want:
            got = cb.call("eval_mass", {"doc_id": src_doc,
                                        "density_g_cm3": 1.0}).get("volume_mm3")
            if got and abs(got - want) > 1e-3 * want:
                row["note"] = (f"regenerated volume {got:.1f} != recorded {want:.1f} mm3 "
                               "(generator drift?)")
        step_path = step_dir / f"{family}_{sample_dir.name}.step"
        cb.call("export_file", {"doc_id": src_doc, "path": str(step_path), "format": "step"})
        _close(src_doc)
        src_doc = None

        imp = cb.call("import_file", {"path": str(step_path), "as_solid": False})
        ref_doc = imp.get("doc_id")
        if not ref_doc:
            raise RuntimeError("STEP re-import produced no document")

        res = _reverse_engineer(ref_doc)
        new_doc = res.get("doc_id")
        if not res.get("ok"):
            raise RuntimeError(f"reverse_engineer failed: {res.get('error')}")
        row.update(_score(res))
        if res.get("notes") and "note" not in row:
            row["note"] = _clip("; ".join(res["notes"]))
    except Exception as e:  # noqa: BLE001 — a failing sample is a RESULT, not a crash.
        row["error"] = f"{type(e).__name__}: {e}"
        row.setdefault("match_pct", None)
        row.setdefault("pct_faces_claimed", 0.0)
        row.setdefault("features_recovered", 0)
    finally:
        for d in (src_doc, ref_doc, new_doc):
            _close(d)
    row["seconds"] = round(time.monotonic() - t0, 1)
    return row


def _eval_step_fixture(step_path: Path) -> dict:
    """sw_fixtures/*.step are ALREADY tree-stripped interchange files: import + recon only."""
    row: dict[str, Any] = {"family": "sw_fixtures", "sample": step_path.name}
    t0 = time.monotonic()
    ref_doc = new_doc = None
    try:
        imp = cb.call("import_file", {"path": str(step_path), "as_solid": False})
        ref_doc = imp.get("doc_id")
        if not ref_doc:
            raise RuntimeError("STEP import produced no document")
        res = _reverse_engineer(ref_doc)
        new_doc = res.get("doc_id")
        if not res.get("ok"):
            raise RuntimeError(f"reverse_engineer failed: {res.get('error')}")
        row.update(_score(res))
        if res.get("notes"):
            row["note"] = _clip("; ".join(res["notes"]))
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"
        row.setdefault("match_pct", None)
        row.setdefault("pct_faces_claimed", 0.0)
        row.setdefault("features_recovered", 0)
    finally:
        for d in (ref_doc, new_doc):
            _close(d)
    row["seconds"] = round(time.monotonic() - t0, 1)
    return row


# ---------------------------------------------------------------- aggregation + report


def _median_match(rows: list[dict]) -> float:
    """Median match_pct with degraded/None scored as 0.0 (a compare that could not verify
    the rebuild is NOT evidence of a match — honest numbers over pretty ones)."""
    return statistics.median((r.get("match_pct") or 0.0) for r in rows)


def _verdict(rows: list[dict]) -> dict:
    med_match = _median_match(rows)
    med_claimed = statistics.median(r.get("pct_faces_claimed", 0.0) for r in rows)
    justified = med_match < GATE_MATCH_PCT or med_claimed < GATE_FACES_CLAIMED_PCT
    return {
        "n": len(rows),
        "median_match_pct": round(med_match, 1),
        "median_pct_faces_claimed": round(med_claimed, 1),
        "median_features_recovered": statistics.median(
            r.get("features_recovered", 0) for r in rows),
        "degraded_compares": sum(1 for r in rows if r.get("match_pct") is None
                                 and "error" not in r),
        "errors": sum(1 for r in rows if "error" in r),
        "adapter_justified": justified,
    }


def _fmt_match(r: dict) -> str:
    m = r.get("match_pct")
    if m is None:
        return "err" if "error" in r else "0.0 (degraded)"
    return f"{m:.1f}"


def _render_report(rows_by_family: dict[str, list[dict]], *, dataset_root: Path,
                   subset_n: Optional[int], sw_note: str, total_s: float) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lib = "unresolved"
    try:
        lib = str(cb._resolve_lib())  # noqa: SLF001 — report provenance, best effort
    except Exception:  # noqa: BLE001
        pass
    mode = (f"**STRATIFIED SUBSET**: the first {subset_n} sample(s) per family, sorted by "
            "directory name (deterministic, seed-free), chosen to stay inside the "
            "~40-minute budget." if subset_n is not None
            else "**FULL SET**: every sample in the dataset was evaluated.")

    L: list[str] = []
    L.append("# Recognition residual — deterministic reverse-engineering eval")
    L.append("")
    L.append("GENERATED by `tools/gsl_eval.py` — do not hand-edit; re-run the script.")
    L.append("")
    L.append(f"- Date: {now}")
    L.append(f"- Engine: `{lib}`")
    L.append(f"- Dataset: `{dataset_root}` (source parts regenerated from each sample's "
             "recorded family+seed, exported to STEP, re-imported tree-stripped, then run "
             "through `mcp_server/recon_tools.py:reverse_engineer`)")
    L.append(f"- Sample selection: {mode}")
    L.append(f"- SolidWorks STEP fixtures: {sw_note}")
    L.append(f"- Total runtime: {total_s / 60.0:.1f} min")
    L.append("")
    L.append("This report is the **GNN adapter go/no-go input** (`tools/gsl_gnn/README.md` "
             "gate). Binding rule (HANDOFF_AI_EXPERIENCE.md §TODO-3 — pruned 2026-07-23, review doc §3.4):")
    L.append("")
    L.append(f"> Adapter training is justified ONLY for a family whose median match_pct < "
             f"{GATE_MATCH_PCT:.0f}% **or** median pct_faces_claimed < "
             f"{GATE_FACES_CLAIMED_PCT:.0f}%.")
    L.append("")
    L.append("Scoring notes: `match_pct` = boolean common volume / max(source, rebuild) "
             "from `eval_compare` (world-frame, no ICP). A degraded compare (no boolean "
             "verdict) scores **0.0** in the medians — a rebuild that cannot be verified is "
             "not counted as a match. `pct_faces_claimed` = source faces claimed by the 5 "
             "deterministic recognition passes (incl. fillet/chamfer candidates). "
             "`features_recovered` = editable features actually applied and kept.")
    L.append("")

    # ---- per-family verdicts first (the decision table)
    L.append("## Per-family verdict")
    L.append("")
    L.append("| family | n | median match_pct | median faces claimed % | median features "
             "recovered | degraded | errors | verdict |")
    L.append("|---|---|---|---|---|---|---|---|")
    flagged: list[str] = []
    for fam in sorted(rows_by_family):
        v = _verdict(rows_by_family[fam])
        verdict = ("**ADAPTER JUSTIFIED**" if v["adapter_justified"]
                   else "deterministic sufficient")
        if v["adapter_justified"]:
            flagged.append(fam)
        L.append(f"| {fam} | {v['n']} | {v['median_match_pct']} | "
                 f"{v['median_pct_faces_claimed']} | {v['median_features_recovered']} | "
                 f"{v['degraded_compares']} | {v['errors']} | {verdict} |")
    L.append("")

    L.append("## Go/no-go summary")
    L.append("")
    if flagged:
        L.append(f"**GO** for adapter training on: {', '.join(f'`{f}`' for f in flagged)} "
                 "— the deterministic recognizer's residual on these families clears the "
                 "gate. All other families: **NO-GO** (deterministic recognition is "
                 "sufficient; training would add an unnecessary learned dependency).")
    else:
        L.append("**NO-GO** for adapter training on every family evaluated: no family's "
                 "median crossed the gate. Do not train the GNN on this evidence.")
    L.append("")

    # ---- raw per-sample rows (nothing suppressed)
    L.append("## Per-sample results")
    L.append("")
    L.append("| family | sample | match_pct | faces claimed % | features recovered | "
             "recognized as | rejected | candidates | s | note |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for fam in sorted(rows_by_family):
        for r in rows_by_family[fam]:
            note = r.get("error") or r.get("note") or ""
            L.append(f"| {fam} | {r['sample']} | {_fmt_match(r)} | "
                     f"{r.get('pct_faces_claimed', 0.0)} | "
                     f"{r.get('features_recovered', 0)} | {r.get('kind') or '—'} | "
                     f"{r.get('rejected', '—')} | {r.get('candidates', '—')} | "
                     f"{r.get('seconds', '—')} | {note} |")
    L.append("")
    L.append("Interpretation guardrails: `match_pct` alone can flatter a family whose base "
             "solid dominates its volume (a recovered base with a missed dress-up still "
             "scores high); read it together with `faces claimed %` and the per-sample "
             "`candidates`/`rejected` counts. Families whose base is NOT prismatic or "
             "turned (sweeps, ribs, shells) are expected deterministic failures — that is "
             "exactly the residual this report exists to measure.")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=str, default="corpora/gsl_dataset",
                    help="gsl_dataset root (default corpora/gsl_dataset)")
    ap.add_argument("--sw-fixtures", type=str, default="corpora/sw_fixtures",
                    help="directory scanned for *.step fixtures (default corpora/sw_fixtures)")
    ap.add_argument("--per-family", type=int, default=None,
                    help="stratified subset: first N samples per family sorted by directory "
                         "name (default: all)")
    ap.add_argument("--out", type=str, default="docs/RECOGNITION_RESIDUAL.md",
                    help="report path (default docs/RECOGNITION_RESIDUAL.md)")
    args = ap.parse_args(argv)

    if not cb.available():
        print("FATAL: libchudware could not be located/loaded — build core first "
              "(and restage services/dist/Chudware after rebuilds).", file=sys.stderr)
        return 1

    dataset_root = Path(args.dataset)
    if not dataset_root.is_dir():
        print(f"FATAL: dataset root {dataset_root} not found — run "
              "tools/gsl_dataset_gen.py first.", file=sys.stderr)
        return 1

    families = sorted(d.name for d in dataset_root.iterdir()
                      if d.is_dir() and any((d / s / "meta.json").is_file()
                                            for s in os.listdir(d)))
    step_dir = Path(tempfile.mkdtemp(prefix="gsl_eval_step_"))

    t_start = time.monotonic()
    rows_by_family: dict[str, list[dict]] = {}
    for fam in families:
        fam_dir = dataset_root / fam
        samples = sorted(d for d in fam_dir.iterdir()
                         if d.is_dir() and (d / "meta.json").is_file())
        if args.per_family is not None:
            samples = samples[: args.per_family]
        for s in samples:
            row = _eval_gsl_sample(fam, s, step_dir)
            rows_by_family.setdefault(fam, []).append(row)
            print(f"[{fam}/{s.name}] match={_fmt_match(row)} "
                  f"claimed={row.get('pct_faces_claimed')}% "
                  f"features={row.get('features_recovered')} "
                  f"({row.get('seconds')}s)"
                  + (f"  ERROR: {row['error']}" if "error" in row else ""))

    sw_dir = Path(args.sw_fixtures)
    sw_steps = sorted(sw_dir.glob("*.step")) + sorted(sw_dir.glob("*.STEP")) \
        if sw_dir.is_dir() else []
    if sw_steps:
        for p in sw_steps:
            row = _eval_step_fixture(p)
            rows_by_family.setdefault("sw_fixtures", []).append(row)
            print(f"[sw_fixtures/{p.name}] match={_fmt_match(row)} "
                  f"claimed={row.get('pct_faces_claimed')}% "
                  f"features={row.get('features_recovered')} ({row.get('seconds')}s)"
                  + (f"  ERROR: {row['error']}" if "error" in row else ""))
        sw_note = f"{len(sw_steps)} file(s) from `{sw_dir}` evaluated (import + recon only)."
    else:
        sw_note = (f"none — `{sw_dir}` contains no `*.step` files at eval time "
                   "(only native .SLDPRT fixtures, which this eval does not cover).")

    total_s = time.monotonic() - t_start
    report = _render_report(rows_by_family, dataset_root=dataset_root,
                            subset_n=args.per_family, sw_note=sw_note, total_s=total_s)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(f"\nWrote {out} ({sum(len(v) for v in rows_by_family.values())} samples, "
          f"{total_s / 60.0:.1f} min)")
    if total_s > TIME_BUDGET_S:
        print("WARNING: run exceeded the ~40-minute budget — next time use --per-family N "
              "(deterministic stratified subset).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
