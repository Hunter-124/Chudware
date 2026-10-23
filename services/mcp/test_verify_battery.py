"""Tests for the verify-battery API and gap verifiers."""
from __future__ import annotations

import json
import pytest


# ── groundedness checker tests (no engine needed) ──────────────────────────

def test_groundedness_exact_match():
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness("The part weighs 150 g and is 50 mm tall.",
                                {"mass": 150.0, "height": 50.0})
    assert result["score"] == pytest.approx(1.0, abs=0.01)
    assert len(result["matched"]) == 2


def test_groundedness_unit_conversion():
    from tools.translator.groundedness import check_groundedness
    # 0.05 m == 50 mm  — cross-unit match
    result = check_groundedness("The height is 0.05 m.", {"height": 50.0})
    assert result["score"] == pytest.approx(1.0, abs=0.01)


def test_groundedness_ksi_mpa():
    from tools.translator.groundedness import check_groundedness
    # 250 MPa == 36.26 ksi — cross-unit match
    result = check_groundedness("Yield stress 36.26 ksi.", {"yield_mpa": 250.0})
    assert result["score"] == pytest.approx(1.0, abs=0.05)


def test_groundedness_spurious():
    from tools.translator.groundedness import check_groundedness
    # A numeral in the text with NO state match = hallucination risk -> spurious
    result = check_groundedness("The part is 999 mm tall.", {"height": 50.0})
    assert result["score"] < 0.5
    assert len(result["spurious"]) == 1
    assert result["spurious"][0]["extracted"] == 999.0
    # the uncited state slot is informational, never penalized
    assert result["unmentioned_state"][0]["slot"] == "height"


def test_groundedness_vacuous():
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness("The design looks great.", {})
    assert result["score"] == pytest.approx(1.0)


def test_groundedness_attached_unit():
    from tools.translator.groundedness import check_groundedness
    # No space between number and unit ("50mm") must still parse
    result = check_groundedness("Plate is 50mm x 3in.", {"width": 50.0, "thickness": 76.2})
    assert result["score"] == pytest.approx(1.0, abs=0.01)
    assert len(result["matched"]) == 2


def test_groundedness_no_false_unit_from_word():
    from tools.translator.groundedness import check_groundedness
    # "3 more" must NOT read as "3 m" (3000 mm); bare 3 still matches slot 3.0
    result = check_groundedness("Add 3 more ribs.", {"rib_count": 3.0})
    assert result["score"] == pytest.approx(1.0)
    assert result["matched"][0]["unit"] == ""


# ── unit_ranges tests (no engine needed) ────────────────────────────────────

def test_unit_ranges_clean():
    from services.verify.unit_ranges import check_unit_ranges
    # Typical steel bracket: volume ~50000 mm³, mass ~400 g
    state = {"volume": 50000.0, "mass": 400.0}
    assert check_unit_ranges(state) == []


def test_unit_ranges_ksi_confusion():
    from services.verify.unit_ranges import check_unit_ranges
    # A blatant out-of-range: stress of 1e7 MPa (clearly wrong)
    state = {"max_stress": 1e7}
    violations = check_unit_ranges(state)
    assert len(violations) >= 1
    assert violations[0]["field"] == "max_stress"


def test_unit_ranges_density_ok():
    from services.verify.unit_ranges import check_unit_ranges
    state = {"density": 7.85}  # steel g/cm³
    assert check_unit_ranges(state) == []


def test_unit_ranges_density_bad():
    from services.verify.unit_ranges import check_unit_ranges
    state = {"density": 7850.0}  # steel in kg/m³ — out of g/cm³ range
    violations = check_unit_ranges(state)
    assert len(violations) == 1


# ── req_join gap verifier tests (no engine needed) ──────────────────────────

def test_req_join_all_pass():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "pass"},
        {"id": "R2", "status": "verified"},
    ]))
    assert r["ok"] is True
    assert r["score"] == pytest.approx(1.0)


def test_req_join_partial():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "pass"},
        {"id": "R2", "status": "fail"},
    ]))
    assert r["ok"] is False
    assert r["score"] == pytest.approx(0.5)
    assert "R2" in r["unmet"]


def test_req_join_empty():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[]))
    assert r["ok"] is True


def test_req_join_verified_without_evidence_is_inconsistent():
    from services.verify.battery import _gap_req_join
    # aicad.req/1: a "verified" claim with an explicitly empty evidence list must fail the join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "verified", "trace": {"features": ["f1"], "evidence": []}},
        {"id": "R2", "status": "verified", "trace": {"features": [], "evidence": ["study:s1"]}},
    ]))
    assert r["ok"] is False
    assert r["score"] == pytest.approx(0.5)
    assert r["inconsistent"] == ["R1"]


def test_req_join_waived_excluded():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "verified"},
        {"id": "R2", "status": "waived"},
    ]))
    assert r["ok"] is True
    assert r["total"] == 1


# ── check_metadata tests ─────────────────────────────────────────────────────

def test_check_metadata_structure():
    from services.verify.battery import check_metadata
    meta = check_metadata()
    assert isinstance(meta, list)
    assert len(meta) >= 20  # at least the checks from driver notes
    names = {m["name"] for m in meta}
    assert "check_sketch" in names
    assert "run_cfd_accurate" in names
    assert "groundedness" in names
    # platform tagging
    cfd_acc = next(m for m in meta if m["name"] == "run_cfd_accurate")
    assert cfd_acc["platform"] == "posix"
    assert cfd_acc["cost"] == "minutes"


# ── battery API contract tests (no engine — mock the _get_fn dispatch) ──────

def test_verify_returns_stable_structure(monkeypatch):
    """verify() returns {scores, findings, elapsed_ms, skipped} with sorted keys."""
    import services.verify.battery as bat

    # Mock _get_fn to avoid needing the engine
    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": True, "summary": "stub"})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    result = bat.verify(doc_id="fake-doc", checks=["check_interference"])
    assert "scores" in result
    assert "findings" in result
    assert "elapsed_ms" in result
    assert "skipped" in result
    assert list(result["scores"].keys()) == sorted(result["scores"].keys())


def test_verify_deterministic(monkeypatch):
    """Two runs with same inputs return identical scores."""
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": True})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    r1 = bat.verify(doc_id="x", checks=["check_interference", "check_symmetry"])
    r2 = bat.verify(doc_id="x", checks=["check_interference", "check_symmetry"])
    assert r1["scores"] == r2["scores"]


def test_verify_skips_posix_on_windows(monkeypatch):
    """run_cfd_accurate is skipped on non-posix platforms."""
    import sys
    import services.verify.battery as bat
    monkeypatch.setattr(sys, "platform", "win32")

    def mock_get_fn(module_key, fn_name):
        return lambda **kw: json.dumps({"ok": True})
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    result = bat.verify(doc_id="x", checks=["run_cfd_accurate"])
    assert "run_cfd_accurate" in result["skipped"]
    assert "run_cfd_accurate" not in result["scores"]


def test_verify_rejects_mixed_valid_and_unknown_before_running(monkeypatch):
    """One bad name rejects the whole MCP call and teaches the canonical registry."""
    import services.verify.battery as bat
    from mcp.server.fastmcp import FastMCP
    from services.mcp.verify_tools import register_verify_tools

    calls = []
    monkeypatch.setitem(
        bat.GAP_FNS, "is_manifold",
        lambda **_: calls.append("is_manifold") or json.dumps({"ok": True}),
    )
    mcp = FastMCP("verify-battery-contract")
    register_verify_tools(mcp)
    tool = mcp._tool_manager._tools["verify_battery"]

    result = json.loads(tool.fn(
        checks=["is_manifold", "min_wall_thickness"], doc_id="x",
    ))

    assert result["ok"] is False
    assert "verify_battery() got unknown check(s) 'min_wall_thickness'" in result["error"]
    valid_names = [entry["name"] for entry in bat.check_metadata()]
    assert str(valid_names) in result["error"]
    assert calls == []


def test_verify_aliases_watertight_without_changing_success_shape(monkeypatch):
    """An accepted alias still runs the canonical check and returns the normal battery."""
    import services.verify.battery as bat

    monkeypatch.setitem(
        bat.GAP_FNS, "is_manifold",
        lambda **_: json.dumps({"ok": True, "summary": "manifold"}),
    )
    result = bat.verify(doc_id="x", checks=["watertight"])
    assert result["scores"] == {"is_manifold": 1.0}
    assert result["interpreted"] == [
        {"from": "watertight", "to": "is_manifold"},
    ]
    assert result["findings"] == []


def test_extract_score_dfm_uses_dfm_pass_and_failed_checks():
    from services.verify.battery import _extract_score

    score, findings = _extract_score(
        "dfm_check", "dfm",
        json.dumps({
            "ok": True,
            "dfm_pass": False,
            "checks": [
                {"check": "min_wall", "ok": False,
                 "note": "thinnest wall 0.50mm vs fdm minimum 0.8mm"},
            ],
        }),
    )
    assert score == 0.0
    assert findings == [{
        "check": "dfm_check",
        "ok": False,
        "detail": "thinnest wall 0.50mm vs fdm minimum 0.8mm",
    }]


def test_verify_skips_solid_checks_on_a_study_only_doc(monkeypatch):
    """2026-10-08 clamp-HPU run: a saved hydraulic circuit (no geometry) scored 0.0 on
    eval_check / is_manifold / measure_part and the agent reported "geometry failures"
    to the user. A study-only document has no solid to check: honest SKIP."""
    import services.verify.battery as bat
    from services.mcp import core_bridge

    monkeypatch.setattr(core_bridge, "call", lambda method, params=None: {
        "studies": [{"id": "s1", "type": "hydraulic_circuit"}]}
        if method == "sim_study_list" else {})
    ran = []
    monkeypatch.setitem(bat.GAP_FNS, "is_manifold",
                        lambda **_: ran.append(1) or json.dumps({"ok": False}))
    result = bat.verify(doc_id="d1", checks=["is_manifold"])
    assert ran == [] and result["scores"] == {}
    assert result["skipped"] == ["is_manifold"]
    assert "hydraulic_circuit" in result["findings"][0]["detail"]

    # A truly empty document (no studies either) still fails the solid checks.
    monkeypatch.setattr(core_bridge, "call", lambda method, params=None: {"studies": []})
    result = bat.verify(doc_id="d1", checks=["is_manifold"])
    assert ran == [1] and result["scores"] == {"is_manifold": 0.0}


@pytest.mark.parametrize("engine", [
    # 2026-10-10 six-axis arm: 36 placed components, Motion studies, no own features.
    {"doc_tree": {"tree": {"features": [], "components": [{"id": f"k{i}"} for i in range(36)]}},
     "asm_world_bodies": {"bodies": [{"name": f"b{i}"} for i in range(36)]}},
    # World solids with no feature or component entries still are solid geometry.
    {"doc_tree": {"tree": {"features": []}},
     "asm_world_bodies": {"bodies": [{"name": "Body1"}]}},
], ids=["assembly_components", "world_solids_only"])
def test_verify_runs_solid_checks_on_a_doc_with_solids_and_motion_studies(monkeypatch, engine):
    """A document with solid geometry is not study-only, even with no features and with
    Motion studies: eval_check, is_manifold and measure_part must run."""
    import services.verify.battery as bat
    from services.mcp import core_bridge

    replies = dict(engine, sim_study_list={"studies": [{"id": "s1", "type": "motion"}]})
    monkeypatch.setattr(core_bridge, "call",
                        lambda method, params=None: replies.get(method, {}))
    ran = []
    for gap in ("is_manifold", "eval_check_direct"):
        monkeypatch.setitem(bat.GAP_FNS, gap,
                            lambda _g=gap, **_: ran.append(_g) or json.dumps({"ok": True}))
    monkeypatch.setattr(bat, "_get_fn", lambda module_key, fn_name: (
        lambda doc_id=None: ran.append(fn_name) or json.dumps({"ok": True})))

    result = bat.verify(doc_id="asm", checks=["eval_check", "is_manifold", "measure_part"])
    assert sorted(ran) == ["eval_check_direct", "is_manifold", "measure_part"]
    assert result["scores"] == {"eval_check": 1.0, "is_manifold": 1.0, "measure_part": 1.0}
    assert result["skipped"] == []


def _ambiguous_measure_between(seen):
    def measure_between(a_query=None, b_query=None, doc_id=None):
        seen.append((a_query, b_query))
        return json.dumps({
            "ok": False, "code": "invalid_op",
            "error": "measure ref matched multiple elements; narrow it with body_id, "
                     "pick, near or index",
            "detail": {"reason": "ambiguous"},
        })
    return measure_between


def test_ambiguous_battery_chosen_measure_probe_is_not_evaluated(monkeypatch):
    """2026-10-10: the battery chose "top"/"bottom" on a 36-solid assembly, the engine
    refused the ref as ambiguous, and the battery scored a geometry failure (0.0)."""
    import services.verify.battery as bat

    seen = []
    monkeypatch.setattr(bat, "_get_fn", lambda module_key, fn_name:
                        _ambiguous_measure_between(seen))
    monkeypatch.setattr(bat, "_derive_measure_face_pair", lambda doc_id: ("top", "bottom"))

    result = bat.verify(doc_id="asm", checks=["measure_between"])
    assert seen == [("top", "bottom")]
    assert result["scores"] == {}
    assert result["skipped"] == ["measure_between"]
    [finding] = result["findings"]
    assert finding["ok"] is True and finding["not_evaluated"] is True
    assert finding["reason"] == "probe_ref_ambiguous"
    assert "not a geometry failure" in finding["detail"]


def test_ambiguous_caller_measure_ref_still_fails(monkeypatch):
    """Only the battery's own probe choice is exempt. A caller ref that matches more than
    one element stays a scored failure with the engine's reason."""
    import services.verify.battery as bat

    seen = []
    monkeypatch.setattr(bat, "_get_fn", lambda module_key, fn_name:
                        _ambiguous_measure_between(seen))

    result = bat.verify(doc_id="asm", checks=["measure_between"],
                        a_query={"of": "face", "side": "top"}, b_query="bottom face")
    assert seen == [({"of": "face", "side": "top"}, "bottom")]
    assert result["scores"] == {"measure_between": 0.0}
    assert result["skipped"] == []
    assert "matched multiple elements" in result["findings"][0]["detail"]


def _probe_face(sid, z, normal, body=None, **extra):
    face = {"sid": sid, "kind": "face", "surf": "plane",
            "centroid": [0, 0, z], "normal": [0, 0, normal], **extra}
    if body is not None:
        face["body"] = body
    return face


def _measure_battery_engine(monkeypatch, faces, measurement=None):
    """Run battery -> real measure_between adapter -> fake native boundary."""
    from mcp.server.fastmcp import FastMCP
    from services.mcp import cad_eval_tools, core_bridge
    from services.verify import battery as bat

    calls = []

    def engine(method, args):
        if method == "doc_tree":
            return {"tree": {"features": [{"id": "f1", "type": "box"}]}}
        if method == "doc_list":
            return {"docs": [{"doc_id": "part", "name": "part"}]}
        if method == "doc_query":
            assert args["query"] == {"of": "face"}  # uncapped, all owners
            return {"matches": faces}
        if method == "eval_measure":
            calls.append(args["refs"])
            return measurement if measurement is not None else {"distance": 10.0}
        raise AssertionError(f"unexpected engine call: {method}")

    monkeypatch.setattr(core_bridge, "call", engine)
    monkeypatch.setattr(core_bridge, "consume_foreign_edits", lambda: [])
    mcp = FastMCP("battery-measure-consumer")
    cad_eval_tools.register_cad_eval_tools(mcp)
    monkeypatch.setattr(bat, "_get_fn", lambda module, name:
                        mcp._tool_manager._tools[name].fn)
    return calls


def test_automatic_probe_keeps_body_provenance_for_repeated_sids(monkeypatch):
    from services.verify import battery as bat

    faces = [_probe_face("Fabc123", 10, 1, body=1),
             _probe_face("Fdef456", 0, -1, body=1),
             _probe_face("Fabc123", 10, 1, body=2),
             _probe_face("Fdef456", 0, -1, body=2)]
    calls = _measure_battery_engine(monkeypatch, faces)
    result = bat.verify("part", ["measure_between"])

    assert result["scores"] == {"measure_between": 1.0}
    [refs] = calls
    assert {ref["sid"] for ref in refs} == {"Fabc123", "Fdef456"}
    assert len({ref["body_id"] for ref in refs}) == 1
    assert all("selector" not in ref for ref in refs)  # world-space readbacks


def test_automatic_probe_accepts_globally_unique_world_readbacks(monkeypatch):
    from services.verify import battery as bat

    calls = _measure_battery_engine(monkeypatch, [
        _probe_face("Fabc123", 10, 1, component="instance-a"),
        _probe_face("Fdef456", 0, -1, component="instance-a"),
    ])
    result = bat.verify("part", ["measure_between"])
    assert result["scores"] == {"measure_between": 1.0}
    assert calls == [[{"sid": "Fabc123"}, {"sid": "Fdef456"}]]


@pytest.mark.parametrize("faces", [
    [],
    [{"side": "top"}, {"side": "bottom"}],
    [_probe_face("Fabc123", 10, 1), _probe_face("Fabc123", 10, 1),
     _probe_face("Fdef456", 0, -1), _probe_face("Fdef456", 0, -1)],
    [_probe_face("Fabc123", 10, 1, body=1),
     _probe_face("Fabc123", 10, 1, body=1),
     _probe_face("Fdef456", 0, -1, body=1)],
    [_probe_face("Fabc123", 10, 1, component="instance-a"),
     _probe_face("Fabc123", 10, 1, component="instance-b"),
     _probe_face("Fdef456", 0, -1, component="instance-a"),
     _probe_face("Fdef456", 0, -1, component="instance-b")],
    [_probe_face("Fabc123", 0, 1), _probe_face("Fdef456", 0, -1)],
])
def test_automatic_probe_skips_unsupported_topology_without_measuring(monkeypatch, faces):
    from services.verify import battery as bat

    calls = _measure_battery_engine(monkeypatch, faces)
    result = bat.verify("part", ["measure_between"])

    assert calls == []
    assert result["scores"] == {}
    assert result["skipped"] == ["measure_between"]
    [finding] = result["findings"]
    assert finding["not_evaluated"] is True
    assert finding["reason"] == "probe_ref_unsupported"


@pytest.mark.parametrize("a_ref,b_ref", [
    ({"sid": "Fabc123", "body_id": 2}, {"sid": "Fdef456", "body_id": 2}),
    ("Fabc123", "Fdef456"),
    ("f1", "outer cylindrical face"),
    ("top of component-a", "bottom of component-b"),
    ({"of": "face", "side": "top"}, "bottom"),
    ("", {}),
])
def test_explicit_measure_endpoints_are_never_replaced(monkeypatch, a_ref, b_ref):
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_study_only_kinds", lambda doc_id: [])
    # Inspect what the battery actually dispatches, including invalid caller refs.
    def measure(a_query=None, b_query=None, doc_id=None):
        assert a_query == a_ref
        assert b_query == b_ref
        return json.dumps({"ok": False, "error": "explicit selector refused"})
    monkeypatch.setattr(bat, "_get_fn", lambda module, name: measure)
    result = bat.verify("part", ["measure_between"], a_query=a_ref, b_query=b_ref)
    assert result["scores"] == {"measure_between": 0.0}
    assert result["skipped"] == []


def test_partial_explicit_ambiguous_probe_is_still_a_failure(monkeypatch):
    from services.verify import battery as bat

    seen = []
    monkeypatch.setattr(bat, "_study_only_kinds", lambda doc_id: [])
    monkeypatch.setattr(bat, "_derive_measure_face_pair", lambda doc_id:
                        ({"sid": "Fabc123"}, {"sid": "Fdef456"}))
    monkeypatch.setattr(bat, "_get_fn", lambda module, name:
                        _ambiguous_measure_between(seen))
    result = bat.verify("part", ["measure_between"], a_query={"of": "face"})
    assert seen == [({"of": "face"}, {"sid": "Fdef456"})]
    assert result["scores"] == {"measure_between": 0.0}
    assert result["skipped"] == []


@pytest.mark.parametrize("measurement", [
    {}, {"distance": None}, {"distance": float("nan")},
    {"distance": float("inf")}, {"distance": True},
])
def test_measurement_without_finite_evidence_cannot_pass(monkeypatch, measurement):
    from services.verify import battery as bat

    _measure_battery_engine(monkeypatch, [], measurement=measurement)
    result = bat.verify("part", ["measure_between"],
                        a_query={"sid": "Fabc123"}, b_query={"sid": "Fdef456"})
    assert result["scores"] == {"measure_between": 0.0}
    assert result["findings"][0]["ok"] is False




def test_verify_findings_deterministic(monkeypatch):
    """Findings list (not just scores) is identical across repeated runs."""
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": False, "summary": f"fail-{fn_name}"})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    checks = ["check_interference", "check_symmetry", "check_sketch"]
    r1 = bat.verify(doc_id="x", checks=checks)
    r2 = bat.verify(doc_id="x", checks=list(reversed(checks)))  # order-independent
    assert r1["scores"] == r2["scores"]
    assert r1["findings"] == r2["findings"]


def test_verify_overhead_under_50ms(monkeypatch):
    """DoD: battery overhead over the underlying checks is <50 ms (reward-loop viable).

    With every check stubbed to a no-op, elapsed_ms IS the dispatcher overhead.
    """
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        return lambda **kw: json.dumps({"ok": True})
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    checks = ["check_interference", "check_symmetry", "check_sketch",
              "measure_part", "ecad_run_erc", "cam_simulate"]
    bat.verify(doc_id="x", checks=checks)          # warm the signature cache path
    result = bat.verify(doc_id="x", checks=checks)
    assert result["elapsed_ms"] < 50.0


def test_extract_score_interference_reads_clear_not_ok():
    """check_interference ok=true means the check completed; clear is the pass verdict."""
    from services.verify.battery import _extract_score
    clash = {"ok": True, "complete": True, "clear": False,
             "interferences": [{"a": "b1", "b": "b2", "volume": 12.0}],
             "summary": "Interference found: 1 interfering pair(s)"}
    score, findings = _extract_score("check_interference", "binary", json.dumps(clash))
    assert score == 0.0
    assert findings[0]["detail"].startswith("Interference found")
    clean = {"ok": True, "complete": True, "clear": True, "interferences": []}
    assert _extract_score("check_interference", "binary", json.dumps(clean)) == (1.0, [])
    incomplete = {"ok": False, "complete": False, "clear": False, "interferences": []}
    assert _extract_score("check_interference", "binary", json.dumps(incomplete))[0] == 0.0


def test_extract_score_interface_check_refusal_is_not_a_pass():
    """An interface_check refusal (ok=false, no findings list) must not score 1.0."""
    from services.verify.battery import _extract_score
    refusal = {"ok": False, "code": "unknown_component", "error": "unknown component id(s)"}
    score, findings = _extract_score("interface_check", "findings", json.dumps(refusal))
    assert score == 0.0
    assert "unknown component" in findings[0]["detail"]



# ── CAM scoring keys (machined_fraction + gouges) ───────────────────────────

def test_extract_score_machined_fraction_clean():
    """machined_fraction 0..1 maps 1:1 when gouges==0."""
    from services.verify.battery import _extract_score
    score, findings = _extract_score(
        "cam_simulate", "scalar",
        json.dumps({"ok": True, "machined_fraction": 0.92, "gouges": 0}))
    assert score == pytest.approx(0.92)
    assert findings == []


def test_extract_score_machined_fraction_gouge_gates_to_zero():
    """Any gouge forces score=0.0 even when machined_fraction is high."""
    from services.verify.battery import _extract_score
    score, findings = _extract_score(
        "cam_simulate", "scalar",
        json.dumps({"ok": True, "machined_fraction": 0.96, "gouges": 1,
                    "max_gouge_mm": 0.35}))
    assert score == 0.0
    assert len(findings) == 1
    assert findings[0]["check"] == "cam_simulate"
    assert "gouge" in findings[0]["detail"].lower()
    assert "1" in findings[0]["detail"]  # gouge count surfaced


def test_extract_score_multiple_gouges_surfaced():
    """Gouge count is surfaced in the finding detail string."""
    from services.verify.battery import _extract_score
    score, findings = _extract_score(
        "cam_simulate", "scalar",
        json.dumps({"ok": False, "machined_fraction": 0.75, "gouges": 3,
                    "max_gouge_mm": 1.2}))
    assert score == 0.0
    assert "3" in findings[0]["detail"]


def test_extract_score_zero_gouges_uses_machined_fraction():
    """Explicit gouges=0 still falls through to machined_fraction (not bare ok fallback)."""
    from services.verify.battery import _extract_score
    score, findings = _extract_score(
        "cam_simulate", "scalar",
        json.dumps({"ok": True, "machined_fraction": 0.55, "gouges": 0}))
    assert score == pytest.approx(0.55)
    assert findings == []


def test_extract_score_no_gouges_key_falls_through():
    """When gouges key is absent, machined_fraction is still recognized."""
    from services.verify.battery import _extract_score
    score, findings = _extract_score(
        "cam_simulate", "scalar",
        json.dumps({"ok": True, "machined_fraction": 0.88}))
    assert score == pytest.approx(0.88)


# ── Scalar metrics nested under `summary` (2026-08-04) ───────────────────────
#
# run_fea_static / _modal / the CFD tools return
# {ok, doc_id, material, summary:{min_fos, max_von_mises_mpa, …}}. _extract_score
# scanned ONLY the top level, matched nothing, and fell through to
# `1.0 if ok else 0.0` — grading "the solver ran" instead of "the part passed".
# Measured live: an 80x50x4 aluminium plate at 50 kN solves ok=True with
# min_fos 0.9944 (a FAILING margin) and scored a perfect 1.0.


def test_extract_score_reads_min_fos_nested_under_summary():
    """A failing FoS must score on the FoS, not on "the solver returned ok"."""
    from services.verify.battery import _extract_score
    score, _ = _extract_score(
        "run_fea_static", "scalar",
        json.dumps({"ok": True, "doc_id": "doc-1",
                    "summary": {"min_fos": 0.9944,
                                "max_von_mises_mpa": 277.5624}}))
    assert score == pytest.approx(0.9944 / 2.0)
    assert score < 1.0, "a part below FoS 2.0 must not score a clean 1.0"


def test_extract_score_top_level_metric_still_wins():
    """Top level keeps precedence — tools that already flatten are untouched."""
    from services.verify.battery import _extract_score
    score, _ = _extract_score(
        "run_fea_static", "scalar",
        json.dumps({"ok": True, "min_fos": 4.0, "summary": {"min_fos": 0.5}}))
    assert score == pytest.approx(1.0)


def test_extract_score_string_summary_is_not_treated_as_metrics():
    """`summary` is a human string on several checks — it must not be indexed."""
    from services.verify.battery import _extract_score
    score, _ = _extract_score(
        "run_fea_static", "scalar",
        json.dumps({"ok": True, "summary": "solved in 3 s"}))
    assert score == pytest.approx(1.0)   # bare-ok fallback, no crash


# ── _gap_is_manifold issues-list fix ────────────────────────────────────────

def test_gap_is_manifold_issues_free_edges_fails(monkeypatch):
    """A free_edges issue in the issues list → non-manifold (not watertight)."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "free_edges", "severity": "error", "count": 4}],
                "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_is_manifold(doc_id="fake"))
    assert r["ok"] is False
    assert r["free_edges"] == 1  # one free_edges issue entry
    assert "not watertight" in r["summary"]


def test_gap_is_manifold_tiny_edges_only_is_manifold(monkeypatch):
    """tiny_edges warnings alone (curved seam/pole) do NOT fail manifold."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "tiny_edges", "severity": "warning", "count": 2}],
                "tiny_edges": 2}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_is_manifold(doc_id="fake"))
    assert r["ok"] is True
    assert r["free_edges"] == 0
    assert r["summary"] == "manifold"


def test_gap_is_manifold_no_issues_scalar_fallback_manifold(monkeypatch):
    """Empty issues list + free_edges==0 scalar → manifold (scalar fallback path)."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [], "free_edges": 0, "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_is_manifold(doc_id="fake"))
    assert r["ok"] is True


def test_gap_is_manifold_no_issues_scalar_fallback_non_manifold(monkeypatch):
    """Empty issues list + free_edges>0 scalar → non-manifold (scalar fallback path)."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [], "free_edges": 4, "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_is_manifold(doc_id="fake"))
    assert r["ok"] is False
    assert r["free_edges"] == 4


def test_gap_is_manifold_error_severity_fails(monkeypatch):
    """Any issue with severity==error → non-manifold, even if kind is not free_edges."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "invalid_face", "severity": "error"}],
                "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_is_manifold(doc_id="fake"))
    assert r["ok"] is False




# ── _gap_eval_check_direct issues-list fix (same latent bug fixed in
#    _gap_is_manifold: eval_check emits an `issues` list, not bare scalars) ──

def test_gap_eval_check_direct_issues_free_edges_fails(monkeypatch):
    """A free_edges issue in the issues list -> not ok."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "free_edges", "severity": "error", "count": 4}],
                "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_eval_check_direct(doc_id="fake"))
    assert r["ok"] is False


def test_gap_eval_check_direct_tiny_edges_only_is_ok(monkeypatch):
    """tiny_edges warnings alone do NOT fail the check."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "tiny_edges", "severity": "warning", "count": 2}],
                "tiny_edges": 2}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_eval_check_direct(doc_id="fake"))
    assert r["ok"] is True


def test_gap_eval_check_direct_error_severity_fails(monkeypatch):
    """Any issue with severity==error fails, even if kind is not free_edges."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [{"kind": "invalid_face", "severity": "error"}],
                "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_eval_check_direct(doc_id="fake"))
    assert r["ok"] is False


def test_gap_eval_check_direct_no_issues_scalar_fallback_ok(monkeypatch):
    """Empty issues list + free_edges==0 + invalid_faces==0 scalars -> ok (fallback path)."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [], "free_edges": 0, "invalid_faces": 0, "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_eval_check_direct(doc_id="fake"))
    assert r["ok"] is True


def test_gap_eval_check_direct_no_issues_scalar_fallback_not_ok(monkeypatch):
    """Empty issues list + invalid_faces>0 scalar -> not ok (fallback path)."""
    import services.verify.battery as bat

    def fake_cb_call(op, args):
        return {"issues": [], "free_edges": 0, "invalid_faces": 2, "tiny_edges": 0}

    import services.mcp.core_bridge as cb
    monkeypatch.setattr(cb, "call", fake_cb_call)

    r = json.loads(bat._gap_eval_check_direct(doc_id="fake"))
    assert r["ok"] is False


# ── mega-15: battery argument adapters + honest SKIP ────────────────────────

def test_adapt_check_sketch_skip_when_no_sketches(monkeypatch):
    """check_sketch with no sketches → honest SKIP, not signature zero."""
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_sketch_ids", lambda doc_id: [])
    monkeypatch.setattr(bat, "_get_fn", lambda mk, fn: (lambda sketch, doc_id=None: __import__("json").dumps({"ok": True, "dof": 0})))

    r = bat.verify("doc-mech", ["check_sketch"])
    assert "check_sketch" in r["skipped"]
    assert "check_sketch" not in r["scores"]
    assert any(f.get("skipped") and f.get("check") == "check_sketch" for f in r["findings"])
    assert any("no sketches" in (f.get("detail") or "") for f in r["findings"])


def test_adapt_check_sketch_runs_with_derived_id(monkeypatch):
    """check_sketch derives every sketch id and scores the complete document."""
    from services.verify import battery as bat
    import json

    seen = []

    def _fn(sketch, doc_id=None):
        seen.append((sketch, doc_id))
        return json.dumps({"ok": True, "dof": 0, "status": "full",
                           "fully_defined": True, "pinned_only": False})

    monkeypatch.setattr(bat, "_sketch_ids", lambda doc_id: ["s1", "s2"])
    monkeypatch.setattr(bat, "_get_fn", lambda mk, fn: _fn)

    r = bat.verify("doc-mech", ["check_sketch"])
    assert "check_sketch" not in r["skipped"]
    assert r["scores"]["check_sketch"] == 1.0
    assert seen == [("s1", "doc-mech"), ("s2", "doc-mech")]


def test_adapt_measure_between_skip_no_features(monkeypatch):
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_doc_features", lambda doc_id: [])
    monkeypatch.setattr(bat, "_query_geometry_faces", lambda doc_id: [])
    monkeypatch.setattr(
        bat, "_get_fn",
        lambda mk, fn: (lambda a_query, b_query=None, doc_id=None: __import__("json").dumps({"ok": True})),
    )
    r = bat.verify("doc-mech", ["measure_between"])
    assert "measure_between" in r["skipped"]
    assert "measure_between" not in r["scores"]
    assert any("no features" in (f.get("detail") or "") for f in r["findings"])


def test_adapt_measure_between_runs_with_derived_queries(monkeypatch):
    from services.verify import battery as bat
    import json

    seen = {}

    def _fn(a_query, b_query=None, doc_id=None):
        seen["a"] = a_query
        seen["b"] = b_query
        return json.dumps({"ok": True, "distance": 10.0})

    monkeypatch.setattr(bat, "_query_geometry_faces", lambda doc_id: [
        _probe_face("Fabc123", 10, 1), _probe_face("Fdef456", 0, -1),
    ])
    monkeypatch.setattr(bat, "_get_fn", lambda mk, fn: _fn)
    r = bat.verify("doc-mech", ["measure_between"])
    assert r["scores"]["measure_between"] == 1.0
    assert {seen["a"]["sid"], seen["b"]["sid"]} == {"Fabc123", "Fdef456"}
    assert seen["a"] != seen["b"]


def test_adapt_dim_coverage_skip_no_drawing(monkeypatch):
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_is_drawing_doc", lambda doc_id: False)
    r = bat.verify("doc-mech", ["dim_coverage"])
    assert "dim_coverage" in r["skipped"]
    assert "dim_coverage" not in r["scores"]
    assert any(
        "no drawing" in (f.get("detail") or "").lower()
        for f in r["findings"]
    )


def test_adapt_dim_coverage_runs_on_drawing(monkeypatch):
    from services.verify import battery as bat
    import json

    monkeypatch.setattr(bat, "_is_drawing_doc", lambda doc_id: True)

    class _CB:
        @staticmethod
        def call(method, params):
            if method == "drawing_get":
                return {
                    "sheets": [{
                        "views": [{"annotations": [{"t": "d1"}, {"t": "d2"}]}],
                    }],
                    "source_doc_id": "part1",
                }
            if method == "doc_tree":
                return {"tree": {"features": [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}, {"id": "f4"}]}}
            raise AssertionError(method)

    import sys
    monkeypatch.setitem(sys.modules, "services.mcp.core_bridge", _CB)
    # also patch import path used inside gap fn
    monkeypatch.setattr(
        "services.mcp.core_bridge.call", _CB.call, raising=False
    )

    # Re-bind via battery's import site: inject into the module used by gap.
    import services.mcp.core_bridge as cb_mod
    monkeypatch.setattr(cb_mod, "call", _CB.call)

    r = bat.verify("drw-1", ["dim_coverage"])
    assert "dim_coverage" not in r["skipped"]
    assert r["scores"]["dim_coverage"] == pytest.approx(0.5)


def test_adapt_impedance_skip_no_board(monkeypatch):
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_find_ecad_board_doc", lambda preferred=None: None)
    monkeypatch.setattr(
        bat, "_get_fn",
        lambda mk, fn: (lambda net_or_ref="", target=50.0, doc_id="": __import__("json").dumps({"ok": True})),
    )
    r = bat.verify("doc-mech", ["ecad_check_impedance"])
    assert "ecad_check_impedance" in r["skipped"]
    assert any("no open board" in (f.get("detail") or "") for f in r["findings"])


def test_adapt_impedance_runs_with_board_net(monkeypatch):
    from services.verify import battery as bat
    import json

    seen = {}

    def _fn(net_or_ref="", target=50.0, doc_id=""):
        seen["net"] = net_or_ref
        seen["doc_id"] = doc_id
        return json.dumps({"ok": True, "z0": 50.0, "score": 1.0})

    monkeypatch.setattr(bat, "_find_ecad_board_doc", lambda preferred=None: "pcb-1")
    monkeypatch.setattr(bat, "_board_routed_nets", lambda bid: ["GND", "CLK"])
    monkeypatch.setattr(bat, "_get_fn", lambda mk, fn: _fn)
    r = bat.verify("doc-mech", ["ecad_check_impedance"])
    assert r["scores"]["ecad_check_impedance"] == 1.0
    assert seen["doc_id"] == "pcb-1"
    assert seen["net"] == "CLK"  # prefers non-power


def test_adapt_diff_impedance_skip_no_pair(monkeypatch):
    from services.verify import battery as bat

    monkeypatch.setattr(bat, "_find_ecad_board_doc", lambda preferred=None: "pcb-1")
    monkeypatch.setattr(bat, "_board_routed_nets", lambda bid: ["GND", "CLK"])
    monkeypatch.setattr(bat, "_diff_pair_nets", lambda nets: None)
    monkeypatch.setattr(
        bat, "_get_fn",
        lambda mk, fn: (lambda net_p, net_n, target=100.0, doc_id="": __import__("json").dumps({"ok": True})),
    )
    r = bat.verify("doc-mech", ["ecad_check_diff_impedance"])
    assert "ecad_check_diff_impedance" in r["skipped"]
    assert any("differential pair" in (f.get("detail") or "") for f in r["findings"])


def test_adapt_diff_impedance_runs_with_pair(monkeypatch):
    from services.verify import battery as bat
    import json

    seen = {}

    def _fn(net_p, net_n, target=100.0, doc_id=""):
        seen["net_p"] = net_p
        seen["net_n"] = net_n
        seen["doc_id"] = doc_id
        return json.dumps({"ok": True, "z_diff": 100.0, "score": 1.0})

    monkeypatch.setattr(bat, "_find_ecad_board_doc", lambda preferred=None: "pcb-1")
    monkeypatch.setattr(bat, "_board_routed_nets", lambda bid: ["USB_DP", "USB_DN"])
    monkeypatch.setattr(bat, "_diff_pair_nets", lambda nets: ("USB_DP", "USB_DN"))
    monkeypatch.setattr(bat, "_get_fn", lambda mk, fn: _fn)
    r = bat.verify("doc-mech", ["ecad_check_diff_impedance"])
    assert r["scores"]["ecad_check_diff_impedance"] == 1.0
    assert seen == {"net_p": "USB_DP", "net_n": "USB_DN", "doc_id": "pcb-1"}


def test_forced_power_args_use_pin_xy():
    """mega-15: force-inject power bonds to pin world xy, not orphan (0,0)."""
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "name": "1", "type": "passive", "xy": [12.5, -3.0]},
                    {"number": "2", "name": "2", "type": "passive", "xy": [12.5, 5.0]},
                ],
            },
        },
    ]
    args = _ecad_forced_tool_args("ecad_add_power", calls)
    assert args["net"] == "GND"
    assert args["x"] == 12.5
    assert args["y"] in (-3.0, 5.0)


def test_forced_wire_args_use_place_refs_and_pins():
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = [
        {
            "tool": "ecad_place_symbol", "ok": True,
            "result": {
                "ref": "U1",
                "pins": [{"number": "3", "name": "VCC", "type": "power_in", "xy": [0, 0]}],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "name": "1", "type": "passive", "xy": [1, 1]},
                    {"number": "2", "name": "2", "type": "passive", "xy": [1, 2]},
                ],
            },
        },
    ]
    args = _ecad_forced_tool_args("ecad_wire", calls)
    # ecad_wire emits a symmetric ``sch_connect`` op (services/mcp/
    # ecad_sch_tools.py:261) — from/to only order the human-readable hint, so
    # the contract is WHICH pins get joined, not their orientation. Assert the
    # endpoint pair: the resistor's pin 1 must bond to U1's VCC pin 3, both
    # derived from the placement refs/pins in the ledger.
    ends = {
        (args["from_ref"], str(args["from_pin"])),
        (args["to_ref"], str(args["to_pin"])),
    }
    assert ends == {("R1", "1"), ("U1", "3")}, args


def test_chain_need_forces_erc_after_drc():
    from services.agent.agent_turn import _ecad_chain_need

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc", "ecad_run_erc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True},
        {"tool": "ecad_place_symbol", "ok": True, "args": {"libid": "Device:R", "value": "1k"},
         "result": {"ref": "R1"}},
        {"tool": "ecad_wire", "ok": True, "result": {"net": "N1", "net_size": 2}},
        {"tool": "board_from_schematic", "ok": True, "result": {"doc_id": "b"}},
        {"tool": "ecad_run_drc", "ok": True, "result": {"errors": 0}},
    ]
    assert _ecad_chain_need(calls, allow) == "ecad_run_erc"
