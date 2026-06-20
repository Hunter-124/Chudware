#!/usr/bin/env python3
"""measure_diff.py — differential-test runner for measure_constraint.

For every fixture in ``corpus/*.json`` this:
  1. solves the sketch with the Python oracle (``solve_sketch``) to get the
     SOLVED entity coords;
  2. for EACH DIMENSIONAL constraint in the sketch (distance / angle / radius /
     diameter), computes the measurement TWO ways against those same solved
     coords:
       * Python oracle  — ``sketch_solver.measure_constraint(constraint, coords)``
       * C++ port       — ``sketch_cli`` "measure" mode
         (input {"mode":"measure","constraint":{...},"entity_coords":{...}}
          -> {"value": <number|null>});
  3. diffs the two within --tol (default 1e-6). A None/null on one side must be
     None/null on the other.

Exit code is 0 iff every measured constraint across every fixture PASSES.

This mirrors tools/solver_diff.py (same venv self-discovery + CLI discovery); see
that file's header for how the interpreter is re-discovered.

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/measure_diff.py

Useful flags:
    --tol 1e-6            measurement comparison tolerance
    --cli  <path>         path to sketch_cli(.exe) (else auto-discovered)
    --corpus <dir>        corpus directory (default: <repo>/corpus)
    --filter <substr>     only run fixtures whose name contains <substr>
    --build               configure+build core (Release) before running
    -v / --verbose        print every measured constraint (not just failures)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make stdout/stderr tolerate non-ASCII on a legacy (cp1252) Windows console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

# --- repo layout -------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"

_DIMENSIONAL_TYPES = ("distance", "angle", "radius", "diameter")


# ---------------------------------------------------------------------------
# Interpreter self-discovery (so the script runs under whatever you launch it
# with, as long as a numpy/scipy interpreter is reachable). Mirrors solver_diff.
# ---------------------------------------------------------------------------

def _have_deps() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        return True
    except Exception:
        return False


def _discover_python() -> Optional[List[str]]:
    appdata = os.environ.get("APPDATA", "")
    venv = Path(appdata) / "AI-CAD" / "runtime" / "venv" / "Scripts" / "python.exe"
    candidates: List[List[str]] = []
    if venv.exists():
        candidates.append([str(venv)])
    candidates.append(["py", "-3.11"])
    candidates.append(["python"])
    for cmd in candidates:
        try:
            out = subprocess.run(
                cmd + ["-c", "import numpy, scipy; print('ok')"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0 and "ok" in out.stdout:
                return cmd
        except Exception:
            continue
    return None


def _reexec_if_needed() -> None:
    if _have_deps():
        return
    if os.environ.get("_MEASURE_DIFF_REEXEC"):
        sys.stderr.write(
            "measure_diff: this interpreter lacks numpy/scipy and re-exec failed.\n"
            "Run with the project venv:\n"
            '  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" '
            "tools/measure_diff.py\n"
        )
        sys.exit(3)
    cmd = _discover_python()
    if not cmd:
        sys.stderr.write(
            "measure_diff: no interpreter with numpy+scipy found "
            "(checked %APPDATA%\\AI-CAD\\runtime\\venv, py -3.11, python).\n"
        )
        sys.exit(3)
    env = dict(os.environ, _MEASURE_DIFF_REEXEC="1")
    full = cmd + [str(Path(__file__).resolve())] + sys.argv[1:]
    sys.stderr.write(f"measure_diff: re-exec under {' '.join(cmd)}\n")
    sys.exit(subprocess.run(full, env=env).returncode)


# ---------------------------------------------------------------------------
# C++ CLI discovery + invocation (same layout as solver_diff)
# ---------------------------------------------------------------------------

def _discover_cli() -> Optional[Path]:
    names = ["sketch_cli.exe", "sketch_cli"]
    search = [
        REPO_ROOT / "core" / "build" / "bin" / "Release",
        REPO_ROOT / "core" / "build" / "bin" / "Debug",
        REPO_ROOT / "core" / "build" / "bin",
    ]
    for d in search:
        for n in names:
            p = d / n
            if p.exists():
                return p
    build = REPO_ROOT / "core" / "build"
    if build.exists():
        hits = list(build.rglob("sketch_cli.exe")) + list(build.rglob("sketch_cli"))
        hits = [h for h in hits if h.is_file()]
        if hits:
            hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return hits[0]
    return None


def _build_core() -> None:
    print("[build] configuring core ...")
    subprocess.run(
        ["cmake", "-S", "core", "-B", "core/build",
         "-G", "Visual Studio 17 2022", "-A", "x64"],
        cwd=REPO_ROOT, check=True,
    )
    print("[build] building core (Release) ...")
    subprocess.run(
        ["cmake", "--build", "core/build", "--config", "Release"],
        cwd=REPO_ROOT, check=True,
    )


def run_cpp_measure(cli: Path, constraint: dict, coords: dict) -> Optional[float]:
    """Invoke sketch_cli's measure mode -> Optional[float] (None == null)."""
    payload = json.dumps({"mode": "measure", "constraint": constraint,
                          "entity_coords": coords})
    proc = subprocess.run(
        [str(cli), "-"], input=payload, capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sketch_cli (measure) exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"sketch_cli produced non-JSON output: {e}\n--- stdout ---\n{proc.stdout}")
    v = out.get("value", None)
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# Python oracle invocation (in-process)
# ---------------------------------------------------------------------------

def solve_py(fixture: dict) -> Dict[str, Any]:
    from engine.doc.sketch_solver import solve_sketch
    sol = solve_sketch(fixture.get("sketch", {}), fixture.get("env", {}), warm=None)
    return sol.entities


def measure_py(constraint: dict, coords: dict) -> Optional[float]:
    from engine.doc.sketch_solver import measure_constraint
    v = measure_constraint(constraint, coords)
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _close(a: Optional[float], b: Optional[float], tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_fixtures(corpus: Path, name_filter: Optional[str]) -> List[Tuple[str, dict]]:
    out: List[Tuple[str, dict]] = []
    for path in sorted(corpus.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            fx = json.load(f)
        name = fx.get("name", path.stem)
        if name_filter and name_filter not in name:
            continue
        out.append((name, fx))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="measurement comparison tolerance (default 1e-6)")
    ap.add_argument("--cli", type=str, default=None,
                    help="path to sketch_cli executable (else auto-discovered)")
    ap.add_argument("--corpus", type=str, default=str(CORPUS_DIR),
                    help="corpus directory (default: <repo>/corpus)")
    ap.add_argument("--filter", type=str, default=None,
                    help="only run fixtures whose name contains this substring")
    ap.add_argument("--build", action="store_true",
                    help="configure+build core (Release) before running")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every measured constraint, not just failures")
    args = ap.parse_args()

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    if args.build:
        _build_core()

    cli = Path(args.cli) if args.cli else _discover_cli()
    if not cli or not cli.exists():
        sys.stderr.write(
            "measure_diff: could not find sketch_cli. Build core first:\n"
            "  cmake -S core -B core/build -G \"Visual Studio 17 2022\" -A x64\n"
            "  cmake --build core/build --config Release\n"
            "or pass --cli <path>, or run with --build.\n")
        return 3

    corpus = Path(args.corpus)
    fixtures = load_fixtures(corpus, args.filter)
    if not fixtures:
        sys.stderr.write(f"measure_diff: no fixtures found in {corpus}\n")
        return 3

    print(f"measure_diff: {len(fixtures)} fixture(s) | cli={cli} | tol={args.tol:g}")
    print("-" * 72)

    n_fix_pass = 0
    n_meas_total = 0
    n_meas_pass = 0
    n_dim_constraints = 0
    failures: List[str] = []

    for name, fx in fixtures:
        try:
            coords = solve_py(fx)
        except Exception as e:
            print(f"FAIL  {name}\n      python solve raised: {e}")
            failures.append(name)
            continue

        sketch = fx.get("sketch", {})
        constraints = sketch.get("constraints", []) or []
        dim_constraints = [c for c in constraints
                           if isinstance(c, dict) and c.get("type") in _DIMENSIONAL_TYPES]

        fixture_diffs: List[str] = []
        for c in dim_constraints:
            n_dim_constraints += 1
            cid = c.get("id", "?")
            ctype = c.get("type")
            try:
                mpy = measure_py(c, coords)
            except Exception as e:
                fixture_diffs.append(f"[{cid}:{ctype}] python measure raised: {e}")
                continue
            try:
                mcpp = run_cpp_measure(cli, c, coords)
            except Exception as e:
                fixture_diffs.append(f"[{cid}:{ctype}] C++ measure raised: {e}")
                continue

            n_meas_total += 1
            if _close(mpy, mcpp, args.tol):
                n_meas_pass += 1
                if args.verbose:
                    print(f"      ok  {name}/{cid} ({ctype}): py={mpy} cpp={mcpp}")
            else:
                d = abs((mpy or 0.0) - (mcpp or 0.0)) if (
                    mpy is not None and mcpp is not None) else float("nan")
                fixture_diffs.append(
                    f"[{cid}:{ctype}] py={mpy!r} cpp={mcpp!r} (absdiff={d:.3e} > {args.tol:g})")

        if not fixture_diffs:
            print(f"PASS  {name}  ({len(dim_constraints)} dim constraint(s))")
            n_fix_pass += 1
        else:
            print(f"FAIL  {name}")
            for d in fixture_diffs:
                print(f"      - {d}")
            failures.append(name)

    print("-" * 72)
    total = len(fixtures)
    print(f"summary: {n_fix_pass}/{total} fixtures passed | "
          f"{n_meas_pass}/{n_meas_total} measurements matched "
          f"({n_dim_constraints} dimensional constraint(s) seen)"
          + (f" | FAILED: {failures}" if failures else ""))
    return 0 if n_fix_pass == total else 1


if __name__ == "__main__":
    _reexec_if_needed()
    sys.exit(main())
