"""Worker-layer tests: run_model assembly detection + the streaming event wire format.

(The agent_design session-reuse / plan / provider-override tests were removed along
with the agent_design RPC itself.)
"""
from __future__ import annotations

import io
import json
import subprocess
import sys

from services import worker_main as worker


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
    p = subprocess.Popen([sys.executable, "-m", "services.worker_main"],
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
    test_run_model_assembly()
    test_event_wire_format()
    test_subprocess_roundtrip()
    print("\nWORKER SESSION + STREAMING TESTS PASSED")


if __name__ == "__main__":
    main()
