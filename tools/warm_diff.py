#!/usr/bin/env python3
"""warm_diff.py — differential-test runner for the warm-start path of solve_sketch.

For a handful of corpus fixtures this exercises the per-entity warm-start gate in
BOTH engines and checks they agree on:

  (a) the FINAL warm-solved coordinates (Python vs C++, within --tol);
  (b) warm re-solve == cold solve for an UNCHANGED sketch — warm must NOT change the
      answer (checked on both the Python and the C++ side);
  (c) the per-entity warm/cold GATING DECISION (which entities warm-start vs
      cold-start), against an EDITED variant of each fixture (one entity's params
      rewritten under the same id -> that entity must cold-start, the rest warm).

The C++ side is driven through sketch_cli's "warm" mode:
  input  {"mode":"warm","sketch":{...},"after":{...optional},"env":{...}}
  output {"warm":{entities,converged}, "cold":{entities,converged},
          "decision":{entity id -> "warm"|"cold"}}

The Python decision is reproduced from the ORACLE's own gate (json.dumps sort_keys
signature + per-entity var-count check), so this asserts the C++ decision matches the
documented Python gate. Exit code 0 iff every check passes.

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/warm_diff.py

Mirrors tools/solver_diff.py for venv + CLI self-discovery.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"

# A small, representative set: a rectangle bracket, a fixed-corner nested-circles
# case, a slot-dims case, an angle case. Each must solve to dof 0 so warm==cold is a
# clean equality. (Overridable with --filter.)
_DEFAULT_FIXTURES = [
    "bracket_full",
    "complex_nested_circles_fixed_corner",
    "ported_slot_dims",
    "ported_angle",
    "midpoint",
]


# --- interpreter self-discovery (mirrors solver_diff) ------------------------

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
    if os.environ.get("_WARM_DIFF_REEXEC"):
        sys.stderr.write("warm_diff: this interpreter lacks numpy/scipy and re-exec failed.\n")
        sys.exit(3)
    cmd = _discover_python()
    if not cmd:
        sys.stderr.write("warm_diff: no interpreter with numpy+scipy found.\n")
        sys.exit(3)
    env = dict(os.environ, _WARM_DIFF_REEXEC="1")
    full = cmd + [str(Path(__file__).resolve())] + sys.argv[1:]
    sys.stderr.write(f"warm_diff: re-exec under {' '.join(cmd)}\n")
    sys.exit(subprocess.run(full, env=env).returncode)


# --- CLI discovery (mirrors solver_diff) -------------------------------------

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


def run_cpp_warm(cli: Path, sketch: dict, after: dict, env: dict) -> dict:
    payload = json.dumps({"mode": "warm", "sketch": sketch, "after": after, "env": env})
    proc = subprocess.run(
        [str(cli), "-"], input=payload, capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sketch_cli (warm) exited {proc.returncode}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


# --- Python oracle -----------------------------------------------------------

def py_solve(sketch: dict, env: dict, warm=None):
    from engine.doc.sketch_solver import solve_sketch
    return solve_sketch(sketch, env, warm=warm)


def py_decision(after: dict, env: dict, warm_state) -> Dict[str, str]:
    """Reproduce the oracle's per-entity warm/cold gate for `after` against warm_state.

    Identical predicate to solve_sketch's warm path: an entity warm-starts iff the
    prior state has a var-index block for it of matching length AND the entity's
    json.dumps(sort_keys) signature is unchanged."""
    # Allocate `after` standalone to get its var_map + signatures (sol.state carries them).
    after_state = py_solve(after, env, warm=None).state
    cur_sig = after_state.ent_sig
    dec: Dict[str, str] = {}
    for e in after.get("entities", []):
        eid = e["id"]
        widx = warm_state.var_map.get(eid)
        idx = after_state.var_map.get(eid)
        if widx is None or idx is None or len(widx) != len(idx):
            dec[eid] = "cold"
            continue
        if warm_state.ent_sig.get(eid) != cur_sig.get(eid):
            dec[eid] = "cold"
        else:
            dec[eid] = "warm"
    return dec


# --- comparison helpers ------------------------------------------------------

_VECTOR_KEYS = {"p", "p1", "p2", "mid", "center"}
_SCALAR_KEYS = {"radius", "start", "end", "width", "rx", "ry", "rot", "a0", "a1",
                "t0", "t1", "rho"}


def coord_diffs(a: dict, b: dict, tol: float, label: str) -> List[str]:
    diffs: List[str] = []
    if set(a) != set(b):
        diffs.append(f"{label}: entity id sets differ py/ref={sorted(set(a))} other={sorted(set(b))}")
    for eid in sorted(set(a) & set(b)):
        ea, eb = a[eid], b[eid]
        for key in sorted(set(ea) | set(eb)):
            if key not in _VECTOR_KEYS and key not in _SCALAR_KEYS:
                continue
            va, vb = ea.get(key), eb.get(key)
            if va is None or vb is None:
                continue
            if key in _VECTOR_KEYS:
                for i, (x, y) in enumerate(zip(va, vb)):
                    if abs(float(x) - float(y)) > tol:
                        diffs.append(f"{label} {eid}.{key}[{i}]: {x!r} vs {y!r}")
            else:
                if abs(float(va) - float(vb)) > tol:
                    diffs.append(f"{label} {eid}.{key}: {va!r} vs {vb!r}")
    return diffs


def edited_variant(sketch: dict) -> Tuple[dict, Optional[str]]:
    """Return a copy of `sketch` with ONE entity's geometry nudged (same id), plus
    that entity's id. The nudged entity must cold-start; the rest warm-start."""
    s = copy.deepcopy(sketch)
    ents = s.get("entities", [])
    for e in ents:
        # Nudge the first entity that carries a movable coordinate field.
        for fld in ("p1", "center", "p"):
            if fld in e and isinstance(e[fld], list) and len(e[fld]) >= 2:
                e[fld] = [e[fld][0] + 7.0, e[fld][1] + 5.0]
                return s, e["id"]
        if "radius" in e:
            e["radius"] = float(e["radius"]) + 3.0
            return s, e["id"]
    return s, None


# --- main --------------------------------------------------------------------

def load_named(corpus: Path, names: List[str]) -> List[Tuple[str, dict]]:
    out: List[Tuple[str, dict]] = []
    for name in names:
        p = corpus / f"{name}.json"
        if not p.exists():
            sys.stderr.write(f"warm_diff: fixture {name} not found at {p}\n")
            continue
        with open(p, "r", encoding="utf-8") as f:
            out.append((name, json.load(f)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tol", type=float, default=1e-6)
    ap.add_argument("--cli", type=str, default=None)
    ap.add_argument("--corpus", type=str, default=str(CORPUS_DIR))
    ap.add_argument("--filter", type=str, default=None,
                    help="run all corpus fixtures whose name contains this substring "
                         "(instead of the default short list)")
    args = ap.parse_args()

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    cli = Path(args.cli) if args.cli else _discover_cli()
    if not cli or not cli.exists():
        sys.stderr.write("warm_diff: could not find sketch_cli (build core first).\n")
        return 3

    corpus = Path(args.corpus)
    if args.filter:
        names = sorted(p.stem for p in corpus.glob("*.json") if args.filter in p.stem)
        fixtures = load_named(corpus, names)
    else:
        fixtures = load_named(corpus, _DEFAULT_FIXTURES)
    if not fixtures:
        sys.stderr.write("warm_diff: no fixtures to run.\n")
        return 3

    print(f"warm_diff: {len(fixtures)} fixture(s) | cli={cli} | tol={args.tol:g}")
    print("-" * 72)

    n_pass = 0
    failures: List[str] = []
    for name, fx in fixtures:
        sketch = fx.get("sketch", {})
        env = fx.get("env", {})
        diffs: List[str] = []

        try:
            # ---- Python: cold, then warm-from-self (unchanged) ----
            cold = py_solve(sketch, env, warm=None)
            warm_self = py_solve(sketch, env, warm=cold.state)
            # ---- Python: edited variant warm-from-base + gate decision ----
            after, edited_id = edited_variant(sketch)
            warm_edit_py = py_solve(after, env, warm=cold.state)
            dec_py = py_decision(after, env, cold.state)
        except Exception as e:
            print(f"FAIL  {name}\n      python raised: {e}")
            failures.append(name)
            continue

        # (b-Python) warm-from-self must equal cold for an UNCHANGED sketch.
        diffs += coord_diffs(cold.entities, warm_self.entities, args.tol,
                             "py warm-self != cold")

        # ---- C++ unchanged (after == sketch) ----
        try:
            cpp_same = run_cpp_warm(cli, sketch, sketch, env)
        except Exception as e:
            print(f"FAIL  {name}\n      C++ warm(same) raised: {e}")
            failures.append(name)
            continue

        # (a) C++ warm coords vs Python warm-self coords.
        diffs += coord_diffs(warm_self.entities, cpp_same["warm"]["entities"], args.tol,
                             "py-warm vs cpp-warm (same)")
        # (b-C++) C++ warm == C++ cold for the unchanged sketch.
        diffs += coord_diffs(cpp_same["cold"]["entities"], cpp_same["warm"]["entities"],
                             args.tol, "cpp warm != cold (same)")
        # All entities must warm-start when nothing changed.
        for eid, d in cpp_same["decision"].items():
            if d != "warm":
                diffs.append(f"cpp decision(same) {eid}={d!r}, expected 'warm'")

        # ---- C++ edited variant warm-from-base ----
        try:
            cpp_edit = run_cpp_warm(cli, sketch, after, env)
        except Exception as e:
            print(f"FAIL  {name}\n      C++ warm(edit) raised: {e}")
            failures.append(name)
            continue

        # (a) C++ warm-edit coords vs Python warm-edit coords.
        diffs += coord_diffs(warm_edit_py.entities, cpp_edit["warm"]["entities"], args.tol,
                             "py-warm vs cpp-warm (edit)")
        # (c) GATING DECISION parity: Python vs C++, and the edited entity is 'cold'.
        dec_cpp = cpp_edit["decision"]
        if dec_py != dec_cpp:
            keys = sorted(set(dec_py) | set(dec_cpp))
            for k in keys:
                if dec_py.get(k) != dec_cpp.get(k):
                    diffs.append(f"decision[{k}]: py={dec_py.get(k)!r} cpp={dec_cpp.get(k)!r}")
        if edited_id is not None and dec_cpp.get(edited_id) != "cold":
            diffs.append(f"edited entity {edited_id} should cold-start, "
                         f"cpp says {dec_cpp.get(edited_id)!r}")

        if not diffs:
            n_entities = len(sketch.get("entities", []))
            print(f"PASS  {name}  (entities={n_entities}, edited={edited_id}, "
                  f"decision={dec_cpp})")
            n_pass += 1
        else:
            print(f"FAIL  {name}")
            for d in diffs:
                print(f"      - {d}")
            failures.append(name)

    print("-" * 72)
    total = len(fixtures)
    print(f"summary: {n_pass}/{total} passed"
          + (f", FAILED: {failures}" if failures else ""))
    return 0 if n_pass == total else 1


if __name__ == "__main__":
    _reexec_if_needed()
    sys.exit(main())
