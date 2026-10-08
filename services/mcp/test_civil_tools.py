"""Offline consumer-facing civil MCP flows; real Python engines, fake study RPC only."""
from __future__ import annotations

import copy
import csv
import io
import json
import math
from pathlib import Path

import pytest

from services.civil import alignment, dem, epanet_inp, landxml, planning, schema, swmm_inp, terrain
from services.mcp import civil_common as cc, civil_tools, core_bridge

_DATA = Path(__file__).resolve().parents[1] / "civil" / "data"


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Store:
    """Document study persistence only: engineering numbers never come from this fake."""
    def __init__(self):
        self.studies = []
        self.next_id = 0

    def call(self, method, params=None):
        params = params or {}
        if method == "sim_study_list":
            return {"studies": copy.deepcopy(self.studies)}
        if method == "sim_study_add":
            self.next_id += 1
            record = copy.deepcopy(params["study"])
            record["id"] = f"civil-study-{self.next_id}"
            self.studies.append(record)
            return {"study_id": record["id"]}
        if method == "sim_study_edit":
            for i, record in enumerate(self.studies):
                if record["id"] == params["study_id"]:
                    self.studies[i] = copy.deepcopy(params["study"])
                    self.studies[i]["id"] = record["id"]
                    return {"ok": True}
            raise RuntimeError("no such study")
        raise AssertionError(f"Unexpected daemon RPC: {method}")

    def study(self):
        assert len(self.studies) == 1
        return self.studies[0]


@pytest.fixture(autouse=True)
def isolated_session():
    sites, active = copy.deepcopy(cc.SITES), dict(cc.ACTIVE)
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    yield
    cc.SITES.clear()
    cc.SITES.update(sites)
    cc.ACTIVE.clear()
    cc.ACTIVE.update(active)


@pytest.fixture
def store(monkeypatch):
    result = _Store()
    monkeypatch.setattr(core_bridge, "call", result.call)
    monkeypatch.setattr(cc, "_doc", lambda doc_id: doc_id)
    return result


@pytest.fixture
def tools():
    mcp = _StubMCP()
    civil_tools.register_civil_tools(mcp)
    return mcp.tools


def _call(tools, tool, **kwargs):
    result = json.loads(tools[tool](**kwargs))
    assert result["ok"], result
    return result


def _new(tools, **kwargs):
    return _call(tools, "civil_site_new", name="Site", **kwargs)


def _edit(tools, collection, element, **kwargs):
    return _call(tools, "civil_site_edit", ops=[{"op": "upsert", "collection": collection, "element": element}], **kwargs)


def _water():
    return schema.water_network("water", "Distribution", junctions=[
        {"id": "J", "x": 100, "y": 0, "elevation": 10, "demand_m3s": 0.01, "pattern": None}],
        reservoirs=[{"id": "R", "x": 0, "y": 0, "head": 50, "elevation": 12, "pattern": None}],
        pipes=[{"id": "P", "from": "R", "to": "J", "length": 100, "diameter": 0.2, "roughness": 120,
                "minor_loss": 0, "status": "open", "material": "PVC", "vertices": []}])


def _gravity():
    return schema.gravity_network("gravity", "Sanitary", nodes=[
        {"id": "A", "type": "manhole", "x": 0, "y": 0, "rim": 5, "invert": 1, "diameter": 1.2},
        {"id": "B", "type": "outfall", "x": 10, "y": 0, "rim": 5, "invert": 1, "diameter": 1.2}],
        conduits=[{"id": "C", "from": "A", "to": "B", "shape": "circular", "diameter": 0.3,
                   "outer_diameter_m": 0.35, "length": 10, "n": 0.013, "material": "PVC", "vertices": []}])


def _plane(tools, *, sid="eg", a=0.0, b=0.0, c=0.0, low=0.0, high=10.0, doc_id=None):
    points = [[x, y, a*x + b*y + c] for x, y in ((low, low), (high, low), (high, high), (low, high))]
    return _call(tools, "civil_surface_build", surface_id=sid, points=points,
                 boundary=[[low, low], [high, low], [high, high], [low, high]], doc_id=doc_id)


def _ascii(tmp_path):
    path = tmp_path / "plane.asc"
    # z=10+x+y at the cell centres (north-up). D8 reaches the south-west outlet.
    path.write_text("ncols 3\nnrows 3\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n"
                    "13 14 15\n12 13 14\n11 12 13\n", encoding="utf-8")
    return path


def test_site_new_edit_summary_and_atomic_invalid_edit(tools, store):
    created = _new(tools, crs="EPSG:26915", doc_id="doc")
    assert created["crs"]["horizontal"] == "EPSG:26915"
    assert created["counts"]["water_networks"] == 0 and created["saved_to_study"]
    edited = _edit(tools, "water_networks", _water(), doc_id="doc")
    assert edited["counts"]["water_networks"] == 1
    summary = _call(tools, "civil_site_summary")
    assert summary["water_networks"][0]["counts"]["pipes"] == 1
    assert summary["water_networks"][0]["counts"]["junctions"] == 1
    assert summary["validation_errors"] == []
    bad = json.loads(tools["civil_site_edit"](ops=[{"op": "remove", "collection": "water_networks", "id": "water"},
                                                {"op": "upsert", "collection": "surfaces", "element": {"id": "bad"}}], doc_id="doc"))
    assert bad["reason"] == "invalid_site"
    assert store.study()["params"]["model"]["water_networks"] == [_water()]
    assert _call(tools, "civil_site_summary")["counts"]["water_networks"] == 1


def test_landxml_import_populates_metric_surface_network_alignment_and_provenance(tools, store):
    _new(tools, doc_id="doc")
    result = _call(tools, "civil_import", source=str(_DATA / "landxml_metric.xml"), format="landxml", doc_id="doc")
    assert result["counts"]["surfaces"] == result["counts"]["gravity_networks"] == result["counts"]["alignments"] == 1
    assert result["surfaces"][0]["point_count"] == 5
    assert result["surfaces"][0]["triangle_count"] == 4
    assert result["gravity_networks"][0]["counts"] == {"nodes": 2, "conduits": 1}
    assert result["alignments"][0]["length_m"] == pytest.approx(60)
    model = store.study()["params"]["model"]
    assert model["surfaces"][0]["points"][0] == [100, 200, 10]
    assert model["gravity_networks"][0]["conduits"][0]["diameter"] == pytest.approx(0.3)
    assert result["dropped"] == landxml.read(_DATA / "landxml_metric.xml")["dropped"]
    assert model["provenance"][0]["diameter_to_m"] == pytest.approx(0.001)
    assert model["provenance"][0]["imported_at"] and model["provenance"][0]["kind"] == "landxml"
    assert "model" not in result and "points" not in result["surfaces"][0]


def test_import_collisions_are_atomic_replace_is_explicit_and_merge_false_replaces(tools):
    _new(tools)
    source = str(_DATA / "landxml_metric.xml")
    _call(tools, "civil_import", source=source, format="landxml")
    before = _call(tools, "civil_site_summary", detail=True)["model"]
    refusal = json.loads(tools["civil_import"](source=source, format="landxml"))
    assert refusal["reason"] == "id_collision"
    assert _call(tools, "civil_site_summary", detail=True)["model"] == before
    replaced = _call(tools, "civil_import", source=source, format="landxml", options={"replace": True})
    assert replaced["counts"]["surfaces"] == 1 and replaced["provenance_count"] == 2
    _edit(tools, "water_networks", _water())
    cutover = _call(tools, "civil_import", source=source, format="landxml", merge=False)
    assert cutover["counts"]["water_networks"] == 0 and cutover["counts"]["gravity_networks"] == 1


def test_epanet_tool_export_import_exact_network_roundtrip(tools, store, tmp_path):
    _new(tools)
    network = _water()
    _edit(tools, "water_networks", network)
    path = tmp_path / "distribution.inp"
    exported = _call(tools, "civil_export", format="epanet", path=str(path), network_id="water")
    assert exported["network_id"] == "water"
    assert "[JUNCTIONS]" in path.read_text() and "[PIPES]" in path.read_text()
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    imported = _call(tools, "civil_import", source=str(path), format="epanet", doc_id="doc")
    assert imported["water_networks"][0]["counts"]["pipes"] == 1
    assert store.study()["params"]["model"]["water_networks"] == [network]
    assert imported["dropped"] == epanet_inp.read(path)["dropped"]
    assert imported["provenance"][0]["metadata_restored"]


def test_plane_volume_persistence_load_and_edit_invalidation(tools, store):
    _new(tools, doc_id="doc")
    built = _plane(tools, a=0.1, b=0.2, c=1, doc_id="doc")
    assert built["point_count"] == 4 and built["triangle_count"] == 2
    result = _call(tools, "civil_surface_analyze", kind="volume_to_elevation", surface_id="eg", elevation=0, doc_id="doc")
    # Integral over [0,10]^2: 100*(1+.1*5+.2*5)=250 m³ above datum.
    assert result["fill_m3"] == pytest.approx(250) and result["cut_m3"] == pytest.approx(0)
    expected = copy.deepcopy(store.study())
    assert expected["results"]["earthwork"]["volume_to_elevation:eg"]["fill_m3"] == pytest.approx(250)
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    loaded = _call(tools, "civil_load", doc_id="doc", detail=True)
    assert loaded["model"] == expected["params"]["model"] and loaded["results"] == expected["results"]
    edited = _call(tools, "civil_site_edit", ops=[{"op": "set_criteria", "criteria": {"profile": "custom", "overrides": {}}}], doc_id="doc")
    assert edited["cleared_result_keys"] == ["earthwork"] and edited["result_keys"] == []
    assert store.study()["results"] == {}
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    assert _call(tools, "civil_load", doc_id="doc")["result_keys"] == []


def test_rename_keeps_one_persisted_study_and_loads_renamed_model(tools, store):
    _new(tools, doc_id="doc")
    before_id = store.study()["id"]
    _call(tools, "civil_site_edit", ops=[{"op": "rename", "name": "Renamed"}], doc_id="doc")
    assert store.study()["id"] == before_id and store.study()["name"] == "Renamed"
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    assert _call(tools, "civil_load", doc_id="doc")["site_name"] == "Renamed"


def test_surface_volume_runs_coexist_and_stage_storage_matches_constant_plane(tools, store):
    _new(tools, doc_id="doc")
    _plane(tools, c=1)
    _plane(tools, sid="bottom", c=0)
    first = _call(tools, "civil_surface_analyze", kind="volume", surface_id="eg", other_surface_id="bottom", doc_id="doc")
    assert first["fill_m3"] == pytest.approx(100)
    second = _call(tools, "civil_surface_analyze", kind="stage_storage", surface_id="eg",
                   polygon=[[0, 0], [10, 0], [10, 10], [0, 10]], stages=[0, 1, 2, 3], doc_id="doc")
    assert [row[1] for row in second["stage_storage"]] == pytest.approx([0, 0, 100, 200])
    assert set(store.study()["results"]["earthwork"]) == {"volume:eg:bottom", "stage_storage:eg"}
    qto = _call(tools, "civil_quantities")
    assert [r["quantity"] for r in qto["items"] if r["item"] == "earthwork_fill"] == pytest.approx([100])


def test_contour_slope_profile_compaction_detail_and_no_model_pollution(tools, store):
    _new(tools, doc_id="doc")
    _plane(tools, a=0.03, b=-0.04, c=2, doc_id="doc")
    model = copy.deepcopy(store.study()["params"]["model"])
    contours = _call(tools, "civil_surface_analyze", kind="contours", surface_id="eg", interval=0.1, doc_id="doc")
    assert contours["contour_level_count"] > 1 and "contours" not in contours
    full = _call(tools, "civil_surface_analyze", kind="contours", surface_id="eg", interval=0.1, detail=True)
    assert len(full["contours"]) == contours["contour_level_count"]
    slope = _call(tools, "civil_surface_analyze", kind="slope", surface_id="eg")
    assert slope["summary"]["mean_slope_pct"] == pytest.approx(5) and "triangles" not in slope
    profile = _call(tools, "civil_surface_analyze", kind="profile", surface_id="eg", polyline=[[0, 0], [10, 0]], step=1, detail=True)
    assert len(profile["samples"]) == 11
    assert [r["z_m"] for r in profile["samples"]] == pytest.approx([2 + .03*i for i in range(11)])
    assert store.study()["params"]["model"] == model and store.study()["results"] == {}


def test_alignment_pi_geometry_and_sight_distance_match_engine(tools, store):
    _new(tools, doc_id="doc")
    pis = [[0, 0], [200, 0], [200, 200]]
    vertical = {"pvis": [{"station": 0, "elevation": 100, "curve_length": 0},
                         {"station": 180, "elevation": 105.4, "curve_length": 100},
                         {"station": 300, "elevation": 103, "curve_length": 0}]}
    created = _call(tools, "civil_alignment", alignment_id="road", pis=pis, radii=[50], vertical=vertical, doc_id="doc")
    assert created["length_m"] == pytest.approx(300 + 25*math.pi)
    result = _call(tools, "civil_alignment_analyze", kind="sight_distance", alignment_id="road", design_speed_kmh=100, doc_id="doc")
    expected_al = alignment.from_pis(pis, [50], id="road", name="road")
    expected_al = alignment.build(expected_al["horizontal"], vertical, id="road", name="road")
    expected = alignment.vertical_curve_checks(expected_al, 100, store.study()["params"]["model"]["criteria"])
    assert result["computed_ssd_m"] == pytest.approx(0.278*100*2.5 + .039*100**2/3.4)
    assert result["stopping_sight_distance_m"] == 185
    assert result["checks"] == expected["checks"] and result["curves"] == expected["curves"]
    assert store.study()["results"] == {}  # Deterministic check is not a model edit or persisted earthwork.


def test_alignment_sections_earthwork_and_quantities_use_stored_totals(tools, store):
    _new(tools, doc_id="doc")
    _plane(tools, low=-20, high=30)
    _call(tools, "civil_alignment", alignment_id="road", horizontal=[{"type": "line", "start": [0, 0], "end": [10, 0]}],
          vertical={"pvis": [{"station": 0, "elevation": 2, "curve_length": 0},
                             {"station": 10, "elevation": 2, "curve_length": 0}]})
    template = {"width_left": 3, "width_right": 3, "cross_slope_pct": 0, "cut_slope": 2, "fill_slope": 2}
    sections = _call(tools, "civil_alignment_analyze", kind="sections", alignment_id="road", surface_id="eg", template=template, stations=[0, 10], doc_id="doc")
    assert sections["sections_count"] == 2 and "sections" not in sections
    result = _call(tools, "civil_alignment_analyze", kind="earthwork", alignment_id="road", surface_id="eg", template=template, stations=[0, 10], method="prismoidal", doc_id="doc")
    # A=6*2 + 2 triangles*(4*2/2)=20 m²; V=20*10=200 m³.
    assert result["total_fill_m3"] == pytest.approx(200, abs=1e-6)
    assert result["total_cut_m3"] == pytest.approx(0)
    run = store.study()["results"]["earthwork"]["earthwork:road:eg"]
    assert len(run["sections"]) == 2 and len(run["mid_sections"]) == 1
    qto = _call(tools, "civil_quantities", doc_id="doc")
    fill = next(row for row in qto["items"] if row["item"] == "earthwork_fill")
    assert fill["quantity"] == pytest.approx(200) and fill["element_ids"] == ["road"]
    profile = _call(tools, "civil_alignment_analyze", kind="design_vs_existing", alignment_id="road", surface_id="eg", step=5, detail=True)
    assert [p["fill_depth_m"] for p in profile["samples"]] == [2, 2, 2]
    existing = _call(tools, "civil_alignment_analyze", kind="profile", alignment_id="road", surface_id="eg", step=5)
    assert existing["samples_count"] == 3 and existing["profile_summary"]["ground_elevation_range_m"] == [0, 0]


def test_grade_pad_persists_geometry_and_frustum_earthwork(tools, store):
    _new(tools)
    _plane(tools, low=-10, high=20)
    result = _call(tools, "civil_grade_pad", surface_id="eg", pad_polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
                   pad_elevation=2, cut_slope=2, fill_slope=2, graded_surface_id="fg", doc_id="doc")
    assert result["volumes"]["fill_m3"] == pytest.approx(1208/3, rel=1e-7)
    assert result["surface"]["id"] == "fg" and "points" not in result["surface"]
    st = store.study()
    fg = next(surface for surface in st["params"]["model"]["surfaces"] if surface["id"] == "fg")
    assert terrain.surface_sampler(fg)(5, 5) == pytest.approx(2)
    assert st["results"]["earthwork"]["grade_pad:fg"]["surface"] == fg
    qto = _call(tools, "civil_quantities")
    assert next(row["quantity"] for row in qto["items"] if row["item"] == "earthwork_fill") == pytest.approx(1208/3, rel=1e-7)


@pytest.mark.parametrize("fmt", ["dem_ascii", "dem_xyz"])
def test_dem_import_retains_full_grid_and_tin_without_returning_arrays(tools, store, tmp_path, fmt):
    source = _ascii(tmp_path)
    options = {"surface_id": "raster"}
    if fmt == "dem_xyz":
        source = tmp_path / "plane.xyz"
        source.write_text("\n".join(f"{x} {y} {10+x+y}" for x in (.5, 1.5, 2.5) for y in (.5, 1.5, 2.5)), encoding="utf-8")
        options["cell_size"] = 1
    result = _call(tools, "civil_import", source=str(source), format=fmt, options=options, doc_id="doc")
    assert result["surfaces"][0]["point_count"] == 9 and result["surfaces"][0]["triangle_count"] == 8
    surface = store.study()["params"]["model"]["surfaces"][0]
    assert terrain.surface_sampler(surface)(1, 1) == pytest.approx(12)
    assert len(surface["source"]["dem"]["z"]) == 3
    assert result["dropped"] == [] and result["provenance"][0]["kind"] == fmt


def test_dem_surface_build_and_watershed_create_routed_catchment(tools, store, tmp_path):
    _new(tools)
    _edit(tools, "gravity_networks", _gravity())
    path = _ascii(tmp_path)
    built = _call(tools, "civil_surface_build", surface_id="dem", dem_path=str(path), dem_stride=1, doc_id="doc")
    assert built["point_count"] == 9 and "source" not in built
    expected = dem.watershed(dem.flow_direction_d8(dem.fill_depressions(dem.read_ascii_grid(path))), [.5, .5])
    result = _call(tools, "civil_watershed", dem_path=str(path), outlet_xy=[.5, .5], catchment_id="lot", outlet_node="A", doc_id="doc")
    assert result["cells"] == expected["cells"] == 9 and result["area_m2"] == pytest.approx(9)
    catch = store.study()["params"]["model"]["catchments"][0]
    assert catch["outlet"] == "A" and catch["polygon"] == expected["boundary"]["coordinates"][0][:-1]
    assert catch["runoff_coeff"] is None and catch["cn"] is None  # No hydrologic parameters invented.
    assert "mask" not in result and "boundary" not in result


def test_quantity_trench_cost_unpriced_items_and_csv_exports(tools, store, tmp_path):
    _new(tools)
    _edit(tools, "gravity_networks", _gravity())
    trench = {"side_clearance_m": .2, "bedding_depth_m": .1, "sample_step_m": 2}
    qto = _call(tools, "civil_quantities", trench=trench, doc_id="doc")
    rows = {r["item"]: r for r in qto["items"]}
    assert rows["pipe_length_plan"]["quantity"] == pytest.approx(10)
    assert rows["trench_excavation"]["quantity"] == pytest.approx((.35+2*.2)*(5-1+.1)*10)
    rates = {"pipe_length_plan": {"unit": "m", "unit_price": 20, "currency": "USD", "source": "Project quote", "date": "2026-10-07"}}
    cost = _call(tools, "civil_cost_estimate", price_book=rates, contingency_pct=10, escalation={"factor": 1.2}, doc_id="doc")
    assert cost["totals_by_currency"]["USD"]["subtotal"] == pytest.approx(200)
    assert cost["totals_by_currency"]["USD"]["total"] == pytest.approx(264)
    assert not cost["complete"] and "trench_excavation" in {r["item"] for r in cost["unpriced"]}
    assert len(cost["unpriced"]) == len(qto["items"])-1
    assert store.study()["results"]["qto"]["items"] == qto["items"]
    for fmt in ("qto_csv", "cost_csv"):
        path = tmp_path / (fmt + ".csv")
        _call(tools, "civil_export", format=fmt, path=str(path))
        exported = list(csv.DictReader(io.StringIO(path.read_text())))
        assert len(exported) == len(qto["items"])
        if fmt == "cost_csv":
            assert {row["priced"] for row in exported} == {"True", "False"}
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    loaded = _call(tools, "civil_load", doc_id="doc", detail=True)
    assert loaded["results"]["cost"]["totals_by_currency"]["USD"]["total"] == pytest.approx(264)
    edited = _call(tools, "civil_site_edit", ops=[{"op": "remove", "collection": "gravity_networks", "id": "gravity"}], doc_id="doc")
    assert edited["cleared_result_keys"] == ["cost", "qto"] and store.study()["results"] == {}
    assert json.loads(tools["civil_cost_estimate"](price_book=rates))["reason"] == "no_qto"


@pytest.mark.parametrize("fmt,suffix", [("geojson", ".json"), ("geopackage", ".gpkg")])
def test_gis_tool_exports_imports_project_crs_and_reports_dropped(tools, store, tmp_path, fmt, suffix):
    _new(tools, crs="EPSG:26915")
    network = _gravity()
    for node in network["nodes"]:
        node["x"] += 500000
    _edit(tools, "gravity_networks", network)
    _edit(tools, "structures", {"id": "culvert", "kind": "culvert"})
    path = tmp_path / ("site" + suffix)
    exported = _call(tools, "civil_export", format=fmt, path=str(path))
    assert exported["dropped"][0]["id"] == "culvert"
    if fmt == "geojson":
        assert json.loads(path.read_text())["features"][0]["geometry"]["coordinates"] == pytest.approx([-93, 0], abs=1e-9)
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    imported = _call(tools, "civil_import", source=str(path), format=fmt, doc_id="doc")
    assert imported["counts"]["gravity_networks"] == 1 and imported["dropped"] == []
    restored = store.study()["params"]["model"]["gravity_networks"][0]
    assert restored["nodes"][0]["x"] == pytest.approx(500000, abs=1e-6)
    assert restored["conduits"][0]["diameter"] == pytest.approx(.3)
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    _new(tools)  # An empty unknown-CRS target adopts the source's declared project CRS.
    adopted = _call(tools, "civil_import", source=str(path), format=fmt)
    assert adopted["crs"]["horizontal"] == "EPSG:26915"


def test_swmm_tool_import_export_preserves_si_geometry_and_uninterpreted_rows(tools, store, tmp_path):
    imported = _call(tools, "civil_import", source=str(_DATA / "swmm_roundtrip.inp"), format="swmm", options={"network_id": "storm"}, doc_id="doc")
    assert imported["gravity_networks"][0]["counts"] == {"nodes": 5, "conduits": 4}
    assert imported["counts"]["catchments"] == 1 and "TIMESERIES" in imported["dropped"]
    network = store.study()["params"]["model"]["gravity_networks"][0]
    assert network["conduits"][0]["diameter"] == pytest.approx(.3048)
    path = tmp_path / "storm.inp"
    _call(tools, "civil_export", format="swmm", path=str(path), network_id="storm")
    text = path.read_text()
    assert "[TIMESERIES]" in text and "RAIN 01/01/2020 00:00 0" in text
    roundtrip = swmm_inp.import_inp(text, id="storm")
    assert roundtrip["network"]["conduits"][0]["diameter"] == pytest.approx(.3048)
    assert roundtrip["catchments"][0]["area_m2"] == pytest.approx(2.5*4046.8564224)


def test_landxml_and_ifc_exports_contain_real_alignment_network_geometry(tools, tmp_path):
    _new(tools, crs="EPSG:26915")
    _edit(tools, "gravity_networks", _gravity())
    _call(tools, "civil_alignment", alignment_id="road", horizontal=[{"type": "line", "start": [0, 0], "end": [10, 0]}])
    xml = tmp_path / "civil.xml"
    _call(tools, "civil_export", format="landxml", path=str(xml))
    fragment = landxml.read(xml)["model_fragment"]
    assert len(fragment["gravity_networks"][0]["conduits"]) == 1
    assert alignment.length(fragment["alignments"][0]) == pytest.approx(10)
    ifc = tmp_path / "civil.ifc"
    result = _call(tools, "civil_export", format="ifc", path=str(ifc))
    assert result["entity_counts"]["IfcAlignment"] == 1 and result["entity_counts"]["IfcPipeSegment"] == 1
    assert "IFC4X3_ADD2" in ifc.read_text() and "IFCPIPESEGMENT" in ifc.read_text()


def test_schedule_cpm_4d_state_and_msproject_export_roundtrip(tools, store, tmp_path):
    _new(tools)
    _edit(tools, "gravity_networks", _gravity())
    tasks = [{"id": "excavate", "duration_d": 2, "element_ids": ["C"]},
             {"id": "install", "duration_d": 3, "element_ids": ["C"], "predecessors": [{"id": "excavate", "type": "FS", "lag_d": 1}]}]
    result = _call(tools, "civil_schedule", tasks=tasks, start_date="2026-10-07", day=2.5, doc_id="doc")
    assert result["project_duration_d"] == 6 and result["critical_path"] == ["excavate", "install"]
    assert result["links_4d"]["elements"]["C"]["install_window_d"] == [0, 6]
    assert result["state"]["elements"]["C"] == "in-progress"
    path = tmp_path / "project.xml"
    _call(tools, "civil_export", format="msproject", path=str(path))
    restored = planning.import_msproject_xml(path.read_text())
    assert restored["project_duration_d"] == pytest.approx(6)
    assert restored["tasks"][1]["element_ids"] == ["C"]
    assert store.study()["results"]["schedule"]["start_date"] == "2026-10-07"


def test_crs_transform_and_destination_scale_factors_match_central_meridian(tools):
    result = _call(tools, "civil_crs_transform", points=[[-93, 0, 100]], src="EPSG:4326", dst="EPSG:26915", scale_factors=True)
    assert result["points"][0] == pytest.approx([500000, 0, 100], abs=1e-6)
    assert result["scale_factors"][0]["grid_factor"] == pytest.approx(.9996, abs=1e-8)
    reverse = _call(tools, "civil_crs_transform", points=result["points"], src="EPSG:26915", dst="EPSG:4326")
    assert reverse["points"][0] == pytest.approx([-93, 0, 100], abs=1e-9)
    assert result["provenance"]["always_xy"]


def test_named_refusals_for_missing_sites_qto_and_invalid_formats(tools, store, tmp_path):
    assert json.loads(tools["civil_site_summary"]())["reason"] == "no_civil_site"
    assert json.loads(tools["civil_site_summary"](site="missing"))["reason"] == "unknown_site"
    assert json.loads(tools["civil_load"](doc_id="empty"))["reason"] == "no_civil_site"
    assert json.loads(tools["civil_import"](source="ignored", format="invalid"))["reason"] == "invalid_format"
    assert json.loads(tools["civil_export"](format="invalid", path=str(tmp_path / "none")))["reason"] == "invalid_format"
    _new(tools)
    assert json.loads(tools["civil_cost_estimate"](price_book={}))["reason"] == "no_qto"
    assert json.loads(tools["civil_export"](format="epanet", path=str(tmp_path / "missing.inp")))["reason"] == "network_id_required"
    assert not (tmp_path / "missing.inp").exists()


def test_gis_merge_rejects_incompatible_crs_without_changing_target(tools, tmp_path):
    _new(tools, crs="EPSG:26915")
    network = _gravity()
    for node in network["nodes"]:
        node["x"] += 500000
    _edit(tools, "gravity_networks", network)
    path = tmp_path / "source.gpkg"
    _call(tools, "civil_export", format="geopackage", path=str(path))
    cc.SITES.clear()
    cc.ACTIVE.update(name=None)
    _new(tools, crs="EPSG:26916")
    refusal = json.loads(tools["civil_import"](source=str(path), format="geopackage"))
    assert refusal["reason"] == "crs_mismatch"
    summary = _call(tools, "civil_site_summary")
    assert summary["crs"]["horizontal"] == "EPSG:26916" and summary["counts"]["gravity_networks"] == 0
