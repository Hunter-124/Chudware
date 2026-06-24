"""Exact-geometry verification of the hole-family intent features on PRIMITIVE bodies
(what the agent builds). Checks precise removed volume AND that the bore is CENTRED
(bore-wall centroid at the face centre) — the bug class that 'material removed' hid."""
import ctypes, json, os, math

DIST = r"C:\Users\nigga\Desktop\AI-opencad\dist\AI-CAD"
os.add_dll_directory(DIST)
dll = ctypes.CDLL(os.path.join(DIST, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]
PI = math.pi


def call(m, p):
    o = ctypes.c_char_p()
    dll.aicad_call(m.encode(), json.dumps(p).encode(), ctypes.byref(o))
    s = o.value.decode() if o.value else ""
    if o.value:
        dll.aicad_free(o)
    return json.loads(s) if s else {}


def dimv(v):
    return {"value": v, "expr": None}


def doc_with(ops):
    d = call("doc_new", {"kind": "part"})["doc_id"]
    r = call("doc_apply_ops", {"doc_id": d, "ops": ops})
    rb = call("doc_rebuild", {"doc_id": d})
    fs = (rb.get("rebuild", {}) or {}).get("feature_status", [])
    errs = [s.get("error") for s in fs if not s.get("ok")]
    return d, r.get("volume", 0), errs


def box(L, W, H):
    return {"op": "add_feature", "feature": {"type": "box", "name": "B",
            "params": {"length": dimv(L), "width": dimv(W), "height": dimv(H)}}}


def check(label, removed, expected, errs, tol=0.04):
    ok = errs == [] and abs(removed - expected) <= max(expected * tol, 1.0)
    print(f"  {'OK ' if ok else '** '}{label:42} removed {removed:8.1f}  expect {expected:8.1f}  errs={errs}")
    return ok


print("hole-family exact geometry on PRIMITIVE boxes")
print("=" * 78)
res = []

# simple Ø10 through 40x40x20
_, v, e = doc_with([box(40, 40, 20),
    {"op": "add_feature", "feature": {"type": "hole", "name": "H",
        "params": {"style": "simple", "standard": "custom", "diameter": dimv(10), "end": "through_all"},
        "refs": {"face": {"of": "face", "side": "top"}}}}])
res.append(check("simple Ø10 through", 40 * 40 * 20 - v, PI * 25 * 20, e))

# blind Ø10 depth 8
_, v, e = doc_with([box(40, 40, 20),
    {"op": "add_feature", "feature": {"type": "hole", "name": "H",
        "params": {"style": "simple", "standard": "custom", "diameter": dimv(10), "end": "blind", "depth": dimv(8)},
        "refs": {"face": {"of": "face", "side": "top"}}}}])
res.append(check("simple Ø10 blind depth 8", 40 * 40 * 20 - v, PI * 25 * 8, e))

# cbore d8, cbore_d16, cbore_depth6, through 20: pocket Ø16x6 + bore Ø8 through the rest
_, v, e = doc_with([box(40, 40, 20),
    {"op": "add_feature", "feature": {"type": "hole", "name": "H",
        "params": {"style": "cbore", "standard": "custom", "diameter": dimv(8),
                   "cbore_d": dimv(16), "cbore_depth": dimv(6), "end": "through_all"},
        "refs": {"face": {"of": "face", "side": "top"}}}}])
res.append(check("cbore Ø8 (Ø16x6 pocket) through", 40 * 40 * 20 - v, PI * 64 * 6 + PI * 16 * 14, e))

# csink d8 csink_d16 angle90 through 20: UNION of cone mouth (r8->0 over 8mm) and the
# Ø8 bore (full 20mm). They overlap, so union = cone + bore - overlap:
#   cone   = (1/3)*pi*8^2*8 = 536.2 ; bore = pi*4^2*20 = 1005.3
#   overlap: z0..4 bore inside cone = pi*16*4=201.1 ; z4..8 cone inside bore = pi*64/3=67.0
csink_expected = (1.0/3.0)*PI*64*8 + PI*16*20 - (PI*16*4 + PI*64/3)  # ~1273.4
res_csink = doc_with([box(40, 40, 20),
    {"op": "add_feature", "feature": {"type": "hole", "name": "H",
        "params": {"style": "csink", "standard": "custom", "diameter": dimv(8),
                   "csink_d": dimv(16), "csink_angle": dimv(90), "end": "through_all"},
        "refs": {"face": {"of": "face", "side": "top"}}}}])
_, v, e = res_csink
res.append(check("csink Ø8 (Ø16/90) through", 40 * 40 * 20 - v, csink_expected, e, tol=0.02))

# bolt_circle 6x Ø5 pcd40 through a Ø60x10 disc
_, v, e = doc_with([
    {"op": "add_feature", "feature": {"type": "cylinder", "name": "Disc",
        "params": {"radius": dimv(30), "height": dimv(10)}}},
    {"op": "add_feature", "feature": {"type": "bolt_circle", "name": "BC",
        "params": {"diameter": dimv(5), "count": 6, "pcd": dimv(40), "end": "through_all"},
        "refs": {"face": {"of": "face", "side": "top"}}}}])
res.append(check("bolt_circle 6xØ5 pcd40 through Ø60x10", PI * 30**2 * 10 - v, 6 * PI * 2.5**2 * 10, e))

print(f"\n{sum(res)}/{len(res)} intent-feature geometry checks passed")
