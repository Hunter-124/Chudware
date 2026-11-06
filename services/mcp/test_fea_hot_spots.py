"""Behavioral coverage for static hot spots, field probes and convergence evidence."""
import json

import pytest

from services.mcp import cad_sim_tools as sim
from services.mcp import fea_probe


def test_static_persists_real_hot_spots_and_attribution(monkeypatch):
    saved = []
    monkeypatch.setattr(sim, "_persist_study", lambda doc, record: saved.append(record) or "s1")
    monkeypatch.setattr(fea_probe, "attribute_hot_spots", lambda doc, spots, radius: (
        [{**spot, "face_sid": "F12345", "owner_feature": "Fillet1"} for spot in spots],
        {"concave_blends": [{"radius_mm": 3.0}]}))
    raw = {"ok": True, "summary": {"max_von_mises_mpa": 80, "max_disp_mm": 0.1,
                                     "min_fos": 2},
           "n_dof": 30, "n_nodes": 10, "n_elems": 2,
           "hot_spot_cluster_radius_mm": 2,
           "median_von_mises_mpa": 40,
           "hot_spots": [{"id": "node-2", "location_mm": [1, 2, 3], "von_mises_mpa": 120,
                           "stress_to_median_ratio": 3, "factor_of_safety": 1.5,
                           "included_in_summary": False}],
           "low_fos_regions": [{"id": "node-2", "location_mm": [1, 2, 3],
                                "von_mises_mpa": 120, "factor_of_safety": 1.5}]}
    out = sim._collect_static({"doc": "d", "material": {"name": "steel"}}, raw, [])
    spot = out["summary"]["hot_spots"][0]
    assert spot["location_mm"] == [1, 2, 3]
    assert spot["face_sid"] == "F12345" and spot["owner_feature"] == "Fillet1"
    assert spot["factor_of_safety"] == 1.5 and spot["included_in_summary"] is False
    assert saved[0]["results"]["hot_spots"] == [spot]
    assert saved[0]["results"]["low_fos_regions"][0]["face_sid"] == "F12345"
    assert out["summary"]["median_von_mises_mpa"] == 40 and spot["stress_to_median_ratio"] == 3
    assert saved[0]["doc_geom"]["min_fillet_radius_mm"] == 3


def test_attribution_failure_keeps_real_peaks_without_inventing_face(monkeypatch):
    saved = []
    monkeypatch.setattr(sim, "_persist_study", lambda doc, record: saved.append(record) or "s")
    def fail(*args):
        raise RuntimeError("geometry unavailable")
    monkeypatch.setattr(fea_probe, "attribute_hot_spots", fail)
    raw = {"ok": True, "summary": {"max_von_mises_mpa": 1, "max_disp_mm": 1,
                                     "min_fos": 2}, "n_dof": 30, "n_nodes": 10,
           "n_elems": 2, "hot_spots": [{"location_mm": [0, 0, 0], "von_mises_mpa": 1}]}
    out = sim._collect_static({"doc": "d", "material": {}}, raw, [])
    assert "geometry unavailable" in out["hot_spot_attribution_unavailable"]
    assert "face_sid" not in saved[0]["results"]["hot_spots"][0]


def test_surface_attribution_uses_triangle_sid_owner_and_edge_evidence(monkeypatch):
    pytest.importorskip("pyvista")
    np = pytest.importorskip("numpy")
    from services.mcp import cad_common, dfm_common, print_tools
    from services.dfam.mesh import FaceInfo, PrintMesh

    mesh = PrintMesh(np.array([[0., 0, 0], [1, 0, 0], [0, 1, 0]]),
                     np.array([[0, 1, 2]]), np.array([0]),
                     [FaceInfo("F12345", 1, 0, None)])
    monkeypatch.setattr(print_tools, "load_doc", lambda doc: (mesh, "part", {}))
    monkeypatch.setattr(cad_common, "_face_attribution", lambda doc: {
        "faces": [{"sid": "F12345"}], "owner": ["feature-1"]})
    monkeypatch.setattr(cad_common, "_doc_mesh", lambda doc: {"edges": [
        {"body": 1, "edge_id": 7, "pts": np.array([[0, 0, 0], [1, 0, 0]])}]})
    monkeypatch.setattr(dfm_common.DfmContext, "edge_analysis", lambda self, **kw: {
        "edges": [{"body": 1, "topo": 7, "sid": "E12345",
                   "faces": ["F12345", "F67890"]}], "concave_blends": []})
    spots, _ = fea_probe.attribute_hot_spots("d", [
        {"location_mm": [0.2, 0.01, 0]}, {"location_mm": [0.2, 0.2, 5]}], 1)
    assert spots[0]["face_sid"] == "F12345"
    assert spots[0]["owner_feature"] == "feature-1"
    assert spots[0]["at_sharp_edge"] is True and spots[0]["sharp_edge_sid"] == "E12345"
    assert spots[1]["at_sharp_edge"] is False and "face_sid" not in spots[1]


def test_probe_interpolates_actual_vtu_and_rejects_outside(tmp_path, monkeypatch):
    pv = pytest.importorskip("pyvista")
    np = pytest.importorskip("numpy")
    cells = np.array([4, 0, 1, 2, 3])
    mesh = pv.UnstructuredGrid(cells, np.array([10], dtype=np.uint8),
                               np.array([[0., 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]))
    mesh.point_data["von_mises"] = np.array([0., 10, 20, 30])
    mesh.point_data["factor_of_safety"] = np.array([4., 4, 4, 4])
    path = tmp_path / "field.vtu"
    mesh.save(str(path))
    result = fea_probe.probe_vtu(str(path), [0.1, 0.2, 0.3])
    assert result["fields"]["von_mises_mpa"] == pytest.approx(14)
    assert result["fields"]["factor_of_safety"] == pytest.approx(4)
    with pytest.raises(ValueError, match="outside"):
        fea_probe.probe_vtu(str(path), [3, 3, 3])
    region = fea_probe.probe_region(str(path), region={"min_mm": [-0.1, -0.1, -0.1],
                                                       "max_mm": [1.1, 1.1, 0.1]})
    assert region["n_nodes"] == 3
    assert region["fields"]["von_mises_mpa"]["max"] == 20
    assert region["fields"]["von_mises_mpa"]["max_location_mm"] == [0, 1, 0]
    with pytest.raises(ValueError, match="No VTU nodes"):
        fea_probe.probe_region(str(path), region={"center_mm": [10, 10, 10], "radius_mm": 0.1})
    from services.dfam.mesh import FaceInfo, PrintMesh
    from services.mcp import print_tools
    face_mesh = PrintMesh(np.asarray(mesh.points), np.array([[0, 1, 2]]),
                          np.array([0]), [FaceInfo("F12345", 1, 0, None)])
    monkeypatch.setattr(print_tools, "load_doc", lambda doc: (face_mesh, "part", {}))
    monkeypatch.setattr(fea_probe, "_doc", lambda doc: doc)
    face = fea_probe.probe_region(str(path), doc_id="d", face_sid="F12345")
    assert face["n_nodes"] == 3
    assert face["fields"]["von_mises_mpa"]["max"] == 20


def test_local_convergence_probes_same_point_and_refuses_missing_field(monkeypatch):
    seen = []
    monkeypatch.setattr(fea_probe, "probe_vtu", lambda path, p: seen.append((path, p)) or
                        {"fields": {"von_mises_mpa": 105}})
    step = {"vtu_path": "/tmp/leg.vtu", "monitor_point_mm": [1, 2, 3]}
    out = {"ok": True, "summary": {}, "n_dof": 30, "n_nodes": 10, "n_elems": 2}
    row = sim._convergence_row(step, out, "local_von_mises_mpa", 100)
    assert row["monitored_value"] == 105 and row["delta_pct_vs_prev"] == 5
    assert seen == [("/tmp/leg.vtu", [1, 2, 3])]
    monkeypatch.setattr(fea_probe, "probe_vtu", lambda *args: {"fields": {}})
    missing = sim._convergence_row(step, out, "local_von_mises_mpa", 100)
    assert missing["monitored_value"] is None and missing["error"]
    monkeypatch.setattr(fea_probe, "probe_region", lambda path, **kwargs: {
        "n_nodes": 4, "fields": {"von_mises_mpa": {"max": 125}}})
    face = sim._convergence_row({"vtu_path": "/tmp/leg.vtu", "monitor_face_sid": "F12345",
                                 "doc_id": "d"}, out, "local_von_mises_mpa", 100)
    assert face["monitored_value"] == 125 and face["sampled_nodes"] == 4


def test_convergence_evidence_attaches_to_finest_study(monkeypatch):
    saved = []
    study = {"id": "s1", "type": "fea_static", "name": "Static"}
    monkeypatch.setattr(sim.cb, "call", lambda method, params: {"studies": [study]})
    monkeypatch.setattr(sim, "_persist_study", lambda doc, rec: saved.append(rec.copy()) or "s1")
    rows = [{"study_id": "s1", "n_dof": 100, "n_elems": 10, "monitored_value": 100,
             "delta_pct_vs_prev": None},
            {"study_id": "s1", "n_dof": 200, "n_elems": 20, "monitored_value": 102,
             "delta_pct_vs_prev": 2}]
    result = sim._convergence_envelope("d", "max_von_mises_mpa", 5, rows, [])
    assert result["converged"] and result["saved_to_study"]
    assert saved[-1]["mesh_convergence"]["values"] == [100, 102]
    sim._convergence_envelope("d", "local_von_mises_mpa", 5, rows, [])
    assert "mesh_convergence" not in saved[-1]
    assert saved[-1]["convergence_evidence"]["monitor"] == "local_von_mises_mpa"


def test_growing_local_convergence_is_suspected_not_proven_singular():
    rows = [{"n_dof": 100, "n_elems": 10, "monitored_value": 100, "delta_pct_vs_prev": None},
            {"n_dof": 200, "n_elems": 20, "monitored_value": 120, "delta_pct_vs_prev": 20},
            {"n_dof": 400, "n_elems": 40, "monitored_value": 150, "delta_pct_vs_prev": 25}]
    result = sim._convergence_envelope("d", "local_von_mises_mpa", 5, rows, [])
    assert not result["converged"] and result["trend"] == "growing_possible_singularity"


def test_advisor_uses_measured_sharp_edge_hotspot_only():
    from services.agent.advisor.sim_advisor import postcheck

    peak = {"at_sharp_edge": True, "sharp_edge_warning": "Measured near a sharp edge.",
            "face_sid": "F12345", "owner_feature": "feature-1",
            "location_mm": [1, 2, 3], "von_mises_mpa": 120}
    study = {"type": "fea_static", "material": {"name": "steel", "yield_mpa": 250},
             "results": {"max_von_mises_mpa": 120, "hot_spots": [peak]}}
    findings = postcheck(study)["items"]
    hint = next(item for item in findings if item["finding"] == peak["sharp_edge_warning"])
    assert hint["refs"]["face_sid"] == "F12345" and hint["refs"]["feature"] == "feature-1"
    assert hint["source"]["detail"] == "distance to tessellated concave edge"
    peak["at_sharp_edge"] = None
    assert all(item["finding"] != peak["sharp_edge_warning"] for item in postcheck(study)["items"])


def test_probe_selects_stored_study_and_multiple_points(monkeypatch):
    class Registrar:
        def tool(self):
            return lambda fn: setattr(self, "fn", fn) or fn
    registrar = Registrar()
    fea_probe.register_fea_probe(registrar)
    monkeypatch.setattr(fea_probe, "_doc", lambda doc: doc or "d")
    monkeypatch.setattr(fea_probe.cb, "call", lambda *args: {
        "studies": [{"id": "study-1", "name": "one", "vtu_path": "/tmp/result.vtu"}]})
    monkeypatch.setattr(fea_probe, "probe_vtu", lambda path, point: {
        "ok": True, "vtu_path": path, "point_mm": point, "fields": {"von_mises_mpa": 5}})
    out = json.loads(registrar.fn(study="one", points=[[0, 0, 0], [1, 0, 0]]))
    assert out["study_id"] == "study-1" and len(out["samples"]) == 2
    error = json.loads(registrar.fn(study="missing", point_mm=[0, 0, 0]))
    assert not error["ok"] and "study" in error["error"]


def test_convergence_resolves_hotspot_id_into_fixed_location(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(sim, "_doc", lambda doc: doc or "d")
    monkeypatch.setattr(sim.cb, "call", lambda *args: {"studies": [{
        "id": "s1", "name": "Static", "results": {"hot_spots": [
            {"id": "node-7", "location_mm": [1, 2, 3]}]}}]})
    monkeypatch.setattr(sim, "_build_static_study", lambda *args, **kwargs: {
        "doc": "d", "analysis": "fea_static", "study": {"fineness": 0.5},
        "material": {"name": "steel"}})
    monkeypatch.setattr(sim, "_autosave_all_open", lambda: None)
    monkeypatch.setattr(sim, "_archive_existing_vtu", lambda *args: None)
    captured = {}
    class Ladder:
        def __init__(self, plan, doc, monitor, tol, notes, continuing):
            captured.update(plan=plan, monitor=monitor)
        def pump(self, deadline):
            return {"ok": True}
    monkeypatch.setattr(sim, "_ConvergenceLadder", Ladder)
    mcp = FastMCP("test-local-hotspot")
    sim.register_cad_sim_tools(mcp)
    tool = mcp._tool_manager._tools["run_fea_convergence"].fn
    out = json.loads(tool(fixtures=["bottom"], loads=[{"face": "top", "force": [0, 0, -1]}],
                          sizes_mm=[2, 1], vtu_dir=str(tmp_path), monitor_study="s1",
                          monitor_hot_spot_id="node-7"))
    assert out["ok"] and captured["monitor"] == "local_von_mises_mpa"
    assert all(step["monitor_point_mm"] == [1, 2, 3] and step["monitor_hot_spot_id"] == "node-7"
               for step, _, _ in captured["plan"])
    out = json.loads(tool(fixtures=[], loads=[], sizes_mm=[2, 1], vtu_dir=str(tmp_path),
                          monitor_hot_spot_id="node-missing"))
    assert not out["ok"] and "not found" in out["error"]


def test_applied_material_and_thermal_assumptions(monkeypatch):
    from services.verify import battery
    monkeypatch.setattr(battery, "_applied_material_key", lambda doc: ("aluminum", None))
    assert sim._material_for_doc(None, "d", []) == "aluminum"
    assert sim._material_for_doc("steel", "d", []) == "steel"
    monkeypatch.setattr(battery, "_applied_material_key", lambda doc: (None, "mixed bodies"))
    with pytest.raises(ValueError, match="mixed bodies"):
        sim._material_for_doc(None, "d", [])
    monkeypatch.setattr(battery, "_applied_material_key", lambda doc: (None, None))
    with pytest.raises(ValueError, match="Assign a material"):
        sim._material_for_doc(None, "d", [])
    monkeypatch.setattr(sim, "_resolve_material_name", lambda value: {"name": value, "density_g_cm3": 2.7})
    monkeypatch.setattr(sim, "material_thermal_props", lambda name: (
        {"conductivity_w_mk": 205, "cp_j_kgk": 900},
        [{"property": "conductivity_w_mk", "value": 205, "source": "family default"},
         {"property": "cp_j_kgk", "value": 900, "source": "family default"}]))
    assumptions = []
    result = sim._resolve_study_material("aluminum", "thermal_transient",
                                         conductivity_w_mk=10, assumptions=assumptions)
    assert result["conductivity_w_mk"] == 10 and result["cp_j_kgk"] == 900
    assert [row["property"] for row in assumptions] == ["cp_j_kgk"]


def test_guard_error_handles_mapping_and_json(monkeypatch):
    for value in ({"ok": False, "error": "bad"}, '{"ok":false,"error":"bad"}'):
        monkeypatch.setattr(sim, "_err", lambda exc, value=value: value)
        assert json.loads(sim._guard_error(ValueError("bad")))["error"] == "bad"


def test_live_static_peak_is_stored_and_matches_raw_peak():
    from services.mcp.test_cad_sim_tools import _block, _static
    from services.mcp.cad_common import cb

    doc = _block("hot_spot_persistence")
    out = _static(doc, fixtures=["bottom"],
                  loads=[{"face": "top", "force": [0, 0, -500]}],
                  mesh_fineness="coarse")
    assert out["ok"], out
    spots = out["summary"]["hot_spots"]
    assert spots and len(spots[0]["location_mm"]) == 3
    assert spots[0]["von_mises_mpa"] == pytest.approx(
        out["summary"]["max_von_mises_raw_mpa"], abs=0.001)
    assert spots[0]["factor_of_safety"] >= 0
    assert spots[0]["id"].startswith("node-")
    assert out["summary"]["low_fos_regions"][0]["factor_of_safety"] >= 0
    assert out["summary"]["median_von_mises_mpa"] >= 0
    study = next(s for s in cb.call("sim_study_list", {"doc_id": doc})["studies"]
                 if s["id"] == out["study_id"])
    assert study["results"]["hot_spots"] == spots
