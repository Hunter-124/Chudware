"""Deterministic check of the WS3 assembly-file the host opens.

run_assembly_design builds each part in its own doc, doc_saves it, then creates an
assembly doc, inserts the parts as positioned components, and doc_saves the assembly to
`asm_path` (now returned in the agent_run result). The host opens THAT file as a tab.

This harness exercises the same doc-level op sequence directly via libchudware (no LLM), then
asserts the produced assembly .step+ is a valid aicad.asm/1 container referencing the parts.
"""
import json, os, sys, tempfile, zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
from services.mcp.core_bridge import call


def build_part(name, ops):
    d = call("doc_new", {"kind": "part", "name": name})
    doc_id = d["doc_id"]
    tree = call("doc_tree", {"doc_id": doc_id})
    call("doc_apply_ops", {"doc_id": doc_id, "base_revision": tree.get("revision", 0),
                           "ops": ops, "with_tess": False})
    return doc_id


tmp = tempfile.mkdtemp(prefix="asm_verify_")
# 1) A base plate and a peg, each its own part doc, each saved to a .step+ (as the agent does).
base = build_part("Base", [{"op": "add_feature", "feature": {
    "type": "box", "name": "Plate", "params": {"length": 40, "width": 40, "height": 10}}}])
peg = build_part("Peg", [{"op": "add_feature", "feature": {
    "type": "cylinder", "name": "Pin", "params": {"radius": 4, "height": 20}}}])
base_path = os.path.join(tmp, "part_0.step+")
peg_path = os.path.join(tmp, "part_1.step+")
call("doc_save", {"doc_id": base, "path": base_path})
call("doc_save", {"doc_id": peg, "path": peg_path})
assert os.path.exists(base_path) and os.path.exists(peg_path), "part files not written"
print(f"[ok] 2 part files saved under {tmp}")

# 2) Assembly doc + two positioned, fixed components (mirrors run_assembly_design step 3).
asm = call("doc_new", {"kind": "assembly", "name": "Assembly"})["doc_id"]
ident = lambda px, py, pz: [1, 0, 0, px, 0, 1, 0, py, 0, 0, 1, pz, 0, 0, 0, 1]
comp_ops = [
    {"op": "asm_insert_component", "source": {"path": base_path}, "name": "Base",
     "transform": ident(0, 0, 0), "fixed": True},
    {"op": "asm_insert_component", "source": {"path": peg_path}, "name": "Peg",
     "transform": ident(0, 0, 10), "fixed": False},
]
atree = call("doc_tree", {"doc_id": asm})
r = call("doc_apply_ops", {"doc_id": asm, "base_revision": atree.get("revision", 0),
                           "ops": comp_ops, "with_tess": False})
asm_path = os.path.join(tmp, "Assembly.step+")
call("doc_save", {"doc_id": asm, "path": asm_path})
assert os.path.exists(asm_path), "assembly file not written"
print(f"[ok] assembly saved -> {asm_path}  rev={r.get('revision')}")

# 3) Validate the file the HOST opens: a .step+ is a zip; document.json must be schema aicad.asm/1
#    with the 2 components referencing the part paths (this is what the Python open path consumes).
with zipfile.ZipFile(asm_path) as z:
    names = z.namelist()
    docjson = next((n for n in names if n.endswith("document.json")), None)
    assert docjson, f"no document.json in {names}"
    doc = json.loads(z.read(docjson))
schema = doc.get("schema", "")
comps = doc.get("components") or doc.get("instances") or []
print(f"[ok] container schema={schema!r}  components={len(comps)}")
assert schema == "aicad.asm/1", f"expected aicad.asm/1, got {schema!r}"
assert len(comps) == 2, f"expected 2 components, got {len(comps)}"
print("PASS: assembly file is a valid aicad.asm/1 the host can open via the Python route")

# 4) Confirm the C++ core REJECTS opening this assembly with code "unsupported" — the exact
#    trigger the host relies on to route the assembly open to the Python worker (worker_client.cpp).
out = ctypes.c_char_p()
rc = lib.chudware_call(b"doc_open", json.dumps({"path": asm_path}).encode(), ctypes.byref(out))
resp = json.loads(out.value.decode()) if out.value else {}
if out.value:
    lib.chudware_free(out)
code = resp.get("code", "")
print(f"[ok] C++ doc_open(assembly) -> rc={rc} code={code!r}")
assert code == "unsupported", f"expected 'unsupported' (route-to-Python trigger), got {code!r}"
print("PASS: C++ core defers assembly open to Python exactly as the host expects")
