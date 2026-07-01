"""Drive the EXACT closed vase profile the live model emitted straight through
doc_apply_ops, to isolate whether the engine builds a face+solid from this
spline+lines loop (revolve), independent of the agent."""
import ctypes, json, os

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "AI-CAD")
os.add_dll_directory(DIST)
dll = ctypes.CDLL(os.path.join(DIST, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]


def call(method, params):
    out = ctypes.c_char_p()
    rc = dll.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    s = out.value.decode() if out.value else ""
    if out.value:
        dll.aicad_free(out)
    try:
        return rc, (json.loads(s) if s else {})
    except Exception:
        return rc, {"_raw": s[:600]}


_, r = call("doc_new", {"kind": "part"})
doc = r["doc_id"]

ops = [
    {"op": "add_feature", "feature": {"type": "sketch", "name": "Prof",
        "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.front"}}},
    {"op": "sketch_add_entity", "sketch": "$0",
        "entity": {"type": "spline", "points": [[5, 0], [15, 20], [30, 45], [20, 60]]}},
    {"op": "sketch_add_entity", "sketch": "$0",
        "entity": {"type": "line", "p1": [20, 60], "p2": [0, 60]}},
    {"op": "sketch_add_entity", "sketch": "$0",
        "entity": {"type": "line", "p1": [0, 0], "p2": [0, 60]}},
    {"op": "sketch_add_entity", "sketch": "$0",
        "entity": {"type": "line", "p1": [5, 0], "p2": [0, 0]}},
    {"op": "add_feature", "feature": {"type": "revolve", "name": "Rev",
        "params": {"mode": "boss", "angle": {"value": 360, "expr": None}},
        "refs": {"profile": [{"kind": "region", "sketch": "$0", "centroid": [10, 30], "hint_index": 0}],
                 "axis": {"kind": "datum", "feature": "axis.z"}}}},
]
rc, resp = call("doc_apply_ops", {"doc_id": doc, "ops": ops})
print("rc=", rc, "error=", resp.get("error"))
for s in (resp.get("feature_status") or resp.get("statuses") or []):
    print("  feature:", s.get("id"), s.get("type"), "ok=", s.get("ok"), s.get("error", ""))

# Also: does the sketch alone produce a closed region? Ask doc_sketch_regions if present.
_, tr = call("doc_tree", {"doc_id": doc})
t = tr.get("tree", {})
feats = t.get("features", []) if isinstance(t, dict) else []
print("features in tree:", [(f.get("type"), f.get("name")) for f in feats])
