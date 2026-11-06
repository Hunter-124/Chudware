"""Contract and real-engine coverage for geometry-based stress screening."""
from __future__ import annotations

import json

import pytest

from services.mcp import stress_tools as st


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(st, "_doc", lambda doc: doc or "current")
    registry = Registry()
    st.register_stress_tools(registry)
    return registry.tools


def _edge(sid="edge1", dev=90):
    return {"kind": "concave", "sid": sid, "body": 1, "topo": 1,
            "mid": [1, 2, 3], "dihedral_deg": 180 + dev, "deviation_deg": dev,
            "length_mm": 20, "faces": ["f1", "f2"], "owner_feature": "feature1"}


def _mock_engine(monkeypatch, *, dry_revision=7):
    calls = []

    def call(method, params):
        calls.append((method, params))
        if method == "eval_edge_analysis":
            assert params["include_ligaments"] is True
            return {"revision": 7, "complete": True, "truncated": False,
                    "bodies": [{"body_id": 1}], "edges": [_edge()],
                    "concave_blends": [{"body": 1, "face_sid": "blend1", "radius_mm": 1,
                                        "center": [2, 3, 4], "owner_feature": "fillet1"}],
                    "ligaments": {"rows": [{"body": 1, "face_sid": "hole1", "other_face_sid": "boundary1",
                                             "owner_feature": "hole_feature", "kind": "bore_boundary",
                                             "distance_mm": 2, "radius_mm": 4, "point": [1, 1, 1],
                                             "other_point": [3, 1, 1],
                                             "basis": "minimum_distance_between_finite_faces"}],
                                  "bodies": [{"body": 1, "incomplete": False}], "truncated": False}}
        if method == "doc_bbox":
            return {"size": [100, 50, 20]}
        if method == "doc_query":
            if params["query"]["of"] == "edge":
                return {"matches": [{"body": 1, "topo": 1, "sid": "edge1", "kind": "edge",
                                     "centroid": [1, 2, 3], "surf": "line"}]}
            return {"matches": [{"body": 1, "topo": 8, "sid": "wall1", "owner_feature": "wall"}]}
        if method == "eval_thickness":
            return {"min_thickness_mm": 1, "mean_thickness_mm": 5,
                    "minimum_sample": {"at": [0, 0, 0], "opposite_at": [0, 0, 1], "face_id": 8},
                    "measurement_basis": {"method": "inscribed_sphere", "spacing_mm": 0.2}}
        if method == "doc_dry_run_ops":
            return {"ok": True, "source_revision": dry_revision, "rebuild": {"ok": True,
                    "feature_status": [{"id": "f99", "ok": True, "rolled_back": False,
                                        "data": {"fillet": {"requested": {"radius": 0.25},
                                                            "effective": {"radius": 0.2},
                                                            "adjustment": "upper", "selected_edges": 1,
                                                            "covered_edges": 1}}}]}}
        raise AssertionError(method)

    monkeypatch.setattr(st.cb, "call", call)
    return calls


def test_registration_inventory_verification_and_unassumed_load(tools, monkeypatch):
    calls = _mock_engine(monkeypatch)
    assert set(tools) == {"find_stress_risers", "eval_curvature", "eval_geometry_analysis"}
    out = json.loads(tools["find_stress_risers"](doc_id="d", load_hint="tension"))
    assert out["ok"] and out["revision"] == 7 and not out["inventory_incomplete"]
    rows = {r["kind"]: r for r in out["findings"]}
    assert set(rows) == {"sharp_concave_edge", "concave_radius", "hole_ligament_candidate", "thin_neck_candidate"}
    sharp = rows["sharp_concave_edge"]
    assert sharp["sid"] == "edge1" and sharp["location"] == [1, 2, 3] and sharp["owner_feature"] == "feature1"
    fix = sharp["fix"]
    selector = fix["ops"][0]["feature"]["refs"]["edges"][0]
    assert selector["sid"] == "edge1" and selector["centroid"] == [1, 2, 3]
    assert fix["requested_radius_mm"] == 0.25
    assert fix["verification"]["status"] == "built" and fix["verification"]["builds"] is True
    assert fix["verification"]["effective_radius_mm"] == 0.2
    assert rows["concave_radius"]["evidence"]["radius_to_min_thickness"] == 1
    assert rows["thin_neck_candidate"]["sid"] == "wall1"
    assert rows["hole_ligament_candidate"]["evidence"]["other_face_sid"] == "boundary1"
    assert "not certified" in rows["hole_ligament_candidate"]["evidence"]["note"]
    assert all(r["kt_estimate"]["value"] is None for r in rows.values())
    assert "Williams" in sharp["kt_estimate"]["source"] and "singular" in sharp["kt_estimate"]["reason"]
    assert sharp["sids"] == ["edge1"] and sharp["basis"] == "eval_edge_analysis"
    assert any("not treated as validated" in n for n in out["notes"])
    assert sum(m == "doc_dry_run_ops" for m, _ in calls) == 1


def test_revision_mismatch_does_not_claim_fix_verification(tools, monkeypatch):
    _mock_engine(monkeypatch, dry_revision=8)
    out = json.loads(tools["find_stress_risers"](doc_id="d"))
    sharp = next(r for r in out["findings"] if r["kind"] == "sharp_concave_edge")
    assert sharp["fix"]["verification"]["status"] == "not_verified"
    assert "revision changed" in sharp["fix"]["verification"]["reason"]


def test_paging_severity_and_skipped_verification(tools, monkeypatch):
    calls = _mock_engine(monkeypatch)
    fn = tools["find_stress_risers"]
    first = json.loads(fn(doc_id="d", min_severity="medium", limit=1, verify_fixes=False))
    second = json.loads(fn(doc_id="d", min_severity="medium", limit=1, offset=first["next_offset"], verify_fixes=False))
    assert first["next_offset"] == 1 and first["total_findings"] == 3
    assert first["findings"][0] != second["findings"][0]
    assert all(m != "doc_dry_run_ops" for m, _ in calls)
    assert any("verification skipped" in n for n in first["notes"])


def test_incomplete_and_missing_data_are_disclosed(tools, monkeypatch):
    def call(method, params):
        if method == "eval_edge_analysis":
            return {"revision": 1, "complete": False, "truncated": False, "bodies": [{"body_id": 1}],
                    "edges": [], "concave_blends": [], "unclassified_count": 2}
        if method == "doc_bbox":
            return {}
        if method == "doc_query":
            return {"matches": []}
        if method == "eval_thickness":
            raise ValueError("thickness unavailable")
        raise AssertionError(method)
    monkeypatch.setattr(st.cb, "call", call)
    out = json.loads(tools["find_stress_risers"](doc_id="d"))
    assert out["inventory_incomplete"] and out["findings"] == []
    assert any("ligament" in n for n in out["notes"])
    assert any("thickness unavailable" in n for n in out["notes"])
    assert any("unclassified" in n or "could not be classified" in n for n in out["notes"])


def test_geometry_and_curvature_wrappers_bound_and_route(tools, monkeypatch):
    calls = []
    def call(method, params):
        calls.append((method, params))
        if method == "eval_curvature":
            return {"aicm_path": "/tmp/field.aicm", "min_curvature": -1, "max_curvature": 2, "huge": [1] * 200}
        if method == "eval_geometry_analysis":
            return {"revision": 5, "complete": False, "source_failures": [], "bodies": [{"body_id": 2,
                    "gap_scan_complete": False, "findings": [{"kind": "short_edge", "edge_id": i} for i in range(7)]}]}
        raise AssertionError(method)
    monkeypatch.setattr(st.cb, "call", call)
    curve = json.loads(tools["eval_curvature"](doc_id="d", body=2, kind="gaussian"))
    assert curve["units"] == "1/mm²" and "huge" not in curve
    assert calls[0][1] == {"doc_id": "d", "body": 2, "kind": "gaussian"}
    geo = json.loads(tools["eval_geometry_analysis"](doc_id="d", min_edge_length=0, offset=2, limit=2))
    assert [f["edge_id"] for f in geo["findings"]] == [2, 3]
    assert geo["next_offset"] == 4 and not geo["complete"]
    assert not geo["bodies"][0]["gap_scan_complete"]
    assert calls[-1][1]["min_edge_length"] == 0


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"offset": -1}, {"body": 0}, {"min_severity": "extreme"}])
def test_invalid_screen_arguments_are_errors(tools, kwargs):
    out = json.loads(tools["find_stress_risers"](doc_id="d", **kwargs))
    assert not out["ok"]


def test_real_bracket_riser_and_evaluation_fields(tools, monkeypatch):
    if not st.cb.available():
        pytest.skip("native engine unavailable")
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    cb = st.cb
    doc = cb.call("doc_new", {"name": "stress_tool_bracket", "kind": "part"})["doc_id"]
    try:
        result = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10), "at": [34, 20, 5]}),
            cb.feat("box", "Wall", {"length": cb.dim(5), "width": cb.dim(40), "height": cb.dim(40), "at": [8.5, 20, 30]}),
        ]})
        assert all(s["ok"] for s in result["rebuild"]["feature_status"]), result
        before = cb.call("doc_tree", {"doc_id": doc})
        out = json.loads(tools["find_stress_risers"](doc_id=doc, limit=20))
        assert out["ok"], out
        sharp = next(r for r in out["findings"] if r["kind"] == "sharp_concave_edge")
        assert sharp["sid"] and sharp["location"] and sharp["evidence"]["dihedral_deg"] == pytest.approx(270, abs=1)
        assert sharp["kt_estimate"]["value"] is None
        assert sharp["fix"]["verification"]["status"] in ("built", "failed", "not_verified")
        assert cb.call("doc_tree", {"doc_id": doc}) == before
        curvature = json.loads(tools["eval_curvature"](doc_id=doc, kind="max", body=1))
        assert curvature["ok"] and curvature["aicm_path"]
        geometry = json.loads(tools["eval_geometry_analysis"](doc_id=doc, limit=1))
        assert geometry["ok"] and len(geometry["findings"]) <= 1 and geometry["bodies"]
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_real_bore_reports_measured_ligament_candidate(tools, monkeypatch):
    if not st.cb.available():
        pytest.skip("native engine unavailable")
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    cb = st.cb
    doc = cb.call("doc_new", {"name": "stress_tool_bore", "kind": "part"})["doc_id"]
    try:
        result = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box",
                "params": {"length": 40, "width": 30, "height": 20}}},
            {"op": "add_feature", "feature": {"type": "hole",
                "params": {"style": "simple", "d": 6, "end": "blind", "depth": 10},
                "refs": {"face": {"of": "face", "side": "top"}}}},
        ]})
        assert all(s["ok"] for s in result["rebuild"]["feature_status"]), result
        out = json.loads(tools["find_stress_risers"](doc_id=doc, verify_fixes=False, limit=100))
        assert out["ok"], out
        candidates = [r for r in out["findings"] if r["kind"] == "hole_ligament_candidate"]
        assert candidates, out
        row = candidates[0]
        assert row["sid"] and row["location"] and row["evidence"]["distance_mm"] > 0
        assert row["evidence"]["bore_radius_mm"] == pytest.approx(3)
        assert row["kt_estimate"]["value"] is None
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_native_edge_pages_keep_revision_and_only_compute_ligaments_once(monkeypatch):
    calls = []
    def call(method, params):
        assert method == "eval_edge_analysis"
        calls.append(params)
        if params["cursor"] == 0:
            return {"revision": 3, "edges": [_edge("e1")], "concave_blends": [],
                    "truncated": True, "next_cursor": 1, "ligaments": {"rows": [], "bodies": []}}
        return {"revision": 3, "edges": [_edge("e2")], "truncated": False}
    monkeypatch.setattr(st.cb, "call", call)
    _, edges, _, truncated = st._edge_inventory("d", 1)
    assert [e["sid"] for e in edges] == ["e1", "e2"] and not truncated
    assert [p["include_ligaments"] for p in calls] == [True, False]
    assert all(p["body"] == 1 for p in calls)


def test_native_edge_pages_reject_revision_changes(monkeypatch):
    def call(method, params):
        if params["cursor"] == 0:
            return {"revision": 3, "edges": [], "truncated": True, "next_cursor": 1}
        return {"revision": 4, "edges": [], "truncated": False}
    monkeypatch.setattr(st.cb, "call", call)
    with pytest.raises(ValueError, match="Document changed"):
        st._edge_inventory("d", None)


def test_real_fillet_dry_run_and_filleted_bracket_comparison(tools, monkeypatch):
    if not st.cb.available():
        pytest.skip("native engine unavailable")
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
    cb = st.cb
    docs = []
    try:
        for name in ("stress_sharp_comparison", "stress_fillet_comparison"):
            doc = cb.call("doc_new", {"name": name, "kind": "part"})["doc_id"]
            docs.append(doc)
            built = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
                cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(20),
                                         "height": cb.dim(10), "at": [34, 10, 5]}),
                cb.feat("box", "Wall", {"length": cb.dim(5), "width": cb.dim(20),
                                         "height": cb.dim(40), "at": [8.5, 10, 30]}),
            ]})
            assert built["rebuild"]["ok"] and all(
                s["ok"] for s in built["rebuild"]["feature_status"]), built

        original = json.loads(tools["find_stress_risers"](
            doc_id=docs[0], limit=100, verify_fixes=False))
        snapshot = cb.call("doc_tree", {"doc_id": docs[1]})
        candidate = json.loads(tools["find_stress_risers"](
            doc_id=docs[1], limit=100, verify_fixes=True))
        assert original["ok"] and candidate["ok"], (original, candidate)
        old_edges = [r for r in original["findings"] if r["kind"] == "sharp_concave_edge"]
        target = next(r for r in candidate["findings"] if r["kind"] == "sharp_concave_edge")
        assert old_edges and target["fix"]["verification"]["status"] == "built", target
        evidence = target["fix"]["verification"]
        assert evidence["selected_edges"] == evidence["covered_edges"] == 1
        assert 0 < evidence["effective_radius_mm"] <= evidence["requested_radius_mm"]
        assert cb.call("doc_tree", {"doc_id": docs[1]}) == snapshot

        applied = cb.call("doc_apply_ops", {"doc_id": docs[1], "ops": target["fix"]["ops"]})
        assert applied["rebuild"]["ok"] and all(
            s["ok"] for s in applied["rebuild"]["feature_status"]), applied
        after = json.loads(tools["find_stress_risers"](
            doc_id=docs[1], limit=100, verify_fixes=False))
        assert after["ok"], after
        new_edges = [r for r in after["findings"] if r["kind"] == "sharp_concave_edge"]
        blends = [r for r in after["findings"] if r["kind"] == "concave_radius"]
        assert len(new_edges) < len(old_edges), (original, after)
        assert any(b["evidence"]["radius_mm"] == pytest.approx(
            evidence["effective_radius_mm"], rel=1e-5) for b in blends), after
        assert cb.call("doc_tree", {"doc_id": docs[1]}) != snapshot
    finally:
        for doc in docs:
            cb.call("doc_close", {"doc_id": doc})


@pytest.mark.parametrize("response,expected", [
    ({"ok": False, "source_revision": 7, "error": "rejected"}, False),
    ({"ok": False, "source_revision": 7, "skipped": "budget_exceeded"}, None),
    ({"ok": False, "error": "revision unknown"}, None),
])
def test_dry_run_build_verdict_distinguishes_failure_from_unknown(monkeypatch, response, expected):
    monkeypatch.setattr(st.cb, "call", lambda method, params: response)
    proposal = {"ops": []}
    st._verify("d", proposal, 7)
    assert proposal["verification"]["builds"] is expected


def test_ligament_world_point_fix_uses_measured_axis_and_verifies_own_feature(tools, monkeypatch):
    _mock_engine(monkeypatch)
    original_call = st.cb.call
    def call(method, params):
        if method == "eval_edge_analysis":
            result = original_call(method, params)
            result["ligaments"]["rows"][0].update(
                face_id=7, point=[4, 0, 5], other_point=[6, 0, 5])
            return result
        if method == "doc_query" and params["query"]["of"] == "face":
            return {"matches": [{"body": 1, "topo": 7, "sid": "hole1",
                                 "axis": [0, 0, 1], "axis_point": [0, 0, 0]},
                                {"body": 1, "topo": 8, "sid": "wall1", "owner_feature": "wall"}]}
        if method == "doc_tree":
            return {"revision": 7, "tree": {"features": [{"id": "hole_feature", "type": "hole",
                    "params": {"world_points": [[0, 0, 5]]}}]}}
        if method == "doc_dry_run_ops" and params["ops"][0]["op"] == "edit_feature":
            return {"ok": True, "source_revision": 7, "rebuild": {"ok": True, "feature_status": [
                {"id": "hole_feature", "ok": True}, {"id": "later_feature", "ok": True}]}}
        return original_call(method, params)
    monkeypatch.setattr(st.cb, "call", call)
    out = json.loads(tools["find_stress_risers"](doc_id="d"))
    row = next(r for r in out["findings"] if r["kind"] == "hole_ligament_candidate")
    fix = row["fix"]
    assert fix["ops"] == [{"op": "edit_feature", "id": "hole_feature",
                           "params": {"world_points": [[-2, 0, 5]]}}]
    assert fix["verification"]["builds"] is True
    assert "does not establish an improved ligament" in fix["basis"]


def test_ligament_fix_refuses_ambiguous_hole_coordinates():
    item = {"owner_feature": "f1", "kind": "bore_bore", "other_owner_feature": "f1",
            "point": [4, 0, 0], "other_point": [6, 0, 0], "distance_mm": 2, "radius_mm": 4}
    face = {"axis": [0, 0, 1], "axis_point": [0, 0, 0]}
    hole = {"id": "f1", "type": "hole", "params": {"world_points": [[0, 0, 0]]}}
    proposal, reason = st._ligament_fix(item, face, [hole])
    assert proposal is None and "same owner" in reason
    hole["params"]["world_points"].append([1, 2, 3])
    proposal, reason = st._ligament_fix(item, face, [hole])
    assert proposal is None and "unambiguous" in reason


def test_finding_ids_are_unique_for_shared_bore_and_stable_across_pages(tools, monkeypatch):
    _mock_engine(monkeypatch)
    original_call = st.cb.call
    def call(method, params):
        result = original_call(method, params)
        if method == "eval_edge_analysis":
            rows = result["ligaments"]["rows"]
            rows[0].update(face_id=7, other_face_id=8)
            rows.append({**rows[0], "other_face_sid": "boundary2", "other_face_id": 9})
        return result
    monkeypatch.setattr(st.cb, "call", call)
    fn = tools["find_stress_risers"]
    whole = json.loads(fn(doc_id="d", limit=100, verify_fixes=False))
    ids = [r["id"] for r in whole["findings"]]
    assert len(ids) == len(set(ids))
    ligament_ids = [r["id"] for r in whole["findings"] if r["kind"] == "hole_ligament_candidate"]
    assert len(ligament_ids) == 2 and ligament_ids[0] != ligament_ids[1]
    paged = []
    offset = 0
    while offset is not None:
        result = json.loads(fn(doc_id="d", offset=offset, limit=1, verify_fixes=False))
        paged.extend(r["id"] for r in result["findings"])
        offset = result["next_offset"]
    assert paged == ids


def test_local_shoulder_screen_is_qualified_and_uses_both_chart_cases(tools, monkeypatch):
    _mock_engine(monkeypatch)
    original_call = st.cb.call
    def call(method, params):
        if method == "eval_edge_analysis":
            result = original_call(method, params)
            result["edges"][0]["face_topo"] = [2, 3]
            result["concave_blends"][0].update(topo=6, adjacent_face_topo=[4, 5])
            return result
        if method == "eval_local_thickness":
            face = params["face_topo"]
            width = 40 if face in (2, 4) else 50
            return {"revision": 7, "face_topo": face, "face_sid": f"face{face}",
                    "samples": [{"at": [1, 1, 1], "edge_at": [1, 0, 1],
                                 "offset_mm": 1, "thickness_mm": width}],
                    "measurement_basis": {"spacing_mm": 0.5, "budget_limited": False}}
        return original_call(method, params)
    monkeypatch.setattr(st.cb, "call", call)
    out = json.loads(tools["find_stress_risers"](doc_id="d", verify_fixes=False))
    blend = next(r for r in out["findings"] if r["kind"] == "concave_radius")
    screen = blend["kt_screen"]
    assert screen["status"] == "hypothetical" and screen["D_over_d"] == 1.25
    assert screen["r_over_d"] == pytest.approx(1 / 40)
    assert set(screen["cases"]) == {"tension", "bending"}
    assert all("Roark" in c["citation"] and c["assumed_L_over_D"] >= 6
               for c in screen["cases"].values())
    assert "not actual-part Kt" in screen["reason"] and blend["kt_estimate"]["value"] is None
    sharp = next(r for r in out["findings"] if r["kind"] == "sharp_concave_edge")
    assert sharp["kt_screen"]["value"] == "unbounded (sharp re-entrant)"
    assert sharp["kt_estimate"]["value"] is None


def test_local_shoulder_sampling_refuses_stale_or_budget_limited_evidence(monkeypatch):
    def call(method, params):
        return {"revision": 8, "face_topo": params["face_topo"], "samples": [],
                "measurement_basis": {"spacing_mm": 1}}
    monkeypatch.setattr(st.cb, "call", call)
    row = {"body": 1, "topo": 5, "face_topo": [1, 2]}
    sides, error = st._local_sides("d", 7, row, False)
    assert not sides and "revision" in error
    assert st._shoulder_screen(sides, 1)["status"] == "not_evaluated"
    def limited(method, params):
        return {"revision": 7, "face_topo": params["face_topo"], "samples": [],
                "measurement_basis": {"spacing_mm": 1, "budget_limited": True}}
    monkeypatch.setattr(st.cb, "call", limited)
    sides, error = st._local_sides("d", 7, row, False)
    assert not sides and "budget" in error


def test_shoulder_chart_rejects_missing_step_and_out_of_range_ratios():
    assert st._shoulder_screen([{"thickness_mm": 5}, {"thickness_mm": 5}], 1)["status"] == "not_evaluated"
    screen = st._shoulder_screen([{"thickness_mm": 4}, {"thickness_mm": 6}], 1000)
    assert screen["status"] == "not_evaluated" and "band" not in screen


def test_local_sampling_windows_cover_later_candidates(tools, monkeypatch):
    _mock_engine(monkeypatch)
    original_call = st.cb.call
    sampled = []
    def call(method, params):
        if method == "eval_edge_analysis":
            result = original_call(method, params)
            result["edges"] = [dict(_edge(f"edge{i}"), topo=i, face_topo=[1, 2])
                               for i in range(1, 7)]
            result["concave_blends"] = [{"body": 1, "topo": i, "face_sid": f"blend{i}",
                                         "radius_mm": 1, "center": [0, 0, 0],
                                         "adjacent_face_topo": [1, 2]} for i in range(1, 6)]
            return result
        if method == "eval_local_thickness":
            sampled.append((params.get("edge_topo"), params.get("blend_topo")))
            return {"revision": 7, "face_topo": params["face_topo"], "samples": [],
                    "unavailable_reason": "no_local_measured_samples",
                    "measurement_basis": {"spacing_mm": 1}}
        return original_call(method, params)
    monkeypatch.setattr(st.cb, "call", call)
    fn = tools["find_stress_risers"]
    first = json.loads(fn(doc_id="d", limit=100, verify_fixes=False))
    assert first["local_sampling"]["next_local_offset"] == 4
    assert {x for x, _ in sampled if x} == {1, 2, 3, 4}
    assert {x for _, x in sampled if x} == {1, 2, 3, 4}
    sampled.clear()
    second = json.loads(fn(doc_id="d", limit=100, verify_fixes=False, local_offset=4))
    assert second["local_sampling"]["next_local_offset"] is None
    assert {x for x, _ in sampled if x} == {5, 6}
    assert {x for _, x in sampled if x} == {5}
    assert {r["id"] for r in first["findings"]} == {r["id"] for r in second["findings"]}


def test_verified_pages_attempt_every_fix_and_advance_effective_limit(tools, monkeypatch):
    calls = _mock_engine(monkeypatch)
    original_call = st.cb.call
    def call(method, params):
        if method == "eval_edge_analysis":
            result = original_call(method, params)
            result["edges"] = [dict(_edge(f"edge{i}"), topo=i) for i in range(1, 13)]
            return result
        if method == "doc_query" and params["query"]["of"] == "edge":
            return {"matches": [{"body": 1, "topo": i, "sid": f"edge{i}", "kind": "edge",
                                  "centroid": [i, 2, 3], "surf": "line"} for i in range(1, 13)]}
        return original_call(method, params)
    monkeypatch.setattr(st.cb, "call", call)
    fn = tools["find_stress_risers"]
    first = json.loads(fn(doc_id="d", limit=100, verify_fixes=True))
    assert first["requested_limit"] == 100 and first["effective_limit"] == 8
    assert len(first["findings"]) == 8 and first["next_offset"] == 8
    first_fixes = [r["fix"] for r in first["findings"] if r.get("fix")]
    assert first_fixes and all(f["verification"]["status"] != "not_run" for f in first_fixes)
    assert sum(method == "doc_dry_run_ops" for method, _ in calls) == len(first_fixes)
    second = json.loads(fn(doc_id="d", limit=100, offset=first["next_offset"], verify_fixes=True))
    second_fixes = [r["fix"] for r in second["findings"] if r.get("fix")]
    assert second_fixes and all(f["verification"]["status"] != "not_run" for f in second_fixes)
    assert sum(method == "doc_dry_run_ops" for method, _ in calls) == len(first_fixes) + len(second_fixes)
    assert not ({r["id"] for r in first["findings"]} & {r["id"] for r in second["findings"]})
    unverified = json.loads(fn(doc_id="d", limit=100, verify_fixes=False))
    assert unverified["effective_limit"] == 100 and unverified["next_offset"] is None
