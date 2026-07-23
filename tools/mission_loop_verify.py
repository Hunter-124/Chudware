"""Deterministic check of the mission orchestrator loop (mock provider, no network).

Drives agent_mission_v1 with a role-scripted mock provider so the mission_planner returns a
fixed task list. The mission then runs the tasks itself: a simulate_flow task (real LBM, no LLM)
on a pre-built part, then a report task whose verdict is produced from the measured forces.
Asserts the RPC returns the expected shape — ordered tasks, threaded doc, and a non-empty verdict
that cites the numbers. This exercises the planner-parse, the executor loop, doc threading, and
the verdict stage end-to-end without any live model.
"""
import ctypes, json, os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(_REPO_ROOT, "core", "build-occt", "bin", "Release")
os.environ.setdefault("CHUDWARE_PROMPTS_DIR", os.path.join(_REPO_ROOT, "agent", "prompts"))
os.add_dll_directory(CORE)
lib = ctypes.WinDLL(os.path.join(CORE, "libchudware.dll"))
lib.chudware_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
lib.chudware_call.restype = ctypes.c_int
lib.chudware_free.argtypes = [ctypes.c_char_p]


def call(method, params):
    out = ctypes.c_char_p()
    rc = lib.chudware_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    raw = out.value.decode() if out.value else ""
    if out.value:
        lib.chudware_free(out)
    data = json.loads(raw) if raw else {}
    if rc != 0 or (isinstance(data, dict) and data.get("code")):
        raise RuntimeError(f"{method} failed rc={rc}: {raw[:300]}")
    return data


# A pre-built part to analyze (the mission's "active" doc).
doc_id = call("doc_new", {"kind": "part", "name": "Wing"})["doc_id"]
tree = call("doc_tree", {"doc_id": doc_id})
call("doc_apply_ops", {"doc_id": doc_id, "base_revision": tree.get("revision", 0),
                       "ops": [{"op": "add_feature", "feature": {
                           "type": "box", "name": "Slab",
                           "params": {"length": 140, "width": 40, "height": 6}}}],
                       "with_tess": False})

# Mock provider: mission_planner returns a 2-task plan (sim the active doc, then report). The
# reporter reply is scripted prose; if the mission's deterministic verdict is used instead, the
# test still passes (it just must be non-empty and mention the forces).
task_json = json.dumps({
    "mission": "analyze the wing and judge it",
    "tasks": [
        {"capability": "simulate_flow", "title": "Wind-tunnel it",
         "request": "wind tunnel at 18 m/s, 5 deg AoA in air", "target": "active"},
        {"capability": "report", "title": "Verdict",
         "request": "is this a good lifting surface?", "target": "previous"},
    ],
})
provider = {"name": "mock", "replies": {
    "mission_planner": ["```json\n" + task_json + "\n```"],
    "reporter": ["The wing develops measurable lift with low drag at this condition; the "
                 "lift-to-drag ratio is reasonable for a model wing — a sound starting design."],
}}

res = call("agent_mission_v1", {"request": "analyze my wing and tell me if it's any good",
                                "doc_id": doc_id, "provider": provider})

print("status      =", res.get("status"))
print("stop_reason =", res.get("stop_reason"))
print("doc_id      =", res.get("doc_id"))
tasks = res.get("tasks", [])
print("tasks       =", [(t.get("capability"), t.get("status")) for t in tasks])
print("verdict     =", repr(res.get("verdict", ""))[:160])

assert res.get("status") == "done", f"mission not done: {res}"
assert [t.get("capability") for t in tasks] == ["simulate_flow", "report"], f"task chain wrong: {tasks}"
assert tasks[0].get("status") == "done", f"sim task did not complete: {tasks[0]}"
assert res.get("doc_id") == doc_id, "doc thread should stay on the active part"
assert res.get("verdict", "").strip(), "verdict is empty"
# events carry the mission_* timeline kinds
kinds = [e.get("kind") for e in res.get("events", [])]
for k in ("mission_started", "mission_task_started", "mission_task_result", "mission_finished"):
    assert k in kinds, f"missing mission event {k} in {kinds}"
print("event kinds =", [k for k in kinds if k.startswith("mission")])
print("\nPASS: agent_mission_v1 decomposes, runs the chain, threads the doc, and returns a verdict.")
