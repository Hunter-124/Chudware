"""Worker-layer tests: session reuse across agent_design RPC calls + event forwarding.

Drives engine.worker's handlers directly with a mock provider (no model/API needed), so
the session registry and the streaming `emit` contract the native client consumes are
exercised deterministically.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

from agent import config
from agent.providers.mock_provider import MockProvider
from engine import worker


def _factory(replies: list[str]):
    return lambda *a, **k: MockProvider(replies)


def test_session_reuse_reset_and_streaming() -> None:
    original = config.get_provider
    worker._SESSIONS.clear()
    events: list[dict] = []
    emit = events.append
    try:
        # turn 1 — fresh session
        config.get_provider = _factory([
            '```python\nresult = Box(15, 15, 15)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
            '```json\n{"done": true, "notes": "ok"}\n```',
        ])
        r1 = worker._agent_design(
            {"request": "a 15mm cube", "name": "wkr", "session_id": "s1", "plan": False}, emit)
        assert r1["success"] and r1["turn"] == 1, "turn 1 should be the first turn"
        assert "s1" in worker._SESSIONS, "session should be registered"

        # turn 2 — same session id must continue (refine), not restart
        config.get_provider = _factory([
            '```python\nresult = Box(15, 15, 30)\n```\n```json\n{"done": false, "notes": "taller"}\n```',
            '```json\n{"done": true, "notes": "ok"}\n```',
        ])
        r2 = worker._agent_design(
            {"request": "twice as tall", "name": "wkr", "session_id": "s1", "plan": False},
            emit)
        assert r2["turn"] == 2, "same session_id must advance the turn counter"
        assert "15, 15, 30" in (r2["final_code"] or ""), "turn 2 should refine to the taller cube"

        # reset — start a brand-new session under the same id
        config.get_provider = _factory([
            '```python\nresult = Box(8, 8, 8)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
            '```json\n{"done": true, "notes": "ok"}\n```',
        ])
        r3 = worker._agent_design(
            {"request": "small cube", "name": "wkr", "session_id": "s1", "reset": True,
             "plan": False}, emit)
        assert r3["turn"] == 1, "reset must start a new session at turn 1"

        # events were forwarded in the structured shape the UI consumes
        it = [e for e in events if e.get("kind") == "iteration"]
        st = [e for e in events if e.get("kind") == "status"]
        assert it and st, "worker must forward status + iteration events"
        assert it[0].get("code") and "Box(" in it[0]["code"], "iteration events carry code"
        print("[worker] turns:", r1["turn"], r2["turn"], "reset->", r3["turn"],
              " events:", len(events), " (iter", len(it), "/ status", len(st), ")")
    finally:
        config.get_provider = original


def test_plan_event_forwarded() -> None:
    """With plan mode on, the worker must forward the "plan" event (the GUI's plan card)
    and the run result must carry the parsed plan."""
    original = config.get_provider
    worker._SESSIONS.clear()
    events: list[dict] = []
    plan_reply = ('```json\n{"name": "cube", "summary": "one box", "parts": '
                  '[{"name": "cube", "purpose": "demo", "dimensions": {"side": 15}, '
                  '"features": ["none"]}], "fit": ""}\n```')
    try:
        config.get_provider = _factory([
            plan_reply,
            '```python\nresult = Box(15, 15, 15)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
            '```json\n{"done": true, "notes": "ok"}\n```',
        ])
        r = worker._agent_design(
            {"request": "a 15mm cube", "name": "wkrplan", "session_id": "sp", "plan": True},
            events.append)
        assert r["success"], "plan-mode run should succeed"
        plans = [e for e in events if e.get("kind") == "plan"]
        assert len(plans) == 1 and plans[0]["plan"]["parts"][0]["name"] == "cube", \
            "the worker must forward exactly one plan event"
        assert r["plan"] and r["plan"]["name"] == "cube", "the run result carries the plan"
        print("[worker-plan] plan event forwarded; run.plan:", r["plan"]["name"])
    finally:
        config.get_provider = original


def test_run_model_assembly() -> None:
    """run_model must auto-detect a multi-part script and return the assembly result
    (is_assembly + per-part exports + fit report), while a single-`result` script stays
    flat (is_assembly False)."""
    # single-part: unchanged shape
    single = worker._run_model({"code": "result = Box(12, 12, 12)", "name": "wkr_single"}, lambda *_: None)
    assert single["ok"] and single["stl_path"], "single-part run_model should still build"
    assert single["is_assembly"] is False, "single-part must not be flagged as an assembly"

    # multi-part: two overlapping boxes -> interference reported
    code = ('a = Box(20, 20, 20)\n'
            'b = Pos(10, 0, 0) * Box(20, 20, 20)\n'
            'parts = {"a": a, "b": b}\n')
    asm = worker._run_model({"code": code, "name": "wkr_asm"}, lambda *_: None)
    assert asm["ok"] and asm["is_assembly"], "multi-part run_model should flag is_assembly"
    assert asm["stl_path"], "assembly should export a combined STL"
    names = {p["name"] for p in asm["parts"]}
    assert names == {"a", "b"} and all(p["stl_path"] for p in asm["parts"]), \
        "each part should be exported"
    assert asm["fit"] and asm["fit"]["interferences"], "overlap should be reported as interference"
    overlap = asm["fit"]["interferences"][0]["volume"]
    assert abs(overlap - 4000.0) < 1.0, f"overlap should be ~4000 mm^3, got {overlap}"
    print("[worker] single is_assembly:", single["is_assembly"],
          " asm parts:", sorted(names), " overlap:", round(overlap, 1), "mm^3")


def test_event_wire_format() -> None:
    """serve() must wrap progress events as {id, event, data} lines — exactly the shape the
    native client's call() routes to its onEvent callback."""
    rid = 7

    def emit_event(data: dict, _rid=rid) -> None:
        worker._emit({"id": _rid, "event": data.get("kind", "event"), "data": data})

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        emit_event({"kind": "iteration", "index": 2, "ok": True, "dims": "10 x 10 x 10 mm",
                    "code": "result = Box(10,10,10)", "diff": None})
    finally:
        sys.stdout = old
    obj = json.loads(buf.getvalue().strip())
    assert obj["id"] == rid and obj["event"] == "iteration", "event must carry id + kind"
    d = obj["data"]
    assert d["index"] == 2 and d["ok"] is True and d["code"], "data must carry the iteration"
    assert d["diff"] is None, "null fields stay null (client tolerates them)"
    print("[wire] event line:", json.dumps(obj)[:80], "...")


def test_subprocess_roundtrip() -> None:
    """Drive the real worker over stdio (the refactored (params, emit) dispatch) end to end."""
    p = subprocess.Popen([sys.executable, "-m", "engine.worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, bufsize=1)

    def rpc(obj: dict) -> dict:
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        r = rpc({"id": 1, "method": "ping", "params": {}})
        assert r["id"] == 1 and r["ok"] and r["result"]["pong"], "ping should pong"
        r = rpc({"id": 2, "method": "run_model",
                 "params": {"code": "result = Box(12, 12, 12)", "name": "wkr_proto"}})
        assert r["id"] == 2 and r["ok"] and r["result"]["ok"], "run_model should build"
        assert r["result"]["stl_path"], "run_model should emit an STL path"
        rpc({"id": 3, "method": "shutdown", "params": {}})
        print("[subprocess] ping + run_model + shutdown OK over real pipes")
    finally:
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        p.wait(timeout=10)


def main() -> None:
    test_session_reuse_reset_and_streaming()
    test_plan_event_forwarded()
    test_run_model_assembly()
    test_event_wire_format()
    test_subprocess_roundtrip()
    print("\nWORKER SESSION + STREAMING TESTS PASSED")


if __name__ == "__main__":
    main()
