#!/usr/bin/env python3
"""simsuite runner — discovers *_bench.py modules, runs them serially, writes
results.json + REPORT.md. See README.md for the per-case dict contract."""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path

SUITE_DIR = Path(__file__).resolve().parent
REPO = SUITE_DIR.parent.parent

REQUIRED_KEYS = {"domain", "case", "total_s", "status"}
NUMERIC_KEYS = ("setup_s", "mesh_s", "solve_s", "total_s", "metric", "ref", "rel_err")


def _discover(only: set[str] | None) -> list[Path]:
    mods = sorted(SUITE_DIR.glob("*_bench.py"))
    if only:
        mods = [m for m in mods if m.stem.removesuffix("_bench") in only]
    return mods


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _validate(case: dict, src: str) -> dict:
    missing = REQUIRED_KEYS - case.keys()
    if missing:
        raise ValueError(f"{src}: case missing keys {sorted(missing)}: {case}")
    for k in NUMERIC_KEYS:
        v = case.get(k)
        if v is not None and not isinstance(v, (int, float)):
            raise ValueError(f"{src}: {case.get('case')}: {k} must be numeric or None")
    case.setdefault("detail", "")
    return case


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="coarse/fast smoke mode")
    ap.add_argument("--only", default="", help="comma-separated domain list")
    ap.add_argument("--out", default="", help="results dir override")
    args = ap.parse_args()

    os.environ.setdefault("AI_CAD_ENGINE_DAEMON", "0")
    sys.path.insert(0, str(SUITE_DIR))
    import resguard
    budget = resguard.install()  # before any engine/solver library loads
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    out_dir = Path(args.out) if args.out else SUITE_DIR / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    all_cases: list[dict] = []
    module_errors: dict[str, str] = {}
    for mod_path in _discover(only):
        name = mod_path.stem.removesuffix("_bench")
        if resguard.should_abort():
            module_errors[name] = "skipped: resource budget exhausted by an earlier module"
            print(f"[simsuite] {name} SKIPPED (resource budget)", file=sys.stderr)
            continue
        print(f"[simsuite] {name} ...", flush=True)
        try:
            mod = _load(mod_path)
            cases = mod.run(quick=args.quick)
            all_cases += [_validate(dict(c), name) for c in cases]
        except (KeyboardInterrupt, MemoryError):
            # resguard watchdog hard-stop or RLIMIT backstop: record and move on;
            # should_abort() gates the remaining modules
            module_errors[name] = "aborted by resguard (memory budget exceeded)"
            print(f"[simsuite] {name} ABORTED by resguard", file=sys.stderr)
        except Exception:
            module_errors[name] = traceback.format_exc()
            print(f"[simsuite] {name} FAILED to run:\n{module_errors[name]}", file=sys.stderr)

    payload = {
        "timestamp": stamp,
        "quick": args.quick,
        "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?"),
        "budget": {"mem_gb": round(budget.mem_bytes / 2**30, 2),
                   "total_gb": round(budget.total_bytes / 2**30, 2),
                   "workers": budget.workers},
        "cases": all_cases,
        "module_errors": module_errors,
    }
    (out_dir / "results.json").write_text(json.dumps(payload, indent=2))

    # REPORT.md — accuracy table + slowest-first timing table
    lines = [f"# simsuite baseline — {stamp}{' (quick)' if args.quick else ''}", ""]
    # A case-level `skip` (optional dep absent — OpenFOAM/ngspice/openEMS, or a case
    # too big for the host per resguard.fits) is expected and does NOT fail the gate;
    # only true `fail`s and module_errors do. A resguard budget ABORT is recorded as a
    # module_error (not a case skip), so it still fails the gate as it should.
    failed = [c for c in all_cases if c["status"] == "fail"]
    skipped = [c for c in all_cases if c["status"] == "skip"]
    lines += [f"{len(all_cases)} cases, {len(failed)} failed, {len(skipped)} skipped, "
              f"{len(module_errors)} module errors", ""]
    lines += ["## Timing (slowest first)", "",
              "| domain | case | setup_s | mesh_s | solve_s | total_s | status |",
              "|---|---|---|---|---|---|---|"]
    for c in sorted(all_cases, key=lambda c: -(c.get("total_s") or 0)):
        lines.append("| {domain} | {case} | {setup} | {mesh} | {solve} | {total:.2f} | {status} |".format(
            domain=c["domain"], case=c["case"],
            setup=_fmt(c.get("setup_s")), mesh=_fmt(c.get("mesh_s")),
            solve=_fmt(c.get("solve_s")), total=c.get("total_s") or 0.0, status=c["status"]))
    lines += ["", "## Accuracy", "",
              "| domain | case | metric | ref | rel_err | status |", "|---|---|---|---|---|---|"]
    for c in all_cases:
        if c.get("ref") is None:
            continue
        lines.append("| {domain} | {case} | {m:.6g} | {r:.6g} | {e} | {status} |".format(
            domain=c["domain"], case=c["case"], m=c.get("metric") or float("nan"),
            r=c["ref"], e=_fmt(c.get("rel_err"), "{:.3%}"), status=c["status"]))
    if module_errors:
        lines += ["", "## Module errors", ""]
        for k, v in module_errors.items():
            lines += [f"### {k}", "```", v.strip()[-2000:], "```", ""]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"[simsuite] wrote {out_dir}/results.json and REPORT.md "
          f"({len(failed)} failed, {len(skipped)} skipped, {len(module_errors)} module errors)")
    return 1 if (failed or module_errors) else 0


def _fmt(v, spec="{:.2f}"):
    return "-" if v is None else spec.format(v)


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
