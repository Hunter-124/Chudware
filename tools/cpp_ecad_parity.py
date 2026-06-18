"""cpp_ecad_parity.py — Python-vs-C++ ECAD document/session parity.

Companion to cpp_session_parity.py (CAD docs). Drives the SAME ECAD op batches
through the Python session (engine.ecad.session / engine.ecad.rpc) and the C++
core (libaicad.dll: ecad_new / ecad_apply_ops / ecad_undo / ecad_redo / ecad_tree
/ ecad_describe) and compares the resulting document tree + revision + id_map +
kind for EXACT structural equality.

Op sequences are taken from engine/ecad/test_ops.py + test_rpc.py +
test_hierarchy.py (the fixture source).

Scope: the document/ops layer ONLY. The `compile` field (netlist/ERC/DRC) is
DEFERRED to a later wave, so it is normalized out of the tree compare on both
sides; everything else (every entity the ops mutate) must match byte-for-byte
after normalizing the random document id.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_ecad_parity.py

Exit code = number of mismatched steps (0 = full parity).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- normalize
def norm_tree(tree: dict) -> dict:
    """Canonicalize a document tree for cross-engine compare: pin the random doc
    `id` to a constant (Python=uuid4, C++=session id) and strip each zone's
    `filled_polys`. The copper-pour fill is produced by the COMPILE layer
    (engine.ecad.zonefill, run inside session._compile) which is DEFERRED in this
    C++ foundation wave — so Python stamps `filled_polys` onto a zone after an
    add/edit while the C++ session does not. That is a compile-layer artifact, not
    an ops divergence; everything the OPS write (id/net/layer/polygon/clearance/
    fill/thermal) is compared as-is."""
    import copy
    t = copy.deepcopy(tree)
    t["id"] = "<id>"
    for z in t.get("zones", []):
        z.pop("filled_polys", None)
    return t


def cmp_resp(label, py, cpp, *, check_warn=False):
    diffs = []
    if py.get("revision") != cpp.get("revision"):
        diffs.append(f"rev {py.get('revision')}!={cpp.get('revision')}")
    if py.get("kind") != cpp.get("kind"):
        diffs.append(f"kind {py.get('kind')}!={cpp.get('kind')}")
    if py.get("id_map") != cpp.get("id_map"):
        diffs.append(f"id_map {py.get('id_map')}!={cpp.get('id_map')}")
    pt, ct = norm_tree(py.get("tree", {})), norm_tree(cpp.get("tree", {}))
    if pt != ct:
        for k in sorted(set(pt) | set(ct)):
            if pt.get(k) != ct.get(k):
                diffs.append(f"tree.{k} differ")
    if check_warn and bool(py.get("warnings")) != bool(cpp.get("warnings")):
        diffs.append("warnings presence differ")
    ok = not diffs
    print(f"  {label:<34} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:120]}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- engines
def py_methods():
    from engine.ecad.rpc import METHODS

    def noop(_):
        pass

    class PyRpc:
        def call(self, method, params):
            return METHODS[method](params, noop)
    return PyRpc()


class CppRpc:
    def __init__(self, core):
        self.core = core

    def call(self, method, params):
        rc, r = self.core.call(method, params)
        if rc != 0 and r.get("code"):
            raise _CppErr(r)
        return r


class _CppErr(Exception):
    def __init__(self, env):
        self.env = env
        super().__init__(env.get("message", ""))


# --------------------------------------------------------------------------- fixtures
def _res_def():
    return {
        "libid": "Device:R", "name": "R", "description": "Resistor",
        "reference_prefix": "R", "value": "R", "units_count": 1, "power": False,
        "pins": [
            {"number": "1", "name": "1", "pos": [0.0, 2.54], "angle": 90.0,
             "length": 2.54, "type": "passive", "unit": 1},
            {"number": "2", "name": "2", "pos": [0.0, -2.54], "angle": 270.0,
             "length": 2.54, "type": "passive", "unit": 1},
        ],
    }


def _add_R(pos, ref=None):
    op = {"op": "sch_add_symbol", "libid": "Device:R", "pos": pos}
    if ref is not None:
        op["ref"] = ref
    return op


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[ecad-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[ecad-parity] libaicad ABI={core.abi()}")
    py, cpp = py_methods(), CppRpc(core)
    mism = 0
    total = 0

    def step(diff):
        nonlocal mism, total
        total += 1
        mism += diff
        return diff

    def new(kind, name=None):
        p = {"kind": kind}
        if name is not None:
            p["name"] = name
        return py.call("ecad_new", p), cpp.call("ecad_new", p)

    def apply(pid, cid, base, ops):
        pr = py.call("ecad_apply_ops", {"doc_id": pid, "base_revision": base, "ops": ops})
        cr = cpp.call("ecad_apply_ops", {"doc_id": cid, "base_revision": base, "ops": ops})
        return pr, cr

    # The C++ stub for an unresolved libid (Device:R) injects a 2-pin "U" stub def
    # into lib_cache; Python's session does the same (library module absent) ->
    # identical stub. To exercise pin_world / sch_connect with a KNOWN def, we seed
    # the SAME explicit `def` on the add op so both engines store it verbatim.
    RDEF = _res_def()

    # ---- 1) schematic: new + placeholder batch (symbol $0 + sch_connect via $0) ----
    pr0, cr0 = new("sch", "TestSch")
    step(cmp_resp("sch new", pr0, cr0))
    pid, cid = pr0["doc_id"], cr0["doc_id"]
    batch = [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10.0, 10.0], "def": RDEF},
        {"op": "sch_connect", "from": {"sym": "$0", "pin": "1"}, "to": {"sym": "$0", "pin": "2"}},
    ]
    pr, cr = apply(pid, cid, 0, batch)
    step(cmp_resp("sch placeholder batch (s1+w1)", pr, cr))

    # ---- 2) add wire, then undo / redo / tree ----
    pr, cr = apply(pid, cid, 1, [{"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]}])
    step(cmp_resp("sch add_wire", pr, cr))
    pr, cr = py.call("ecad_undo", {"doc_id": pid}), cpp.call("ecad_undo", {"doc_id": cid})
    step(cmp_resp("sch undo", pr, cr))
    pr, cr = py.call("ecad_redo", {"doc_id": pid}), cpp.call("ecad_redo", {"doc_id": cid})
    step(cmp_resp("sch redo", pr, cr))
    pr = py.call("ecad_tree", {"doc_id": pid})
    cr = cpp.call("ecad_tree", {"doc_id": cid})
    # tree response has no id_map; compare rev/kind/tree.
    step(cmp_resp("sch tree", {**pr, "id_map": {}}, {**cr, "id_map": {}}))

    # ---- 3) patch edit: value + footprint, then clear footprint with null ----
    pe, ce = new("sch", "Patch")
    pid, cid = pe["doc_id"], ce["doc_id"]
    apply(pid, cid, 0, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [0.0, 0.0], "def": RDEF}])
    pr, cr = apply(pid, cid, 1, [{"op": "sch_edit_symbol", "id": "s1", "value": "10k",
                                  "footprint": "Resistor_SMD:R_0603"}])
    step(cmp_resp("sch edit_symbol set", pr, cr))
    pr, cr = apply(pid, cid, 2, [{"op": "sch_edit_symbol", "id": "s1", "footprint": None}])
    step(cmp_resp("sch edit_symbol clear", pr, cr))

    # ---- 4) delete symbol (wire left dangling) + delete wire ----
    de, dce = new("sch", "Del")
    pid, cid = de["doc_id"], dce["doc_id"]
    apply(pid, cid, 0, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0], "def": RDEF},
                        {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]}])
    pr, cr = apply(pid, cid, 1, [{"op": "sch_delete_symbol", "id": "s1"}])
    step(cmp_resp("sch delete_symbol", pr, cr))
    pr, cr = apply(pid, cid, 2, [{"op": "sch_delete_wire", "id": "w1"}])
    step(cmp_resp("sch delete_wire", pr, cr))

    # ---- 5) auto-ref assignment (R1, explicit R5, then R2, R3) ----
    ae, ace = new("sch", "AutoRef")
    pid, cid = ae["doc_id"], ace["doc_id"]
    pr, cr = apply(pid, cid, 0, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0], "def": RDEF}])
    step(cmp_resp("sch auto-ref R1", pr, cr))
    pr, cr = apply(pid, cid, 1, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [10, 0],
                                  "ref": "R5", "def": RDEF}])
    step(cmp_resp("sch explicit R5", pr, cr))
    pr, cr = apply(pid, cid, 2, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [20, 0], "def": RDEF}])
    step(cmp_resp("sch auto-ref R2 (skips R5)", pr, cr))

    # ---- 6) sch_annotate reset ----
    sa, sca = new("sch", "Annot")
    pid, cid = sa["doc_id"], sca["doc_id"]
    apply(pid, cid, 0, [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [30, 0], "ref": "R9", "def": RDEF},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10, 0], "ref": "R7", "def": RDEF},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [20, 0], "ref": "R8", "def": RDEF}])
    pr, cr = apply(pid, cid, 1, [{"op": "sch_annotate", "scope": "all", "mode": "reset"}])
    step(cmp_resp("sch annotate reset", pr, cr))

    # ---- 7) shared: rename_doc + set_field + nop ----
    she, sce = new("sch", "Shared")
    pid, cid = she["doc_id"], sce["doc_id"]
    apply(pid, cid, 0, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0], "def": RDEF}])
    pr, cr = apply(pid, cid, 1, [
        {"op": "rename_doc", "name": "Renamed"},
        {"op": "set_field", "target": {"kind": "symbols", "id": "s1"}, "key": "MPN",
         "value": "RC0603FR-0710KL"}])
    step(cmp_resp("sch rename+set_field", pr, cr))
    pr, cr = apply(pid, cid, 2, [{"op": "nop"}])
    step(cmp_resp("sch nop", pr, cr))

    # ---- 8) PCB batch: outline + footprint(inline def) + track ----
    fp_def = {"fpid": "R_0603", "value": "10k", "attr": "smd", "pads": [
        {"id": "p1", "number": "1", "pos": [-0.8, 0.0], "angle": 0.0, "shape": "roundrect",
         "size": [0.9, 0.95], "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "net": "GND"},
        {"id": "p2", "number": "2", "pos": [0.8, 0.0], "angle": 0.0, "shape": "roundrect",
         "size": [0.9, 0.95], "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "net": "GND"},
    ]}
    outline = [
        {"type": "line", "p1": [0.0, 0.0], "p2": [50.0, 0.0]},
        {"type": "line", "p1": [50.0, 0.0], "p2": [50.0, 40.0]},
        {"type": "line", "p1": [50.0, 40.0], "p2": [0.0, 40.0]},
        {"type": "line", "p1": [0.0, 40.0], "p2": [0.0, 0.0]},
    ]
    pb, cpb = new("pcb", "PCBTest")
    step(cmp_resp("pcb new", pb, cpb))
    pid, cid = pb["doc_id"], cpb["doc_id"]
    pr, cr = apply(pid, cid, 0, [
        {"op": "pcb_set_outline", "shapes": outline, "closed": True},
        {"op": "pcb_add_footprint", "fpid": "R_0603", "pos": [10.0, 10.0], "ref": "R1", "def": fp_def},
        {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "p1": [10.0, 10.0], "p2": [20.0, 10.0]},
    ])
    step(cmp_resp("pcb outline+fp1+t1", pr, cr))

    # ---- 9) pcb_add_tracks polyline (ids $0, $0#1) ----
    pt2, cpt2 = new("pcb", "Poly")
    pid, cid = pt2["doc_id"], cpt2["doc_id"]
    pr, cr = apply(pid, cid, 0, [{"op": "pcb_add_tracks", "net": "GND", "layer": "F.Cu",
                                  "pts": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]}])
    step(cmp_resp("pcb add_tracks polyline", pr, cr))

    # ---- 10) pcb_add_via + pcb_set_stackup + pcb_add_zone + edit_zone ----
    pv, cpv = new("pcb", "PcbMore")
    pid, cid = pv["doc_id"], cpv["doc_id"]
    pr, cr = apply(pid, cid, 0, [{"op": "pcb_add_via", "net": "GND", "at": [5.0, 5.0]}])
    step(cmp_resp("pcb add_via", pr, cr))
    pr, cr = apply(pid, cid, 1, [{"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
                                  "polygon": [[1, 1], [39, 1], [39, 19], [1, 19]]}])
    step(cmp_resp("pcb add_zone", pr, cr))
    pr, cr = apply(pid, cid, 2, [{"op": "pcb_edit_zone", "id": "z1", "clearance": 0.5}])
    step(cmp_resp("pcb edit_zone", pr, cr))
    pr, cr = apply(pid, cid, 3, [{"op": "pcb_set_rules", "clearance": 0.25}])
    step(cmp_resp("pcb set_rules", pr, cr))

    # ---- 11) hierarchy: sch_add_sheet + sch_add_sheet_symbol + sch_add_sheet_pin ----
    he, hce = new("sch", "Hier")
    pid, cid = he["doc_id"], hce["doc_id"]
    batch = [
        {"op": "sch_add_sheet", "name": "child", "parent": "sh1"},
        {"op": "sch_add_symbol", "libid": "Device:R", "sheet": "sh1", "pos": [0.0, -2.54], "def": RDEF},
        {"op": "sch_add_wire", "sheet": "sh1", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        {"op": "sch_add_sheet_symbol", "sheet": "sh1", "child": "$0", "pos": [10.0, 0.0],
         "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]},
        {"op": "sch_add_symbol", "libid": "Device:R", "sheet": "$0", "pos": [0.0, -2.54], "def": RDEF},
        {"op": "sch_add_wire", "sheet": "$0", "pts": [[0.0, 0.0], [20.0, 0.0]]},
        {"op": "sch_add_label", "sheet": "$0", "pos": [20.0, 0.0], "text": "CLK", "scope": "hier"},
    ]
    pr, cr = apply(pid, cid, 0, batch)
    step(cmp_resp("sch hierarchy build", pr, cr))
    hs_id = pr["tree"]["sheet_symbols"][0]["id"]
    pr, cr = apply(pid, cid, 1, [{"op": "sch_add_sheet_pin", "id": hs_id, "name": "RST",
                                  "pos": [10.0, 5.0], "shape": "output"}])
    step(cmp_resp("sch add_sheet_pin", pr, cr))

    # ---- 12) ecad_describe parity ----
    pd = py.call("ecad_describe", {"doc_id": pid})
    cd = cpp.call("ecad_describe", {"doc_id": cid})
    dd = []
    for k in ("kind", "name", "revision", "symbols", "wires"):
        if pd.get(k) != cd.get(k):
            dd.append(f"{k} {pd.get(k)}!={cd.get(k)}")
    print(f"  {'sch describe':<34} {'MATCH' if not dd else 'DIFF: ' + '; '.join(dd)}")
    step(0 if not dd else 1)

    # ---- 13) error parity: stale revision / atomic-validation / unknown object ----
    ee, ece = new("sch", "Err")
    pid, cid = ee["doc_id"], ece["doc_id"]
    apply(pid, cid, 0, [{"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0], "def": RDEF}])

    def err_code_py(method, params):
        from engine.ecad.ops import OpError
        try:
            py.call(method, params)
            return None
        except OpError as e:
            return e.code

    def err_code_cpp(method, params):
        try:
            cpp.call(method, params)
            return None
        except _CppErr as e:
            return e.env.get("code")

    err_cases = [
        ("stale_revision", "ecad_apply_ops",
         {"doc_id": "<P>", "base_revision": 0, "ops": [_add_R([1, 1])]}, "stale_revision"),
        ("atomic bad mirror", "ecad_apply_ops",
         {"doc_id": "<P>", "base_revision": 1, "ops": [
             {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0, 0], "mirror": "diagonal",
              "def": RDEF}]}, "invalid_op"),
        ("unknown symbol edit", "ecad_apply_ops",
         {"doc_id": "<P>", "base_revision": 1, "ops": [
             {"op": "sch_edit_symbol", "id": "s999", "value": "x"}]}, "unknown_object"),
        ("forward placeholder", "ecad_apply_ops",
         {"doc_id": "<P>", "base_revision": 1, "ops": [
             {"op": "sch_edit_symbol", "id": "$0", "value": "1k"}]}, "invalid_op"),
    ]
    for label, method, params, want in err_cases:
        pp = dict(params); pp["doc_id"] = pid
        cp = dict(params); cp["doc_id"] = cid
        pc = err_code_py(method, pp)
        cc = err_code_cpp(method, cp)
        ok = pc == cc == want
        print(f"  {('err ' + label):<34} {'MATCH' if ok else f'DIFF py={pc} cpp={cc} want={want}'}")
        step(0 if ok else 1)

    # ---- 14) pcb error parity: outline-not-closed / bad layer / bad net ----
    perr, cperr = new("pcb", "PErr")
    pid, cid = perr["doc_id"], cperr["doc_id"]
    pcb_err = [
        ("outline_not_closed", [{"op": "pcb_set_outline", "closed": True, "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"type": "line", "p1": [10.0, 0.0], "p2": [10.0, 10.0]}]}], "outline_not_closed"),
        ("track bad layer", [{"op": "pcb_add_track", "net": "GND", "layer": "F.SilkS",
                              "p1": [0, 0], "p2": [1, 0]}], "layer_not_in_stackup"),
        ("track bad net", [{"op": "pcb_add_track", "net": "NOPE", "layer": "F.Cu",
                            "p1": [0, 0], "p2": [1, 0]}], "net_not_found"),
    ]
    for label, ops, want in pcb_err:
        pc = err_code_py("ecad_apply_ops", {"doc_id": pid, "base_revision": 0, "ops": ops})
        cc = err_code_cpp("ecad_apply_ops", {"doc_id": cid, "base_revision": 0, "ops": ops})
        ok = pc == cc == want
        print(f"  {('err pcb ' + label):<34} {'MATCH' if ok else f'DIFF py={pc} cpp={cc} want={want}'}")
        step(0 if ok else 1)

    print(f"\n[ecad-parity] {total - mism}/{total} steps at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
