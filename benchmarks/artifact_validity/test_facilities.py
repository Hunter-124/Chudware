"""Facilities tasks F1–F4: oracle unit tests + graders against REAL saved deliverables.

Fixture builders drive the production MCP tools directly (stub-MCP registration, no model,
no daemon: ``CHUDWARE_ENGINE_DAEMON=0``) to write genuine ``deliverable.step+`` files, which
are then graded exactly like a trial: :func:`build_evidence` reopens the file and every
mandatory check runs. A correct deliverable must PASS every check; each adversarial variant
(wrong input, unit mistake, unsaved study, missing/solid geometry, fabricated result,
missing/empty/wrong file) must FAIL — never ERROR, never PASS.

Run: CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest -q \
         benchmarks/artifact_validity/test_facilities.py
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Callable

import pytest

os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"

from benchmarks.artifact_validity import oracles as orc  # noqa: E402
from benchmarks.artifact_validity.evidence import Evidence, build_evidence  # noqa: E402
from benchmarks.artifact_validity.tasks import facilities as fac  # noqa: E402
from benchmarks.artifact_validity.tasks.schema import (  # noqa: E402
    CheckResult, Status, Task, check_ids, task_status)
from services.mcp import core_bridge as cb  # noqa: E402

_HAVE_CORE = cb.available()
live = pytest.mark.skipif(not _HAVE_CORE, reason="libchudware not staged — graders reopen .step+")


# ═══════════════════════════════════════════════════════════ oracle unit tests

def test_fluid_power_reference_values():
    q = orc.pump_flow_lpm(10.0, 1500.0, 0.95)
    assert q == pytest.approx(14.25, abs=1e-12)
    assert orc.circle_area_mm2(50.0) == pytest.approx(1963.495, abs=1e-3)
    v = orc.cylinder_speed_mms(q, 50.0)
    assert v == pytest.approx(120.958, abs=1e-3)
    assert orc.stroke_time_s(300.0, v) == pytest.approx(2.480, abs=1e-3)


def test_colebrook_iteration_matches_moody_and_laminar():
    # Moody/Colebrook reference: Re 1e5, ε/D 1e-4 → f = 0.01851
    assert orc.colebrook(1e5, 1e-4) == pytest.approx(0.01851, rel=1e-3)
    # Haaland's explicit approximation agrees with Colebrook to ~1.5 % at Re 1e6, ε/D 1e-5
    haaland = (-1.8 * math.log10((1e-5 / 3.7) ** 1.11 + 6.9 / 1e6)) ** -2
    assert orc.colebrook(1e6, 1e-5) == pytest.approx(haaland, rel=0.015)
    # the converged value satisfies Colebrook–White itself
    f = orc.colebrook(4e4, 8.7e-4)
    rhs = -2.0 * math.log10(8.7e-4 / 3.7 + 2.51 / (4e4 * math.sqrt(f)))
    assert 1.0 / math.sqrt(f) == pytest.approx(rhs, rel=1e-12)
    assert orc.colebrook(1000.0, 1e-3) == pytest.approx(0.064)


def test_darcy_weisbach_segment_by_hand():
    # 12 m³/h through NPS 2 Sch 40 (ID 52.48 mm): v = Q/A
    seg = orc.pipe_segment(12.0, 52.48, 10.0, 0.0, 998.2, 0.001002, 0.046)
    assert seg["velocity_ms"] == pytest.approx(12.0 / 3600.0 / (math.pi / 4 * 0.05248 ** 2))
    assert seg["reynolds"] == pytest.approx(998.2 * seg["velocity_ms"] * 0.05248 / 0.001002)
    f = seg["friction_factor"]
    dp = (f * 10.0 / 0.05248 + 0.0) * 0.5 * 998.2 * seg["velocity_ms"] ** 2 / 1000.0
    assert seg["dp_kpa"] == pytest.approx(dp)
    with_k = orc.pipe_segment(12.0, 52.48, 10.0, 2.0, 998.2, 0.001002, 0.046)
    assert with_k["dp_kpa"] - seg["dp_kpa"] == pytest.approx(
        2.0 * 0.5 * 998.2 * seg["velocity_ms"] ** 2 / 1000.0)


def test_b31_3_audit_values():
    od, wall = orc.pipe_od_wall_mm(2, "40")
    assert (od, wall) == (60.3, 3.91)
    t = orc.b31_3_pressure_thickness_mm(1.0, od, 137.9)
    assert t == pytest.approx(0.2180, abs=5e-5)
    assert orc.b31_3_required_nominal_mm(t, 1.5, 0.125) == pytest.approx(1.9634, abs=5e-5)
    # MAWP inverts eq. 3a on the available wall
    mawp = orc.b31_3_mawp_kpa(wall, 1.5, 0.125, od, 137.9)
    t_a = wall * 0.875 - 1.5
    assert orc.b31_3_pressure_thickness_mm(mawp / 1000.0, od, 137.9) == pytest.approx(t_a)


def test_asme_viii_audit_values():
    # SA-516-70 S = 138 MPa, E = 0.85, P = 1000 kPa, CA 3 on R = 753 / D = 1506 (audit C.3)
    assert orc.ug27_shell_thickness_mm(1.0, 753.0, 138.0, 0.85) + 3.0 == pytest.approx(
        9.4524, abs=5e-5)
    assert orc.ug32d_ellipsoidal_thickness_mm(1.0, 1506.0, 138.0, 0.85) + 3.0 == pytest.approx(
        9.4249, abs=5e-5)
    assert orc.vessel_volume_m3(1500.0, 3000.0) == pytest.approx(6.18501, abs=5e-6)
    # MAWP back-calculation is the exact inverse of the thickness formula
    t = 7.0
    p = orc.ug27_shell_mawp_mpa(t, 753.0, 138.0, 0.85)
    assert orc.ug27_shell_thickness_mm(p, 753.0, 138.0, 0.85) == pytest.approx(t)
    p = orc.ug32d_ellipsoidal_mawp_mpa(t, 1506.0, 138.0, 0.85)
    assert orc.ug32d_ellipsoidal_thickness_mm(p, 1506.0, 138.0, 0.85) == pytest.approx(t)
    assert orc.ug99b_hydrotest_kpa(1000.0, 1.0) == pytest.approx(1300.0)
    assert orc.static_head_kpa(1000.0, 1000.0) == pytest.approx(9.80665)
    assert orc.round_up_to_series(9.69, fac.PLATE_SERIES_MM) == 10.0
    assert orc.round_up_to_series(10.0, fac.PLATE_SERIES_MM) == 10.0
    assert orc.round_up_to_series(40.0, fac.PLATE_SERIES_MM) is None


def test_air_and_duct_references():
    assert orc.air_density(20.0) == pytest.approx(1.2041, abs=1e-4)
    assert orc.air_viscosity(20.0) == pytest.approx(1.8133e-5, rel=1e-4)
    # kinematic viscosity of dry air at 20 °C ≈ 1.51e-5 m²/s
    assert orc.air_viscosity(20.0) / orc.air_density(20.0) == pytest.approx(1.51e-5, rel=0.01)
    # Huebscher: a square duct's De is ≈ 1.093·side
    assert orc.huebscher_de_mm(400.0, 400.0) == pytest.approx(1.093 * 400.0, rel=1e-3)
    rho, mu = orc.air_density(20.0), orc.air_viscosity(20.0)
    d = orc.equal_friction_diameter_mm(1.0, 1.0, 0.09, rho, mu)
    assert orc.duct_friction(1.0, d, 0.09, rho, mu)["gradient_pa_m"] == pytest.approx(1.0, rel=1e-9)
    assert orc.select_round_duct_mm(1.0, 1.0, 10.0, 0.09, rho, mu) == 450.0
    # velocity cap governs a high flow: smallest series D with v ≤ 4 m/s
    assert orc.select_round_duct_mm(1.0, 1.0, 4.0, 0.09, rho, mu) == 600.0
    seg = orc.duct_segment(0.4, 15.0, 0.3, density=rho, viscosity=mu, roughness_mm=0.09,
                           width_mm=450.0, height_mm=250.0)
    assert seg["velocity_ms"] == pytest.approx(0.4 / (0.45 * 0.25))
    assert seg["dp_fittings_pa"] == pytest.approx(0.3 * 0.5 * rho * seg["velocity_ms"] ** 2)


def test_critical_path_sums_the_tree():
    path, total = orc.critical_path([
        {"id": "main", "parent": None, "dp_total_pa": 20.0},
        {"id": "a", "parent": "main", "dp_total_pa": 9.0},
        {"id": "b", "parent": "main", "dp_total_pa": 10.0},
        {"id": "b1", "parent": "b", "dp_total_pa": 0.5},
    ])
    assert path == ["main", "b", "b1"] and total == pytest.approx(30.5)
    with pytest.raises(ValueError):
        orc.critical_path([{"id": "x", "parent": "y", "dp_total_pa": 1.0}])


# ═══════════════════════════════════════════════════════════ task definitions

_LEAKS = {  # oracle outputs that must never appear in the model-visible prompt
    "F1": ("14.25", "120.9", "2.48", "1963", "176"),
    "F2": ("0.218", "1.963", "5.028", "244.9", "241.3", "240.3", "9016", "52.48"),
    "F3": ("9.69", "9.66", "1046", "1051", "1360", "6.185", "1036", "36.7", "1520", "3770"),
    "F4": ("441.7", "364.3", "312.8", "362.8", "30.5", "21.0", "1.204", "6.28"),
}


def test_facility_tasks_are_registered_and_well_formed():
    from benchmarks.artifact_validity.tasks import REGISTRY

    for task in fac.TASKS:
        assert REGISTRY[task.id] is task
        assert task.domain == "facilities" and task.final_artifact == "deliverable.step+"
        assert f"`{task.final_artifact}`" in task.prompt
        ids = check_ids(task)
        assert len(ids) == len(task.checks) == len(set(ids))
        assert all(i.startswith(f"{task.id}.") for i in ids)
        for leak in _LEAKS[task.id]:
            assert leak not in task.prompt, (task.id, leak)
    assert [t.id for t in fac.TASKS] == ["F1", "F2", "F3", "F4"]


def test_task_oracle_constants_match_the_audit():
    assert fac.F1_Q_LPM == pytest.approx(14.25)
    assert fac.F1_V_MMS == pytest.approx(120.958, abs=1e-3)
    assert fac.F2_T_P == pytest.approx(0.2180, abs=5e-5)
    assert fac.F2_T_REQ == pytest.approx(1.9634, abs=5e-5)
    assert fac.F2_ID == pytest.approx(52.48)
    assert fac.F3_VOLUME == pytest.approx(6.18501, abs=5e-6)
    assert (fac.F3_NOM_SHELL, fac.F3_NOM_HEAD) == (10.0, 10.0)
    assert fac.F3_MAWP >= 1000.0
    assert fac.F4_ROUND == {"main": 450.0, "a": 400.0}


# ═══════════════════════════════════════════════════════════ fixture builders


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture(scope="module")
def tools(cad_tools) -> dict[str, Callable]:
    """CAD document tools + the piping/hydraulic/industrial tools, as an agent sees them."""
    from services.mcp import hydraulic_tools as ht
    from services.mcp import piping_tools as pt

    m = _StubMCP()
    pt.register_piping_tools(m)
    ht.register_hydraulic_tools(m)
    return {**cad_tools, **m.tools}


def _j(raw) -> dict:
    return json.loads(raw) if isinstance(raw, str) else raw


def _ok(res, what: str) -> dict:
    res = _j(res)
    assert res.get("ok", True) and not res.get("error"), (what, res)
    return res


def _reset_sessions() -> None:
    from services.mcp import hydraulic_tools as ht
    from services.mcp import piping_tools as pt

    ht._HYD.clear()
    ht._HYD["doc"] = None
    pt._NETWORKS.clear()
    pt._PIPING.update({"doc": None, "result": None, "boundary": None, "fluid": None,
                       "pumps": None, "network": None})


def _new_doc(tools, name: str) -> str:
    return _ok(tools["new_part"](name=name, fresh=True), "new_part")["doc_id"]


def _feat(feature: dict) -> dict:
    return {"op": "add_feature", "feature": feature}


def _save(tools, doc: str, out: Path, *, allow_empty: bool = False) -> Path:
    try:
        _ok(tools["save_part"](path=str(out), doc_id=doc, allow_empty=allow_empty), "save_part")
    finally:
        cb.call("doc_close", {"doc_id": doc})
    assert out.is_file() and out.stat().st_size > 0
    return out


def build_f1(tools, out: Path, *, bore: float = 50.0, duration: float = 4.0,
             persist: bool = True, plate: bool = True) -> Path:
    """HPU press circuit + 800×500×20 plate + 3D layout on the plate, saved as .step+."""
    _reset_sessions()
    doc = _new_doc(tools, "f1_hpu")
    if plate:
        _ok(tools["add_features"](doc_id=doc, ops=[_feat({
            "type": "box", "name": "HPU Mounting Plate",
            "params": {"length": 800.0, "width": 500.0, "height": 20.0}, "at": [0.0, 0.0, -10.0]})]),
            "plate")
    target = doc if persist else None
    _ok(tools["hydraulic_new"](name=fac.F1_CIRCUIT, doc_id=target), "hydraulic_new")
    parts = [("pump", {"displacement_cc_rev": 10.0, "speed_rpm": 1500.0, "vol_efficiency": 0.95}),
             ("valve_relief", {"set_pressure_bar": 160.0}),
             ("valve_directional", {"center_condition": "tandem", "rated_flow_lpm": 30.0,
                                    "rated_dp_bar": 5.0}),
             ("cylinder_linear", {"bore_mm": bore, "rod_mm": 28.0, "stroke_mm": 300.0}),
             ("reservoir", {})]
    ids = {}
    for kind, params in parts:
        r = _ok(tools["hydraulic_add_component"](type=kind, params=params, doc_id=target), kind)
        ids[kind] = r["doc"]["components"][-1]["id"]
    pump, rel, val, cyl, tank = (ids[k] for k, _ in parts)
    for net, conns in (("P", [(pump, "P"), (rel, "P"), (val, "P")]),
                       ("A", [(val, "A"), (cyl, "A")]), ("B", [(val, "B"), (cyl, "B")]),
                       ("T", [(rel, "T"), (val, "T"), (tank, "T"), (pump, "T")])):
        _ok(tools["hydraulic_add_hose"](
            net_name=net, connections=[{"component_id": c, "port": p} for c, p in conns],
            doc_id=target), f"hose {net}")
    _ok(tools["hydraulic_simulate"](
        duration_s=duration, valve_schedule=[{"t_s": 0.1, "component_id": val, "position": "a"}],
        loads={cyl: {"force_n": 10_000.0, "mass_kg": 200.0}}, output_dt_s=0.005,
        doc_id=target), "simulate")
    _ok(tools["hydraulic_bom"](doc_id=target), "bom")
    if persist:
        positions = {pump: [-250.0, 100.0, 120.0], rel: [-100.0, 150.0, 60.0],
                     val: [50.0, 0.0, 80.0], cyl: [250.0, -150.0, 150.0],
                     tank: [-250.0, -120.0, 200.0]}
        _ok(tools["hydraulic_layout_3d"](positions=positions, doc_id=doc), "layout")
    return _save(tools, doc, out, allow_empty=not plate)


def build_f2(tools, out: Path, *, design_kpa: float = 1000.0, demand_key: str = "demand",
             valves: bool = True, persist: bool = True) -> Path:
    """Cooling-water tree S–T–A/B with tee + balancing valves, solve, B31.3 check, BOM."""
    _reset_sessions()
    doc = _new_doc(tools, "f2_cw")
    target = doc if persist else None
    net = fac.F2_NETWORK
    spec = {"nps": 2, "schedule": "40", "material": "A106-B"}
    n = fac.F2_NODES
    built = _ok(tools["piping_edit"](ops=[
        {"op": "add_run", "path": [list(n["S"]), list(n["T"])], "name": "Main", **spec},
        {"op": "add_run", "path": [list(n["T"]), list(n["A"])], "name": "Branch A", **spec},
        {"op": "add_run", "path": [list(n["T"]), list(n["B"])], "name": "Branch B", **spec},
    ], network=net, doc_id=target), "piping_edit runs")
    run_a, run_b = built["piping"]["runs"][1]["id"], built["piping"]["runs"][2]["id"]
    if valves:
        valve = {"k_model": {"kind": "constant", "K": 5.0}}
        _ok(tools["piping_edit"](ops=[
            {"op": "add_fitting", "type": "custom", "at": [24_000.0, 0.0, 0.0], "run_id": run_a,
             "params": {**valve, "label": "BV-A"}},
            {"op": "add_fitting", "type": "custom", "at": [10_000.0, 19_000.0, 0.0],
             "run_id": run_b, "params": {**valve, "label": "BV-B"}},
        ], network=net, doc_id=target), "piping_edit valves")
    boundary = [{"at": list(n["S"]), "pressure": 250.0},
                {"at": list(n["A"]), demand_key: 6.0},
                {"at": list(n["B"]), demand_key: 6.0}]
    _ok(tools["piping_solve"](doc=net, boundary=boundary, fluid={"rho": 998.2, "mu": 0.001002},
                              network=net, doc_id=target), "piping_solve")
    _ok(tools["piping_spec_check"](design_pressure_kpa=design_kpa, design_temp_c=40.0,
                                   corrosion_allowance_mm=1.5, mill_tolerance_frac=0.125,
                                   joint_factor=1.0, piping=net, network=net, doc_id=target),
        "spec_check")
    _ok(tools["piping_bom"](piping=net, network=net, doc_id=target), "bom")
    return _save(tools, doc, out, allow_empty=True)


def _ellipse(a: float, b: float, z0: float, t0: float, t1: float, n: int = 13) -> list:
    return [[a * math.cos(t), z0 + b * math.sin(t)]
            for t in (t0 + (t1 - t0) * k / (n - 1) for k in range(n))]


def _vessel_profile(inner_d: float, tt: float, t_shell: float, t_head: float,
                    hollow: bool) -> list:
    """Half profile (u = radius, v = z) of the pressure boundary, bottom tangent at z = 0."""
    a, b = inner_d / 2.0, inner_d / 4.0
    ao, bo = a + t_head, b + t_head
    h = math.pi / 2.0
    outer = [
        {"type": "spline", "points": _ellipse(ao, bo, tt, h, 0.0)},
        {"type": "line", "p1": [ao, tt], "p2": [a + t_shell, tt]},
        {"type": "line", "p1": [a + t_shell, tt], "p2": [a + t_shell, 0.0]},
        {"type": "line", "p1": [a + t_shell, 0.0], "p2": [ao, 0.0]},
        {"type": "spline", "points": _ellipse(ao, bo, 0.0, 0.0, -h)},
    ]
    outer = [e for e in outer if e["type"] != "line" or e["p1"] != e["p2"]]
    if not hollow:
        return outer + [{"type": "line", "p1": [0.0, -bo], "p2": [0.0, tt + bo]}]
    inner = [
        {"type": "spline", "points": _ellipse(a, b, 0.0, -h, 0.0)},
        {"type": "line", "p1": [a, 0.0], "p2": [a, tt]},
        {"type": "spline", "points": _ellipse(a, b, tt, 0.0, h)},
    ]
    return inner + [{"type": "line", "p1": [0.0, tt + b], "p2": [0.0, tt + bo]}] + outer + [
        {"type": "line", "p1": [0.0, -bo], "p2": [0.0, -b]}]


def build_f3(tools, out: Path, *, ca: float = 3.0, hollow: bool = True,
             geom_inner_d: float = 1500.0, persist: bool = True) -> Path:
    """Vessel sizing study V-101 + revolved hollow shell/heads at the nominal plates."""
    _reset_sessions()
    doc = _new_doc(tools, "f3_vessel")
    sized = _ok(tools["vessel_size"](
        inner_diameter_mm=1500.0, tan_tan_length_mm=3000.0, design_pressure_kpa=1000.0,
        design_temp_c=40.0, material="SA-516-70", corrosion_allowance_mm=ca,
        joint_efficiency=0.85, head_type="ellipsoidal_2_1", fluid_density_kgm3=998.0,
        fill_fraction=0.8, orientation="vertical", doc_id=doc if persist else None,
        name=fac.F3_VESSEL), "vessel_size")
    t_shell, t_head = sized["shell"]["t_nominal_mm"], sized["heads"]["t_nominal_mm"]
    profile = _vessel_profile(geom_inner_d, 3000.0, t_shell, t_head, hollow)
    _ok(tools["add_features"](doc_id=doc, ops=[
        _feat({"type": "sketch", "name": "Vessel Half Profile",
               "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.front"}}),
        *({"op": "sketch_add_entity", "sketch": "$0", "entity": e} for e in profile),
        _feat({"type": "revolve", "name": "Shell + 2:1 Heads", "params": {"mode": "new", "angle": 360},
               "refs": {"profile": "$0", "axis": "axis.z"}}),
    ]), "vessel geometry")
    return _save(tools, doc, out)


def build_f4(tools, out: Path, *, flows: dict | None = None, roughness: float = 0.09,
             bodies: bool = True, fabricate: dict | None = None) -> Path:
    """Duct tree sizing AHU-1 Supply + one solid body per segment on the prescribed layout."""
    _reset_sessions()
    doc = _new_doc(tools, "f4_duct")
    segs = []
    for sid, s in fac.F4_SEGMENTS.items():
        row = {"id": sid, **{k: v for k, v in s.items() if v is not None}}
        row["airflow_m3s"] = (flows or {}).get(sid, s["airflow_m3s"])
        segs.append(row)
    sized = _ok(tools["duct_size"](segments=segs, friction_pa_per_m=1.0, max_velocity_ms=10.0,
                                   air_temp_c=20.0, roughness_mm=roughness, doc_id=doc,
                                   name=fac.F4_SYSTEM), "duct_size")
    rows = {r["id"]: r for r in sized["segments"]}
    if fabricate:
        from services.mcp.cad_common import _persist_study

        st = next(s for s in cb.call("sim_study_list", {"doc_id": doc})["studies"]
                  if s.get("type") == "duct_system")
        rec = copy.deepcopy(st)
        rec["results"].update(fabricate)
        assert _persist_study(doc, rec)
    if bodies:
        dm, da = rows["main"]["diameter_mm"], rows["a"]["diameter_mm"]
        w, h = rows["b"]["width_mm"], rows["b"]["height_mm"]
        _ok(tools["add_features"](doc_id=doc, ops=[
            _feat({"type": "cylinder", "name": "Duct main", "params": {"radius": dm / 2, "height": 10_000.0},
                   "at": [0.0, 0.0, 0.0], "axis": "+x", "centered": False, "merge": False}),
            _feat({"type": "cylinder", "name": "Duct a", "params": {"radius": da / 2, "height": 8_000.0},
                   "at": [10_000.0, 0.0, 0.0], "axis": "+y", "centered": False, "merge": False}),
            _feat({"type": "box", "name": "Duct b", "params": {"length": w, "width": 15_000.0, "height": h},
                   "at": [10_000.0, -7_500.0, 0.0], "merge": False}),
        ]), "duct bodies")
    return _save(tools, doc, out, allow_empty=not bodies)


BUILDERS = {"F1": build_f1, "F2": build_f2, "F3": build_f3, "F4": build_f4}
TASKS = {t.id: t for t in fac.TASKS}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Series files and saves land in tmp_path; tool session state starts empty."""
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    if _HAVE_CORE:
        _reset_sessions()
    yield
    if _HAVE_CORE:
        _reset_sessions()


@pytest.fixture(scope="module")
def correct(tools, tmp_path_factory) -> dict[str, Path]:
    """One correct deliverable per task, built once for the module."""
    if not _HAVE_CORE:
        pytest.skip("libchudware not staged")
    root = tmp_path_factory.mktemp("facilities_correct")
    out = {}
    with pytest.MonkeyPatch.context() as mp:
        for tid, build in BUILDERS.items():
            work = root / tid
            work.mkdir()
            mp.setenv("CHUDWARE_PROJECT_DIR", str(work))
            mp.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(work))
            out[tid] = build(tools, work / "deliverable.step+")
    return out


def grade(task: Task, path: Path) -> tuple[Status, dict[str, CheckResult], Evidence]:
    ev = build_evidence(path)
    results = [c(ev) for c in task.checks]
    status = task_status(Status.PASS, ev.exists, results, check_ids(task))
    return status, {r.id: r for r in results}, ev


def _explain(results: dict[str, CheckResult]) -> str:
    return "\n".join(f"{r.id}: {r.status.value} — {r.detail} | measured={r.measured!r:.600}"
                     for r in results.values() if r.status is not Status.PASS)


# ═══════════════════════════════════════════════════════════ correct deliverables


@live
@pytest.mark.parametrize("tid", list(BUILDERS))
def test_correct_deliverable_passes_every_check(tid, correct):
    status, results, ev = grade(TASKS[tid], correct[tid])
    assert ev.opened and not ev.probe_errors, ev.summary()
    assert status is Status.PASS, _explain(results)
    assert len(results) == len(TASKS[tid].checks)


@live
def test_a_failed_turn_never_passes_even_with_a_correct_file(correct):
    task = TASKS["F3"]
    ev = build_evidence(correct["F3"])
    results = [c(ev) for c in task.checks]
    assert task_status(Status.FAIL, True, results, check_ids(task)) is Status.FAIL
    assert task_status(Status.ERROR, True, results, check_ids(task)) is Status.ERROR


# ═══════════════════════════════════════════════════════════ adversarial deliverables

#: (task, variant, builder kwargs, check ids that MUST fail)
ADVERSARIAL = [
    ("F1", "wrong_bore", {"bore": 63.0}, {"F1.components", "F1.steady_extension", "F1.stroke"}),
    ("F1", "study_not_saved", {"persist": False}, set(check_ids(fac.F1_TASK)) - {
        "F1.mounting_plate"}),
    ("F1", "no_mounting_plate", {"plate": False}, {"F1.mounting_plate", "F1.layout"}),
    ("F1", "simulation_too_short", {"duration": 1.5},
     {"F1.duty", "F1.stroke", "F1.relief", "F1.results_finite"}),
    ("F2", "wrong_design_pressure", {"design_kpa": 600.0}, {"F2.b31_3"}),
    ("F2", "demand_in_wrong_units", {"demand_key": "demand_lps"},
     {"F2.boundary", "F2.mass_balance", "F2.hydraulics", "F2.node_pressures"}),
    ("F2", "missing_balancing_valves", {"valves": False},
     {"F2.fittings", "F2.hydraulics", "F2.node_pressures"}),
    ("F2", "study_not_saved", {"persist": False}, set(check_ids(fac.F2_TASK))),
    ("F3", "wrong_corrosion_allowance", {"ca": 1.5},
     {"F3.inputs", "F3.shell", "F3.heads", "F3.mawp", "F3.hydrotest"}),
    ("F3", "solid_not_hollow", {"hollow": False}, {"F3.geometry_wall"}),
    ("F3", "wrong_geometry_diameter", {"geom_inner_d": 1400.0}, {"F3.geometry_envelope"}),
    ("F3", "study_not_saved", {"persist": False}, set(check_ids(fac.F3_TASK)) - {
        "F3.geometry_envelope", "F3.geometry_wall"}),
    ("F4", "wrong_branch_flow", {"flows": {"b": 0.5}}, {"F4.inputs"}),
    ("F4", "wrong_roughness", {"roughness": 0.15}, {"F4.inputs", "F4.pressure_terms"}),
    ("F4", "fabricated_fan_static", {"fabricate": {"fan_static_pa": 20.0,
                                                   "critical_path": ["main", "a"]}},
     {"F4.critical_path"}),
    ("F4", "missing_geometry", {"bodies": False}, {"F4.geometry"}),
]


@live
@pytest.mark.parametrize("tid,variant,kwargs,must_fail", ADVERSARIAL,
                         ids=[f"{a[0]}-{a[1]}" for a in ADVERSARIAL])
def test_adversarial_deliverable_fails(tid, variant, kwargs, must_fail, tools, tmp_path):
    path = BUILDERS[tid](tools, tmp_path / "deliverable.step+", **kwargs)
    status, results, ev = grade(TASKS[tid], path)
    assert ev.opened, ev.summary()
    errors = [r for r in results.values() if r.status is Status.ERROR]
    assert not errors, _explain(results)
    failed = {r.id for r in results.values() if r.status is Status.FAIL}
    assert must_fail <= failed, (variant, sorted(must_fail - failed), _explain(results))
    assert status is Status.FAIL


@live
@pytest.mark.parametrize("tid", list(BUILDERS))
def test_missing_and_empty_deliverables_fail_every_check(tid, tmp_path):
    task = TASKS[tid]
    missing = tmp_path / "deliverable.step+"
    status, results, _ev = grade(task, missing)
    assert status is Status.FAIL
    assert all(r.status is Status.FAIL for r in results.values()), _explain(results)
    missing.write_bytes(b"")
    status, results, ev = grade(task, missing)
    assert ev.open_error and status is Status.FAIL
    assert all(r.status is Status.FAIL for r in results.values()), _explain(results)


@live
@pytest.mark.parametrize("graded,built", [(g, b) for g in BUILDERS for b in BUILDERS if g != b])
def test_another_tasks_deliverable_fails(graded, built, correct):
    """A stale/wrong document (a correct deliverable for a DIFFERENT task) never passes."""
    status, results, _ev = grade(TASKS[graded], correct[built])
    assert status is Status.FAIL
    assert not [r for r in results.values() if r.status is Status.ERROR], _explain(results)
    assert results[f"{graded}.study"].status is Status.FAIL


@live
def test_series_file_removed_after_save_fails(tools, tmp_path):
    """The saved run must point at its full-resolution series; a deleted file is a FAIL."""
    path = build_f1(tools, tmp_path / "deliverable.step+")
    ev = build_evidence(path)
    st = next(s for s in ev.studies if s.get("type") == "hydraulic_circuit")
    fac.series_file_path(ev, st["results"]["simulate"]["series_path"]).unlink()
    assert fac.f1_series_file(ev).status is Status.FAIL
