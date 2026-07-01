"""Build a representative part and render the ANNOTATED vision PNG so we can eyeball the
orientation gizmo, the direction-tinted faces, and the named-face callouts."""
import ctypes, json, os

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "AI-CAD")
os.add_dll_directory(DIST)
dll = ctypes.CDLL(os.path.join(DIST, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]


def call(m, p):
    o = ctypes.c_char_p()
    dll.aicad_call(m.encode(), json.dumps(p).encode(), ctypes.byref(o))
    s = o.value.decode() if o.value else ""
    if o.value:
        dll.aicad_free(o)
    return json.loads(s) if s else {}


def dimv(v):
    return {"value": v, "expr": None}


d = call("doc_new", {"kind": "part"})["doc_id"]
call("doc_apply_ops", {"doc_id": d, "ops": [
    {"op": "add_feature", "feature": {"type": "box", "name": "Base",
        "params": {"length": dimv(50), "width": dimv(34), "height": dimv(22)}}},
    {"op": "add_feature", "feature": {"type": "hole", "name": "Bore",
        "params": {"style": "simple", "standard": "custom", "diameter": dimv(12),
                   "end": "through_all"}, "refs": {"face": {"of": "face", "side": "top"}}}},
    {"op": "add_feature", "feature": {"type": "fillet", "name": "Round",
        "params": {"radius": dimv(4)}, "refs": {"edges": {"of": "edge", "parallel": "z"}}}},
]})
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_render_test.png")
print(call("doc_render_png", {"doc_id": d, "out_path": out}))
