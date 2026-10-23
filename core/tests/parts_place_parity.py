#!/usr/bin/env python3
"""place.py parity harness — drives the Python oracle
(engine_oracle/parts_hub/place.py) and the C++ port (core/parts/place.cpp via
test_parts_place --parity-dump) on the SAME fixture placements and diffs the
PlacementResult envelopes + resulting document trees.

Legs covered (the doc legs run the REAL ecad/piping/hydraulic layers on both
sides; the RPC legs use identical stubs — the same monkeypatch shape as
services/parts/test_parts_hub_p3.py):

  core_family, generate_model, import_file, symbol_footprint,
  section_profile, fitting_id, fmu_component + the error envelopes.

Volatile ids/paths (uuids, uuid-hex element ids, temp file paths) are masked
before comparison.

Usage:
  python3 core/tests/parts_place_parity.py [path/to/test_parts_place]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BIN = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    _REPO / "core" / "build-z-place" / "bin" / "test_parts_place")


# ── seam stubs (mirror the C++ PlaceSeams stubs in test_parts_place.cpp) ─────

def _install_stubs(step_path: str) -> None:
    """Inject a fake services.mcp.core_bridge + engine_oracle.spec_sheet_matcher
    so place.py's RPC legs hit the same canned responses the C++ side uses
    (engine_oracle/spec_sheet_matcher.py is already deleted — its
    generate_model is the C++ sourcing build_model seam). The real
    services.parts hub stays live (engine_oracle.parts_hub re-exports it)."""
    import engine_oracle.parts_hub  # noqa: F401 — binds real services.parts first

    core_bridge = types.ModuleType("services.mcp.core_bridge")

    def _call(verb, params):
        if verb == "parts_make":
            return {
                "ok": True,
                "doc_id": "doc-stub-1",
                "name": "SHCS M8x20",
                "bbox": {"min": [0, 0, 0], "max": [8, 8, 20]},
                "datum": {"base_center": [4, 4, 0], "axis": [0, 0, 1]},
            }
        if verb == "import_file":
            return {"doc_id": "doc-gen-1", "body_count": 1}
        return {}

    core_bridge.call = _call  # type: ignore[attr-defined]
    import services.mcp  # canonical package
    services.mcp.core_bridge = core_bridge  # type: ignore[attr-defined]
    sys.modules["services.mcp.core_bridge"] = core_bridge

    ssm = types.ModuleType("engine_oracle.spec_sheet_matcher")
    ssm.generate_model = lambda match: step_path  # type: ignore[attr-defined]
    sys.modules["engine_oracle.spec_sheet_matcher"] = ssm


# ── Python side: run the same scenarios through the oracle ──────────────────

def _record(gref, domain="fastener"):
    return {"part_id": f"{domain}:X:1", "domain": domain, "title": "fixture",
            "geometry_ref": gref}


def run_python_scenarios(fixtures: dict) -> dict:
    from engine_oracle.parts_hub import place as oracle

    out: dict = {}

    def run(name, part_id, record=None, doc=None, params=None):
        # The C++ port takes the PartRecord pre-fetched (the hub lookup is a
        # seam); feed the oracle the same record by stubbing parts_hub.get.
        if record is not None:
            orig = oracle_place_get(record)
            try:
                out[name] = oracle.place(part_id, doc=doc, params=params)
            finally:
                orig()
        else:
            out[name] = oracle.place(part_id, doc=doc, params=params)

    def oracle_place_get(rec):
        import services.parts as sp
        old = sp.get
        sp.get = lambda _pid: rec
        # place.py does `from ..parts_hub import get` → engine_oracle.parts_hub
        import engine_oracle.parts_hub as ph
        old_ph = ph.get
        ph.get = lambda _pid: rec

        def restore():
            sp.get = old
            ph.get = old_ph
        return restore

    run("core_family", "fastener:ISO4762:M8",
        record=_record({"kind": "core_family", "family": "shcs", "size": "M8"}),
        params={"length_mm": 20})
    run("generate_model", "fastener:BOLTS:M8x25",
        record=_record({"kind": "generate_model",
                        "match": {"source": "bolts", "family": "shcs",
                                  "size": "M8", "length_mm": 25}}))
    run("import_file", "catalog:mcmaster:1234",
        record=_record({"kind": "import_file", "path": fixtures["step_path"]},
                       "catalog"))
    run("symbol_footprint", "ecad:Device:R",
        record=_record({"kind": "symbol_footprint", "libid": "Device:R"}, "ecad"),
        doc=fixtures["sch_doc"], params={"pos": [10, 20], "value": "1k", "ref": "R1"})
    # The oracle mutates the input doc in place; expose it like the C++ port's
    # result["doc"] so the diff can compare document trees.
    out["symbol_footprint"]["doc"] = fixtures["sch_doc"]
    run("section_profile", "section:AISC:W8x31",
        record=_record({"kind": "section_profile", "standard": "AISC",
                        "designation": "W8x31"}, "section"),
        doc=fixtures["struct_doc"],
        params={"kind": "beam", "length_mm": 3000.0, "material": "steel"})
    out["section_profile"]["doc"] = fixtures["struct_doc"]
    run("fitting_id", "fitting:elbow:NPS1",
        record=_record({"kind": "fitting_id", "fitting_id": "elbow:NPS1"}, "fitting"),
        doc=fixtures["piping_doc"], params={"type": "elbow", "nps": 1.0})
    out["fitting_id"]["doc"] = fixtures["piping_doc"]
    run("fmu_component", "hydraulic:pump:gear-1cc",
        record=_record({"kind": "fmu_component", "component_id": "pump:gear-1cc"},
                       "hydraulic"),
        doc=fixtures["hydraulic_doc"],
        params={"type": "pump", "displacement_cc_rev": 1.0})
    out["fmu_component"]["doc"] = fixtures["hydraulic_doc"]

    run("unknown_part_id", "fastener:NONSTD:XXXXX_NOT_REAL")
    run("unknown_kind", "fastener:X:1", record=_record({"kind": "wat"}))
    run("symbol_no_doc", "ecad:Device:R",
        record=_record({"kind": "symbol_footprint", "libid": "Device:R"}, "ecad"))
    run("section_no_doc", "section:AISC:W8x31",
        record=_record({"kind": "section_profile", "standard": "AISC",
                        "designation": "W8x31"}, "section"))
    run("acquire_login_required", "catalog:mcmaster:91292A029",
        record={"part_id": "catalog:mcmaster:91292A029", "domain": "catalog",
                "ok": False, "reason": "login_required", "site": "mcmaster",
                "source_url": "https://www.mcmaster.com/91292A029/",
                "title": "Alloy Steel Socket Head Screw"})
    return out


# ── masking + diff ───────────────────────────────────────────────────────────

_VOLATILE_KEYS = {"step_path", "imported_from"}


def mask(node, _key=None):
    """Recursively mask volatile values: uuids, uuid-hex element ids, paths.

    `lib_cache` is masked wholesale: the C++ ecad port's ensure_lib_def
    (core/ecad/ops.cpp) deliberately skips the engine.ecad.library.resolve leg
    and writes a stub def where the Python oracle resolves a full library def.
    That is a pre-existing core/ecad gap, orthogonal to the place.py port — the
    placed symbol op itself (doc["symbols"]) is compared in full.
    """
    import re
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "lib_cache":
                out[k] = "<LIB_CACHE>"  # see docstring — core/ecad stub-def gap
            elif k in _VOLATILE_KEYS and isinstance(v, str):
                out[k] = "<PATH>"
            elif k in ("id", "doc_id", "entity_id") and isinstance(v, str) and (
                    re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v)
                    or re.fullmatch(r"[0-9a-f]{12}", v)):
                out[k] = "<UUID>"
            else:
                out[k] = mask(v)
        return out
    if isinstance(node, list):
        return [mask(v) for v in node]
    return node


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="chudware_place_parity_"))
    step = tmp / "generated.step"
    step.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
    _install_stubs(str(step))

    # Fixture docs from the PYTHON factories — both sides start byte-identical.
    from engine_oracle.ecad import schema as ecad_schema
    from engine_oracle.structural.elements import new_structure
    from engine_oracle.piping.schema import new_piping
    from engine_oracle.hydraulic.schema import new_hydraulic

    py_fixtures = {
        "step_path": str(step),
        "sch_doc": ecad_schema.new_schematic("TestSchematic"),
        "struct_doc": new_structure("TestStructure"),
        "piping_doc": new_piping("TestPiping"),
        "hydraulic_doc": new_hydraulic("TestHydraulic"),
    }
    fixtures_path = tmp / "fixtures.json"
    fixtures_path.write_text(json.dumps(py_fixtures), encoding="utf-8")

    # C++ side: same fixture docs (fresh, unmutated copies).
    cpp_fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
    out_path = tmp / "cpp_out.json"
    proc = subprocess.run(
        [str(BIN), "--parity-dump", str(fixtures_path), str(out_path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        print("FAIL: test_parts_place --parity-dump exited", proc.returncode)
        return 1
    cpp = json.loads(out_path.read_text(encoding="utf-8"))

    py = run_python_scenarios(py_fixtures)

    failures = 0
    for name in sorted(set(py) | set(cpp)):
        a, b = mask(py.get(name)), mask(cpp.get(name))
        if a == b:
            print(f"  PASS {name}")
        else:
            failures += 1
            print(f"  FAIL {name}")
            print("    python:", json.dumps(a, sort_keys=True, default=str)[:1200])
            print("    c++   :", json.dumps(b, sort_keys=True, default=str)[:1200])
    print(f"\n{'PARITY OK' if not failures else 'PARITY FAIL'}"
          f" — {len(py)} scenarios, {failures} mismatches (fixtures: {tmp})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
