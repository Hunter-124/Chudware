"""Verify run_assembly_design's per-part rotation (placement.rot) reaches the geometry.

run_assembly_design builds a 4x4 component transform R(rot)+translation (Euler deg, X then Y
then Z, R=Rz*Ry*Rx) and feeds it to asm_insert_component. This harness reproduces that exact
formula and confirms that inserting a long-X part with rot=[0,0,90] swaps its world X/Y extents
(i.e. the rotation is honored end-to-end through the assembly compile + world placement).
"""
import ctypes, json, math, os, tempfile

DIST = r"C:\Users\nigga\Desktop\AI-opencad\dist\AI-CAD"
os.add_dll_directory(DIST)
lib = ctypes.WinDLL(os.path.join(DIST, "libaicad.dll"))
lib.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
lib.aicad_call.restype = ctypes.c_int
lib.aicad_free.argtypes = [ctypes.c_char_p]


def call(method, params):
    out = ctypes.c_char_p()
    rc = lib.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    raw = out.value.decode() if out.value else ""
    if out.value:
        lib.aicad_free(out)
    data = json.loads(raw) if raw else {}
    if rc != 0 or (isinstance(data, dict) and data.get("code")):
        raise RuntimeError(f"{method} failed rc={rc}: {raw[:300]}")
    return data


def placement_transform(pos, rot):
    """Exact mirror of run.cpp::placement_transform (R = Rz*Ry*Rx, Euler degrees)."""
    d2r = math.pi / 180.0
    cx, sx = math.cos(rot[0] * d2r), math.sin(rot[0] * d2r)
    cy, sy = math.cos(rot[1] * d2r), math.sin(rot[1] * d2r)
    cz, sz = math.cos(rot[2] * d2r), math.sin(rot[2] * d2r)
    r00, r01, r02 = cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx
    r10, r11, r12 = sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx
    r20, r21, r22 = -sy, cy * sx, cy * cx
    return [r00, r01, r02, pos[0],
            r10, r11, r12, pos[1],
            r20, r21, r22, pos[2],
            0, 0, 0, 1]


tmp = tempfile.mkdtemp(prefix="asm_rot_")
# A long-X bar: 40 (X) x 6 (Y) x 6 (Z). Rotating 90 deg about Z must make it 6 (X) x 40 (Y).
d = call("doc_new", {"kind": "part", "name": "Bar"})
doc_id = d["doc_id"]
tree = call("doc_tree", {"doc_id": doc_id})
call("doc_apply_ops", {"doc_id": doc_id, "base_revision": tree.get("revision", 0),
                       "ops": [{"op": "add_feature", "feature": {
                           "type": "box", "name": "Bar",
                           "params": {"length": 40, "width": 6, "height": 6}}}],
                       "with_tess": False})
bar_path = os.path.join(tmp, "bar.step+")
call("doc_save", {"doc_id": doc_id, "path": bar_path})

asm = call("doc_new", {"kind": "assembly", "name": "RotTest"})["doc_id"]
ops = [
    {"op": "asm_insert_component", "source": {"path": bar_path}, "name": "Straight",
     "transform": placement_transform([0, 0, 0], [0, 0, 0]), "fixed": True},
    {"op": "asm_insert_component", "source": {"path": bar_path}, "name": "Yawed90",
     "transform": placement_transform([0, 0, 0], [0, 0, 90]), "fixed": False},
]
atree = call("doc_tree", {"doc_id": asm})
call("doc_apply_ops", {"doc_id": asm, "base_revision": atree.get("revision", 0),
                       "ops": ops, "with_tess": False})

world = call("asm_world_bodies", {"doc_id": asm})
by_name = {b["name"]: b["bbox"]["size"] for b in world["bodies"]}
print("world sizes:", json.dumps(by_name))
s_straight = by_name["Straight"]
s_yawed = by_name["Yawed90"]
# Straight bar: X≈40, Y≈6.   Yawed-90 bar: X≈6, Y≈40 (extents swapped by the rotation).
assert abs(s_straight[0] - 40) < 0.6 and abs(s_straight[1] - 6) < 0.6, f"straight wrong: {s_straight}"
assert abs(s_yawed[0] - 6) < 0.6 and abs(s_yawed[1] - 40) < 0.6, f"yaw not applied: {s_yawed}"
print("PASS: placement.rot is honored — a 90 deg yaw swaps the part's world X/Y extents")
