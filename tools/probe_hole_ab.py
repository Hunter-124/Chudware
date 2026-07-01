"""A/B the hole diameter forms to find which halves the bore. 40x40x20 plate, Ø10
through-all on top => correct removal = pi*5^2*20 = 1571 (vol 30429)."""
import ctypes, json, os, math

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


def run(label, hole_params, face):
    d = call("doc_new", {"kind": "part"})["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {"type": "box", "name": "P",
            "params": {"length": dimv(40), "width": dimv(40), "height": dimv(20)}}},
        {"op": "add_feature", "feature": {"type": "hole", "name": "H",
            "params": hole_params, "refs": {"face": face}}},
    ]
    r = call("doc_apply_ops", {"doc_id": d, "ops": ops})
    rem = 40 * 40 * 20 - r.get("volume", 0)
    rimpl = math.sqrt(rem / (math.pi * 20)) if rem > 0 else 0
    print(f"{label:55} removed {rem:7.1f}  bore r={rimpl:.3f} (want 5.0)")


zsel = {"kind": "face", "surf": "plane", "centroid": [0, 0, 10], "axis": [0, 0, 1],
        "area_or_len": 1600, "owner_feature": "f1", "hint_index": 0}
run("raw number 10.0, no standard, +Z selector", {"style": "simple", "diameter": 10.0}, zsel)
run("dim {value:10}, no standard, +Z selector", {"style": "simple", "diameter": dimv(10)}, zsel)
run("dim {value:10}, standard=custom, +Z selector", {"style": "simple", "standard": "custom", "diameter": dimv(10)}, zsel)
run("dim {value:10}, standard=custom, through_all, query", {"style": "simple", "standard": "custom", "diameter": dimv(10), "end": "through_all"}, {"of": "face", "side": "top"})
run("raw 10.0, through_all, query face", {"style": "simple", "diameter": 10.0, "end": "through_all"}, {"of": "face", "side": "top"})
