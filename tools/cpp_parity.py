"""cpp_parity.py — Python-engine vs C++-core (libaicad.dll) parity dashboard.

The strangler migration (docs/CPP_MIGRATION.md) cuts a feature over to the C++
core only once it matches the Python oracle. This is the offline gate that
measures that, fixture by fixture:

  - Python ORACLE : engine.doc.compiler.compile_document(doc) -> per-feature
                    status + body list (the production compiler).
  - C++  CANDIDATE: libaicad.dll aicad_call("doc_compile_cpp", {"doc": doc})
                    -> per-feature status + body_count + AICM bytes.

For each fixture it diffs per-feature ok and the surviving body count, and prints
a table. Run under the project venv (it imports the engine):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_parity.py \
        --dll core\build-occt\bin\Release\libaicad.dll

Exit code is the number of fixtures that did NOT fully match (0 = full parity),
so it doubles as a CI gate over whatever fixture set is in parity range.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- fixtures
# Part trees in the shape BOTH engines consume (mirrors core/tests/test_document.cpp).
# These exercise the ported C++ subset; as more features land, add fixtures here.

def _line(i, x1, y1, x2, y2):
    return {"id": i, "type": "line", "construction": False,
            "p1": [x1, y1], "p2": [x2, y2]}


def _circle(i, cx, cy, r):
    return {"id": i, "type": "circle", "construction": False,
            "center": [cx, cy], "radius": r}


def _rel(i, t, refs):
    return {"id": i, "type": t, "refs": refs}


def _dim(i, t, refs, value):
    return {"id": i, "type": t, "refs": refs,
            "value": {"value": value, "expr": None}, "driven": False}


def _rect(x0, y0, w, h):
    ents = [_line("e1", x0, y0, x0 + w, y0), _line("e2", x0 + w, y0, x0 + w, y0 + h),
            _line("e3", x0 + w, y0 + h, x0, y0 + h), _line("e4", x0, y0 + h, x0, y0)]
    cons = [_rel("c1", "coincident", ["e1.p2", "e2.p1"]),
            _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
            _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
            _rel("c4", "coincident", ["e4.p2", "e1.p1"]),
            _rel("c5", "horizontal", ["e1"]), _rel("c6", "horizontal", ["e3"]),
            _rel("c7", "vertical", ["e2"]), _rel("c8", "vertical", ["e4"]),
            _rel("c9", "fix", ["e1.p1"]),
            _dim("c10", "distance", ["e1.p1", "e1.p2"], w),
            _dim("c11", "distance", ["e2.p1", "e2.p2"], h)]
    return ents, cons


# Canonical schema (engine/doc/schema.py): a feature is {id, type, params:{...},
# refs:{slot: <selector|datum>}}. sketch.refs.plane = a reserved datum string
# ("plane.top"); extrude.refs.profile = a SEL_LIST of region selectors
# {"kind":"region","sketch":fid}. This is the shape the host actually sends and
# the C++ core must converge to (see docs/CPP_MIGRATION.md "schema convergence").
def _sketch(fid, plane_datum, ents, cons):
    return {"id": fid, "type": "sketch",
            "params": {"entities": ents, "constraints": cons},
            "refs": {"plane": plane_datum}}


def _extrude(fid, sketch_fid, depth):
    return {"id": fid, "type": "extrude",
            "params": {"mode": "new", "depth": depth},
            "refs": {"profile": [{"kind": "region", "sketch": sketch_fid}]}}


def fixtures():
    out = {}

    ents, cons = _rect(0, 0, 30, 20)
    out["box"] = {"features": [
        _sketch("s1", "plane.top", ents, cons), _extrude("x1", "s1", 25.0)]}

    out["cylinder"] = {"features": [
        _sketch("s1", "plane.top", [_circle("e1", 0, 0, 10)],
                [_rel("c1", "fix", ["e1.center"]), _dim("c2", "radius", ["e1"], 10)]),
        _extrude("x1", "s1", 40.0)]}

    re, rc = _rect(0, 0, 40, 40)
    ce = [_circle("h1", 20, 20, 8)]
    cc = [_dim("hc", "radius", ["h1"], 8)]
    out["washer"] = {"features": [
        _sketch("s1", "plane.top", re + ce, rc + cc), _extrude("x1", "s1", 10.0)]}

    return out


# --------------------------------------------------------------------------- C++ DLL
class CppCore:
    def __init__(self, dll_path: Path):
        dll_path = dll_path.resolve()
        if hasattr(os, "add_dll_directory"):
            # Let the DLL's OCCT sibling DLLs (TK*, jemalloc, tbb12) resolve.
            os.add_dll_directory(str(dll_path.parent))
        self.lib = ctypes.CDLL(str(dll_path))
        self.lib.aicad_call.restype = ctypes.c_int
        self.lib.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.POINTER(ctypes.c_char_p)]
        self.lib.aicad_free.restype = None
        self.lib.aicad_free.argtypes = [ctypes.c_char_p]
        self.lib.aicad_abi_version.restype = ctypes.c_char_p

    def abi(self) -> str:
        return self.lib.aicad_abi_version().decode()

    def call(self, method: str, params: dict) -> tuple[int, dict]:
        out = ctypes.c_char_p()
        rc = self.lib.aicad_call(method.encode(), json.dumps(params).encode(),
                                 ctypes.byref(out))
        data = {}
        if out.value:
            try:
                data = json.loads(out.value.decode())
            finally:
                self.lib.aicad_free(out)
        return rc, data


# --------------------------------------------------------------------------- oracle
def python_compile(doc: dict) -> tuple[dict[str, bool], int, str | None]:
    """Run the Python production compiler. Returns (id->ok, body_count, error)."""
    try:
        from engine.doc.compiler import compile_document  # noqa: WPS433
    except Exception as exc:  # pragma: no cover
        return {}, 0, f"import engine failed: {exc}"
    try:
        res = compile_document(doc)
    except Exception as exc:
        return {}, 0, f"compile raised: {exc}"
    status = {s["id"]: bool(s.get("ok")) for s in res.feature_status}
    return status, len(res.bodies), None


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"),
                    help="path to libaicad.dll")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO))

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[parity] libaicad.dll not found at {dll}\n"
              f"         build it: cmake --build core/build-occt --config Release --target aicad")
        return 2
    core = CppCore(dll)
    print(f"[parity] libaicad ABI={core.abi()}  dll={dll}")

    fx = fixtures()
    name_w = max(len(n) for n in fx)
    print(f"\n{'fixture'.ljust(name_w)}  {'py(ok/feat,bodies)':>20}  {'cpp(ok/feat,bodies)':>20}  verdict")
    print("-" * (name_w + 50))

    mismatches = 0
    for name, doc in fx.items():
        py_status, py_bodies, py_err = python_compile(doc)
        rc, cpp = core.call("doc_compile_cpp", {"doc": doc})
        cpp_status = {s["id"]: bool(s.get("ok")) for s in cpp.get("statuses", [])}
        cpp_bodies = cpp.get("body_count", 0)

        py_ok = sum(1 for v in py_status.values() if v)
        cpp_ok = sum(1 for v in cpp_status.values() if v)

        diffs = []
        if py_err:
            diffs.append(f"py-error:{py_err[:40]}")
        if rc != 0 and not cpp_status:
            diffs.append(f"cpp-error:{cpp.get('error','?')[:40]}")
        for fid in py_status:
            if py_status[fid] != cpp_status.get(fid, False):
                diffs.append(fid)
        if py_bodies != cpp_bodies:
            diffs.append(f"bodies {py_bodies}!={cpp_bodies}")

        verdict = "MATCH" if not diffs else "DIFF: " + ", ".join(diffs)
        if diffs:
            mismatches += 1
        print(f"{name.ljust(name_w)}  {f'{py_ok}/{len(py_status)},{py_bodies}':>20}"
              f"  {f'{cpp_ok}/{len(cpp_status)},{cpp_bodies}':>20}  {verdict}")

    print(f"\n[parity] {len(fx) - mismatches}/{len(fx)} fixtures at full parity")
    return mismatches


if __name__ == "__main__":
    raise SystemExit(main())
