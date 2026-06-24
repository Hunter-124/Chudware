"""Drill a through-all 10mm hole on each of the 6 faces of a 40^3 cube and report the
volume removed. A correct through_all removes pi*5^2*40 ~= 3142 mm^3 on every face."""
import ctypes, json, os

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


def tryface(side):
    r = call("doc_new", {"kind": "part"})
    d = r["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {"type": "box", "name": "Cube",
            "params": {"length": dimv(40), "width": dimv(40), "height": dimv(40)}}},
        {"op": "add_feature", "feature": {"type": "hole", "name": "H",
            "params": {"style": "simple", "standard": "custom", "diameter": dimv(10),
                       "end": "through_all"},
            "refs": {"face": {"of": "face", "side": side}}}},
    ]
    resp = call("doc_apply_ops", {"doc_id": d, "ops": ops})
    rb = call("doc_rebuild", {"doc_id": d})
    fs = (rb.get("rebuild", {}) or {}).get("feature_status", [])
    errs = [s.get("error") for s in fs if not s.get("ok")]
    return round(resp.get("volume", 0)), errs


for s in ["top", "bottom", "front", "back", "right", "left"]:
    removed, errs = tryface(s)
    flag = "OK " if abs((64000 - removed) - 3142) < 100 else "** "
    print(f"{flag}hole through {s:7} face: removed {64000 - removed} mm^3 (expect ~3142) errs={errs}")
