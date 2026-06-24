"""Drill a through-all hole of various diameters in a 20mm plate and back out the
actual bore radius from the volume removed (full-through => vol = pi*r^2*20)."""
import ctypes, json, os, math

DIST = r"C:\Users\nigga\Desktop\AI-opencad\dist\AI-CAD"
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


def removed(th, dia):
    d = call("doc_new", {"kind": "part"})["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {"type": "box", "name": "P",
            "params": {"length": dimv(40), "width": dimv(40), "height": dimv(th)}}},
        {"op": "add_feature", "feature": {"type": "hole", "name": "H",
            "params": {"style": "simple", "standard": "custom", "diameter": dimv(dia),
                       "end": "through_all"},
            "refs": {"face": {"of": "face", "side": "top"}}}},
    ]
    r = call("doc_apply_ops", {"doc_id": d, "ops": ops})
    return 40 * 40 * th - r.get("volume", 0)


for dia in [4, 8, 10, 20]:
    rem = removed(20, dia)
    r_impl = math.sqrt(rem / (math.pi * 20)) if rem > 0 else 0
    print(f"dia={dia:2}: removed {rem:7.1f} -> implied bore r={r_impl:.3f}  (correct r={dia/2})")
