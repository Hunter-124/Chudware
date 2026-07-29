#!/usr/bin/env python3
"""artifact_validity runner — the harness's aggregate self-measurement.

WHY THIS EXISTS
    Every dialect/guidance/prompt fix in this repo is justified per-defect: "this call
    used to fail and now it doesn't". That is necessary and not sufficient - it cannot
    tell you whether the agent's OUTPUT got better, and it cannot detect a fix that traded
    one failure for two. This benchmark scores the ARTIFACT: does the part build, is the
    solid watertight, is the volume right, does ERC/DRC come back clean, did the drawing
    get its views and an engine-measured dimension. The model's prose is never scored.

    The methodology mirrors the CADGenBench-style claim build123d-mcp publishes for its
    verification tools (score 0.360 -> 0.457, CAD validity 88% -> 100%): a fixed prompt
    set, objective validity criteria, one number, differed across runs.

RUN (the exact command, on a VERIFIED-CURRENT engine)
    cd /home/hunter/Repositories/CAD
    find core \\( -name '*.cpp' -o -name '*.h' \\) -newer core/build-linux/bin/libchudware.so \\
        -not -path 'core/tests/*' -print -quit          # MUST be empty
    ninja -C core/build-linux bin/libchudware.so        # if it was not

    CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so \\
    SWEEP_MODEL=claude-sonnet-4-5 SWEEP_EFFORT=low SWEEP_PROVIDER=anthropic \\
    .venv-mcp/bin/python -m benchmarks.artifact_validity.runner run --tag baseline

    # later, after a harness change:
    ... runner run --tag after --baseline baseline

    ... runner report  --tag baseline          # re-print a stored report
    ... runner compare --baseline baseline --tag after
    ... runner rescore --tag baseline          # re-score stored turns + artifacts,
                                               #   no model calls, no cost

    Long runs go through the hub process tool, not a shell timeout.

WHAT IT DOES NOT MEASURE
    scoring.LIMITS, printed at the bottom of every report. Read it before quoting the
    number at anyone.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import time
from pathlib import Path

# The ARTIFACT PROBE runs in THIS process, and core_bridge defaults to
# CHUDWARE_ENGINE_DAEMON=1 — i.e. it would attach to whatever chudware-hostd the user has
# running, deposit every probed doc into that machine-wide pool (polluting the user's tab
# strip) and, worse, execute against a binary this benchmark never verified. On this box
# the live daemon came from core/build-occt-linux, a DIFFERENT build tree than the one the
# staleness check covers. Forced off before core_bridge is imported anywhere, so the probe
# uses exactly the CHUDWARE_LIBCHUDWARE the report records. A private daemon would isolate
# too, but it would run the on-disk chudware-hostd binary, which is NOT the .so the
# staleness check verifies - so legacy in-process is the stricter choice here.
import os  # noqa: E402

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from benchmarks.artifact_validity import probe as _probe  # noqa: E402
from benchmarks.artifact_validity.prompts import PROMPTS, PROMPTS_BY_ID  # noqa: E402
from benchmarks.artifact_validity.scoring import (  # noqa: E402
    LIMITS, diff_reports, format_report, score_run,
)
from benchmarks.artifact_validity.turn_driver import (  # noqa: E402
    TurnConfig, run_turn, turn_to_record,
)

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def _results_path(tag: str) -> Path:
    return RESULTS / f"{tag}.json"


def _load(tag: str) -> dict:
    p = _results_path(tag)
    if not p.is_file():
        raise SystemExit(f"no stored result for tag {tag!r} ({p})")
    return json.loads(p.read_text())


def _select(only: str | None) -> list:
    if not only:
        return list(PROMPTS)
    ids = [s.strip() for s in only.split(",") if s.strip()]
    missing = [i for i in ids if i not in PROMPTS_BY_ID]
    if missing:
        raise SystemExit(f"unknown prompt id(s) {missing}; have {sorted(PROMPTS_BY_ID)}")
    return [PROMPTS_BY_ID[i] for i in ids]


def _refuse_if_stale(fp: dict, force: bool) -> None:
    """A stale engine has invalidated live sweeps in this repo before. Refuse by default.

    An UNVERIFIABLE engine is refused just as hard: if CHUDWARE_LIBCHUDWARE does not name a
    real file we cannot run the staleness check at all, and an unverified engine makes the
    whole measurement worthless."""
    if fp.get("engine_mtime") is None:
        msg = ("CANNOT VERIFY THE ENGINE: CHUDWARE_LIBCHUDWARE does not name an existing "
               f"file (got {fp.get('engine')!r}), so the staleness check cannot run. Set\n"
               "  CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so")
        if not force:
            raise SystemExit(msg)
        print("WARNING: " + msg, flush=True)
        return
    stale = [s for s in (fp.get("stale_sources") or []) if s]
    if not stale:
        return
    msg = (f"ENGINE IS STALE: {len(stale)} core source(s) are newer than "
           f"{fp.get('engine')} (first: {stale[:3]}). Relink with\n"
           f"  ninja -C core/build-linux bin/libchudware.so\n"
           f"or pass --allow-stale-engine to record a knowingly-invalid run.")
    if force:
        print("WARNING: " + msg, flush=True)
        return
    raise SystemExit(msg)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = TurnConfig.from_env()
    fp = _probe.engine_fingerprint()
    _refuse_if_stale(fp, args.allow_stale_engine)

    out_root = Path(args.out or (RESULTS / f"runs-{args.tag}"))
    out_root.mkdir(parents=True, exist_ok=True)
    selected = _select(args.only)

    print(f"artifact_validity: {len(selected)} prompt(s), model={cfg.model} "
          f"effort={cfg.effort} provider={cfg.provider}", flush=True)
    print(f"engine={fp.get('engine')} mtime={fp.get('engine_mtime')} "
          f"stale_sources={fp.get('stale_sources')} "
          f"(ignored test TUs: {len(fp.get('stale_test_sources') or [])})", flush=True)
    print(f"artifacts -> {out_root}", flush=True)

    runs: list[dict] = []
    t_start = time.time()
    for i, p in enumerate(selected, 1):
        run_dir = out_root / p.id
        run_dir.mkdir(parents=True, exist_ok=True)
        print("=" * 86, flush=True)
        print(f"[{i}/{len(selected)}] {p.id}: {p.prompt}", flush=True)
        turn = run_turn(p.prompt, f"av-{args.tag}-{p.id}", run_dir, cfg)
        rec = turn_to_record(p.id, p.prompt, turn)
        bad = [c for c in rec["calls"] if not c.get("ok")]
        print(f"  -> success={rec['success']} stop={rec['stop_reason']} "
              f"calls={len(rec['calls'])} failed={len(bad)} {rec['elapsed_s']:.0f}s",
              flush=True)
        for c in bad[:6]:
            print(f"     FAIL {c.get('tool')}", flush=True)
        (run_dir / "record.json").write_text(json.dumps(rec, indent=1, default=str))
        runs.append(rec)
    print(f"\nall turns done in {time.time() - t_start:.0f}s; probing artifacts...",
          flush=True)

    report = _score_and_store(runs, out_root, args.tag, cfg, fp, selected)
    print(format_report(report))
    if args.baseline:
        print()
        print(diff_reports(_load(args.baseline), report))
    return 0


def _score_and_store(runs: list[dict], out_root: Path, tag: str, cfg: TurnConfig,
                     fp: dict, selected: list) -> dict:
    parts: dict[str, list[dict]] = {}
    files: dict[str, list[dict]] = {}
    by_id = {str(r.get("prompt_id")): r for r in runs}
    for p in selected:
        run_dir = out_root / p.id
        try:
            pf, ff = _probe.probe(run_dir, by_id.get(p.id))
        except Exception as exc:  # noqa: BLE001 — one unprobeable prompt must not kill the run
            print(f"  probe({p.id}) raised {type(exc).__name__}: {exc}", flush=True)
            pf, ff = [], []
        parts[p.id], files[p.id] = pf, ff
        print(f"  probed {p.id}: {len(pf)} part doc(s), {len(ff)} file(s)", flush=True)
    (out_root / "artifacts.json").write_text(
        json.dumps({"parts": parts, "files": files}, indent=1, default=str))

    meta = {
        "tag": tag, "when": _dt.datetime.now().isoformat(timespec="seconds"),
        "model": cfg.model, "effort": cfg.effort, "provider": cfg.provider,
        "role": cfg.role, "prompt_ids": [p.id for p in selected],
        "artifacts_dir": str(out_root),
        **{k: fp.get(k) for k in ("engine", "engine_mtime", "daemon_mode",
                                  "stale_sources", "stale_test_sources")},
    }
    report = score_run(runs, parts, files, meta=meta, prompt_set=selected)
    report["runs"] = runs
    # The probe's answers go INSIDE the stored report, not only into the run tree: the raw
    # tree is megabytes of .step+ per run and is gitignored, so a committed baseline must
    # carry everything `rescore` needs to re-derive its score without an engine.
    report["artifacts"] = {"parts": parts, "files": files}
    RESULTS.mkdir(parents=True, exist_ok=True)
    _results_path(tag).write_text(json.dumps(report, indent=1, default=str))
    (RESULTS / f"{tag}.txt").write_text(format_report(report))
    print(f"\nstored -> {_results_path(tag)}", flush=True)
    return report


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-score STORED turns and STORED artifacts. No model calls, no engine needed unless
    --reprobe. This is how a scoring change is validated against a real run for free."""
    old = _load(args.tag)
    runs = old.get("runs") or []
    if not runs:
        raise SystemExit(f"{args.tag}: stored report has no raw turns to re-score")
    out_root = Path((old.get("meta") or {}).get("artifacts_dir") or "")
    selected = _select(",".join((old.get("meta") or {}).get("prompt_ids") or []) or None)
    if args.reprobe:
        cfg = TurnConfig.from_env()
        fp = _probe.engine_fingerprint()
        report = _score_and_store(runs, out_root, args.tag, cfg, fp, selected)
    else:
        art = old.get("artifacts")
        if not art:
            art = json.loads((out_root / "artifacts.json").read_text())
        meta = dict(old.get("meta") or {})
        meta["rescored"] = _dt.datetime.now().isoformat(timespec="seconds")
        report = score_run(runs, art["parts"], art["files"], meta=meta,
                           prompt_set=selected)
        report["runs"] = runs
        report["artifacts"] = art
        _results_path(args.tag).write_text(json.dumps(report, indent=1, default=str))
        (RESULTS / f"{args.tag}.txt").write_text(format_report(report))
    print(format_report(report))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    print(format_report(_load(args.tag)))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    print(diff_reports(_load(args.baseline), _load(args.tag)))
    return 0


def cmd_limits(_args: argparse.Namespace) -> int:
    print(LIMITS)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="artifact_validity",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="drive the prompt set live, score, store")
    r.add_argument("--tag", required=True)
    r.add_argument("--only", help="comma-separated prompt ids")
    r.add_argument("--out", help="artifact dir (default results/runs-<tag>)")
    r.add_argument("--baseline", help="also print a comparison against this stored tag")
    r.add_argument("--allow-stale-engine", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("rescore", help="re-score a stored run without any model calls")
    s.add_argument("--tag", required=True)
    s.add_argument("--reprobe", action="store_true",
                   help="also re-open the stored artifacts with the engine")
    s.set_defaults(fn=cmd_rescore)

    p = sub.add_parser("report", help="print a stored report")
    p.add_argument("--tag", required=True)
    p.set_defaults(fn=cmd_report)

    c = sub.add_parser("compare", help="before/after two stored runs")
    c.add_argument("--baseline", required=True)
    c.add_argument("--tag", required=True)
    c.set_defaults(fn=cmd_compare)

    sub.add_parser("limits", help="print what this benchmark does not measure").set_defaults(
        fn=cmd_limits)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
