"""Sketch advisor regressions using actual native RPC envelopes, without an engine."""
from __future__ import annotations

import pytest

from services.agent.advisor import adapters, engine, stages


def _sketch(fid, *, pinned=False, external=False):
    return {
        "id": fid, "name": f"Profile {fid}", "type": "sketch",
        "params": {
            "entities": [{"id": "e1", "type": "circle", "center": [0, 0], "radius": 5}],
            "constraints": ([{"id": "c1", "type": "fix", "refs": ["e1"]}]
                            if pinned or external else [
                                {"id": "c1", "type": "coincident", "refs": ["e1.center", "origin"]},
                                {"id": "c2", "type": "radius", "refs": ["e1"], "value": {"value": 5}},
                            ]),
        },
    }


def _health(**changes):
    return {"ok": True, "status": "full", "dof": 0, "converged": True,
            "closure": {"closed_chains_ok": True, "open_endpoints": []},
            "free_variables": [], "conflicting": [], **changes}


def test_default_adapter_checks_real_feature_ids_and_later_open_profile(monkeypatch):
    from services.mcp import core_bridge

    calls = []
    features = [_sketch("f3"), {"id": "f4", "type": "box"}, _sketch("f19")]

    def rpc(method, params):
        calls.append((method, params))
        if method == "doc_tree":
            return {"doc_id": "d1", "tree": {"features": features}}
        assert method == "sketch_health"
        assert params["doc_id"] == "d1"
        if params["sketch"] == "f19":
            return _health(closure={"closed_chains_ok": False,
                                    "open_endpoints": ["e1.start", "e2.end"]})
        assert params["sketch"] == "f3"
        return _health()

    monkeypatch.setattr(core_bridge, "call", rpc)
    findings = adapters._check_sketch("d1", adapters._default_rpc(), stages.CHECK_OPEN_PROFILE)
    assert calls == [
        ("doc_tree", {"doc_id": "d1"}),
        ("sketch_health", {"doc_id": "d1", "sketch": "f3"}),
        ("sketch_health", {"doc_id": "d1", "sketch": "f19"}),
    ]
    assert len(findings) == 1
    assert findings[0].kind == "open_profile"
    assert findings[0].refs["sketch"] == "f19"
    assert findings[0].refs["handles"] == ["e1.start", "e2.end"]
    assert findings[0].numbers["open_endpoints"] == 2
    assert findings[0].severity == "blocker"


def test_adapter_keeps_design_intent_and_real_free_variables():
    features = [_sketch("f1"), _sketch("f7", pinned=True), _sketch("f9")]
    features[2]["params"]["constraints"] = []
    features.append({"id": "f8", "type": "extrude", "refs": {"profile": {"sketch": "f7"}}})
    free = [{"entity": "e1", "param": "radius", "var": "r"}]

    def rpc(method, **params):
        if method == "doc_tree":
            return {"tree": {"features": features}}
        assert method == "sketch_health"
        return (_health(status="under", dof=3, free_variables=free,
                        dof_narration="circle e1 radius and center")
                if params["sketch"] == "f9" else _health())

    findings = adapters._check_sketch("d1", rpc, stages.CHECK_SKETCH_HEALTH)
    kinds = {(f.refs["sketch"], f.kind) for f in findings}
    assert ("f7", "pinned_instead_of_constrained") in kinds
    assert ("f7", "no_driving_dimensions") in kinds
    assert ("f7", "unreferenced_profile") in kinds
    under = next(f for f in findings if f.kind == "under_dimensioned")
    assert under.refs["sketch"] == "f9"
    assert under.extra["free_variables"] == free
    assert under.numbers["dof"] == 3
    assert not any(f.refs["sketch"] == "f1" for f in findings)


def test_fixed_external_reference_is_not_a_pinned_profile():
    feature = _sketch("f7", external=True)

    def rpc(method, **params):
        if method == "doc_tree":
            return {"tree": {"features": [feature]}}
        return _health(external_refs=[{"id": "x1", "entity_ids": ["e1"]}])

    assert adapters._check_sketch("d1", rpc, stages.CHECK_SKETCH_HEALTH) == []


def test_failed_sketch_health_does_not_hide_later_sketch():
    visited = []

    def rpc(method, **params):
        if method == "doc_tree":
            return {"tree": {"features": [_sketch("f1"), _sketch("f2")]}}
        visited.append(params["sketch"])
        if params["sketch"] == "f1":
            raise RuntimeError("solver unavailable")
        return _health(closure={"closed_chains_ok": False, "open_endpoints": ["e1.end"]})

    findings = adapters._check_sketch("d1", rpc, stages.CHECK_OPEN_PROFILE)
    assert visited == ["f1", "f2"]
    assert [(f.refs["sketch"], f.kind) for f in findings] == [
        ("f1", "check_error"), ("f2", "open_profile"),
    ]


def test_missing_health_schema_is_not_a_pass():
    def rpc(method, **params):
        return {"tree": {"features": [_sketch("f1")]}} if method == "doc_tree" else {"ok": True}

    findings = adapters._check_sketch("d1", rpc, stages.CHECK_OPEN_PROFILE)
    assert len(findings) == 1 and findings[0].kind == "check_error"
    assert "closure verdict" in findings[0].finding


@pytest.mark.parametrize("kind,severity", [
    ("pinned_instead_of_constrained", "action"),
    ("no_driving_dimensions", "action"),
    ("unreferenced_profile", "action"),
    ("under_dimensioned", "action"),
    ("underconstrained", "hint"),
    ("sketch_conflict", "blocker"),
])
def test_known_sketch_findings_keep_severity_and_actionable_fixes(kind, severity):
    finding = engine.Finding(kind=kind, check=stages.CHECK_SKETCH_HEALTH,
                             severity=severity, finding="repair the sketch",
                             refs={"doc_id": "d1", "sketch": "f7", "entities": ["e1"]})
    item = engine.build_advisory(engine.Observation(stage="sketch", findings=[finding]))["items"][0]
    assert item["severity"] == severity
    assert item["candidate_fixes"]
    assert item["candidate_fixes"][0]["params"]["doc_id"] == "d1"
    assert "check_sketch" in item["helper_tools"]
