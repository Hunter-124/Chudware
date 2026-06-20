#!/usr/bin/env python3
"""solver_diff.py — differential-test runner for the sketch constraint solver.

For every fixture in ``corpus/*.json`` this runs BOTH engines on the identical
input and diffs the results:

  * Python oracle  — ``engine.doc.sketch_solver.solve_sketch(sketch, env)``
  * C++ port       — ``core/build/bin/Release/sketch_cli`` (calls
                     ``aicad::sketch::solve_sketch``)

It compares, per fixture:
  * ``converged``           (exact)
  * ``dof``                 (exact)
  * ``statuses``            (per-entity, exact)
  * ``redundant``           (as a SET of constraint ids)
  * ``conflicting``         (as a SET of constraint ids)
  * entity coordinates      (within --tol, default 1e-5)

and, when a fixture carries an ``expect`` block, additionally asserts the Python
result matches it (a guard against the oracle silently regressing).

Exit code is 0 iff every fixture PASSES; nonzero otherwise.

------------------------------------------------------------------------------
Running
------------------------------------------------------------------------------
Run from the repo root with an interpreter that has numpy + scipy and can
``import engine.doc.sketch_solver`` (the project venv is the surest bet):

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/solver_diff.py

If you launch a different interpreter, this script re-discovers and re-execs the
project venv automatically (so ``py -3.11 tools/solver_diff.py`` also works as
long as the venv exists). Discovery order mirrors build.bat:
  1. %APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe
  2. py -3.11
  3. python

Useful flags:
    --tol 1e-5            coordinate comparison tolerance
    --cli  <path>         path to sketch_cli(.exe) (else auto-discovered under core/build)
    --corpus <dir>        corpus directory (default: <repo>/corpus)
    --filter <substr>     only run fixtures whose name contains <substr>
    --build               configure+build core (Release) before running
    -v / --verbose        print the full result dicts on failure
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

# Entity coordinate keys we diff, per entity type. Non-coordinate keys
# ("type", "construction", "ccw", ...) are ignored.
_VECTOR_KEYS = {"p", "p1", "p2", "mid", "center"}   # list[float]
_SCALAR_KEYS = {"radius", "start", "end", "width", "rx", "ry", "rot"}  # float


# ---------------------------------------------------------------------------
# Interpreter self-discovery (so the script runs under whatever you launch it
# with, as long as a numpy/scipy interpreter is reachable).
# ---------------------------------------------------------------------------

def _have_deps() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        return True
    except Exception:
        return False


def _discover_python() -> Optional[List[str]]:
    """Return a command list for a numpy/scipy interpreter, or None."""
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
    """If the current interpreter lacks numpy/scipy, re-exec under a discovered one."""
    if _have_deps():
        return
    if os.environ.get("_SOLVER_DIFF_REEXEC"):
        # Already tried to re-exec once; avoid an infinite loop.
        sys.stderr.write(
            "solver_diff: this interpreter lacks numpy/scipy and re-exec failed.\n"
            "Run with the project venv:\n"
            '  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" '
            "tools/solver_diff.py\n"
        )
        sys.exit(3)
    cmd = _discover_python()
    if not cmd:
        sys.stderr.write(
            "solver_diff: no interpreter with numpy+scipy found "
            "(checked %APPDATA%\\AI-CAD\\runtime\\venv, py -3.11, python).\n"
        )
        sys.exit(3)
    env = dict(os.environ, _SOLVER_DIFF_REEXEC="1")
    full = cmd + [str(Path(__file__).resolve())] + sys.argv[1:]
    sys.stderr.write(f"solver_diff: re-exec under {' '.join(cmd)}\n")
    sys.exit(subprocess.run(full, env=env).returncode)


# ---------------------------------------------------------------------------
# C++ CLI discovery + invocation
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
    # last resort: a recursive glob under core/build
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


def run_cpp(cli: Path, fixture: dict) -> dict:
    """Invoke sketch_cli with the fixture's {sketch, env} via stdin."""
    payload = json.dumps({"sketch": fixture.get("sketch", {}),
                          "env": fixture.get("env", {})})
    proc = subprocess.run(
        [str(cli), "-"], input=payload, capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"sketch_cli exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"sketch_cli produced non-JSON output: {e}\n--- stdout ---\n{proc.stdout}")


# ---------------------------------------------------------------------------
# Python oracle invocation (in-process)
# ---------------------------------------------------------------------------

def run_python(fixture: dict) -> dict:
    # Import lazily so --help etc. work without the deps, and so the import
    # happens from the repo root (added to sys.path below).
    from engine.doc.sketch_solver import solve_sketch
    sol = solve_sketch(fixture.get("sketch", {}), fixture.get("env", {}), warm=None)
    return {
        "converged": bool(sol.converged),
        "dof": int(sol.dof),
        "redundant": [str(t) for t in sol.redundant],
        "conflicting": [str(t) for t in sol.conflicting],
        "statuses": {str(k): str(v) for k, v in sol.statuses.items()},
        "entities": sol.entities,
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _coord_diffs(py_ents: dict, cpp_ents: dict, tol: float) -> List[str]:
    """Return human-readable mismatches for entity coordinates within tol."""
    diffs: List[str] = []
    py_ids = set(py_ents)
    cpp_ids = set(cpp_ents)
    if py_ids != cpp_ids:
        only_py = py_ids - cpp_ids
        only_cpp = cpp_ids - py_ids
        if only_py:
            diffs.append(f"entity ids only in Python: {sorted(only_py)}")
        if only_cpp:
            diffs.append(f"entity ids only in C++: {sorted(only_cpp)}")
    for eid in sorted(py_ids & cpp_ids):
        pe, ce = py_ents[eid], cpp_ents[eid]
        for key in sorted(set(pe) | set(ce)):
            if key not in _VECTOR_KEYS and key not in _SCALAR_KEYS:
                continue  # ignore type / construction / ccw / etc.
            pv, cv = pe.get(key), ce.get(key)
            if pv is None or cv is None:
                diffs.append(f"{eid}.{key}: present in only one side "
                             f"(py={pv!r}, cpp={cv!r})")
                continue
            if key in _VECTOR_KEYS:
                for i, (a, b) in enumerate(zip(pv, cv)):
                    if not _close(a, b, tol):
                        diffs.append(f"{eid}.{key}[{i}]: py={a!r} cpp={b!r} "
                                     f"(absdiff={abs(a - b):.3e} > {tol:g})")
            else:  # scalar
                if not _close(pv, cv, tol):
                    diffs.append(f"{eid}.{key}: py={pv!r} cpp={cv!r} "
                                 f"(absdiff={abs(pv - cv):.3e} > {tol:g})")
    return diffs


def _close(a: float, b: float, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def compare(py: dict, cpp: dict, tol: float) -> List[str]:
    """Return a list of mismatch strings; empty list == parity."""
    diffs: List[str] = []
    if py["converged"] != cpp["converged"]:
        diffs.append(f"converged: py={py['converged']} cpp={cpp['converged']}")
    if py["dof"] != cpp["dof"]:
        diffs.append(f"dof: py={py['dof']} cpp={cpp['dof']}")
    if py["statuses"] != cpp["statuses"]:
        keys = sorted(set(py["statuses"]) | set(cpp["statuses"]))
        for k in keys:
            a, b = py["statuses"].get(k), cpp["statuses"].get(k)
            if a != b:
                diffs.append(f"status[{k}]: py={a!r} cpp={b!r}")
    if set(py["redundant"]) != set(cpp["redundant"]):
        diffs.append(f"redundant set: py={sorted(set(py['redundant']))} "
                     f"cpp={sorted(set(cpp['redundant']))}")
    if set(py["conflicting"]) != set(cpp["conflicting"]):
        diffs.append(f"conflicting set: py={sorted(set(py['conflicting']))} "
                     f"cpp={sorted(set(cpp['conflicting']))}")
    diffs.extend(_coord_diffs(py.get("entities", {}), cpp.get("entities", {}), tol))
    return diffs


def check_expect(py: dict, expect: dict) -> List[str]:
    """Assert the Python oracle matches a hand-authored expect block."""
    diffs: List[str] = []
    if "converged" in expect and py["converged"] != expect["converged"]:
        diffs.append(f"[expect] converged: py={py['converged']} "
                     f"expect={expect['converged']}")
    if "dof" in expect and py["dof"] != expect["dof"]:
        diffs.append(f"[expect] dof: py={py['dof']} expect={expect['dof']}")
    if "statuses" in expect:
        for k, v in expect["statuses"].items():
            if py["statuses"].get(k) != v:
                diffs.append(f"[expect] status[{k}]: py={py['statuses'].get(k)!r} "
                             f"expect={v!r}")
    if "redundant" in expect and set(py["redundant"]) != set(expect["redundant"]):
        diffs.append(f"[expect] redundant: py={sorted(set(py['redundant']))} "
                     f"expect={sorted(set(expect['redundant']))}")
    if "conflicting" in expect and set(py["conflicting"]) != set(expect["conflicting"]):
        diffs.append(f"[expect] conflicting: py={sorted(set(py['conflicting']))} "
                     f"expect={sorted(set(expect['conflicting']))}")
    return diffs


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
    ap.add_argument("--tol", type=float, default=1e-5,
                    help="coordinate comparison tolerance (default 1e-5)")
    ap.add_argument("--cli", type=str, default=None,
                    help="path to sketch_cli executable (else auto-discovered)")
    ap.add_argument("--corpus", type=str, default=str(CORPUS_DIR),
                    help="corpus directory (default: <repo>/corpus)")
    ap.add_argument("--filter", type=str, default=None,
                    help="only run fixtures whose name contains this substring")
    ap.add_argument("--build", action="store_true",
                    help="configure+build core (Release) before running")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print full result dicts on failure")
    args = ap.parse_args()

    # Make `import engine.doc.sketch_solver` work regardless of cwd.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    if args.build:
        _build_core()

    cli = Path(args.cli) if args.cli else _discover_cli()
    if not cli or not cli.exists():
        sys.stderr.write(
            "solver_diff: could not find sketch_cli. Build core first:\n"
            "  cmake -S core -B core/build -G \"Visual Studio 17 2022\" -A x64\n"
            "  cmake --build core/build --config Release\n"
            "or pass --cli <path>, or run with --build.\n")
        return 3

    corpus = Path(args.corpus)
    fixtures = load_fixtures(corpus, args.filter)
    if not fixtures:
        sys.stderr.write(f"solver_diff: no fixtures found in {corpus}\n")
        return 3

    print(f"solver_diff: {len(fixtures)} fixture(s) | cli={cli} | tol={args.tol:g}")
    print("-" * 72)

    n_pass = 0
    failures: List[str] = []
    for name, fx in fixtures:
        try:
            py = run_python(fx)
        except Exception as e:
            print(f"FAIL  {name}\n      python oracle raised: {e}")
            failures.append(name)
            continue
        try:
            cpp = run_cpp(cli, fx)
        except Exception as e:
            print(f"FAIL  {name}\n      C++ sketch_cli raised: {e}")
            failures.append(name)
            continue

        diffs = compare(py, cpp, args.tol)
        if "expect" in fx:
            diffs = check_expect(py, fx["expect"]) + diffs

        if not diffs:
            print(f"PASS  {name}  (dof={py['dof']}, converged={py['converged']})")
            n_pass += 1
        else:
            print(f"FAIL  {name}")
            for d in diffs:
                print(f"      - {d}")
            if args.verbose:
                print(f"      python: {json.dumps(py)}")
                print(f"      cpp:    {json.dumps(cpp)}")
            failures.append(name)

    print("-" * 72)
    total = len(fixtures)
    print(f"summary: {n_pass}/{total} passed"
          + (f", {total - n_pass} FAILED: {failures}" if failures else ""))
    return 0 if n_pass == total else 1


if __name__ == "__main__":
    _reexec_if_needed()
    sys.exit(main())
