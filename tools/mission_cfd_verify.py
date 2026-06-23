"""Deterministic check of the mission-orchestrator CFD fix (no LLM).

The mission orchestrator chains design/assembly -> simulate_flow. The simulate_flow step runs
agent_simulate_v1, which tessellates the doc and runs the wind tunnel. Before this change,
doc_tessellate_bodies used compile_document for ALL docs — but assembly geometry resolves through
compile_assembly, so an assembly doc tessellated to NOTHING and CFD refused with "no solid
geometry". This harness reproduces the exact path: build parts -> assemble -> agent_simulate_v1,
and asserts the assembly produces real forces (frontal area > 0). It also checks a single part
still simulates (regression).

Uses the freshly-built core DLL directly (no dist staging required).
"""
import ctypes, json, os, tempfile

CORE = r"C:\Users\nigga\Desktop\AI-opencad\core\build-occt\bin\Release"
os.add_dll_directory(CORE)
lib = ctypes.WinDLL(os.path.join(CORE, "libaicad.dll"))
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


def build_part(name, ops):
    doc_id = call("doc_new", {"kind": "part", "name": name})["doc_id"]
    tree = call("doc_tree", {"doc_id": doc_id})
    call("doc_apply_ops", {"doc_id": doc_id, "base_revision": tree.get("revision", 0),
                           "ops": ops, "with_tess": False})
    return doc_id


tmp = tempfile.mkdtemp(prefix="mission_cfd_")

# 1) Single part still simulates (regression on the non-assembly tessellate path).
part = build_part("Wing", [{"op": "add_feature", "feature": {
    "type": "box", "name": "Slab", "params": {"length": 120, "width": 40, "height": 6}}}])
r1 = call("agent_simulate_v1", {"doc_id": part, "request": "wind tunnel at 15 m/s in air"})
print(f"[part] ok={r1.get('ok')} area={r1.get('frontal_area_mm2')} drag={r1.get('drag_n')}")
assert r1.get("ok") and r1.get("frontal_area_mm2", 0) > 0, f"single-part CFD failed: {r1}"
print("PASS: single part simulates")

# 2) Assembly of two parts simulates (THE fix — previously returned empty bodies -> refusal).
base = build_part("Body", [{"op": "add_feature", "feature": {
    "type": "box", "name": "Fuse", "params": {"length": 160, "width": 24, "height": 24}}}])
wing = build_part("Wing", [{"op": "add_feature", "feature": {
    "type": "box", "name": "Plank", "params": {"length": 40, "width": 200, "height": 5}}}])
base_path = os.path.join(tmp, "part_0.step+")
wing_path = os.path.join(tmp, "part_1.step+")
call("doc_save", {"doc_id": base, "path": base_path})
call("doc_save", {"doc_id": wing, "path": wing_path})

asm = call("doc_new", {"kind": "assembly", "name": "Plane"})["doc_id"]
ident = lambda px, py, pz: [1, 0, 0, px, 0, 1, 0, py, 0, 0, 1, pz, 0, 0, 0, 1]
comp_ops = [
    {"op": "asm_insert_component", "source": {"path": base_path}, "name": "Body",
     "transform": ident(0, 0, 0), "fixed": True},
    {"op": "asm_insert_component", "source": {"path": wing_path}, "name": "Wing",
     "transform": ident(60, 0, 12), "fixed": False},
]
atree = call("doc_tree", {"doc_id": asm})
call("doc_apply_ops", {"doc_id": asm, "base_revision": atree.get("revision", 0),
                       "ops": comp_ops, "with_tess": False})

r2 = call("agent_simulate_v1", {"doc_id": asm, "request": "wind tunnel at 15 m/s, 4 deg AoA in air"})
print(f"[asm]  ok={r2.get('ok')} area={r2.get('frontal_area_mm2')} drag={r2.get('drag_n')} "
      f"lift={r2.get('lift_n')} answer={r2.get('answer','')[:80]!r}")
assert r2.get("ok"), f"assembly CFD reported not-ok: {r2}"
assert r2.get("frontal_area_mm2", 0) > 0, f"assembly tessellated to nothing (the bug): {r2}"
print("PASS: assembly simulates — doc_tessellate_bodies resolves assembly component geometry")
print("\nALL PASS: the mission CFD path works on both single parts and assemblies.")
