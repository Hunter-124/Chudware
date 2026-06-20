"""cpp_validate_parity.py — Python-vs-C++ document VALIDATION parity (P15).

Drives a corpus of part documents (one valid baseline + many targeted mutations,
valid and invalid) through BOTH the Python oracle (engine.doc.schema.validate_document)
and the C++ core (libaicad.dll doc_validate → core/doc/validate.cpp), and checks the
ACCEPT/REJECT verdict matches on every document. The load-bearing contract is that the
C++ validator returns errors iff Python does (exact error strings are not compared).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_validate_parity.py

Exit code = number of documents whose verdict diverged (0 = full parity).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


def base_doc() -> dict:
    return {
        "schema": "aicad.part/1", "id": "doc1", "name": "Part1",
        "units": {"system": "MMGS"}, "material": None,
        "equations": {"globals": [{"name": "W", "expr": "40"}]},
        "features": [
            {"id": "f1", "type": "sketch", "name": "Sketch1", "suppressed": False,
             "params": {"entities": [
                 {"id": "e1", "type": "line", "construction": False, "p1": [0, 0], "p2": [40, 0]},
                 {"id": "e2", "type": "circle", "construction": False, "center": [10, 10], "radius": 5},
                 {"id": "e3", "type": "arc", "construction": False, "center": [0, 0],
                  "radius": 5, "start": 0, "end": 90, "ccw": True},
             ], "constraints": [
                 {"id": "c1", "type": "coincident", "refs": ["e1.p2", "e2.center"]},
                 {"id": "c2", "type": "radius", "refs": ["e2"], "value": {"value": 5, "expr": None}},
                 {"id": "c3", "type": "distance", "refs": ["e1.p1", "e1.p2"],
                  "value": {"value": 40, "expr": "W"}},
             ]},
             "refs": {"plane": "plane.top"}},
            {"id": "f2", "type": "extrude", "name": "Boss1", "suppressed": False,
             "params": {"mode": "boss", "end": "blind", "depth": {"value": 10, "expr": None}},
             "refs": {"profile": [{"kind": "region", "sketch": "f1", "centroid": [20, 15],
                                   "area": 1200.0, "hint_index": 0}]}},
        ],
    }


def mut(fn):
    d = base_doc()
    fn(d)
    return d


def corpus():
    cases = [("valid baseline", base_doc())]

    # --- valid variants (must NOT be rejected) ---
    cases.append(("valid expr W*2", mut(lambda d: d["features"][0]["params"]["constraints"][2]
                                        .__setitem__("value", {"value": 80, "expr": "W*2"}))))
    cases.append(("valid expr 1/0 (grammar ok)", mut(lambda d: d["features"][0]["params"]["constraints"][2]
                                                      .__setitem__("value", {"value": 1, "expr": "1/0"}))))
    cases.append(("valid rollback f1", mut(lambda d: d.__setitem__("rollback_after", "f1"))))
    cases.append(("valid config", mut(lambda d: (d.__setitem__("configurations", [
        {"name": "Big", "param_overrides": {"f2.depth": {"value": 20, "expr": None}}, "suppressions": {"f1": False}}]),
        d.__setitem__("active_configuration", "Big")))))
    cases.append(("valid appearances obj", mut(lambda d: d.__setitem__(
        "appearances", {"f2": {"color": [0.2, 0.4, 0.6], "metallic": 0.0, "roughness": 0.5}}))))
    cases.append(("valid origin handle", mut(lambda d: d["features"][0]["params"]["constraints"][0]
                                             .__setitem__("refs", ["e1.p1", "origin"]))))

    # --- invalid variants (must be rejected) ---
    cases.append(("bad units system", mut(lambda d: d["units"].__setitem__("system", "ZZZ"))))
    cases.append(("missing id", mut(lambda d: d.pop("id"))))
    cases.append(("empty name", mut(lambda d: d.__setitem__("name", ""))))
    cases.append(("material not {key}", mut(lambda d: d.__setitem__("material", "steel"))))
    cases.append(("unknown schema", mut(lambda d: d.__setitem__("schema", "aicad.bogus/9"))))
    cases.append(("unknown feature type", mut(lambda d: d["features"][1].__setitem__("type", "bogus"))))
    cases.append(("missing required param", mut(lambda d: d["features"][1]["params"].pop("mode"))))
    cases.append(("unknown ref slot", mut(lambda d: d["features"][1]["refs"].__setitem__("bogus", "x"))))
    cases.append(("required ref missing", mut(lambda d: d["features"][1]["refs"].pop("profile"))))
    cases.append(("bad selector kind", mut(lambda d: d["features"][1]["refs"]["profile"][0]
                                           .__setitem__("kind", "blah"))))
    cases.append(("region missing sketch", mut(lambda d: d["features"][1]["refs"]["profile"][0].pop("sketch"))))
    cases.append(("empty profile list", mut(lambda d: d["features"][1]["refs"].__setitem__("profile", []))))
    cases.append(("bad entity type", mut(lambda d: d["features"][0]["params"]["entities"][0]
                                        .__setitem__("type", "weird"))))
    cases.append(("entity missing field", mut(lambda d: d["features"][0]["params"]["entities"][1].pop("radius"))))
    cases.append(("bad entity id", mut(lambda d: d["features"][0]["params"]["entities"][0]
                                      .__setitem__("id", "x1"))))
    cases.append(("dup entity id", mut(lambda d: d["features"][0]["params"]["entities"][1]
                                      .__setitem__("id", "e1"))))
    cases.append(("bad construction", mut(lambda d: d["features"][0]["params"]["entities"][0]
                                         .__setitem__("construction", "no"))))
    cases.append(("constraint bad arity", mut(lambda d: d["features"][0]["params"]["constraints"][0]
                                             .__setitem__("refs", ["e1.p2"]))))
    cases.append(("unknown entity handle", mut(lambda d: d["features"][0]["params"]["constraints"][0]
                                              .__setitem__("refs", ["e9.p2", "e2.center"]))))
    cases.append(("bad sub handle", mut(lambda d: d["features"][0]["params"]["constraints"][0]
                                       .__setitem__("refs", ["e1.center", "e2.center"]))))
    cases.append(("dim missing value", mut(lambda d: d["features"][0]["params"]["constraints"][1]
                                          .__setitem__("value", {"expr": None}))))
    cases.append(("dim bad expr syntax", mut(lambda d: d["features"][0]["params"]["constraints"][2]
                                            .__setitem__("value", {"value": 1, "expr": "foo("}))))
    cases.append(("dim unknown name", mut(lambda d: d["features"][0]["params"]["constraints"][2]
                                         .__setitem__("value", {"value": 1, "expr": "ZZ+1"}))))
    cases.append(("equations dup name", mut(lambda d: d["equations"]["globals"]
                                           .append({"name": "W", "expr": "5"}))))
    cases.append(("equations bad expr", mut(lambda d: d["equations"]["globals"][0]
                                           .__setitem__("expr", "*"))))
    cases.append(("non-value constraint has value", mut(lambda d: d["features"][0]["params"]["constraints"][0]
                                                       .__setitem__("value", {"value": 1, "expr": None}))))
    cases.append(("rollback bad fid", mut(lambda d: d.__setitem__("rollback_after", "f9"))))
    cases.append(("config empty name", mut(lambda d: d.__setitem__("configurations", [{"name": ""}]))))
    cases.append(("config bad override key", mut(lambda d: d.__setitem__("configurations", [
        {"name": "C", "param_overrides": {"f9.depth": {"value": 1, "expr": None}}}]))))
    cases.append(("appearance out of range", mut(lambda d: d.__setitem__("appearances", {"f1": [2.0, 0.0, 0.0]}))))
    cases.append(("feature missing suppressed", mut(lambda d: d["features"][1].pop("suppressed"))))
    cases.append(("feature bad id", mut(lambda d: d["features"][1].__setitem__("id", "x9"))))
    cases.append(("fillet missing edges+radius", mut(lambda d: d["features"].append(
        {"id": "f3", "type": "fillet", "name": "F", "suppressed": False, "params": {}, "refs": {}}))))
    cases.append(("eq_curve bad expr", mut(lambda d: d["features"][0]["params"]["entities"].append(
        {"id": "e4", "type": "eq_curve", "construction": False, "xt": "t", "yt": "nope(t)",
         "t1": 0, "t2": 1}))))
    return cases


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[validate-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[validate-parity] libaicad ABI={core.abi()}")

    from engine.doc.schema import validate_document as py_validate

    mism = 0
    cases = corpus()
    for label, doc in cases:
        py_errs = py_validate(doc)
        py_valid = (len(py_errs) == 0)
        rc, r = core.call("doc_validate", {"doc": doc})
        cpp_errs = r.get("errors", []) if isinstance(r, dict) else []
        cpp_valid = bool(r.get("ok")) if isinstance(r, dict) else False
        if py_valid == cpp_valid:
            verdict = "valid" if py_valid else "reject"
            print(f"  {label:<34} MATCH ({verdict})")
        else:
            mism += 1
            pe = "" if py_valid else f" py:{py_errs[0][:50]}"
            ce = "" if cpp_valid else f" cpp:{cpp_errs[0][:50] if cpp_errs else ''}"
            print(f"  {label:<34} DIFF py_valid={py_valid} cpp_valid={cpp_valid}{pe}{ce}")

    total = len(cases)
    print(f"\n[validate-parity] {total - mism}/{total} documents at verdict parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
