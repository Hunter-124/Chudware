"""Deterministic save/reopen check of a native assembly .step+ (no LLM, no daemon).

run_assembly_design builds each part in its own doc, doc_saves it, then creates an
assembly doc, inserts the parts as positioned components, and doc_saves the assembly to
`asm_path`; the host opens THAT file natively as a tab.

This harness drives the same public doc RPCs in an isolated in-process engine
(CHUDWARE_ENGINE_DAEMON=0), closes every live doc, reopens the saved assembly with
doc_open, and proves the round trip preserved the components, their placements and the
world geometry: per-component volume and centre of mass equal the part's own mass
properties carried through its 4x4 placement, and the exact world bbox equals the union
of the placed part bboxes. Temporary files are removed on success and on failure.
"""
import json, math, os, shutil, sys, tempfile, zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CHUDWARE_ENGINE_DAEMON"] = "0"  # never attach to a live hostd/GUI engine
from services.mcp.core_bridge import call

ASM_SCHEMA = "aicad.asm/1"
# Row-major 4x4 placements (rotation applied first, then translation).
BASE_POSE = [1, 0, 0, 5, 0, 1, 0, -3, 0, 0, 1, 0, 0, 0, 0, 1]
PEG_POSE = [1, 0, 0, 20, 0, 0, -1, 25, 0, 1, 0, 10, 0, 0, 0, 1]  # +90 deg about X


def place(pose, p):
    return [sum(pose[r * 4 + k] * p[k] for k in range(3)) + pose[r * 4 + 3] for r in range(3)]


def placed_bbox(pose, bbox):
    lo, hi = bbox["min"], bbox["max"]
    corners = [place(pose, [x, y, z]) for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
               for z in (lo[2], hi[2])]
    return ([min(c[i] for c in corners) for i in range(3)],
            [max(c[i] for c in corners) for i in range(3)])


def close_vec(a, b, tol):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def build_part(name, feature, path):
    doc_id = call("doc_new", {"kind": "part", "name": name})["doc_id"]
    tree = call("doc_tree", {"doc_id": doc_id})
    call("doc_apply_ops", {"doc_id": doc_id, "base_revision": tree.get("revision", 0),
                           "ops": [{"op": "add_feature", "feature": feature}],
                           "with_tess": False})
    mass = call("eval_mass", {"doc_id": doc_id})
    assert mass.get("body_count") == 1, f"{name}: expected one solid, got {mass}"
    ref = {"volume": mass["volume_mm3"], "com": mass["com"],
           "bbox": call("doc_bbox", {"doc_id": doc_id})}
    call("doc_save", {"doc_id": doc_id, "path": path})
    assert os.path.exists(path), f"{name}: part file not written"
    return doc_id, ref


def resolved_source(asm_path, component):
    src = (component.get("source") or {}).get("path", "")
    if src and not os.path.isabs(src):
        src = os.path.join(os.path.dirname(asm_path), src)
    return os.path.normpath(src)


def snapshot(doc_id, asm_path):
    """Public-API view of an assembly: tree components, world bodies, mass, exact bbox."""
    tree = call("doc_tree", {"doc_id": doc_id})["tree"]
    comps = [{"id": c["id"], "name": c.get("name"), "fixed": c.get("fixed"),
              "transform": [float(v) for v in c["transform"]],
              "source": resolved_source(asm_path, c)} for c in tree.get("components", [])]
    world = call("asm_world_bodies", {"doc_id": doc_id, "explode": False})["bodies"]
    mass = call("eval_mass", {"doc_id": doc_id})
    return {"schema": tree.get("schema"), "components": comps, "world": world,
            "mass": mass, "bbox": call("doc_bbox", {"doc_id": doc_id})}


def check_geometry(label, snap, refs):
    """World geometry must equal each part's own measurements carried through its pose."""
    comps = {c["id"]: c for c in snap["components"]}
    world, bodies = snap["world"], snap["mass"].get("bodies") or []
    assert len(world) == 2 and len(bodies) == 2, f"{label}: expected 2 world solids: {snap}"
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for wb, mb in zip(world, bodies):  # both iterate the assembly result in body order
        comp = comps[wb["component"]]
        ref = refs[comp["name"]]
        pose = comp["transform"]
        assert math.isclose(mb["volume_mm3"], ref["volume"], rel_tol=1e-9), \
            f"{label}: {comp['name']} volume {mb['volume_mm3']} != part {ref['volume']}"
        expect_com = place(pose, ref["com"])
        assert close_vec(mb["com"], expect_com, 1e-6), \
            f"{label}: {comp['name']} centre of mass {mb['com']} != placed {expect_com}"
        blo, bhi = placed_bbox(pose, ref["bbox"])
        lo = [min(a, b) for a, b in zip(lo, blo)]
        hi = [max(a, b) for a, b in zip(hi, bhi)]
    assert close_vec(snap["bbox"]["min"], lo, 1e-4) and close_vec(snap["bbox"]["max"], hi, 1e-4), \
        f"{label}: world bbox {snap['bbox']} != placed part union {lo}..{hi}"


def main():
    tmp = tempfile.mkdtemp(prefix="asm_verify_")
    open_docs = []
    try:
        base_path = os.path.join(tmp, "part_0.step+")
        peg_path = os.path.join(tmp, "part_1.step+")
        base, base_ref = build_part("Base", {"type": "box", "name": "Plate", "params": {
            "length": 40, "width": 40, "height": 10}}, base_path)
        open_docs.append(base)
        peg, peg_ref = build_part("Peg", {"type": "cylinder", "name": "Pin", "params": {
            "radius": 4, "height": 20}}, peg_path)
        open_docs.append(peg)
        refs = {"Base": base_ref, "Peg": peg_ref}
        print(f"[ok] 2 part files saved under {tmp}")

        asm = call("doc_new", {"kind": "assembly", "name": "Assembly"})["doc_id"]
        open_docs.append(asm)
        atree = call("doc_tree", {"doc_id": asm})
        call("doc_apply_ops", {"doc_id": asm, "base_revision": atree.get("revision", 0),
                               "with_tess": False, "ops": [
            {"op": "asm_insert_component", "source": {"path": base_path}, "name": "Base",
             "transform": BASE_POSE, "fixed": True},
            {"op": "asm_insert_component", "source": {"path": peg_path}, "name": "Peg",
             "transform": PEG_POSE, "fixed": False},
        ]})
        asm_path = os.path.join(tmp, "Assembly.step+")
        call("doc_save", {"doc_id": asm, "path": asm_path})
        assert os.path.exists(asm_path), "assembly file not written"
        before = snapshot(asm, asm_path)
        expected = [("Base", True, BASE_POSE, base_path), ("Peg", False, PEG_POSE, peg_path)]
        assert len(before["components"]) == len(expected), before["components"]
        for comp, (name, fixed, pose, path) in zip(before["components"], expected):
            assert (comp["name"], comp["fixed"], comp["source"]) == \
                (name, fixed, os.path.normpath(path)), comp
            assert close_vec(comp["transform"], pose, 1e-9), comp
        check_geometry("saved", before, refs)
        print(f"[ok] assembly saved -> {asm_path}")

        with zipfile.ZipFile(asm_path) as z:
            names = z.namelist()
            docjson = next((n for n in names if n.endswith("document.json")), None)
            assert docjson, f"no document.json in {names}"
            stored = json.loads(z.read(docjson))
        assert stored.get("schema") == ASM_SCHEMA, f"container schema {stored.get('schema')!r}"
        assert len(stored.get("components") or []) == 2, stored.get("components")
        print(f"[ok] container schema={ASM_SCHEMA!r} components=2")

        # Reopen from disk only: no live part or assembly session may feed the result.
        while open_docs:
            call("doc_close", {"doc_id": open_docs.pop()})
        reopened = call("doc_open", {"path": asm_path})["doc_id"]
        open_docs.append(reopened)
        assert reopened != asm, "doc_open returned the closed assembly session"
        after = snapshot(reopened, asm_path)
        assert after["schema"] == ASM_SCHEMA, f"reopened schema {after['schema']!r}"
        assert after["components"] == before["components"], \
            f"components changed across save/reopen:\n{before['components']}\n{after['components']}"
        check_geometry("reopened", after, refs)
        for wa, wb in zip(before["world"], after["world"]):
            assert wa["component"] == wb["component"], (wa, wb)
            assert close_vec(wa["bbox"]["min"], wb["bbox"]["min"], 1e-6) and \
                close_vec(wa["bbox"]["max"], wb["bbox"]["max"], 1e-6), (wa, wb)
        assert close_vec(after["bbox"]["min"], before["bbox"]["min"], 1e-9) and \
            close_vec(after["bbox"]["max"], before["bbox"]["max"], 1e-9), (before["bbox"], after["bbox"])
        print(f"[ok] reopened {reopened}: components, placements, mass and world bbox "
              f"{after['bbox']['min']}..{after['bbox']['max']} match the saved assembly")
        print("PASS: native doc_open reproduces the saved assembly exactly")
    finally:
        while open_docs:
            call("doc_close", {"doc_id": open_docs.pop()})
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
