"""C35 — micro-orchestration profile tests (mock provider, real document sessions).

House style: plain ``main()`` runner with asserts (see agent/test_orchestrator.py). Also pytest-
collectable (the ``test_*`` functions assert directly). No API key, no network — set
``AI_CAD_PROVIDER=mock`` when running the suite.

Coverage:
  * happy path — a scripted decompose list (box -> fillet) then per-step ops build a REAL box+fillet
    feature tree in a live session, with per-step verify recording status;
  * op-error retry — a step whose first executor reply is invalid ops retries ONCE with the error
    fed back, then succeeds;
  * prompt budgets — every generated prompt (decomposer system <=400 tok, executor total <=2000
    tok) is asserted via the BUDGET helpers (token estimate = len//4).
"""
from __future__ import annotations

import json

from agent.orchestrator import micro
from agent.orchestrator.state import est_tokens
from agent.providers.base import Provider
from engine.doc import session as docsess


# --------------------------------------------------------------------------- scripted provider


class MicroScriptedProvider(Provider):
    """Returns replies keyed by role (decomposer / executor), executor replies consumed in order.

    The micro runner prepends ``ROLE: <role>`` to every system prompt, so we route on that line —
    the same convention RoleScriptedProvider uses. Records every (system, user) prompt for budget
    assertions.
    """

    name = "mock-roles"           # makes should_engage treat it as a local provider
    supports_vision = False

    def __init__(self, decompose: str, executor: list[str]):
        self._decompose = decompose
        self._executor = list(executor)
        self._exec_i = 0
        self.prompts: list[tuple[str, str]] = []   # (system, user_text) per call

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        role = ""
        if system.lower().startswith("role:"):
            role = system.split("\n", 1)[0].split(":", 1)[1].strip()
        user_text = ""
        if messages and messages[-1]["content"]:
            user_text = messages[-1]["content"][0].get("text", "")
        self.prompts.append((system, user_text))
        if role == "decomposer":
            return self._decompose
        # executor: pop the next scripted reply (clamp at the last).
        reply = self._executor[min(self._exec_i, len(self._executor) - 1)]
        self._exec_i += 1
        return reply


def _json_block(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


_STEPS = _json_block([
    {"type": "box", "desc": "40x30x10 base plate", "params": {"length": 40, "width": 30, "height": 10}},
    {"type": "fillet", "desc": "round the +X top edge r=3", "params": {"radius": 3}},
])

_BOX_OPS = _json_block([
    {"op": "add_feature", "feature": {"type": "box", "name": "Box1",
        "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
                   "height": {"value": 10, "expr": None}}}},
])

# The fillet op must reference the box feature's id. The box lands as "f1" in a fresh part doc.
def _fillet_ops(owner: str = "f1") -> str:
    return _json_block([
        {"op": "add_feature", "feature": {"type": "fillet", "name": "Fillet1",
            "params": {"radius": {"value": 3, "expr": None}},
            "refs": {"edges": [{"kind": "edge", "surf": "line", "centroid": [20, 0, 5],
                                "axis": [0, 1, 0], "area_or_len": 30,
                                "owner_feature": owner, "hint_index": 0}]}}},
    ])


# --------------------------------------------------------------------------- tests


def _assert_prompt_budgets(prov: MicroScriptedProvider) -> None:
    """Every captured prompt obeys the budget helpers (decomposer system <=400, executor <=2000)."""
    for system, user_text in prov.prompts:
        role = system.split("\n", 1)[0].split(":", 1)[1].strip() if system.lower().startswith("role:") else ""
        # Strip the "ROLE: x\n" marker for the system-budget check (it is harmless context).
        sys_body = system.split("\n", 1)[1] if "\n" in system else system
        if role == "decomposer":
            assert est_tokens(sys_body) <= micro.DECOMPOSE_SYSTEM_BUDGET_TOKENS, \
                f"decomposer system {est_tokens(sys_body)} > {micro.DECOMPOSE_SYSTEM_BUDGET_TOKENS}"
        if role == "executor":
            total = est_tokens(system) + est_tokens(user_text)
            assert total <= micro.EXEC_PROMPT_BUDGET_TOKENS, \
                f"executor prompt {total} > {micro.EXEC_PROMPT_BUDGET_TOKENS}"


def test_happy_path_box_fillet() -> None:
    """Decompose (box->fillet) then per-step ops build a real box+fillet tree; budgets hold."""
    s = docsess.new_session("part", name="micro_happy")
    prov = MicroScriptedProvider(decompose=_STEPS, executor=[_BOX_OPS, _fillet_ops("f1")])
    events: list[dict] = []
    res = micro.run_micro_design("a 40x30x10 plate with a filleted top edge", s, prov,
                                 emit=events.append)

    assert res.status == "done", f"expected done, got {res.status}/{res.stop_reason}"
    types = [f["type"] for f in s.doc["features"]]
    assert types == ["box", "fillet"], f"expected box+fillet, got {types}"
    assert all(x["ok"] for x in s.last_result.feature_status), "both features must compile"

    # Step progress streamed: a decompose stage_result + two execute stage_results with k/N.
    execs = [e for e in events if e["kind"] == "stage_result" and e["stage"] == "execute"]
    assert len(execs) == 2, f"expected 2 execute step results, got {len(execs)}"
    assert all(e.get("step_total") == 2 for e in execs), "each step result carries step_total"
    assert all(e["verdict"] == "pass" for e in execs), [e["reason"] for e in execs]

    _assert_prompt_budgets(prov)
    print("[micro happy] tree:", types, "events:", len(events),
          "exec results:", [e["verdict"] for e in execs])


def test_op_error_retries_once() -> None:
    """A step whose first executor reply is INVALID ops retries once with the error, then succeeds."""
    s = docsess.new_session("part", name="micro_retry")
    # box step: first reply garbage (no ops array), second reply valid.
    garbage = "here are the ops: {not json at all ["
    prov = MicroScriptedProvider(
        decompose=_json_block([{"type": "box", "desc": "20x20x5 block",
                                "params": {"length": 20, "width": 20, "height": 5}}]),
        executor=[garbage, _BOX_OPS])
    events: list[dict] = []
    res = micro.run_micro_design("a small block", s, prov, emit=events.append)

    assert res.status == "done", f"retry run should finish done, got {res.status}"
    types = [f["type"] for f in s.doc["features"]]
    assert types == ["box"], f"expected the box to land after retry, got {types}"

    # Two executor attempts for the single step: attempt 1 failed to parse, attempt 2 succeeded.
    outs = [e for e in events if e["kind"] == "agent_output" and e["stage"] == "execute"]
    assert len(outs) == 2, f"expected 2 executor attempts, got {len(outs)}"
    assert outs[0]["parsed_ok"] is False, "first attempt should fail to parse"
    assert outs[1]["parsed_ok"] is True, "second attempt should parse"
    # The retry prompt (attempt 2) must carry the previous error feedback.
    retry_user = prov.prompts[-1][1]
    assert "PREVIOUS ATTEMPT FAILED" in retry_user, "retry prompt must append the error feedback"

    _assert_prompt_budgets(prov)
    print("[micro retry] attempts:", len(outs), "-> tree:", types)


def test_edit_bias_edit_feature() -> None:
    """Wave D edit bias (micro profile): a doc that already has a box + an edit-shaped request —
    the decomposer sees the existing features + the edit steer, the executor sees the
    edit_feature steer, and a scripted edit_feature op edits the box in place (no duplicate)."""
    s = docsess.new_session("part", name="micro_editbias")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {"type": "box", "name": "Plate",
        "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
                   "height": {"value": 10, "expr": None}}}}])
    assert [f["id"] for f in s.doc["features"]] == ["f1"]

    steps = _json_block([{"type": "box", "desc": "edit f1: set width to 60",
                          "params": {"feature_id": "f1", "width": 60}}])
    edit_ops = _json_block([{"op": "edit_feature", "id": "f1",
                             "params": {"width": {"value": 60, "expr": None}}}])
    prov = MicroScriptedProvider(decompose=steps, executor=[edit_ops])
    events: list[dict] = []
    res = micro.run_micro_design("make the plate 60mm wide", s, prov, emit=events.append)

    assert res.status == "done", f"expected done, got {res.status}/{res.stop_reason}"
    feats = s.doc["features"]
    assert len(feats) == 1 and feats[0]["type"] == "box", \
        f"edit must NOT add a duplicate feature, got {[f['type'] for f in feats]}"
    assert feats[0]["params"]["width"]["value"] == 60, feats[0]["params"]["width"]

    # Decomposer prompt: existing features (id + key dims) + the decompose edit steer.
    dec_user = prov.prompts[0][1]
    assert "f1 box" in dec_user and "width=30" in dec_user, dec_user
    assert "EDIT BIAS" in dec_user, "decomposer must be steered toward edit steps"
    # Executor prompt: the edit_feature steer.
    exec_user = prov.prompts[1][1]
    assert "EDIT BIAS" in exec_user and "edit_feature" in exec_user, \
        "executor must be steered toward edit_feature"

    _assert_prompt_budgets(prov)
    print("[micro editbias] width 30 -> 60 via edit_feature; both prompts carried the steer")


def test_verifier_volume_direction() -> None:
    """The pure-python verifier flags a 'cut' step that did not shrink the volume."""
    box_step = micro.Step(type="box", desc="base", params={})
    cut_step = micro.Step(type="cylinder", desc="bore", params={"mode": "cut"})
    fs_ok = [{"id": "f1", "ok": True, "error": None}]
    # boss that grew -> verified
    ok, _ = micro.verify_step(box_step, {"features": [{"type": "box", "id": "f1"}]}, fs_ok, 0.0, 1000.0)
    assert ok, "a boss that grew the volume should verify"
    # cut that did NOT shrink -> not verified
    bad, notes = micro.verify_step(cut_step, {"features": [{"type": "cylinder", "id": "f2"}]},
                                   fs_ok, 1000.0, 1000.0)
    assert not bad, f"a cut with no volume drop should fail verify ({notes})"
    print("[micro verify] boss-grow ok, cut-no-shrink flagged")


def test_should_engage() -> None:
    """should_engage: local mock auto-engages; cloud anthropic does not; explicit flags force."""
    prov = MicroScriptedProvider(_STEPS, [_BOX_OPS])
    assert micro.should_engage(prov, {}), "local mock should auto-engage"
    assert micro.should_engage(prov, {"micro_mode": True}), "explicit true engages"
    assert not micro.should_engage(prov, {"micro_mode": False}), "explicit false disables"

    class FakeCloud(Provider):
        name = "anthropic"
    assert not micro.should_engage(FakeCloud(), {}), "cloud anthropic must not auto-engage"
    assert micro.should_engage(FakeCloud(), {"micro_mode": True}), "explicit true overrides locality"
    print("[micro engage] auto/explicit/cloud all correct")


def main() -> None:
    test_happy_path_box_fillet()
    test_op_error_retries_once()
    test_edit_bias_edit_feature()
    test_verifier_volume_direction()
    test_should_engage()
    print("\nMICRO-ORCHESTRATION (C35) TESTS PASSED")


if __name__ == "__main__":
    main()
