#!/usr/bin/env python3
"""Generate the golden parity fixtures for the tolerance/mechanical/materials/dfm ports.

Runs the LIVE Python oracle (engine_oracle via the root `engine` symlink) and writes
canonical (sort_keys, indent=2) JSON goldens next to this script. The C++ doctests
(test_tolerance / test_mechanical / test_materials / test_dfm) replay each case
through the core `call(method, params, result, err)` dispatch entry and require deep
JSON equality with these envelopes.

Envelopes mirror the MCP wrappers exactly:
  * tolerance  — services/mcp/tolerance_tools.py (mode validation, mode scoping,
                 invalid_mode / invalid_input codes)
  * mechanical — services/mcp/mechanical_tools.py ({"ok": True, **to_dict()},
                 _err() -> {"ok": false, "error"})
  * materials  — {"ok": true, "result": <python value or null>} (dispatch shape)
  * dfm        — {"ok": true, "result": ...} / _err() -> {"ok": false, "error"}

Usage:  python3 core/tests/data/gen_r4e6_fixtures.py
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
DATA = pathlib.Path(__file__).resolve().parent

from engine.tolerance.stackup import stackup_analyze as _py_stackup  # noqa: E402
from engine.mechanical.gear_check import gear_check as _py_gear  # noqa: E402
from engine.mechanical.bearing_life import bearing_life as _py_bearing  # noqa: E402
from engine.materials import registry as _py_reg  # noqa: E402
from engine.dfm import process_profiles as _py_pp  # noqa: E402


def _dump(name: str, cases) -> None:
    path = DATA / name
    path.write_text(json.dumps(cases, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"wrote {path} ({len(cases)} cases)")


# --------------------------------------------------------------------- tolerance
def tol_envelope(dims, mode="both"):
    """services/mcp/tolerance_tools.py::stackup_analyze, minus the FastMCP plumbing."""
    valid_modes = {"worst_case", "rss", "both"}
    if mode not in valid_modes:
        return {"ok": False,
                "error": f"Invalid mode {mode!r}. Choose: {sorted(valid_modes)}",
                "code": "invalid_mode"}
    try:
        out = _py_stackup(dims).to_dict()
        if mode == "worst_case":
            out.pop("rss", None)
        elif mode == "rss":
            out.pop("worst_case", None)
        return out
    except (ValueError, TypeError, KeyError) as e:
        return {"ok": False, "error": str(e), "code": "invalid_input"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


DIMS_3 = [
    {"name": "A_slot_width",   "nominal": 40.000, "tol": 0.050, "direction":  1.0},
    {"name": "B_block1_width", "nominal": 17.000, "tol": 0.025, "direction": -1.0},
    {"name": "C_block2_width", "nominal": 20.000, "tol": 0.040, "direction": -1.0},
]

tol_cases = []
def tol(name, dims, mode="both"):
    tol_cases.append({"name": name,
                      "params": {"dims": dims, "mode": mode},
                      "result": tol_envelope(dims, mode)})

tol("textbook_3dim", DIMS_3)
tol("textbook_3dim_wc", DIMS_3, "worst_case")
tol("textbook_3dim_rss", DIMS_3, "rss")
tol("single_dim", [{"name": "d", "nominal": 10.0, "tol": 0.1}])
tol("mixed_assumed_ref_clamp", [
    {"name": "housing", "nominal": 50.0, "tol": 0.05, "ref": "F12", "assumed": True},
    {"name": "gasket", "nominal": 2.0, "tol": 0.20, "direction": -1.0},
    {"name": "cover", "nominal": 1.5, "tol": 0.10, "direction": 2.5},   # clamped to +1
    {"name": "zero_dir", "nominal": 3.25, "tol": 0.02, "direction": 0},  # clamped to +1
])
tol("zero_tol", [{"name": "d1", "nominal": 5.0, "tol": 0.0},
                 {"name": "d2", "nominal": 5.0, "tol": 0.1}])
tol("equal_tols_4", [{"name": f"d{i}", "nominal": 1.0, "tol": 0.1} for i in range(4)])
tol("tie_stable_sort", [
    {"name": "first", "nominal": 1.0, "tol": 0.10},
    {"name": "second", "nominal": 2.0, "tol": 0.10},
])
tol("int_inputs", [{"name": "a", "nominal": 40, "tol": 1, "direction": 1}])
tol("err_empty", [])
tol("err_negative_tol", [{"name": "d", "nominal": 5.0, "tol": -0.1}])
tol("err_missing_nominal", [{"name": "d", "tol": 0.1}])
tol("err_missing_tol", [{"name": "d", "nominal": 0.1}])
tol("err_bad_mode", DIMS_3, "wc")
_dump("tolerance.json", tol_cases)


# -------------------------------------------------------------------- mechanical
def mech_envelope(fn, **kw):
    """services/mcp/mechanical_tools.py: {"ok": True, **to_dict()} / _err()."""
    try:
        return {"ok": True, **fn(**kw).to_dict()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


mech_cases = []
def mech(name, method, **kw):
    mech_cases.append({"name": name, "params": kw,
                       "result": mech_envelope(_py_gear if method == "gear_check"
                                               else _py_bearing, **kw)})
    mech_cases[-1]["method"] = method


# Shigley's Ch. 14 worked example (docstring vector).
mech("gear_shigley_example", "gear_check",
     Wt=1000, module=4, face_width=50, pitch_diameter=100,
     J=0.36, I=0.107, Ko=1.25, Kv=1.52, Ks=1.0, Km=1.8, Cf=1.0)
mech("gear_with_allowables", "gear_check",
     Wt=2500, module=3.0, face_width=40.0, pitch_diameter=75.0,
     J=0.32, I=0.11, Ko=1.5, Kv=1.3, Ks=1.1, Km=1.4, KB=1.2, Cf=1.1, Cp=191.0,
     allowable_bending_stress=200.0, allowable_contact_stress=1200.0)
mech("gear_required_only", "gear_check",
     Wt=500, module=2.5, face_width=25.0, pitch_diameter=60.0, J=0.4, I=0.12)
mech("gear_err_geometry", "gear_check",
     Wt=500, module=0, face_width=25.0, pitch_diameter=60.0, J=0.4, I=0.12)
mech("gear_err_factors", "gear_check",
     Wt=500, module=2.5, face_width=25.0, pitch_diameter=60.0, J=0, I=0.12)
mech("gear_err_wt", "gear_check",
     Wt=-5, module=2.5, face_width=25.0, pitch_diameter=60.0, J=0.4, I=0.12)
# Shigley's Ch. 11 worked example (docstring vector).
mech("bearing_ball_radial", "bearing_life",
     C=30000, Fr=8000, Fa=0, n_rpm=1200, bearing_type="ball")
mech("bearing_roller_mixed", "bearing_life",
     C=50000, Fr=10000, Fa=2500, n_rpm=900, bearing_type="roller",
     X=0.4, Y=1.5, bearing_designation="NU205")
mech("bearing_case_strip", "bearing_life",
     C=20000, Fr=5000, bearing_type=" Ball ")
mech("bearing_err_c", "bearing_life", C=0, Fr=8000)
mech("bearing_err_loads", "bearing_life", C=30000, Fr=0, Fa=0)
mech("bearing_err_type", "bearing_life", C=30000, Fr=8000, bearing_type="needle")
mech("bearing_err_p", "bearing_life", C=30000, Fr=8000, X=0.0, Y=0.0)
mech("bearing_err_nrpm", "bearing_life", C=30000, Fr=8000, n_rpm=0)
_dump("mechanical.json", mech_cases)


# --------------------------------------------------------------------- materials
mat_cases = []
def mat(case, method, **kw):
    fn = {
        "material_get": lambda: _py_reg.get(kw.get("name", "")),
        "material_density": lambda: _py_reg.density(kw.get("name", "")),
        "material_resolve_key": lambda: _py_reg.resolve_key(kw.get("name", "")),
        "material_fea_table": _py_reg.fea_table,
        "material_fea_aliases": _py_reg.fea_aliases,
        "material_cam_data": lambda: _py_reg.cam_data(kw.get("name", "")),
        "material_cam_id": lambda: _py_reg.cam_material_id(kw.get("name", "")),
        "material_cam_materials": _py_reg.cam_materials,
        "material_all": _py_reg.all_materials,
    }[method]
    mat_cases.append({"name": case, "method": method, "params": kw,
                      "result": {"ok": True, "result": fn()}})

mat("get_6061", "material_get", name="6061")
mat("get_alias_case", "material_get", name=" Aluminum 6061-T6 ")
mat("get_unknown", "material_get", name="unobtanium-9000")
mat("density_pla", "material_density", name="pla")
mat("density_unknown", "material_density", name="unobtanium-9000")
mat("resolve_abs_alias", "material_resolve_key", name="ABS")
mat("resolve_unknown", "material_resolve_key", name="unobtanium-9000")
mat("fea_table", "material_fea_table")
mat("fea_aliases", "material_fea_aliases")
mat("cam_data_6061", "material_cam_data", name="6061")
mat("cam_data_no_cam", "material_cam_data", name="pla")
mat("cam_id_6061", "material_cam_id", name="6061")
mat("cam_id_no_cam", "material_cam_id", name="pla")
mat("cam_materials", "material_cam_materials")
_dump("materials.json", mat_cases)


# --------------------------------------------------------------------------- dfm
def dfm_ok(fn):
    try:
        return {"ok": True, "result": fn()}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


dfm_cases = []
def dfm(name, method, params, fn):
    dfm_cases.append({"name": name, "method": method, "params": params,
                      "result": dfm_ok(fn)})


for proc in _py_pp.KNOWN_PROCESSES:
    dfm(f"profile_{proc}", "dfm_get_profile", {"process": proc},
        lambda p=proc: _py_pp.get_profile(p))
dfm("profile_case_norm", "dfm_get_profile", {"process": "  FDM "},
    lambda: _py_pp.get_profile("  FDM "))
dfm("profile_overrides", "dfm_get_profile",
    {"process": "fdm", "overrides": {"hole_comp": 0.30, "xy_comp": 0.12, "bogus": 1, "zzz": "x"}},
    lambda: _py_pp.get_profile("fdm", overrides={"hole_comp": 0.30, "xy_comp": 0.12,
                                                 "bogus": 1, "zzz": "x"}))
dfm("err_profile_unknown", "dfm_get_profile", {"process": "lost_wax"},
    lambda: _py_pp.get_profile("lost_wax"))

dfm("known_processes", "dfm_known_processes", {}, lambda: _py_pp.KNOWN_PROCESSES)

dfm("fit_fdm_slip", "dfm_fit_clearance", {"process": "fdm", "fit_class": "slip"},
    lambda: _py_pp.fit_clearance("fdm", "slip"))
dfm("fit_cnc_press", "dfm_fit_clearance", {"process": "cnc_mill", "fit_class": "press"},
    lambda: _py_pp.fit_clearance("cnc_mill", "press"))
dfm("err_fit_sheet", "dfm_fit_clearance", {"process": "sheet_metal", "fit_class": "slip"},
    lambda: _py_pp.fit_clearance("sheet_metal", "slip"))
dfm("err_fit_unknown_class", "dfm_fit_clearance", {"process": "fdm", "fit_class": "snug"},
    lambda: _py_pp.fit_clearance("fdm", "snug"))
dfm("err_fit_unknown_process", "dfm_fit_clearance",
    {"process": "lost_wax", "fit_class": "slip"},
    lambda: _py_pp.fit_clearance("lost_wax", "slip"))

dfm("modeled_fdm_slip_hole", "dfm_modeled_dim",
    {"nominal_mm": 10.0, "fit_class": "slip", "process": "fdm", "is_hole": True},
    lambda: _py_pp.modeled_dim(10.0, "slip", "fdm", is_hole=True))
dfm("modeled_fdm_slip_shaft", "dfm_modeled_dim",
    {"nominal_mm": 10.0, "fit_class": "slip", "process": "fdm"},
    lambda: _py_pp.modeled_dim(10.0, "slip", "fdm"))
dfm("modeled_cnc_press", "dfm_modeled_dim",
    {"nominal_mm": 12.0, "fit_class": "press", "process": "cnc_mill"},
    lambda: _py_pp.modeled_dim(12.0, "press", "cnc_mill"))

RESOLVE = [
    ("resolve_explicit_print", {"request": "3d print a bracket"}, {}),
    ("resolve_must_block_metal", {"request": "3d print this steel gear"}, {}),
    ("resolve_explicit_machine_from", {"request": "machine from aluminum"}, {}),
    ("resolve_explicit_laser", {"request": "laser cut a gasket"}, {}),
    ("resolve_metal_sheet_cue", {"request": "constant 2mm steel plate with bends"}, {}),
    ("resolve_metal_two_strong", {"request": "threaded steel plate, constant thickness"}, {}),
    ("resolve_metal_no_cues", {"request": "a steel widget"}, {}),
    ("resolve_within_margin", {"request": "bracket"}, {}),
    ("resolve_decisive_cnc", {"request": "threaded flat plate, precision"}, {}),
    ("resolve_plastic", {"request": "plastic housing"}, {}),
    ("resolve_default_fdm", {"request": "a knob"}, {}),
    ("resolve_injection_qty_text", {"request": "draft housing, 5000 units"}, {}),
    ("resolve_explicit_turned", {"request": "turned shaft from round bar"}, {}),
    ("resolve_material_arg_metal", {"request": "housing", "material": "aluminum"}, {}),
    ("resolve_qty_arg", {"request": "widget", "quantity": 5000}, {}),
    ("resolve_dict_spec",
     {"request": {"object": "bracket with bends and flanges", "features": ["2mm plate"]},
      "material": "steel", "quantity": 25}, {}),
    ("resolve_lathe_vs_cnc_tie", {"request": "threaded turned shaft"}, {}),
]
for name, params, _ in RESOLVE:
    dfm(name, "dfm_resolve_process", params,
        lambda p=params: _py_pp.resolve_process(
            p["request"], material=p.get("material"), quantity=p.get("quantity")))

for proc in ("fdm", "injection", "sheet_metal", "cnc_mill"):
    prof = _py_pp.get_profile(proc)
    dfm(f"block_{proc}", "dfm_process_block_text", {"profile": prof},
        lambda p=prof: _py_pp.process_block_text(p))
prof = _py_pp.get_profile("fdm")
prof["assumed"] = True
dfm("block_assumed", "dfm_process_block_text", {"profile": prof},
    lambda: _py_pp.process_block_text(prof))
_dump("dfm.json", dfm_cases)
