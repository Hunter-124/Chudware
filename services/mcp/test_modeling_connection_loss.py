"""Fail-closed registered modeling calls; no native engine or geometry required."""
from __future__ import annotations

import json
import time

import pytest

from services.agent.advisor import preflight
from services.mcp import cad_common, cad_doc_tools, core_bridge


class _Tools:
    def __init__(self):
        self.functions = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.functions[fn.__name__] = fn
            return fn
        return register


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("phase", [
    "doc_list", "doc_checkpoint", "doc_query", "clone_replay", "clone_proposed",
    "doc_close", "live_apply",
])
def test_registered_mutation_stops_at_connection_loss(monkeypatch, phase, background):
    tools = _Tools()
    cad_doc_tools.register_cad_doc_tools(tools)
    monkeypatch.setenv("CHUDWARE_PREFLIGHT", "always")
    monkeypatch.setattr(preflight, "_ledger_append", lambda event: None)
    monkeypatch.setattr(cad_common, "_JOBS", {})
    monkeypatch.setattr(cad_doc_tools, "_JOBS", cad_common._JOBS)
    monkeypatch.setattr(cad_common, "_PROCESS_DOCS", set())
    monkeypatch.setattr(cad_common, "_DOC_ALIASES", {})
    calls = []
    live_mutations = []
    clone_applies = 0
    failed = False
    detail = {"method": "doc_apply_ops", "outcome": "unknown", "retryable": False}

    def engine(method, params=None):
        nonlocal clone_applies, failed
        params = params or {}
        stage = method
        if method == "doc_apply_ops":
            if params.get("doc_id") == "clone":
                clone_applies += 1
                stage = "clone_replay" if clone_applies == 1 else "clone_proposed"
            else:
                stage = "live_apply"
                live_mutations.append(params)
        calls.append(stage)
        if failed:
            pytest.fail(f"engine called after ambiguous loss: {stage}")
        if stage == phase:
            failed = True
            raise core_bridge.CoreError(
                "connection lost; operation outcome unknown",
                code="engine_connection_lost", detail=detail,
            )
        if method == "doc_list":
            return {"docs": [{"doc_id": "live", "name": "Part"}]}
        if method == "doc_new":
            return {"doc_id": "clone"}
        if method == "doc_checkpoint":
            return {"checkpoint": "before"}
        if method == "doc_tree":
            return {"revision": 1, "tree": {"name": "Part", "features": []}}
        if method == "doc_apply_ops":
            return {"revision": 1, "rebuild": {"feature_status": []}}
        return {}

    monkeypatch.setattr(core_bridge, "call", engine)
    result = json.loads(tools.functions["add_features"](
        doc_id="live", ops=[{"op": "rename", "id": "f1", "name": "renamed"}],
        background=background,
    ))
    if result.get("status") == "running":
        deadline = time.monotonic() + 3
        while result.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            result = json.loads(tools.functions["get_job"](result["job_id"]))
    assert result["ok"] is False, result
    assert result["code"] == "engine_connection_lost"
    assert result["detail"] == detail
    assert failed
    assert calls[-1] == phase
    assert len(live_mutations) == (1 if phase == "live_apply" else 0)


def test_project_save_does_not_write_after_name_lookup_connection_loss(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def engine(method, params):
        calls.append(method)
        raise core_bridge.CoreError(
            "connection lost", code="engine_connection_lost",
            detail={"outcome": "unknown"},
        )

    monkeypatch.setattr(core_bridge, "call", engine)
    with pytest.raises(core_bridge.CoreError) as lost:
        cad_common._project_save("live")
    assert lost.value.code == "engine_connection_lost"
    assert calls == ["doc_list"]
    assert list(tmp_path.iterdir()) == []
