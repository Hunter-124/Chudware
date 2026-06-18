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
    `id` to a constant (Python=uuid4, C++=session id). The copper-pour
    `filled_polys` ARE now compared (both engines run the zone fill inside the
    board compile — engine.ecad.zonefill / core/ecad/zonefill.cpp), so they are NO
    LONGER stripped: every byte the ops + compile write (id/net/layer/polygon/
    clearance/fill/thermal AND the filled copper polygons) is compared as-is."""
    import copy
    t = copy.deepcopy(tree)
    t["id"] = "<id>"
    return t


def cmp_compile(label, py, cpp):
    """Compare the SCHEMATIC compile sub-fields EXACTLY: compile.netlist (nets /
    by_pin / components / warnings) + compile.erc (the ordered violation list).

    These are now computed in-process by the C++ core (core/ecad/netlist.cpp +
    erc.cpp) and must match the Python oracle (engine.ecad.netlist/erc) bit-for-bit
    — net naming, net ordering, ERC violation records + their order are all
    load-bearing. `stats` (esp. the non-deterministic `ms` timing) is NOT compared."""
    pc = py.get("compile", {}) or {}
    cc = cpp.get("compile", {}) or {}
    diffs = []
    if pc.get("netlist") != cc.get("netlist"):
        pn, cn = pc.get("netlist") or {}, cc.get("netlist") or {}
        for k in sorted(set(pn) | set(cn)):
            if pn.get(k) != cn.get(k):
                diffs.append(f"netlist.{k} differ")
    if pc.get("erc") != cc.get("erc"):
        diffs.append(f"erc differ (py={len(pc.get('erc') or [])} cpp={len(cc.get('erc') or [])})")
    ok = not diffs
    print(f"  {label:<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:140]}")
    return 0 if ok else 1


def cmp_compile_pcb(label, py, cpp):
    """Compare the BOARD compile sub-fields EXACTLY: compile.drc (the ordered DRC
    violation list — rule/severity/locs/items/message + ORDER), compile.ratsnest
    (the deterministic {net,p1,p2} airwires), and the per-zone filled_polys (the
    copper-pour geometry — vertex-for-vertex). Computed in-process by the C++ core
    (core/ecad/drc.cpp + zonefill.cpp) against the Python oracle (engine.ecad.drc /
    zonefill). Only the non-deterministic `stats.ms` is ignored; everything else is
    load-bearing and compared as-is."""
    pc = py.get("compile", {}) or {}
    cc = cpp.get("compile", {}) or {}
    diffs = []
    if pc.get("drc") != cc.get("drc"):
        pd, cd = pc.get("drc") or [], cc.get("drc") or []
        diffs.append(f"drc differ (py={len(pd)} cpp={len(cd)})")
        for i, (a, b) in enumerate(zip(pd, cd)):
            if a != b:
                diffs.append(f"drc[{i}] {a.get('rule')}/{a.get('message','')[:30]}")
                break
    if pc.get("ratsnest") != cc.get("ratsnest"):
        diffs.append(f"ratsnest differ (py={len(pc.get('ratsnest') or [])} "
                     f"cpp={len(cc.get('ratsnest') or [])})")
    # filled_polys live in the tree (the compile writes them back onto each zone).
    pz = {z.get("id"): z.get("filled_polys", []) for z in py.get("tree", {}).get("zones", [])}
    cz = {z.get("id"): z.get("filled_polys", []) for z in cpp.get("tree", {}).get("zones", [])}
    for zid in sorted(set(pz) | set(cz)):
        if pz.get(zid) != cz.get(zid):
            pn = len(pz.get(zid) or [])
            cn = len(cz.get(zid) or [])
            diffs.append(f"zone {zid} filled_polys differ (py={pn}p cpp={cn}p)")
    ok = not diffs
    print(f"  {label:<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:150]}")
    return 0 if ok else 1


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

    # ---- 15) SCHEMATIC COMPILE parity: netlist + ERC (engine.ecad.netlist/erc) ----
    # Fixtures mirror engine/ecad/test_netlist.py + test_hierarchy.py. The docs are
    # built via ops (so both engines hold an IDENTICAL tree — proven by the steps
    # above) and we then compare compile.netlist + compile.erc EXACTLY. Pin defs use
    # length-0 pins so the world tip == symbol.pos + pin.pos (easy hand-placement),
    # matching the oracle's test library.
    def _pin(number, name, pos, angle, ptype):
        return {"number": number, "name": name, "pos": list(pos), "angle": angle,
                "length": 0.0, "type": ptype, "unit": 1}

    R_DEF = {"libid": "Device:R", "name": "R", "description": "Resistor",
             "reference_prefix": "R", "value": "R", "power": False,
             "pins": [_pin("1", "~", [0.0, 2.54], 90.0, "passive"),
                      _pin("2", "~", [0.0, -2.54], -90.0, "passive")]}
    V_DEF = {"libid": "Sim:V", "name": "V", "description": "Voltage source",
             "reference_prefix": "V", "value": "V", "power": False,
             "pins": [_pin("1", "+", [0.0, 2.54], 90.0, "power_out"),
                      _pin("2", "-", [0.0, -2.54], -90.0, "power_out")]}
    GND_DEF = {"libid": "power:GND", "name": "GND", "description": "Ground",
               "reference_prefix": "#PWR", "value": "GND", "power": True,
               "pins": [_pin("1", "GND", [0.0, 0.0], -90.0, "power_in")]}
    OUT_DEF = {"libid": "Logic:OUT", "name": "U", "description": "Output buffer",
               "reference_prefix": "U", "value": "U", "power": False,
               "pins": [_pin("1", "Y", [0.0, 0.0], 0.0, "output")]}

    def addsym(libid, pos, defn, ref=None, sheet=None):
        op = {"op": "sch_add_symbol", "libid": libid, "pos": list(pos), "def": defn}
        if ref is not None:
            op["ref"] = ref
        if sheet is not None:
            op["sheet"] = sheet
        return op

    def run_compile(label, batch):
        pp, cc = new("sch", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        pr, cr = apply(pid, cid, 0, batch)
        # also confirm the tree itself matched (input parity), then compare compile.
        step(cmp_resp(f"{label} tree", pr, cr))
        step(cmp_compile(f"{label} compile (netlist+erc)", pr, cr))

    # 15a voltage divider (auto N$ nets, three distinct nets, byte-stable naming)
    run_compile("divider", [
        addsym("Sim:V", (0.0, 12.54), V_DEF, ref="V1"),
        addsym("Device:R", (0.0, 7.46), R_DEF, ref="R1"),
        addsym("Device:R", (0.0, 2.38), R_DEF, ref="R2"),
        addsym("power:GND", (0.0, -0.16), GND_DEF, ref="#PWR01"),
        {"op": "sch_add_wire", "pts": [[0.0, 15.08], [0.0, -0.16]]},
    ])

    # 15b crossing without junction does NOT merge (two distinct N$ nets)
    run_compile("crossing", [
        addsym("Device:R", (-5.0, -2.54), R_DEF, ref="R1"),
        addsym("Device:R", (0.0, -7.54), R_DEF, ref="R2"),
        {"op": "sch_add_wire", "pts": [[-5.0, 0.0], [5.0, 0.0]]},
        {"op": "sch_add_wire", "pts": [[0.0, -5.0], [0.0, 5.0]]},
    ])

    # 15c junction at crossing DOES merge the two wires
    run_compile("junction", [
        addsym("Device:R", (-5.0, -2.54), R_DEF, ref="R1"),
        addsym("Device:R", (0.0, -7.54), R_DEF, ref="R2"),
        {"op": "sch_add_wire", "pts": [[-5.0, 0.0], [5.0, 0.0]]},
        {"op": "sch_add_wire", "pts": [[0.0, -5.0], [0.0, 5.0]]},
        {"op": "sch_add_junction", "pos": [0.0, 0.0]},
    ])

    # 15d global label merges two disjoint wires; net named CLK
    run_compile("global_label", [
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R1"),
        addsym("Device:R", (60.0, -2.54), R_DEF, ref="R2"),
        {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        {"op": "sch_add_wire", "pts": [[50.0, 0.0], [60.0, 0.0]]},
        {"op": "sch_add_label", "pos": [10.0, 0.0], "text": "CLK", "scope": "global"},
        {"op": "sch_add_label", "pos": [50.0, 0.0], "text": "CLK", "scope": "global"},
    ])

    # 15e power port priority over a local label (net=VCC wins)
    run_compile("power_priority", [
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R1"),
        {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        {"op": "sch_add_power", "pos": [5.0, 0.0], "net": "VCC"},
        {"op": "sch_add_label", "pos": [10.0, 0.0], "text": "MY_RAIL", "scope": "local"},
    ])

    # 15f ERC seeded faults: pin_not_connected, conflicting_drivers, power_not_driven,
    #     missing_footprint (full violation list + ordering). NOTE: duplicate_ref is
    #     NOT exercisable here — the ops layer rejects a duplicate non-'?' ref at apply
    #     time (schema validation), so a doc with two same-ref symbols can't be built
    #     through sch_add_symbol; the duplicate_ref RULE is ported but covered only by
    #     the hand-built Python oracle test (test_netlist.py::test_erc_seeded_faults).
    run_compile("erc_faults", [
        addsym("Device:R", (0.0, 0.0), R_DEF, ref="R1"),
        addsym("Logic:OUT", (20.0, 0.0), OUT_DEF, ref="U1"),
        addsym("Logic:OUT", (20.0, 0.0), OUT_DEF, ref="U2"),
        addsym("power:GND", (60.0, 0.0), GND_DEF, ref="#PWR02"),
        addsym("power:GND", (60.0, 0.0), GND_DEF, ref="#PWR03"),
    ])

    # 15g no_connect suppresses pin_not_connected on the covered pin
    run_compile("no_connect", [
        addsym("Device:R", (0.0, 0.0), R_DEF, ref="R1"),
        {"op": "sch_add_no_connect", "pos": [0.0, 2.54]},
    ])

    # ---- 16) HIERARCHY compile parity: cross-sheet bridge + hier ERC ----
    def run_compile_hier(label, batch, base_to=None):
        pp, cc = new("sch", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        pr, cr = apply(pid, cid, 0, batch)
        step(cmp_resp(f"{label} tree", pr, cr))
        step(cmp_compile(f"{label} compile (netlist+erc)", pr, cr))

    # 16a cross-sheet bridge: sheet pin CLK <-> child hier label CLK -> one net CLK
    run_compile_hier("hier_bridge", [
        {"op": "sch_add_sheet", "name": "child", "parent": "sh1"},
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R1", sheet="sh1"),
        {"op": "sch_add_wire", "sheet": "sh1", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        {"op": "sch_add_sheet_symbol", "sheet": "sh1", "child": "$0", "pos": [10.0, 0.0],
         "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]},
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R2", sheet="$0"),
        {"op": "sch_add_wire", "sheet": "$0", "pts": [[0.0, 0.0], [20.0, 0.0]]},
        {"op": "sch_add_label", "sheet": "$0", "pos": [20.0, 0.0], "text": "CLK", "scope": "hier"},
    ])

    # 16b hier mismatch ERC: sheet pin CLK with no child label + child label RST not exposed
    run_compile_hier("hier_mismatch", [
        {"op": "sch_add_sheet", "name": "child", "parent": "sh1"},
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R2", sheet="$0"),
        {"op": "sch_add_wire", "sheet": "$0", "pts": [[0.0, 0.0], [20.0, 0.0]]},
        {"op": "sch_add_label", "sheet": "$0", "pos": [20.0, 0.0], "text": "RST", "scope": "hier"},
        {"op": "sch_add_sheet_symbol", "sheet": "sh1", "child": "$0", "pos": [10.0, 0.0],
         "pins": [{"name": "CLK", "pos": [10.0, 0.0], "shape": "input"}]},
    ])

    # 16c local label does NOT span sheets (two distinct classes + collision warning)
    run_compile_hier("local_label_no_span", [
        {"op": "sch_add_sheet", "name": "child", "parent": "sh1"},
        addsym("Device:R", (0.0, -2.54), R_DEF, ref="R1", sheet="sh1"),
        addsym("Device:R", (100.0, -2.54), R_DEF, ref="R2", sheet="$0"),
        {"op": "sch_add_wire", "sheet": "sh1", "pts": [[0.0, 0.0], [5.0, 0.0]]},
        {"op": "sch_add_wire", "sheet": "$0", "pts": [[100.0, 0.0], [105.0, 0.0]]},
        {"op": "sch_add_label", "sheet": "sh1", "pos": [5.0, 0.0], "text": "DATA", "scope": "local"},
        {"op": "sch_add_label", "sheet": "$0", "pos": [105.0, 0.0], "text": "DATA", "scope": "local"},
    ])

    # ---- 17) BOARD COMPILE parity: DRC + ratsnest + zone fill ----
    # Fixtures mirror engine/ecad/test_drc.py + test_zonefill.py. Built via ops so
    # both engines hold an IDENTICAL tree; we then compare compile.drc +
    # compile.ratsnest + each zone's filled_polys EXACTLY (only stats.ms ignored).
    #
    # NOTE on nets: there is no `pcb_add_net` op and validate_board rejects any pad/
    # track/via/zone net not in the board net table (only "GND" by default). But a
    # null pad net validates fine, and same_net(None, None) is False (an unassigned
    # copper island still clears its neighbours), so DIFFERENT-net clearance is built
    # from null-net pads; SAME-net suppression / ratsnest / zones use registered
    # "GND". This exercises every DRC rule + every zonefill path (clearance carve,
    # thermal relief, SMD solid flood, multi-component trace) within the op vocab.

    def _pad(pid, pos, net, size=(0.5, 0.5), shape="circle", layer="F.Cu",
             ptype="smd", drill=None):
        p = {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
             "size": list(size), "type": ptype, "layers": [layer], "drill": drill, "net": net}
        if ptype in ("tht", "np_tht", "connector"):
            p["layers"] = ["F.Cu", "B.Cu"]
        return p

    def _fpdef(pads, attr="smd"):
        return {"fpid": "Test:FP", "value": "", "attr": attr, "pads": pads}

    def _rect_outline(w, h, x0=0.0, y0=0.0):
        x1, y1 = x0 + w, y0 + h
        return [
            {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
            {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
            {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
            {"type": "line", "p1": [x0, y1], "p2": [x0, y0]},
        ]

    def run_compile_pcb(label, batch):
        pp, cc = new("pcb", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        pr, cr = apply(pid, cid, 0, batch)
        step(cmp_resp(f"{label} tree", pr, cr))
        step(cmp_compile_pcb(f"{label} compile (drc+rats+fill)", pr, cr))

    # 17a clearance net-aware: two different-net (null) round pads 0.6mm apart -> 1.
    run_compile_pcb("drc_clearance", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), None)])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.6, 0.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), None)])},
    ])

    # 17b same-net pads close -> NO clearance (net-aware guarantee; both GND).
    run_compile_pcb("drc_same_net", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), "GND")])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.6, 0.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), "GND")])},
    ])

    # 17c track_width below minimum (GND track, width 0.10 < 0.15).
    run_compile_pcb("drc_track_width", [
        {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "p1": [0.0, 0.0],
         "p2": [5.0, 0.0], "width": 0.10},
    ])

    # 17d via_size (drill 0.1 < 0.2) + annular ring on a via.
    run_compile_pcb("drc_via", [
        {"op": "pcb_add_via", "net": "GND", "at": [0.0, 0.0], "drill": 0.45, "diameter": 0.5},
    ])
    run_compile_pcb("drc_via_drill", [
        {"op": "pcb_add_via", "net": "GND", "at": [0.0, 0.0], "drill": 0.1, "diameter": 0.8},
    ])

    # 17e annular ring on a tht pad (min dim 1.0, drill 0.9 -> ring 0.05 < 0.13).
    run_compile_pcb("drc_pad_annular", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), "GND", size=(1.0, 1.0), ptype="tht",
                             drill={"diameter": 0.9, "shape": "round", "offset": [0, 0]})],
                       attr="tht")},
    ])

    # 17f hole_to_hole: two tht pads, drills 0.5, centers 0.6 apart -> gap 0.1 < 0.25.
    run_compile_pcb("drc_hole_to_hole", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), None, size=(1.5, 1.5), ptype="tht",
                             drill={"diameter": 0.5, "shape": "round", "offset": [0, 0]})],
                       attr="tht")},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.6, 0.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), None, size=(1.5, 1.5), ptype="tht",
                             drill={"diameter": 0.5, "shape": "round", "offset": [0, 0]})],
                       attr="tht")},
    ])

    # 17g edge_clearance: pad copper touching the left board edge.
    run_compile_pcb("drc_edge", [
        {"op": "pcb_set_outline", "shapes": _rect_outline(20.0, 20.0), "closed": True},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.1, 10.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), None, size=(0.5, 0.5))])},
    ])

    # 17h ratsnest + unrouted: two unconnected same-net pads (rect shape -> box) far apart.
    run_compile_pcb("drc_ratsnest", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), "GND", size=(0.5, 0.5))])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [10.0, 0.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), "GND", size=(0.5, 0.5))])},
    ])

    # 17i ratsnest MST over 3 pads -> 2 airwires (partial route via a GND track).
    run_compile_pcb("drc_ratsnest_mst", [
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [0.0, 0.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), "GND", size=(0.5, 0.5))])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [10.0, 0.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), "GND", size=(0.5, 0.5))])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [20.0, 0.0], "ref": "FP3",
         "def": _fpdef([_pad("p3", (0.0, 0.0), "GND", size=(0.5, 0.5))])},
        {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "p1": [0.0, 0.0],
         "p2": [10.0, 0.0], "width": 0.25},
    ])

    # 17j ZONE FILL: a GND pour with a different-net (SIG) pad carving a clearance hole,
    #     a same-net GND THT pad (thermal relief ring + spokes), and a same-net GND SMD
    #     pad (solid flood). Mirrors test_zonefill._base_board (track replaced by a SIG
    #     pad since a SIG track can't be registered through the op vocabulary).
    run_compile_pcb("zonefill_base", [
        {"op": "pcb_set_outline", "shapes": _rect_outline(30.0, 20.0), "closed": True},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [15.0, 10.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), None, size=(2.0, 2.0))])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [6.0, 6.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), "GND", size=(2.0, 2.0), ptype="tht",
                             drill={"diameter": 1.0, "shape": "round", "offset": [0, 0]})],
                       attr="tht")},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [24.0, 6.0], "ref": "FP3",
         "def": _fpdef([_pad("p3", (0.0, 0.0), "GND", size=(2.0, 2.0))])},
        {"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
         "polygon": [[2, 2], [28, 2], [28, 18], [2, 18]], "clearance": 0.3,
         "thermal": {"gap": 0.4, "spoke_width": 0.5}},
    ])

    # 17k ZONE FILL with a SIG-pad strip carving a tall corridor that isolates a thin
    #     left region (a second pour topology — multiple traced components). min_width
    #     is not settable via pcb_add_zone, so both engines keep the strip; this
    #     exercises the multi-component trace + keyhole path for parity.
    run_compile_pcb("zonefill_island", [
        {"op": "pcb_set_outline", "shapes": _rect_outline(20.0, 20.0), "closed": True},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [3.0, 10.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), None, size=(0.5, 18.0), shape="rect")])},
        {"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
         "polygon": [[1, 1], [19, 1], [19, 19], [1, 19]], "clearance": 0.3,
         "thermal": {"gap": 0.4, "spoke_width": 0.5}},
    ])

    # ---- 18) DFM parity: engine.ecad.dfm.run_dfm vs core/ecad/dfm.cpp ----
    # Fixtures mirror engine/ecad/test_dfm.py (every rule: a seeded violation +
    # the cleared case). The board doc is built directly (the test boards set
    # graphics/footprints/vias as plain dicts) and passed VERBATIM to both engines:
    # Python -> dfm.run_dfm + the same {violations,summary} grouping rpc.py builds;
    # C++ -> ecad_dfm {board} (stateless inline path). The FULL response
    # (violations list — rule/severity/locs/items/message + ORDER — and the grouped
    # summary) is compared byte-for-byte.
    from engine.ecad import schema as _eschema  # noqa: E402
    from engine.ecad import dfm as _edfm  # noqa: E402

    def _dfm_summary(violations):
        """Replicate engine/ecad/rpc.py::_ecad_dfm summary grouping exactly."""
        errors = [v for v in violations if v.get("severity") == "error"]
        warnings_list = [v for v in violations if v.get("severity") != "error"]
        by_rule = {}
        for v in violations:
            rule = v.get("rule", "?")
            slot = by_rule.setdefault(rule, {"rule": rule, "severity": v.get("severity"),
                                             "count": 0, "messages": []})
            slot["count"] += 1
            if v.get("severity") == "error":
                slot["severity"] = "error"
            if len(slot["messages"]) < 3:
                slot["messages"].append(v.get("message", ""))
        return {"errors": len(errors), "warnings": len(warnings_list),
                "by_rule": sorted(by_rule.values(),
                                  key=lambda g: (g["severity"] != "error", g["rule"]))}

    def _py_dfm(board, rules=None):
        v = _edfm.run_dfm(board, rules=rules)
        return {"violations": v, "summary": _dfm_summary(v)}

    def cmp_dfm(label, board, rules=None):
        py_resp = _py_dfm(board, rules)
        p = {"board": board}
        if rules is not None:
            p["rules"] = rules
        cpp_resp = cpp.call("ecad_dfm", p)
        diffs = []
        if py_resp["violations"] != cpp_resp.get("violations"):
            pv, cv = py_resp["violations"], cpp_resp.get("violations") or []
            diffs.append(f"violations differ (py={len(pv)} cpp={len(cv)})")
            for i, (a, b) in enumerate(zip(pv, cv)):
                if a != b:
                    diffs.append(f"v[{i}] {a.get('rule')}/{a.get('message','')[:40]} "
                                 f"!= {b.get('rule')}/{b.get('message','')[:40]}")
                    break
        if py_resp["summary"] != cpp_resp.get("summary"):
            diffs.append("summary differ")
        ok = not diffs
        print(f"  {('dfm ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:160]}")
        return step(0 if ok else 1)

    # fixture helpers (mirror test_dfm.py)
    def _dfm_board():
        return _eschema.new_board("dfm-test")

    def _dfm_fp(fid, pos, pads=None, graphics=None, side="top"):
        return {"id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "",
                "pos": list(pos), "angle": 0.0, "side": side,
                "pads": pads or [], "graphics": graphics or [],
                "model_3d": None, "attr": "smd", "linked_symbol": None}

    def _dfm_smd_pad(pid, pos, net, size=(1.0, 1.0), shape="rect", layers=None):
        if layers is None:
            layers = ["F.Cu", "F.Mask", "F.Paste"]
        return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
                "size": list(size), "type": "smd", "layers": layers, "drill": None,
                "net": net, "pin": "1"}

    def _dfm_via(vid, at, net, drill=0.4, diameter=0.8):
        return {"id": vid, "net": net, "at": list(at), "drill": drill, "diameter": diameter,
                "layers": ["F.Cu", "B.Cu"], "type": "through"}

    def _dfm_silk_line(gid, p1, p2, width=0.15, layer="F.SilkS"):
        return {"id": gid, "type": "line", "layer": layer, "p1": list(p1), "p2": list(p2),
                "width": width}

    def _dfm_silk_text(gid, pos, height=1.0, layer="F.SilkS"):
        return {"id": gid, "type": "text", "layer": layer, "pos": list(pos), "text": "TEST",
                "size": [height, height], "width": 0.15}

    def _dfm_crtyd_line(gid, p1, p2, layer="F.CrtYd"):
        return {"id": gid, "type": "line", "layer": layer, "p1": list(p1), "p2": list(p2),
                "width": 0.05}

    def _dfm_track(tid, p1, p2, net, width=0.25, layer="F.Cu"):
        return {"id": tid, "net": net, "layer": layer, "width": width, "kind": "segment",
                "p1": list(p1), "p2": list(p2)}

    import math as _math

    # 18a silk_over_pad: silk line crossing a pad (fires) + moved off (clean)
    b = _dfm_board()
    b["nets"] = [{"name": "NA", "class": "default"}]
    b["footprints"] = [_dfm_fp("fp1", (5.0, 5.0),
                               pads=[_dfm_smd_pad("p1", (0.0, 0.0), "NA", size=(2.0, 2.0))])]
    b["graphics"] = [_dfm_silk_line("g1", (3.0, 5.0), (7.0, 5.0), width=0.15)]
    cmp_dfm("silk_over_pad fire", b)
    b2 = json_copy(b); b2["graphics"] = [_dfm_silk_line("g1", (3.0, 10.0), (7.0, 10.0), width=0.15)]
    cmp_dfm("silk_over_pad clean", b2)

    # 18b silk_min_width
    b = _dfm_board(); b["graphics"] = [_dfm_silk_line("g1", (0.0, 0.0), (5.0, 0.0), width=0.08)]
    cmp_dfm("silk_min_width fire", b)
    b2 = json_copy(b); b2["graphics"][0]["width"] = 0.2
    cmp_dfm("silk_min_width clean", b2)

    # 18c silk_min_text
    b = _dfm_board(); b["graphics"] = [_dfm_silk_text("g1", (5.0, 5.0), height=0.5)]
    cmp_dfm("silk_min_text fire", b)
    b2 = json_copy(b); b2["graphics"][0]["size"] = [1.0, 1.0]
    cmp_dfm("silk_min_text clean", b2)

    # 18d mask_sliver
    b = _dfm_board()
    b["nets"] = [{"name": "NA", "class": "default"}, {"name": "NB", "class": "default"}]
    b["footprints"] = [
        _dfm_fp("fp1", (0.0, 0.0), pads=[_dfm_smd_pad("p1", (0.0, 0.0), "NA", size=(1.0, 1.0))]),
        _dfm_fp("fp2", (1.05, 0.0), pads=[_dfm_smd_pad("p2", (0.0, 0.0), "NB", size=(1.0, 1.0))]),
    ]
    cmp_dfm("mask_sliver fire", b)
    b2 = json_copy(b); b2["footprints"][1]["pos"] = [2.0, 0.0]
    cmp_dfm("mask_sliver clean", b2)

    # 18e acid_trap: 30deg fires, 91deg clean, different-net clean
    b = _dfm_board(); b["nets"] = [{"name": "NA", "class": "default"}]
    end_b = (10.0 * _math.cos(_math.radians(30)), 10.0 * _math.sin(_math.radians(30)))
    b["tracks"] = [_dfm_track("t1", (0.0, 0.0), (10.0, 0.0), "NA"),
                   _dfm_track("t2", (0.0, 0.0), end_b, "NA")]
    cmp_dfm("acid_trap 30deg fire", b)
    end_c = (10.0 * _math.cos(_math.radians(91)), 10.0 * _math.sin(_math.radians(91)))
    b2 = json_copy(b); b2["tracks"][1] = _dfm_track("t2", (0.0, 0.0), end_c, "NA")
    cmp_dfm("acid_trap 91deg clean", b2)
    b3 = json_copy(b)
    b3["nets"].append({"name": "NB", "class": "default"})
    b3["tracks"][1] = _dfm_track("t2", (0.0, 0.0), end_b, "NB")
    cmp_dfm("acid_trap diff-net clean", b3)

    # 18f courtyard_overlap (error severity) + separated clean
    cy_a = [_dfm_crtyd_line("g1", (-2.0, -2.0), (2.0, -2.0)),
            _dfm_crtyd_line("g2", (2.0, -2.0), (2.0, 2.0)),
            _dfm_crtyd_line("g3", (2.0, 2.0), (-2.0, 2.0)),
            _dfm_crtyd_line("g4", (-2.0, 2.0), (-2.0, -2.0))]
    cy_b = [_dfm_crtyd_line("g5", (-2.0, -2.0), (2.0, -2.0)),
            _dfm_crtyd_line("g6", (2.0, -2.0), (2.0, 2.0)),
            _dfm_crtyd_line("g7", (2.0, 2.0), (-2.0, 2.0)),
            _dfm_crtyd_line("g8", (-2.0, 2.0), (-2.0, -2.0))]
    b = _dfm_board()
    b["footprints"] = [_dfm_fp("fp1", (0.0, 0.0), graphics=cy_a),
                       _dfm_fp("fp2", (3.0, 0.0), graphics=cy_b)]
    cmp_dfm("courtyard_overlap fire", b)
    b2 = json_copy(b); b2["footprints"][1]["pos"] = [10.0, 0.0]
    cmp_dfm("courtyard_overlap clean", b2)

    # 18g drill_min: 0.1 warning, 0.05 error, 0.2 clean
    b = _dfm_board(); b["nets"] = [{"name": "NA", "class": "default"}]
    b["vias"] = [_dfm_via("v1", (5.0, 5.0), "NA", drill=0.1, diameter=0.5)]
    cmp_dfm("drill_min 0.1 warn", b)
    b2 = json_copy(b); b2["vias"][0]["drill"] = 0.05
    cmp_dfm("drill_min 0.05 error", b2)
    b3 = json_copy(b); b3["vias"][0]["drill"] = 0.2
    cmp_dfm("drill_min 0.2 clean", b3)

    # 18h annular_min: ring 0.1 fires, healthy ring clean
    b = _dfm_board(); b["nets"] = [{"name": "NA", "class": "default"}]
    b["vias"] = [_dfm_via("v1", (5.0, 5.0), "NA", drill=0.3, diameter=0.5)]
    cmp_dfm("annular_min fire", b)
    b2 = json_copy(b); b2["vias"][0]["diameter"] = 0.8; b2["vias"][0]["drill"] = 0.4
    cmp_dfm("annular_min clean", b2)

    # 18i combined board (multiple rules at once) + non-board input
    b = _dfm_board()
    b["nets"] = [{"name": "NA", "class": "default"}, {"name": "NB", "class": "default"}]
    b["footprints"] = [
        _dfm_fp("fp1", (0.0, 0.0), pads=[_dfm_smd_pad("p1", (0.0, 0.0), "NA", size=(1.0, 1.0))]),
        _dfm_fp("fp2", (1.05, 0.0), pads=[_dfm_smd_pad("p2", (0.0, 0.0), "NB", size=(1.0, 1.0))]),
    ]
    b["vias"] = [_dfm_via("v1", (10.0, 10.0), "NA", drill=0.1, diameter=0.5)]
    b["graphics"] = [_dfm_silk_line("g1", (3.0, 0.0), (7.0, 0.0), width=0.08)]
    cmp_dfm("combined multi-rule", b)
    # rules override: tighten silk_min_width so 0.15 line now fires
    b = _dfm_board(); b["graphics"] = [_dfm_silk_line("g1", (0.0, 0.0), (5.0, 0.0), width=0.15)]
    cmp_dfm("rules override silk", b, rules={"silk_min_width": 0.2})

    # ---- 19) IMPEDANCE parity: engine.ecad.impedance vs core/ecad/impedance.cpp ----
    # Fixtures mirror engine/ecad/test_impedance.py (microstrip/stripline/CPWG/diff
    # pair + track_impedance). Python functions are called DIRECTLY (there is no
    # ecad_impedance rpc in the oracle — impedance.py is a pure calculator module);
    # C++ exposes ecad_impedance {mode,...}. Every numeric field is compared to a
    # tight relative tolerance (1e-9) and string fields (model/kind) exactly.
    from engine.ecad import impedance as _eimp  # noqa: E402

    def cmp_imp(label, py_dict, cpp_resp, keys):
        diffs = []
        for k in keys:
            pv = py_dict[k]
            cv = cpp_resp.get(k)
            if isinstance(pv, str):
                if pv != cv:
                    diffs.append(f"{k} {pv!r}!={cv!r}")
            elif pv is None:
                if cv is not None:
                    diffs.append(f"{k} None!={cv}")
            else:
                if cv is None:
                    diffs.append(f"{k} missing")
                else:
                    denom = abs(pv) if pv != 0 else 1.0
                    if abs(pv - cv) / denom > 1e-9:
                        diffs.append(f"{k} {pv!r}!={cv!r} (rel {abs(pv-cv)/denom:.2e})")
        ok = not diffs
        print(f"  {('imp ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:160]}")
        return step(0 if ok else 1)

    _ZK = ["z0", "eeff", "delay_ps_per_mm", "model"]

    # 19a microstrip (50-ohm-class FR-4)
    cmp_imp("microstrip", _eimp.microstrip_z0(0.3, 0.2, 0.035, 4.3),
            cpp.call("ecad_impedance", {"mode": "microstrip", "w": 0.3, "h": 0.2, "t": 0.035, "er": 4.3}),
            _ZK)
    # 19b microstrip w/h < 1 (exercises the 0.04*(1-u)^2 + thin-trace branch)
    cmp_imp("microstrip narrow", _eimp.microstrip_z0(0.15, 0.3, 0.035, 4.3),
            cpp.call("ecad_impedance", {"mode": "microstrip", "w": 0.15, "h": 0.3, "t": 0.035, "er": 4.3}),
            _ZK)
    # 19c stripline
    cmp_imp("stripline", _eimp.stripline_z0(0.3, 0.2, 0.035, 4.3),
            cpp.call("ecad_impedance", {"mode": "stripline", "w": 0.3, "h": 0.2, "t": 0.035, "er": 4.3}),
            _ZK)
    # 19d CPWG (elliptic-integral path) + wider gap
    cmp_imp("coplanar", _eimp.coplanar_z0(0.3, 0.2, 0.2, 0.035, 4.3),
            cpp.call("ecad_impedance", {"mode": "coplanar", "w": 0.3, "s": 0.2, "h": 0.2, "t": 0.035, "er": 4.3}),
            _ZK)
    cmp_imp("coplanar wide", _eimp.coplanar_z0(0.3, 0.6, 0.2, 0.035, 4.3),
            cpp.call("ecad_impedance", {"mode": "coplanar", "w": 0.3, "s": 0.6, "h": 0.2, "t": 0.035, "er": 4.3}),
            _ZK)
    # 19e diff pair (microstrip + stripline + tighter spacing)
    _DK = ["z0_se", "z0_diff", "z0_odd", "eeff", "delay_ps_per_mm", "kind", "model"]
    cmp_imp("diff_pair ms", _eimp.diff_pair_z0(0.2, 0.2, 0.2, 0.035, 4.3, kind="microstrip"),
            cpp.call("ecad_impedance", {"mode": "diff_pair", "w": 0.2, "s": 0.2, "h": 0.2, "t": 0.035,
                                        "er": 4.3, "kind": "microstrip"}), _DK)
    cmp_imp("diff_pair ms tight", _eimp.diff_pair_z0(0.2, 0.05, 0.2, 0.035, 4.3, kind="microstrip"),
            cpp.call("ecad_impedance", {"mode": "diff_pair", "w": 0.2, "s": 0.05, "h": 0.2, "t": 0.035,
                                        "er": 4.3, "kind": "microstrip"}), _DK)
    cmp_imp("diff_pair strip", _eimp.diff_pair_z0(0.2, 0.2, 0.2, 0.035, 4.3, kind="stripline"),
            cpp.call("ecad_impedance", {"mode": "diff_pair", "w": 0.2, "s": 0.2, "h": 0.2, "t": 0.035,
                                        "er": 4.3, "kind": "stripline"}), _DK)

    # 19f track_impedance over a 4-layer board stackup (outer=microstrip, inner=stripline)
    def _board_4layer():
        bb = _eschema.new_board("si")
        bb["stackup"] = {"copper_layers": 4, "thickness": 1.6, "layers": [
            {"id": "F.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d1", "type": "prepreg", "thickness": 0.2, "er": 4.3},
            {"id": "In1.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d2", "type": "core", "thickness": 0.4, "er": 4.5},
            {"id": "In2.Cu", "type": "copper", "thickness": 0.035},
            {"id": "d3", "type": "prepreg", "thickness": 0.2, "er": 4.3},
            {"id": "B.Cu", "type": "copper", "thickness": 0.035}]}
        return bb

    bsi = _board_4layer()
    bsi["tracks"].append({"id": "t1", "net": "SIG", "layer": "F.Cu", "width": 0.3,
                          "kind": "segment", "p1": [0.0, 0.0], "p2": [10.0, 0.0]})
    bsi["nets"].append({"name": "SIG", "class": "default"})
    bsi["tracks"].append({"id": "t2", "net": "SIG2", "layer": "In1.Cu", "width": 0.3,
                          "kind": "segment", "p1": [0.0, 1.0], "p2": [5.0, 1.0]})
    bsi["nets"].append({"name": "SIG2", "class": "default"})

    # The C++ ecad_impedance "track" mode needs the board in a session. Open it via
    # ecad_new then swap the doc in by applying nothing is not possible (ops vocab),
    # so we pass the board inline through a dedicated stateless path: the libaicad
    # "track" mode reads params["board"] when present (else session doc_id).
    _TK = ["z0", "eeff", "delay_ps_per_mm", "kind", "model", "width", "h", "er", "t",
           "layer", "net", "length_mm", "delay_ps"]
    cmp_imp("track F.Cu (microstrip)", _eimp.track_impedance(bsi, "t1"),
            cpp.call("ecad_impedance", {"mode": "track", "board": bsi, "track_or_net": "t1"}), _TK)
    cmp_imp("track In1.Cu (stripline)", _eimp.track_impedance(bsi, "t2"),
            cpp.call("ecad_impedance", {"mode": "track", "board": bsi, "track_or_net": "t2"}), _TK)
    cmp_imp("track by net name", _eimp.track_impedance(bsi, "SIG"),
            cpp.call("ecad_impedance", {"mode": "track", "board": bsi, "track_or_net": "SIG"}), _TK)

    # ---- 20) FAB OUTPUT parity: Gerber / Excellon / BOM / CPL ----------------
    # Fixtures + expected text come from engine/ecad/test_fab.py (_make_test_board)
    # plus extra boards exercising roundrect aperture macros, oval-drill G85 slots,
    # BOM user-field columns, and a non-default origin. Python fab writers are
    # called DIRECTLY (engine.ecad.fab) to get the ground-truth text; C++ exposes
    # ecad_export {kind, board, opts?} -> {"files": {key: text}} (stateless inline
    # board path, mirroring ecad_dfm {board}). Every file is compared BYTE-FOR-BYTE
    # after normalize().
    #
    # normalize() — the ONLY allowed transform, applied IDENTICALLY to both
    # engines' text: there is NOTHING non-deterministic in these formats (no
    # timestamps, no generator version string, no uuid; both engines fix '\n' line
    # endings and integer-scaled coordinates). normalize() therefore only strips a
    # single trailing newline so a comparison never trips on EOF-newline handling —
    # it does NOT touch apertures, D-codes, coordinates, regions, tool tables or any
    # other load-bearing content.
    from engine.ecad.fab import (write_gerbers as _py_write_gerbers,
                                  write_gerber_layer as _py_write_gerber_layer,
                                  write_drill as _py_write_drill,
                                  write_bom as _py_write_bom,
                                  write_placement as _py_write_placement)
    from engine.ecad.test_fab import _make_test_board as _py_make_test_board

    def normalize(text: str) -> str:
        # strip exactly one trailing '\n' (EOF-newline; deterministic on both sides)
        return text[:-1] if text.endswith("\n") else text

    def cmp_fab_file(label, py_text, cpp_text):
        ok = normalize(py_text) == normalize(cpp_text)
        if ok:
            print(f"  {label:<46} MATCH")
        else:
            # show first differing line for diagnosis
            pl = normalize(py_text).splitlines()
            cl = normalize(cpp_text).splitlines()
            firstdiff = "len " + f"{len(pl)}!={len(cl)}"
            for i, (a, b) in enumerate(zip(pl, cl)):
                if a != b:
                    firstdiff = f"line {i+1}: py={a!r} cpp={b!r}"
                    break
            print(f"  {label:<46} DIFF: {firstdiff[:150]}")
        return step(0 if ok else 1)

    def cpp_export(kind, board, opts=None):
        p = {"kind": kind, "board": board}
        if opts is not None:
            p["opts"] = opts
        return cpp.call("ecad_export", p)["files"]

    def run_fab(label, board):
        # gerber: compare every populated layer (the Python write_gerbers writes
        # files keyed by layer_id; we compare CONTENT per layer)
        cpp_g = cpp_export("gerber", board)
        from engine.ecad.schema import copper_layer_ids as _cli
        n_cu = board["stackup"]["copper_layers"]
        layers = list(_cli(n_cu)) + ["F.SilkS", "B.SilkS", "F.Mask", "B.Mask",
                                     "F.Paste", "B.Paste", "F.Fab", "B.Fab", "Edge.Cuts"]
        for lid in layers:
            py_text = _py_write_gerber_layer(board, lid)
            cmp_fab_file(f"{label} gerber {lid}", py_text, cpp_g.get(lid, ""))
        # drill (pth + npth)
        cpp_d = cpp_export("drill", board)
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmp:
            paths = _py_write_drill(board, tmp)
            for key in ("pth", "npth"):
                with open(paths[key], encoding="utf-8") as f:
                    py_text = f.read()
                cmp_fab_file(f"{label} drill {key}", py_text, cpp_d.get(key, ""))
        # bom csv + json
        cpp_bom_csv = cpp_export("bom", board, {"fmt": "csv"})["bom"]
        cmp_fab_file(f"{label} bom csv", _py_write_bom(board, fmt="csv"), cpp_bom_csv)
        cpp_bom_json = cpp_export("bom", board, {"fmt": "json"})["bom"]
        cmp_fab_file(f"{label} bom json", _py_write_bom(board, fmt="json"), cpp_bom_json)
        # cpl
        cpp_cpl = cpp_export("cpl", board)["cpl"]
        cmp_fab_file(f"{label} cpl", _py_write_placement(board), cpp_cpl)

    # 20a the canonical test_fab board (SMD rects, THT round drills, via, GND pour
    #     region, rect outline, bottom-side SMD, virtual+dnp exclusions)
    run_fab("testboard", _py_make_test_board())

    # 20b roundrect aperture macro board (RNDRCT %AM outline polygon — the most
    #     float-sensitive path: 36 cos/sin vertices formatted %.6f) + user-field BOM
    def _roundrect_board():
        from engine.ecad.schema import new_board as _nb
        b = _nb("RR Board")  # note the space -> board_name underscored, but content only
        b["origin"] = [1.0, 2.0]  # non-zero origin exercises board_xy subtraction
        b["nets"] = [{"name": "GND", "class": "default"}, {"name": "VCC", "class": "default"}]
        b["footprints"] = [
            {"id": "fp1", "fpid": "R_0603", "ref": "R1", "value": "1k",
             "pos": [10.0, 10.0], "angle": 0.0, "side": "top", "attr": "smd",
             "fields": {"MPN": "RC0603", "Tol": "1%"},
             "pads": [
                 {"id": "p1", "number": "1", "pos": [-0.8, 0.0], "angle": 0.0,
                  "shape": "roundrect", "size": [0.9, 0.95], "roundrect_ratio": 0.25,
                  "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "drill": None,
                  "net": "GND", "pin": "1"},
                 {"id": "p2", "number": "2", "pos": [0.8, 0.0], "angle": 0.0,
                  "shape": "roundrect", "size": [0.9, 0.95], "roundrect_ratio": 0.25,
                  "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "drill": None,
                  "net": "VCC", "pin": "2"},
             ], "graphics": [], "model_3d": None, "linked_symbol": None},
            {"id": "fp2", "fpid": "R_0603", "ref": "R2", "value": "1k",
             "pos": [20.0, 12.0], "angle": 90.0, "side": "top", "attr": "smd",
             "fields": {"MPN": "RC0603", "Tol": "1%"},
             "pads": [
                 {"id": "p3", "number": "1", "pos": [-0.8, 0.0], "angle": 0.0,
                  "shape": "roundrect", "size": [0.9, 0.95], "roundrect_ratio": 0.25,
                  "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "drill": None,
                  "net": "GND", "pin": "1"},
             ], "graphics": [], "model_3d": None, "linked_symbol": None},
        ]
        return b
    run_fab("roundrect", _roundrect_board())

    # 20c oval-drill G85 slot board (Excellon slot path) + an oval (O,) aperture
    def _oval_board():
        from engine.ecad.schema import new_board as _nb
        b = _nb("Oval Board")
        b["nets"] = [{"name": "GND", "class": "default"}]
        b["footprints"] = [
            {"id": "fp1", "fpid": "Conn", "ref": "J1", "value": "C",
             "pos": [10.0, 10.0], "angle": 30.0, "side": "top", "attr": "tht",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [0.0, 0.0], "angle": 0.0,
                  "shape": "oval", "size": [2.0, 1.0], "type": "tht",
                  "layers": ["F.Cu", "B.Cu", "F.Mask", "B.Mask"],
                  "drill": {"diameter": 0.9, "shape": "oval", "offset": [0, 0]},
                  "net": "GND", "pin": "1"},
             ], "graphics": [], "model_3d": None, "linked_symbol": None},
        ]
        return b
    run_fab("ovaldrill", _oval_board())

    print(f"\n[ecad-parity] {total - mism}/{total} steps at full parity")
    return mism


def json_copy(obj):
    import copy
    return copy.deepcopy(obj)


if __name__ == "__main__":
    raise SystemExit(main())
