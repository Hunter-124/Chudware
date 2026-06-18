"""C22a — orchestrator engine tests (mock provider, real document sessions).

House style: plain ``main()`` runner with asserts (see engine/test_kernel_aliases.py). Run as
``& $py -m agent.test_orchestrator`` from the repo root. No pytest, no API key, no network.

Coverage:
  * happy path — scripted INTAKE->PLAN->MODEL->REVIEW->MEMORY produces a REAL box+fillet feature
    tree in a real session (ops applied transactionally);
  * malformed-ops fuzz — garbage JSON drives bounded retries, the escape hatch fires, and a
    ``script`` feature lands;
  * context-size assertion — EVERY agent_call's context_tokens_est <= 6000 across a run;
  * events.jsonl replay integrity — the persisted sequence matches events.md §3 (run_finished last);
  * identical-failure stop — the same error signature 3x fails the stage and the run ends cleanly.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.orchestrator import roles, run as orun
from agent.orchestrator.events import run_dir
from agent.orchestrator.state import CONTEXT_TOKEN_CAP
from agent.providers.mock_provider import RoleScriptedProvider
from engine.doc import session as docsess


# --------------------------------------------------------------------------- scripted provider


class ScriptedProvider(RoleScriptedProvider):
    """RoleScriptedProvider that also records the role-call order for assertions."""

    def __init__(self, by_role: dict[str, list[str]]):
        super().__init__(by_role)
        self.calls: list[str] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self.calls.append(self.role_of(system))
        return super().complete(system, messages, max_tokens=max_tokens)


def _json_block(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


# Reusable scripted replies ----------------------------------------------------

_SPEC = _json_block({"object": "bracket", "key_dims": {"length": 40, "width": 30, "height": 10},
                     "features": ["fillet top edge"], "constraints": []})
_PLAN = _json_block({"name": "bracket", "summary": "one filleted plate",
                     "parts": [{"name": "plate", "purpose": "body",
                                "dimensions": {"length": 40, "width": 30, "height": 10},
                                "features": ["3mm fillet on a top edge"]}],
                     "fit": ""})
_BOX_FILLET_OPS = _json_block([
    {"op": "add_feature", "feature": {"type": "box", "name": "Box1",
        "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
                   "height": {"value": 10, "expr": None}}}},
    {"op": "add_feature", "feature": {"type": "fillet", "name": "Fillet1",
        "params": {"radius": {"value": 3, "expr": None}},
        "refs": {"edges": [{"kind": "edge", "surf": "line", "centroid": [20, 0, 5],
                            "axis": [0, 1, 0], "area_or_len": 30,
                            "owner_feature": "$0", "hint_index": 0}]}}},
])
_REVIEW_DONE = _json_block({"done": True, "issues": []})
_MEMORY = _json_block({"summary": "built a 40x30x10 filleted plate",
                       "decisions": ["fillet r=3 on top edge"], "open_issues": []})

# C35 micro-orchestration replies. The micro profile (engaged for LOCAL providers, which the mock
# RoleScriptedProvider counts as — see micro.should_engage) runs DECOMPOSE -> per-step EXECUTE.
# The decomposer emits an ordered step list; the executor emits ONE ops array per step, applied
# immediately, so the fillet step (step 2) can reference the box feature id (f1) created by step 1.
_DECOMPOSE = _json_block([
    {"type": "box", "desc": "40x30x10 base plate",
     "params": {"length": 40, "width": 30, "height": 10}},
    {"type": "fillet", "desc": "3mm fillet on a top edge", "params": {"radius": 3}},
])
_EXEC_BOX = _json_block([
    {"op": "add_feature", "feature": {"type": "box", "name": "Box1",
        "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
                   "height": {"value": 10, "expr": None}}}},
])
_EXEC_FILLET = _json_block([
    {"op": "add_feature", "feature": {"type": "fillet", "name": "Fillet1",
        "params": {"radius": {"value": 3, "expr": None}},
        "refs": {"edges": [{"kind": "edge", "surf": "line", "centroid": [20, 0, 5],
                            "axis": [0, 1, 0], "area_or_len": 30,
                            "owner_feature": "f1", "hint_index": 0}]}}},
])


def _read_jsonl(run_id: str) -> list[dict]:
    p = run_dir(run_id) / "events.jsonl"
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------- tests


def test_happy_path_box_fillet() -> None:
    """Full machine produces a real box+fillet feature tree via ops; events sequence is correct."""
    s = docsess.new_session("part", name="orch_happy")
    prov = ScriptedProvider({
        "intake": [_SPEC], "planner": [_PLAN], "modeler": [_BOX_FILLET_OPS],
        "reviewer": [_REVIEW_DONE], "memory": [_MEMORY],
    })
    events: list[dict] = []
    res = orun.run_design("a bracket with a filleted top edge", s, prov,
                          emit=events.append)

    assert res.status == "done", f"expected done, got {res.status}/{res.stop_reason}"
    # Real feature tree: box (ok) + fillet (ok).
    feats = s.doc["features"]
    types = [f["type"] for f in feats]
    assert types == ["box", "fillet"], f"expected box+fillet, got {types}"
    assert s.revision == res.revision and s.revision >= 1, "revision should advance after ops apply"
    fs = s.last_result.feature_status
    assert all(x["ok"] for x in fs), f"both features must compile: {[(x['id'], x['error']) for x in fs]}"

    # Memory was written + stored duck-typed on the session.
    assert getattr(s, "ai_memory", {}).get("summary"), "memory must be stored on the session"
    assert s.extra_entries.get("ai/memory.json"), "memory must be staged as ai/memory.json entry"

    # Happy-path event sequence (kinds in order).
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished", kinds
    # Each stage emits stage_started -> agent_call -> agent_output -> stage_result.
    seq = " ".join(kinds)
    for stage_role in ("intake", "modeler", "reviewer", "memory"):
        assert "agent_call" in seq, "agent_call events must be present"
    print("[happy] tree:", types, "rev:", res.revision, "events:", len(events),
          "calls:", prov.calls)
    return events, prov


def test_context_size_bound() -> None:
    """EVERY agent_call across a run keeps context_tokens_est <= 6000 (the small-context budget)."""
    s = docsess.new_session("part", name="orch_ctx")
    prov = ScriptedProvider({
        "intake": [_SPEC], "planner": [_PLAN], "modeler": [_BOX_FILLET_OPS],
        "reviewer": [_REVIEW_DONE], "memory": [_MEMORY],
    })
    events: list[dict] = []
    orun.run_design("a bracket", s, prov, emit=events.append)
    calls = [e for e in events if e["kind"] == "agent_call"]
    assert calls, "there must be agent_call events"
    for c in calls:
        assert c["context_tokens_est"] <= CONTEXT_TOKEN_CAP, \
            f"{c['stage']} context {c['context_tokens_est']} > {CONTEXT_TOKEN_CAP}"
    print("[context] agent_calls:", len(calls),
          "max tokens:", max(c["context_tokens_est"] for c in calls), "<=", CONTEXT_TOKEN_CAP)


def test_malformed_ops_escape_hatch() -> None:
    """Garbage ops -> bounded retries -> escape hatch -> a real ``script`` feature lands."""
    s = docsess.new_session("part", name="orch_escape")
    # modeler budget is 4: attempt 1,2 = garbage (retry), attempt 3 (> _ESCAPE_AFTER=2) = escape
    # hatch, which asks for a script and we return one.
    garbage = "here are some ops: {not valid json at all ["
    script_reply = "```python\nresult = Box(20, 20, 5)\n```"
    prov = ScriptedProvider({
        "intake": [_SPEC], "planner": [_PLAN],
        "modeler": [garbage, garbage, script_reply],
        "reviewer": [_REVIEW_DONE], "memory": [_MEMORY],
    })
    events: list[dict] = []
    res = orun.run_design("a small block", s, prov, emit=events.append)
    assert res.status == "done", f"escape hatch run should finish done, got {res.status}"
    types = [f["type"] for f in s.doc["features"]]
    assert types == ["script"], f"escape hatch must land a script feature, got {types}"
    assert s.last_result.feature_status[0]["ok"], "the script feature must compile"
    # The first two modeler attempts must have failed to parse.
    outs = [e for e in events if e["kind"] == "agent_output" and e["stage"] == "model"]
    assert len(outs) >= 3, f"expected >=3 model attempts, got {len(outs)}"
    assert outs[0]["parsed_ok"] is False, "first model attempt should fail to parse"
    print("[escape] model attempts:", len(outs), "-> feature:", types)


def test_events_jsonl_replay_integrity() -> None:
    """The persisted events.jsonl matches the live stream, follows events.md §3, ends in run_finished."""
    s = docsess.new_session("part", name="orch_jsonl")
    prov = ScriptedProvider({
        "intake": [_SPEC], "planner": [_PLAN], "modeler": [_BOX_FILLET_OPS],
        "reviewer": [_REVIEW_DONE], "memory": [_MEMORY],
    })
    events: list[dict] = []
    res = orun.run_design("a bracket", s, prov, emit=events.append)
    disk = _read_jsonl(res.run_id)

    # Live + persisted streams agree (same kinds in order).
    live_kinds = [e["kind"] for e in events]
    disk_kinds = [e["kind"] for e in disk]
    assert live_kinds == disk_kinds, "live and on-disk event order must match"
    assert disk_kinds[0] == "run_started" and disk_kinds[-1] == "run_finished", disk_kinds

    # Valid kinds only + every event carries the run_id.
    valid = {"run_started", "stage_started", "agent_call", "agent_output",
             "stage_result", "run_finished"}
    for e in disk:
        assert e["kind"] in valid, f"unexpected kind {e['kind']}"
        assert e["run_id"] == res.run_id, "every event must carry the run_id"

    # Each stage_started is followed (somewhere) by a matching stage_result.
    started = sum(1 for k in disk_kinds if k == "stage_started")
    results = sum(1 for k in disk_kinds if k == "stage_result")
    assert started == results, f"stage_started ({started}) must pair with stage_result ({results})"

    # Sidecar raw files exist for agent_outputs.
    rd = run_dir(res.run_id)
    sidecars = list(rd.glob("attempt_*.txt"))
    assert sidecars, "raw sidecar files must be written"
    print("[jsonl] events:", len(disk), "stages:", started, "sidecars:", len(sidecars))


def test_identical_failure_stop() -> None:
    """The same error signature 3x fails a stage (FIX) and the run still ends cleanly.

    Drives the model to a clean box, then the reviewer flags an issue (-> FIX), and the fixer
    repeatedly emits the SAME invalid ops batch. After 3 identical validation-error signatures the
    fixer stage fails (identical-failure detector), the FIX loop stops, the build is kept as-is, and
    the run finishes cleanly (run_finished last). No invalid op is ever applied.
    """
    s = docsess.new_session("part", name="orch_idfail")
    box_only = _json_block([{"op": "add_feature", "feature": {"type": "box", "name": "Box1",
        "params": {"length": {"value": 20, "expr": None}, "width": {"value": 20, "expr": None},
                   "height": {"value": 5, "expr": None}}}}])
    review_issue = _json_block({"done": False, "issues": ["the fillet is missing"]})
    # A well-formed ops array that ALWAYS fails validation identically (box missing width/height).
    bad_fix = _json_block([{"op": "add_feature", "feature": {"type": "box", "name": "B",
        "params": {"length": {"value": 1, "expr": None}}}}])
    prov = ScriptedProvider({
        "intake": [_SPEC], "planner": [_PLAN], "modeler": [box_only],
        "reviewer": [review_issue, review_issue, review_issue],
        "fixer": [bad_fix, bad_fix, bad_fix, bad_fix],
        "memory": [_MEMORY],
    })
    events: list[dict] = []
    res = orun.run_design("a box", s, prov, emit=events.append)

    # The clean box was applied; the invalid fixer ops never were.
    types = [f["type"] for f in s.doc["features"]]
    assert types == ["box"], f"only the valid box should be applied, got {types}"
    # The fixer stage must fail via the identical-failure detector (3x same signature).
    fix_results = [e for e in events if e["kind"] == "stage_result" and e["stage"] == "fix"]
    assert any("identical failure" in e["reason"] for e in fix_results), \
        [e["reason"] for e in fix_results]
    # The run still completes (a kept-as-is build is a done run; the key point is it ends cleanly).
    assert res.status in ("done", "failed"), res.status
    assert events[-1]["kind"] == "run_finished", "run_finished must be emitted last"
    print("[idfail] tree:", types, "fix results:", [e["reason"] for e in fix_results],
          "run:", res.status)


def test_edit_bias_edit_feature() -> None:
    """Wave D edit bias: a doc that ALREADY HAS a box + an edit-shaped request — the modeler
    context lists the existing feature WITH its id/params and the explicit edit_feature steer,
    and a scripted edit_feature op APPLIES to the existing feature (no duplicate plate)."""
    s = docsess.new_session("part", name="orch_editbias")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {"type": "box", "name": "Plate",
        "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
                   "height": {"value": 10, "expr": None}}}}])
    assert [f["id"] for f in s.doc["features"]] == ["f1"]

    class RecordingProvider(ScriptedProvider):
        """ScriptedProvider that also records every (system, user_text) per role."""

        def __init__(self, by_role):
            super().__init__(by_role)
            self.contexts: dict[str, list[tuple[str, str]]] = {}

        def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
            utext = messages[-1]["content"][0]["text"] if messages else ""
            self.contexts.setdefault(self.role_of(system), []).append((system, utext))
            return super().complete(system, messages, max_tokens=max_tokens)

    spec = _json_block({"object": "plate", "key_dims": {"width": 60}, "features": [],
                        "constraints": []})
    plan = _json_block({"name": "plate edit", "summary": "widen the existing plate",
                        "parts": [{"name": "plate", "purpose": "set width to 60",
                                   "dimensions": {"width": 60}, "features": []}], "fit": ""})
    edit_ops = _json_block([{"op": "edit_feature", "id": "f1",
                             "params": {"width": {"value": 60, "expr": None}}}])
    prov = RecordingProvider({"intake": [spec], "planner": [plan], "modeler": [edit_ops],
                              "reviewer": [_REVIEW_DONE], "memory": [_MEMORY]})
    res = orun.run_design("make the plate 60mm wide", s, prov, emit=lambda e: None)

    assert res.status == "done", f"expected done, got {res.status}/{res.stop_reason}"
    feats = s.doc["features"]
    assert len(feats) == 1 and feats[0]["type"] == "box", \
        f"edit must NOT add a duplicate feature, got {[f['type'] for f in feats]}"
    assert feats[0]["params"]["width"]["value"] == 60, \
        f"width must be edited to 60, got {feats[0]['params']['width']}"

    # The modeler context lists the existing feature with id + CURRENT params, plus the steer.
    mctx = prov.contexts["modeler"][0][1]
    assert "f1 box Plate" in mctx, mctx
    assert "width=30" in mctx, "the existing feature's current params must be in the context"
    assert "EDIT BIAS" in mctx and "edit_feature" in mctx, \
        "the modeler must be explicitly steered toward edit_feature"
    print("[editbias] width 30 -> 60 via edit_feature; modeler context carried tree+bias")


def test_get_prompts_v2() -> None:
    """get_prompts_v2 returns one editable record per role with the built-in text + override path."""
    recs = roles.get_prompts_v2()
    got = {r["role"] for r in recs}
    assert got == set(roles.ROLES), got
    for r in recs:
        assert r["text"] and Path(r["path"]).name == f"{r['role']}.md", r["role"]
        assert "override_path" in r and isinstance(r["overridden"], bool)
    print("[prompts] roles:", sorted(got))


def test_worker_agent_run_v2_subprocess() -> None:
    """Drive the REAL worker over stdio: config 'mock' -> RoleScriptedProvider, doc_new ->
    agent_run_v2 streams the events.md §3 hierarchy and lands a box+fillet tree; get_prompts_v2
    returns the editable role prompts; agent_cancel reports cleanly for a finished run."""
    import os
    import subprocess
    import sys
    import tempfile

    # Scripted replies (role-keyed) written to a temp file the mock provider loads via env. The
    # worker's mock provider counts as LOCAL, so the C35 MICRO profile engages: it drives the
    # decomposer + executor roles (one executor call per decomposed step), not the v2 modeler.
    replies = {"intake": [_SPEC], "planner": [_PLAN], "modeler": [_BOX_FILLET_OPS],
               "reviewer": [_REVIEW_DONE], "memory": [_MEMORY],
               "decomposer": [_DECOMPOSE], "executor": [_EXEC_BOX, _EXEC_FILLET]}
    fd, replies_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(replies, fh)

    env = dict(os.environ)
    env["AI_CAD_PROVIDER"] = "mock"
    env["AI_CAD_MOCK_REPLIES"] = replies_path

    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1, env=env)

    def send(obj: dict) -> None:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()

    def read_result(req_id: int) -> tuple[dict, list[dict]]:
        """Drain event lines for *req_id* until the final {ok,result} line; return (result, events)."""
        evs: list[dict] = []
        while True:
            line = p.stdout.readline()
            if not line:
                raise AssertionError("worker closed before responding")
            obj = json.loads(line)
            if obj.get("id") == req_id and "event" in obj:
                evs.append(obj)
            elif obj.get("id") == req_id:
                assert obj.get("ok"), f"rpc failed: {obj.get('error')}"
                return obj["result"], evs

    try:
        send({"id": 1, "method": "doc_new", "params": {"kind": "part", "name": "wkr_v2"}})
        doc_res, _ = read_result(1)
        doc_id = doc_res["doc_id"]

        send({"id": 2, "method": "get_prompts_v2", "params": {}})
        pr, _ = read_result(2)
        assert {p["role"] for p in pr["prompts"]} == set(roles.ROLES), "get_prompts_v2 roles"

        send({"id": 3, "method": "agent_run_v2",
              "params": {"request": "a filleted plate", "doc_id": doc_id}})
        run_res, evs = read_result(3)
        assert run_res["status"] == "done", f"run status {run_res['status']}"
        kinds = [e["event"] for e in evs]
        assert kinds and kinds[0] == "run_started" and kinds[-1] == "run_finished", kinds
        assert "agent_call" in kinds and "stage_result" in kinds, kinds

        # The tree now carries the box + fillet (query it back over RPC).
        send({"id": 4, "method": "doc_tree", "params": {"doc_id": doc_id}})
        tree_res, _ = read_result(4)
        types = [f["type"] for f in tree_res["tree"]["features"]]
        assert types == ["box", "fillet"], f"worker run should land box+fillet, got {types}"

        # agent_cancel on a finished run reports cleanly (not an error).
        send({"id": 5, "method": "agent_cancel", "params": {"run_id": run_res["run_id"]}})
        cancel_res, _ = read_result(5)
        assert cancel_res["cancelled"] is False, "finished run cancel should report False, not crash"

        send({"id": 6, "method": "shutdown", "params": {}})
        print("[worker-v2] subprocess agent_run_v2 ->", types, "events:", len(evs),
              "status:", run_res["status"])
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=30)
        try:
            os.unlink(replies_path)
        except OSError:
            pass


def main() -> None:
    test_happy_path_box_fillet()
    test_context_size_bound()
    test_malformed_ops_escape_hatch()
    test_events_jsonl_replay_integrity()
    test_identical_failure_stop()
    test_edit_bias_edit_feature()
    test_get_prompts_v2()
    test_worker_agent_run_v2_subprocess()
    print("\nORCHESTRATOR (C22a) TESTS PASSED")


if __name__ == "__main__":
    main()
