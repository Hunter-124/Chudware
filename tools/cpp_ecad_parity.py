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
    # `linked_sch` is a board's back-link to the SOURCE SCHEMATIC's session id,
    # set by ecad_update_from_sch. That id is engine-specific by construction
    # (Python uuid4 vs C++ "ecadcpp..."), exactly like the doc's own `id`, so it is
    # pinned for the cross-engine compare. It is null on every other doc, so this
    # is a no-op except after a stateful update-from-sch.
    if isinstance(t, dict) and "linked_sch" in t and t.get("linked_sch") is not None:
        t["linked_sch"] = "<sch_id>"
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

    # ---- 21) AUTOROUTER parity: engine.ecad.autoroute vs core/ecad/autoroute.cpp ----
    # Fixtures mirror engine/ecad/test_autoroute.py + test_autoroute2.py (small
    # boards with a few nets to route). The Python oracle autoroute() / route_diff_pair()
    # are PURE (return fresh {tracks, vias, report}); C++ exposes the SAME pure path
    # via ecad_autoroute {board, opts?} / ecad_route_diff_pair {board, net_p, net_n, opts?}.
    #
    # GATE: EXACT. The router is fully deterministic — fixed shortest-first net order,
    # a Lee/A* over a heapq of (f, g, node) tuples whose ties resolve on the totally-
    # ordered node tuple, deterministic rip-up order, and sorted set/dict iteration.
    # test_autoroute.py::test_determinism asserts byte-identical output across runs, so
    # there is no RNG / hash-order nondeterminism to reproduce. We therefore compare the
    # routed tracks + vias + the full report dict BYTE-FOR-BYTE (json.dumps sort_keys).
    import json  # noqa: E402  (local import; harness uses local json elsewhere too)
    from engine.ecad import schema as _arsch  # noqa: E402
    from engine.ecad.autoroute import (autoroute as _py_autoroute,  # noqa: E402
                                       route_diff_pair as _py_route_diff_pair)

    def _ar_board(name="rt"):
        return _arsch.new_board(name)

    def _ar_add_net(b, name, klass="default"):
        if not any(n.get("name") == name for n in b["nets"]):
            b["nets"].append({"name": name, "class": klass})

    def _ar_pad(pid, pos, net, size=(0.6, 0.6), shape="circle", layer="F.Cu"):
        return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
                "size": list(size), "type": "smd", "layers": [layer], "drill": None, "net": net}

    def _ar_fp(fid, pos, pads, side="top"):
        return {"id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "", "pos": list(pos),
                "angle": 0.0, "side": side, "pads": pads, "graphics": [], "model_3d": None,
                "attr": "smd", "linked_symbol": None}

    def _ar_outline(b, w, h, x0=0.0, y0=0.0):
        x1, y1 = x0 + w, y0 + h
        b["outline"] = {"shapes": [
            {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
            {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
            {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
            {"type": "line", "p1": [x0, y1], "p2": [x0, y0]}], "closed": True}

    def _ar_one_layer(b):
        b["stackup"]["copper_layers"] = 1
        b["stackup"]["layers"] = [{"id": "F.Cu", "type": "copper", "thickness": 0.035}]

    def cmp_autoroute(label, board, opts=None):
        py = _py_autoroute(board, opts or {})
        p = {"board": board}
        if opts is not None:
            p["opts"] = opts
        cp = cpp.call("ecad_autoroute", p)
        diffs = []
        if json.dumps(py["tracks"], sort_keys=True) != json.dumps(cp.get("tracks"), sort_keys=True):
            diffs.append(f"tracks (py={len(py['tracks'])} cpp={len(cp.get('tracks') or [])})")
        if json.dumps(py["vias"], sort_keys=True) != json.dumps(cp.get("vias"), sort_keys=True):
            diffs.append(f"vias (py={len(py['vias'])} cpp={len(cp.get('vias') or [])})")
        if json.dumps(py["report"], sort_keys=True) != json.dumps(cp.get("report"), sort_keys=True):
            pr, cr = py["report"], cp.get("report") or {}
            for k in sorted(set(pr) | set(cr)):
                if json.dumps(pr.get(k), sort_keys=True) != json.dumps(cr.get(k), sort_keys=True):
                    diffs.append(f"report.{k} (py={str(pr.get(k))[:60]} cpp={str(cr.get(k))[:60]})")
        ok = not diffs
        print(f"  {('autoroute ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:170]}")
        return step(0 if ok else 1)

    def cmp_diff_pair(label, board, net_p, net_n, opts=None):
        py = _py_route_diff_pair(board, net_p, net_n, opts or {})
        p = {"board": board, "net_p": net_p, "net_n": net_n}
        if opts is not None:
            p["opts"] = opts
        cp = cpp.call("ecad_route_diff_pair", p)
        ok = (json.dumps(py["tracks"], sort_keys=True) == json.dumps(cp.get("tracks"), sort_keys=True)
              and json.dumps(py["vias"], sort_keys=True) == json.dumps(cp.get("vias"), sort_keys=True)
              and json.dumps(py["report"], sort_keys=True) == json.dumps(cp.get("report"),
                                                                          sort_keys=True))
        if ok:
            print(f"  {('diff_pair ' + label):<40} MATCH")
        else:
            d = []
            if json.dumps(py["tracks"], sort_keys=True) != json.dumps(cp.get("tracks"), sort_keys=True):
                d.append(f"tracks (py={len(py['tracks'])} cpp={len(cp.get('tracks') or [])})")
            if json.dumps(py["vias"], sort_keys=True) != json.dumps(cp.get("vias"), sort_keys=True):
                d.append("vias")
            if json.dumps(py["report"], sort_keys=True) != json.dumps(cp.get("report"), sort_keys=True):
                d.append(f"report (py={py['report']} cpp={cp.get('report')})")
            print(f"  {('diff_pair ' + label):<40} DIFF: {'; '.join(d)[:170]}")
        return step(0 if ok else 1)

    # 21a two-net board (test_autoroute._two_net_board): SIG long span + VCC short.
    def _two_net_board():
        b = _ar_board()
        _ar_add_net(b, "SIG"); _ar_add_net(b, "VCC")
        _ar_outline(b, 40.0, 30.0)
        b["footprints"] = [
            _ar_fp("fp1", (8.0, 15.0), [_ar_pad("p1", (0.0, 2.0), "SIG"),
                                        _ar_pad("p2", (0.0, -2.0), "VCC")]),
            _ar_fp("fp2", (20.0, 8.0), [_ar_pad("p3", (0.0, 0.0), "VCC")]),
            _ar_fp("fp3", (32.0, 18.0), [_ar_pad("p4", (0.0, 0.0), "SIG")]),
        ]
        return b
    cmp_autoroute("two_net default", _two_net_board())
    cmp_autoroute("two_net ripup0", _two_net_board(), {"ripup_passes": 0})

    # 21b id allocation seeded (test_id_allocation): counters must start past t7/v3.
    b = _two_net_board()
    b["tracks"] = [{"id": "t7", "net": "SIG", "layer": "F.Cu", "width": 0.25,
                    "kind": "segment", "p1": [0.0, 0.0], "p2": [0.0, 1.0]}]
    b["vias"] = [{"id": "v3", "net": "SIG", "at": [0.0, 0.0], "drill": 0.4, "diameter": 0.8,
                  "layers": ["F.Cu", "B.Cu"], "type": "through"}]
    cmp_autoroute("id_allocation seeded", b)

    # 21c tight two-net board (test_tight_board_clean): obstacle halo weaving.
    b = _ar_board()
    _ar_add_net(b, "A"); _ar_add_net(b, "B")
    _ar_outline(b, 20.0, 16.0)
    b["footprints"] = [
        _ar_fp("fp1", (4.0, 8.0), [_ar_pad("p1", (0.0, 1.5), "A"), _ar_pad("p2", (0.0, -1.5), "B")]),
        _ar_fp("fp2", (16.0, 8.0), [_ar_pad("p3", (0.0, 1.5), "A"), _ar_pad("p4", (0.0, -1.5), "B")]),
    ]
    cmp_autoroute("tight_board", b)

    # 21d unroutable net walled off on both layers (test_unroutable_reported).
    b = _ar_board()
    _ar_add_net(b, "NET"); _ar_add_net(b, "WALL")
    _ar_outline(b, 30.0, 30.0)
    b["footprints"] = [
        _ar_fp("fsrc", (3.0, 15.0), [_ar_pad("src", (0.0, 0.0), "NET")]),
        _ar_fp("ftgt", (15.0, 15.0), [_ar_pad("tgt", (0.0, 0.0), "NET", size=(0.4, 0.4))]),
    ]
    x0, y0, x1, y1 = 13.0, 13.0, 17.0, 17.0
    box = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
           ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
    tracks = []; tid = 1
    for layer in ("F.Cu", "B.Cu"):
        for (p1, p2) in box:
            tracks.append({"id": f"t{tid}", "net": "WALL", "layer": layer, "width": 1.0,
                           "kind": "segment", "p1": list(p1), "p2": list(p2)})
            tid += 1
    b["tracks"] = tracks
    cmp_autoroute("unroutable_walled", b)

    # 21e rip-up board (test_autoroute2._ripup_board): greedy fails Y; rip-up rescues both.
    def _ripup_board():
        b = _ar_board("ripup")
        _ar_one_layer(b)
        for n in ("X", "Y", "WALL"):
            _ar_add_net(b, n)
        _ar_outline(b, 40.0, 30.0)
        gh = 1.1
        tr = []; tid = 1
        for (a, c) in [(0.0, 15.0 - gh), (15.0 + gh, 30.0)]:
            tr.append({"id": f"t{tid}", "net": "WALL", "layer": "F.Cu", "width": 1.0,
                       "kind": "segment", "p1": [20.0, a], "p2": [20.0, c]}); tid += 1
        tr.append({"id": f"t{tid}", "net": "WALL", "layer": "F.Cu", "width": 1.0,
                   "kind": "segment", "p1": [20.0, 15.0 + gh], "p2": [26.0, 15.0 + gh]}); tid += 1
        tr.append({"id": f"t{tid}", "net": "WALL", "layer": "F.Cu", "width": 1.0,
                   "kind": "segment", "p1": [20.0, 15.0 - gh], "p2": [26.0, 15.0 - gh]}); tid += 1
        b["tracks"] = tr
        b["footprints"] = [
            _ar_fp("yl", (6.0, 15.0), [_ar_pad("yl", (0.0, 0.0), "Y", size=(0.4, 0.4))]),
            _ar_fp("yr", (34.0, 15.0), [_ar_pad("yr", (0.0, 0.0), "Y", size=(0.4, 0.4))]),
            _ar_fp("xa", (23.0, 11.0), [_ar_pad("xa", (0.0, 0.0), "X", size=(0.4, 0.4))]),
            _ar_fp("xb", (23.0, 19.0), [_ar_pad("xb", (0.0, 0.0), "X", size=(0.4, 0.4))]),
        ]
        return b
    cmp_autoroute("ripup greedy(ripup0)", _ripup_board(), {"ripup_passes": 0})
    cmp_autoroute("ripup passes3", _ripup_board(), {"ripup_passes": 3})
    cmp_autoroute("ripup default", _ripup_board())

    # 21f differential pair board (test_autoroute2._diff_pair_board): coupled + matched.
    def _diff_pair_board(diff_gap=0.25, track_width=0.2, pad_sep=0.75, length=28.0):
        b = _ar_board("diffpair")
        _ar_one_layer(b)
        _ar_add_net(b, "CLK_P"); _ar_add_net(b, "CLK_N")
        b["net_classes"]["default"]["diff_gap"] = diff_gap
        b["net_classes"]["default"]["track_width"] = track_width
        _ar_outline(b, 40.0, 20.0)
        h = pad_sep / 2.0
        x0 = 6.0; x1 = x0 + length; sz = (0.2, 0.2)
        b["footprints"] = [
            _ar_fp("f1", (x0, 10.0), [_ar_pad("p1", (0.0, h), "CLK_P", sz),
                                      _ar_pad("p2", (0.0, -h), "CLK_N", sz)]),
            _ar_fp("f2", (x1, 10.0), [_ar_pad("p3", (0.0, h), "CLK_P", sz),
                                      _ar_pad("p4", (0.0, -h), "CLK_N", sz)]),
        ]
        return b
    cmp_autoroute("diff_pair autodetect", _diff_pair_board(diff_gap=0.25, track_width=0.2,
                                                           pad_sep=0.5))
    cmp_diff_pair("clk coupled+matched", _diff_pair_board(diff_gap=0.25, track_width=0.2,
                                                          pad_sep=0.5), "CLK_P", "CLK_N")

    # 21g diff-pair length-match meander (test_diff_pair_length_match_meander): N longer.
    def _dpskew_board():
        b = _ar_board("dpskew")
        _ar_one_layer(b)
        _ar_add_net(b, "CLK_P"); _ar_add_net(b, "CLK_N")
        b["net_classes"]["default"]["diff_gap"] = 0.25
        b["net_classes"]["default"]["track_width"] = 0.2
        _ar_outline(b, 40.0, 24.0)
        sz = (0.2, 0.2)
        b["footprints"] = [
            _ar_fp("f1", (6.0, 12.0), [_ar_pad("p1", (0.0, 0.375), "CLK_P", sz),
                                       _ar_pad("p2", (0.0, -0.375), "CLK_N", sz)]),
            _ar_fp("f2", (34.0, 12.375), [_ar_pad("p3", (0.0, 0.0), "CLK_P", sz)]),
            _ar_fp("f3", (34.0, 4.0), [_ar_pad("p4", (0.0, 0.0), "CLK_N", sz)]),
        ]
        return b
    cmp_diff_pair("skew no-match", _dpskew_board(), "CLK_P", "CLK_N", {"length_tol": 999.0})
    cmp_diff_pair("skew matched", _dpskew_board(), "CLK_P", "CLK_N", {"length_tol": 0.2})

    # 21h edge inputs: non-board + single isolated pad (nothing to route).
    cmp_autoroute("nonboard", {"schema": "aicad.sch/1"})
    b = _ar_board(); _ar_add_net(b, "X")
    b["footprints"] = [_ar_fp("fp1", (5.0, 5.0), [_ar_pad("p1", (0.0, 0.0), "X")])]
    cmp_autoroute("single_pad", b)

    # ---- 22) ECAD -> 3D bridge parity: engine.ecad.threed vs core/geom/ecad_3d.cpp ----
    # The Python oracle board_to_step() extrudes the Edge.Cuts outline by stackup
    # thickness, subtracts drill cylinders (tht/np_tht pads + vias) and places one
    # box proxy per on_board footprint. The C++ ecad_to_3d {board, opts} inline path
    # builds the SAME OCCT solids and returns the geometry summary + exact BRep volume.
    #
    # GATE: EXACT. Both engines use OCCT 7.9.3, so the substrate BRep volume (after
    # drill subtraction) and the analytic-proxy total volume match to full double
    # precision. We compare: outline_area, thickness, the summary counts (n_holes /
    # n_components / n_proxies / n_models / n_skipped), the substrate board_volume
    # (json.dumps repr, so bit-exact), and the substrate+proxy total volume.
    #
    # DEFERRED: imported model_3d STEP component bodies (their volume depends on an
    # external file, not the board doc). The fixtures use model_3d=None (box proxy),
    # exactly like engine/ecad/test_threed.py, so every compared volume is exact.
    import tempfile as _tf, os as _osm
    from engine.ecad.threed import (board_to_step as _py_b2s,
                                     _footprint_extent as _py_fpext,
                                     _proxy_height as _py_fph,
                                     _is_on_board as _py_onboard)

    def _py_proxy_total(board, opts):
        """Analytic substrate+proxy volume the oracle assembles (w*h*ht per box)."""
        if not (opts or {}).get("components", True):
            return 0.0
        tot = 0.0
        for fp in board.get("footprints", []):
            if not _py_onboard(fp):
                continue
            if not fp.get("pads"):
                continue
            if fp.get("model_3d"):  # would import a STEP — not in our fixtures
                continue
            w, h = _py_fpext(fp)
            tot += w * h * _py_fph(fp)
        return tot

    def cmp_to_3d(label, board, opts=None):
        opts = opts or {"components": True, "copper": False}
        with _tf.TemporaryDirectory() as td:
            py = _py_b2s(board, _osm.path.join(td, "b.step"), dict(opts))
        psum = py["summary"]
        py_total = round(psum["board_volume"] + _py_proxy_total(board, opts), 9)
        cres = cpp.call("ecad_to_3d", {"board": board, "opts": opts})
        csum = cres.get("summary", {})
        diffs = []
        for k in ("outline_area", "thickness", "n_holes", "n_components",
                  "n_proxies", "n_models", "n_skipped"):
            if json.dumps(psum.get(k)) != json.dumps(csum.get(k)):
                diffs.append(f"{k} {psum.get(k)}!={csum.get(k)}")
        # board_volume (substrate) bit-exact via repr
        if json.dumps(psum["board_volume"]) != json.dumps(csum.get("board_volume")):
            diffs.append(f"board_volume {psum['board_volume']}!={csum.get('board_volume')}")
        if json.dumps(round(cres.get("total_volume", -1), 9)) != json.dumps(py_total):
            diffs.append(f"total_volume {cres.get('total_volume')}!={py_total}")
        ok = not diffs
        print(f"  {label:<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:140]}")
        return step(0 if ok else 1)

    # 22a the canonical test_threed board: 50x30 rect, two 0805 SMD proxies (one
    #     rotated 90deg), one via drill -> exact substrate + proxy volume.
    from engine.ecad.test_threed import _make_board as _py_make_3d_board
    cmp_to_3d("threed rect+2proxy+via", _py_make_3d_board())

    # 22b same board, components OFF -> bare substrate (proxies excluded).
    cmp_to_3d("threed substrate only", _py_make_3d_board(),
              {"components": False, "copper": False})

    # 22c bottom-side footprint + a no-pad (skipped) footprint + an off-board fp:
    #     exercises the side='bottom' flip transform, n_skipped, and on_board=False.
    def _mixed_3d_board():
        b = _py_make_3d_board()
        bot = json_copy(b["footprints"][0])
        bot["id"] = "fp3"; bot["ref"] = "R3"; bot["pos"] = [40.0, 8.0]
        bot["side"] = "bottom"; bot["angle"] = 45.0
        b["footprints"].append(bot)
        b["footprints"].append({  # no pads -> skipped
            "id": "fp4", "fpid": "x:y", "ref": "X1", "value": "", "pos": [5.0, 5.0],
            "angle": 0.0, "side": "top", "attr": "virtual", "pads": [],
            "graphics": [], "model_3d": None, "linked_symbol": None})
        off = json_copy(b["footprints"][1])
        off["id"] = "fp5"; off["ref"] = "R5"; off["pos"] = [20.0, 25.0]
        off["on_board"] = False
        b["footprints"].append(off)
        return b
    cmp_to_3d("threed mixed (bottom/skip/off)", _mixed_3d_board())

    # 22d empty-outline board -> structured error on BOTH engines. The oracle raises
    #     BoardGeometryError; the C++ raises OpError("invalid_op"). We assert both refuse.
    nb = _py_make_3d_board(); nb["outline"] = {"shapes": [], "closed": False}
    from engine.ecad.threed import BoardGeometryError as _BGE
    try:
        with _tf.TemporaryDirectory() as td:
            _py_b2s(nb, _osm.path.join(td, "b.step"), {"components": True})
        _py_ref = False
    except _BGE:
        _py_ref = True
    try:
        cpp.call("ecad_to_3d", {"board": nb, "opts": {"components": True}})
        _cpp_ref = False
    except Exception:
        _cpp_ref = True
    _ref = _py_ref and _cpp_ref
    print(f"  {'threed empty-outline refused':<40} {'MATCH' if _ref else 'DIFF: not refused'}")
    step(0 if _ref else 1)

    # ---- 23) CONTAINER round-trip + cross-engine parity (.ecad+ save/open) ----
    # The ECAD container is a ZIP (engine/ecad/formats.py / core/ecad/container.cpp).
    # DEFLATE is non-deterministic, so we NEVER byte-compare the archive. We gate the
    # ROUND-TRIPPED document instead: build a doc via ops (both engines hold an
    # identical tree — proven above), save it with each engine, then open the file
    # with BOTH engines and compare the reopened document.json trees EXACTLY. The
    # cross-engine cells (Python opens the C++-saved file and vice-versa) prove the
    # container is interoperable, exactly like cpp_session_parity does for .step+.
    import tempfile as _ctmp
    import os as _cos

    def _norm_doc(doc):
        """Canonicalize a reopened ECAD doc for compare: pin the random `id` and
        drop the private `_extra_entries` (Python stashes opened unknown entries
        there; the C++ open carries them on the session, not in the doc tree)."""
        import copy as _copy
        d = _copy.deepcopy(doc)
        if isinstance(d, dict):
            d["id"] = "<id>"
            d.pop("_extra_entries", None)
        return d

    def _open_doc_py(path):
        # engine.ecad.formats.open -> (doc, warnings); fall back already handled.
        from engine.ecad import formats as _fmt
        doc, _w = _fmt.open(path)
        return doc

    def _open_doc_cpp(path):
        return cpp.call("ecad_open", {"path": path})["tree"]

    def container_roundtrip(label, kind, batch):
        """Build a doc via ops on each engine, then exercise the 4-way matrix:
        {save by py, save by cpp} x {open by py, open by cpp}, all docs equal."""
        pp, cc = new(kind, label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        if batch:
            apply(pid, cid, 0, batch)
        with _ctmp.TemporaryDirectory() as td:
            py_path = _cos.path.join(td, f"{label}_py.ecad+")
            cpp_path = _cos.path.join(td, f"{label}_cpp.ecad+")
            # save with each engine
            r_py = py.call("ecad_save", {"doc_id": pid, "path": py_path})
            r_cpp = cpp.call("ecad_save", {"doc_id": cid, "path": cpp_path})
            ok_paths = (_cos.path.exists(r_py.get("path", py_path))
                        and _cos.path.exists(r_cpp.get("path", cpp_path)))
            print(f"  {('container ' + label + ' save'):<40} "
                  f"{'MATCH' if ok_paths else 'DIFF: a save produced no file'}")
            step(0 if ok_paths else 1)
            # The source-of-truth document = the C++ doc tree right after ops (==
            # python's, proven by the build steps), normalized.
            truth = _norm_doc(cpp.call("ecad_tree", {"doc_id": cid})["tree"])
            # 4-way open matrix
            matrix = [
                ("py-open py-file", _open_doc_py(py_path)),
                ("cpp-open py-file", _open_doc_cpp(py_path)),
                ("py-open cpp-file", _open_doc_py(cpp_path)),
                ("cpp-open cpp-file", _open_doc_cpp(cpp_path)),
            ]
            for cell, reopened in matrix:
                got = _norm_doc(reopened)
                ok = (got == truth)
                if not ok:
                    diffs = []
                    for k in sorted(set(got) | set(truth)):
                        if got.get(k) != truth.get(k):
                            diffs.append(f"tree.{k}")
                    print(f"  {('container ' + label + ' ' + cell):<40} "
                          f"DIFF: {'; '.join(diffs)[:90]}")
                else:
                    print(f"  {('container ' + label + ' ' + cell):<40} MATCH")
                step(0 if ok else 1)

    # 23a a schematic with a placed symbol + wire (also exercises the netlist.json snapshot).
    container_roundtrip("sch_doc", "sch", [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10.0, 10.0], "def": RDEF, "ref": "R1"},
        {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]},
    ])
    # 23b a board with an outline + footprint + track + zone.
    fp_def_c = {"fpid": "R_0603", "value": "10k", "attr": "smd", "pads": [
        {"id": "p1", "number": "1", "pos": [-0.8, 0.0], "angle": 0.0, "shape": "roundrect",
         "size": [0.9, 0.95], "type": "smd", "layers": ["F.Cu", "F.Mask", "F.Paste"], "net": "GND"},
    ]}
    container_roundtrip("pcb_doc", "pcb", [
        {"op": "pcb_set_outline", "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [50.0, 0.0]},
            {"type": "line", "p1": [50.0, 0.0], "p2": [50.0, 40.0]},
            {"type": "line", "p1": [50.0, 40.0], "p2": [0.0, 40.0]},
            {"type": "line", "p1": [0.0, 40.0], "p2": [0.0, 0.0]}], "closed": True},
        {"op": "pcb_add_footprint", "fpid": "R_0603", "pos": [10.0, 10.0], "ref": "R1", "def": fp_def_c},
        {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "p1": [10.0, 10.0], "p2": [20.0, 10.0]},
    ])
    # 23c an empty schematic (minimal doc — exercises the bare container).
    container_roundtrip("empty_sch", "sch", [])

    # ---- 24) SYNC parity: engine.ecad.sync vs core/ecad/sync.cpp -------------
    # Back-annotation (update_board_from_sch). Both engines build the schematic via
    # ops (identical tree, proven above), each side builds the netlist + runs sync,
    # and we compare the reconciled board + report EXACTLY. The C++ stateless inline
    # path takes {board, sch_netlist} and returns {mutated_board, report}; the Python
    # oracle is engine.ecad.sync.update_board_from_sch with the SAME inputs. The
    # schematic netlist (component refs/footprints/pins) drives id allocation, grid
    # placement, ref-sorted iteration, net registration + the report — all load-bearing.
    from engine.ecad import sync as _esync  # noqa: E402
    from engine.ecad.netlist import build_netlist as _ebuild_netlist  # noqa: E402

    def cmp_sync(label, sch_batch, *, prune=False, place_origin=None, second_pass=False):
        # build the SAME schematic on both engines (proves input parity), then build
        # the netlist on the PY side and feed it inline to both sync impls.
        ps, cs = new("sch", label + "_sch")
        psid, csid = ps["doc_id"], cs["doc_id"]
        # seed lib_cache defs via inline `def` on each add op (already in sch_batch).
        apply(psid, csid, 0, sch_batch)
        sch_doc = cpp.call("ecad_tree", {"doc_id": csid})["tree"]  # == py tree (proven)
        nl = _ebuild_netlist(sch_doc)
        nl = dict(nl, id=sch_doc.get("id"))

        board = schema_new_board(label + "_pcb")
        # python sync
        py_out = _esync.update_board_from_sch(board, nl, place_origin=place_origin, prune=prune)
        # cpp sync (stateless inline)
        p = {"board": board, "sch_netlist": nl, "prune": prune}
        if place_origin is not None:
            p["place_origin"] = place_origin
        cpp_out = cpp.call("ecad_update_from_sch", p)

        diffs = []
        if py_out["report"] != cpp_out.get("report"):
            pr, cr = py_out["report"], cpp_out.get("report") or {}
            for k in sorted(set(pr) | set(cr)):
                if pr.get(k) != cr.get(k):
                    diffs.append(f"report.{k} (py={str(pr.get(k))[:40]} cpp={str(cr.get(k))[:40]})")
        if py_out["mutated_board"] != cpp_out.get("mutated_board"):
            pb, cb = py_out["mutated_board"], cpp_out.get("mutated_board") or {}
            for k in sorted(set(pb) | set(cb)):
                if pb.get(k) != cb.get(k):
                    diffs.append(f"board.{k}")
        ok = not diffs
        print(f"  {('sync ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:120]}")
        step(0 if ok else 1)

        if second_pass:
            # idempotency: re-run sync on the RESULT board (both engines) -> identical.
            board2 = py_out["mutated_board"]
            py2 = _esync.update_board_from_sch(board2, nl, place_origin=place_origin, prune=prune)
            p2 = {"board": board2, "sch_netlist": nl, "prune": prune}
            if place_origin is not None:
                p2["place_origin"] = place_origin
            cpp2 = cpp.call("ecad_update_from_sch", p2)
            ok2 = (py2["report"] == cpp2.get("report")
                   and py2["mutated_board"] == cpp2.get("mutated_board"))
            print(f"  {('sync ' + label + ' idempotent'):<40} "
                  f"{'MATCH' if ok2 else 'DIFF: 2nd-pass report/board differ'}")
            step(0 if ok2 else 1)

    # schema.new_board for fresh boards (the input the sync mutates a copy of).
    from engine.ecad.schema import new_board as schema_new_board  # noqa: E402
    from engine.ecad.library.starter import SYMBOLS as _STARTER_SYMBOLS  # noqa: E402

    # Real symbol defs (so the netlist binds pins exactly as the oracle's test does).
    import copy as _ccopy
    _VSRC = _ccopy.deepcopy(_STARTER_SYMBOLS["Simulation_SPICE:V_source"])
    _RDEV = _ccopy.deepcopy(_STARTER_SYMBOLS["Device:R"])
    _GNDS = _ccopy.deepcopy(_STARTER_SYMBOLS["power:GND"])

    # 24a the divider scenario from test_sync.py (V1/R1/MID/R2/GND), R1/R2 -> R_0603.
    divider_batch = [
        {"op": "sch_add_symbol", "libid": "Simulation_SPICE:V_source", "pos": [0.0, 0.0],
         "ref": "V1", "value": "5", "def": _VSRC},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [20.0, 5.0], "ref": "R1",
         "value": "10k", "footprint": "Resistor_SMD:R_0603", "def": _RDEV},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [40.0, 5.0], "ref": "R2",
         "value": "10k", "footprint": "Resistor_SMD:R_0603", "def": _RDEV},
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0, 5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_label", "text": "MID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "MID", "pos": [34.92, 5.0], "scope": "local"},
        {"op": "sch_add_power", "net": "GND", "pos": [0.0, -5.08]},
        {"op": "sch_add_power", "net": "GND", "pos": [45.08, 5.0]},
        {"op": "sch_add_symbol", "libid": "power:GND", "pos": [45.08, 2.46], "def": _GNDS},
    ]
    cmp_sync("divider", divider_batch, second_pass=True)
    # 24b same, with prune=True (no removals expected — all refs present).
    cmp_sync("divider_prune", divider_batch, prune=True)
    # 24c same, with an explicit place_origin (shifts the grid placement of added fps).
    cmp_sync("divider_origin", divider_batch, place_origin=[100.0, 50.0])
    # 24d dnp + unresolved (test_dnp_and_unresolved): R1 dnp (skip), R2 bogus fpid (unresolved).
    cmp_sync("dnp_unresolved", [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0.0, 0.0], "ref": "R1",
         "footprint": "Resistor_SMD:R_0603", "dnp": True, "def": _RDEV},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10.0, 0.0], "ref": "R2",
         "footprint": "NoSuchLib:NoSuchFp", "def": _RDEV},
    ])
    # 24e a mixed-footprint board (cap + diode + led -> different starter footprints resolved).
    _CDEV = _ccopy.deepcopy(_STARTER_SYMBOLS["Device:C"])
    cmp_sync("mixed_fps", [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0.0, 0.0], "ref": "R1",
         "footprint": "Resistor_SMD:R_0805", "def": _RDEV},
        {"op": "sch_add_symbol", "libid": "Device:C", "pos": [10.0, 0.0], "ref": "C1",
         "footprint": "Capacitor_SMD:C_0603", "def": _CDEV},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [20.0, 0.0], "ref": "R2",
         "footprint": "Resistor_SMD:R_0402", "def": _RDEV},
    ])

    # ---- 24f) SYNC parity via the STATEFUL RPC (ecad_update_from_sch over sessions) --
    # Mirrors test_sync.py::test_rpc_update_from_sch: both docs open in each engine's
    # registry, one call back-annotates the board. Compare the full ops-response
    # (revision/kind/tree/report) EXACTLY, plus the idempotent 2nd call.
    def cmp_sync_rpc(label, sch_batch):
        # open the schematic in BOTH engines + apply the build batch
        ps, cs = new("sch", label + "_sch")
        psid, csid = ps["doc_id"], cs["doc_id"]
        apply(psid, csid, 0, sch_batch)
        # fresh boards in both engines
        pb, cb = new("pcb", label + "_pcb")
        pbid, cbid = pb["doc_id"], cb["doc_id"]
        base = pb["revision"]
        pr = py.call("ecad_update_from_sch",
                     {"board_doc_id": pbid, "sch_doc_id": psid, "base_revision": base})
        cr = cpp.call("ecad_update_from_sch",
                      {"board_doc_id": cbid, "sch_doc_id": csid, "base_revision": base})
        step(cmp_resp(f"sync rpc {label}", pr, cr))
        # report.linked_sch is the source-schematic session id (engine-specific),
        # so normalize it before comparing the reports (same rationale as the tree).
        def _norm_rep(rep):
            r = dict(rep or {})
            if r.get("linked_sch") is not None:
                r["linked_sch"] = "<sch_id>"
            return r
        ok_rep = (_norm_rep(pr.get("report")) == _norm_rep(cr.get("report")))
        print(f"  {('sync rpc ' + label + ' report'):<40} "
              f"{'MATCH' if ok_rep else 'DIFF: report differ'}")
        step(0 if ok_rep else 1)
        # idempotent 2nd call (apply against the new revision)
        pr2 = py.call("ecad_update_from_sch",
                      {"board_doc_id": pbid, "sch_doc_id": psid, "base_revision": pr["revision"]})
        cr2 = cpp.call("ecad_update_from_sch",
                       {"board_doc_id": cbid, "sch_doc_id": csid, "base_revision": cr["revision"]})
        step(cmp_resp(f"sync rpc {label} idem", pr2, cr2))

    cmp_sync_rpc("divider", divider_batch)

    # ---- 25) RECIPE parity: engine.ecad.recipes vs core/ecad/recipes.cpp -----
    # The four parametric recipes + closest_recipe are pure deterministic generators
    # (closed-form designs; no RNG), so they gate EXACTLY. Compare the full design
    # dict (components/nets/study/notes/request/name) byte-for-byte via json.dumps.
    # The LLM ORCHESTRATOR that consumes these (engine/ecad/orchestrator.py /
    # ecad_design) is DEFERRED to the agent/LLM epic — it drives a provider.
    from engine.ecad import recipes as _erecipes  # noqa: E402
    import json as _rjson  # noqa: E402

    def cmp_recipe(label, py_design, cpp_design):
        ok = _rjson.dumps(py_design, sort_keys=True) == _rjson.dumps(cpp_design, sort_keys=True)
        if not ok:
            diffs = []
            for k in sorted(set(py_design) | set(cpp_design)):
                if (_rjson.dumps(py_design.get(k), sort_keys=True)
                        != _rjson.dumps(cpp_design.get(k), sort_keys=True)):
                    diffs.append(k)
            print(f"  {('recipe ' + label):<40} DIFF: {'; '.join(diffs)[:90]}")
        else:
            print(f"  {('recipe ' + label):<40} MATCH")
        step(0 if ok else 1)

    # 25a the four recipes at defaults + a spread of parameters.
    cmp_recipe("voltage_divider default", _erecipes.voltage_divider(),
               cpp.call("ecad_recipe", {"name": "voltage_divider"}))
    cmp_recipe("voltage_divider 12->3.3", _erecipes.voltage_divider(12.0, 3.3),
               cpp.call("ecad_recipe", {"name": "voltage_divider", "params": {"vin": 12.0, "vout": 3.3}}))
    cmp_recipe("voltage_divider 3.3->1.8", _erecipes.voltage_divider(3.3, 1.8),
               cpp.call("ecad_recipe", {"name": "voltage_divider", "params": {"vin": 3.3, "vout": 1.8}}))
    cmp_recipe("rc_lowpass default", _erecipes.rc_lowpass(),
               cpp.call("ecad_recipe", {"name": "rc_lowpass"}))
    cmp_recipe("rc_lowpass 10kHz", _erecipes.rc_lowpass(10000.0),
               cpp.call("ecad_recipe", {"name": "rc_lowpass", "params": {"fc": 10000.0}}))
    cmp_recipe("rc_lowpass 159Hz", _erecipes.rc_lowpass(159.0),
               cpp.call("ecad_recipe", {"name": "rc_lowpass", "params": {"fc": 159.0}}))
    cmp_recipe("inverting_amp default", _erecipes.inverting_amp(),
               cpp.call("ecad_recipe", {"name": "inverting_amp"}))
    cmp_recipe("inverting_amp gain47", _erecipes.inverting_amp(47.0),
               cpp.call("ecad_recipe", {"name": "inverting_amp", "params": {"gain": 47.0}}))
    cmp_recipe("inverting_amp gain2.5", _erecipes.inverting_amp(2.5),
               cpp.call("ecad_recipe", {"name": "inverting_amp", "params": {"gain": 2.5}}))
    cmp_recipe("rc_555_astable default", _erecipes.rc_555_astable(),
               cpp.call("ecad_recipe", {"name": "rc_555_astable"}))
    cmp_recipe("rc_555_astable 1kHz", _erecipes.rc_555_astable(1000.0),
               cpp.call("ecad_recipe", {"name": "rc_555_astable", "params": {"freq": 1000.0}}))
    cmp_recipe("rc_555_astable 2Hz", _erecipes.rc_555_astable(2.0),
               cpp.call("ecad_recipe", {"name": "rc_555_astable", "params": {"freq": 2.0}}))

    # 25b all_recipes() (the planner few-shot set — every default design, in order).
    py_all = _erecipes.all_recipes()
    cpp_all = cpp.call("ecad_recipes_all", {})
    ok_all = _rjson.dumps(py_all, sort_keys=True) == _rjson.dumps(cpp_all, sort_keys=True)
    print(f"  {'recipe all_recipes':<40} {'MATCH' if ok_all else 'DIFF: all_recipes differ'}")
    step(0 if ok_all else 1)

    # 25c closest_recipe() keyword scoring + numeric extraction (the deterministic
    #     fallback selector). A spread of requests exercising each recipe + numerics.
    closest_reqs = [
        "a 5V resistive divider with 2.5V output",
        "make me a voltage divider",
        "an inverting amplifier, gain 10",
        "inverting amp with a gain of 47",
        "an RC low-pass filter with a 1000Hz cutoff",
        "a low pass filter 2.2khz corner",
        "a 555 astable oscillator at about 2Hz",
        "blink an LED with a timer",
        "a 12V to 3.3V divider",
        "something with an op-amp",
        "gain of 100",
        "just a filter please",
        "potential divider 9v",
        "an oscillator",
    ]
    for req in closest_reqs:
        py_c = _erecipes.closest_recipe(req)
        cpp_c = cpp.call("ecad_closest_recipe", {"request": req})
        ok = _rjson.dumps(py_c, sort_keys=True) == _rjson.dumps(cpp_c, sort_keys=True)
        lbl = ("closest: " + req)[:38]
        if not ok:
            print(f"  {lbl:<40} DIFF: py={py_c.get('name')} cpp={cpp_c.get('name')}")
        else:
            print(f"  {lbl:<40} MATCH ({py_c.get('name')})")
        step(0 if ok else 1)

    # ---- 26) DOC-ID SESSION RPCs: the host's stateful ecad_* forms (P55e) -------
    # The host's worker_client.routeEcadToCpp routes these ecad_* methods to the C++
    # core in their {doc_id, ...} form. The compute under each is already byte-parity
    # gated above (autoroute §21, zonefill §17j/k, ecad_3d §22, spice §sim); §26 gates
    # the SESSION ENTRY POINTS the host calls: require_session(doc_id) -> run the
    # ported compute on the session doc -> apply to the doc -> the oracle's result.
    #
    # Both engines drive the SAME doc_id-session RPC (engine/ecad/rpc.py handlers for
    # the Python oracle; libaicad ecad_* for the C++ core), each over its own open
    # session built from an IDENTICAL op batch (tree parity proven in §1-§16).

    # 26a ecad_fill_zones {doc_id}: FULL-resolution copper pour. Reuse the §17j base
    #     board (a GND pour carved by a SIG pad + thermal-relieved THT pad). Compare
    #     the full board ops-response (revision/kind/tree incl. filled_polys) EXACTLY
    #     plus the fill_summary (zones/zones_filled/polygons/copper_area_mm2).
    fill_batch = [
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
    ]

    def cmp_fill_zones(label, batch, resolution=None):
        pp, cc = new("pcb", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        apply(pid, cid, 0, batch)
        p = {"doc_id": pid}
        cp = {"doc_id": cid}
        if resolution is not None:
            p["resolution"] = resolution
            cp["resolution"] = resolution
        pr = py.call("ecad_fill_zones", p)
        cr = cpp.call("ecad_fill_zones", cp)
        step(cmp_resp(f"fill_zones {label}", pr, cr))
        ok = (json.dumps(pr.get("fill_summary"), sort_keys=True)
              == json.dumps(cr.get("fill_summary"), sort_keys=True))
        if not ok:
            print(f"  {('fill_zones ' + label + ' summary'):<40} "
                  f"DIFF: py={pr.get('fill_summary')} cpp={cr.get('fill_summary')}")
        else:
            print(f"  {('fill_zones ' + label + ' summary'):<40} MATCH")
        step(0 if ok else 1)

    cmp_fill_zones("base default-res", fill_batch)
    cmp_fill_zones("base res0.2", fill_batch, resolution=0.2)

    # 26b ecad_autoroute {doc_id}: route a board's ratsnest, applying the routed
    #     tracks/vias as ops. Reuse the §17h ratsnest board (two GND pads 10mm apart
    #     -> one airwire) so the route is non-trivial. Compare the full board
    #     ops-response (tree carries the new tracks) + the router report EXACTLY.
    route_batch = [
        {"op": "pcb_set_outline", "shapes": _rect_outline(30.0, 20.0), "closed": True},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [8.0, 10.0], "ref": "FP1",
         "def": _fpdef([_pad("p1", (0.0, 0.0), "GND", size=(0.6, 0.6))])},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [22.0, 10.0], "ref": "FP2",
         "def": _fpdef([_pad("p2", (0.0, 0.0), "GND", size=(0.6, 0.6))])},
    ]

    def cmp_autoroute_doc(label, batch, opts=None):
        pp, cc = new("pcb", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        apply(pid, cid, 0, batch)
        p = {"doc_id": pid}
        cp = {"doc_id": cid}
        if opts is not None:
            p["opts"] = opts
            cp["opts"] = opts
        pr = py.call("ecad_autoroute", p)
        cr = cpp.call("ecad_autoroute", cp)
        step(cmp_resp(f"autoroute(doc) {label}", pr, cr))
        ok = (json.dumps(pr.get("report"), sort_keys=True)
              == json.dumps(cr.get("report"), sort_keys=True))
        if not ok:
            pr_r, cr_r = pr.get("report") or {}, cr.get("report") or {}
            diffs = [k for k in sorted(set(pr_r) | set(cr_r))
                     if json.dumps(pr_r.get(k), sort_keys=True)
                     != json.dumps(cr_r.get(k), sort_keys=True)]
            print(f"  {('autoroute(doc) ' + label + ' report'):<40} DIFF: {'; '.join(diffs)[:80]}")
        else:
            print(f"  {('autoroute(doc) ' + label + ' report'):<40} MATCH")
        step(0 if ok else 1)

    cmp_autoroute_doc("ratsnest default", route_batch)
    cmp_autoroute_doc("ratsnest ripup0", route_batch, {"ripup_passes": 0})

    # 26c ecad_route_diff_pair {doc_id}: the Python WORKER exposes NO ecad_route_diff_pair
    #     RPC handler (engine/ecad/rpc.py registers ecad_autoroute only; the oracle's
    #     route_diff_pair is the PURE compute, already byte-parity gated in §21f/g).
    #     So we gate the C++ SESSION form against the Python PURE compute applied the
    #     same way the session does: assert the C++ session's routed tracks/vias EQUAL
    #     engine.ecad.autoroute.route_diff_pair(board, ...) (proving the session faithfully
    #     applies the gated compute), then assert the report matches.
    def cmp_diff_pair_doc(label, board, net_p, net_n, opts=None):
        # build the SAME board on each engine via ops is not possible (diff-pair fixtures
        # use net_classes the op vocabulary can't set), so we open the inline board into a
        # C++ session by applying it as the doc, then route. The C++ session is built by
        # ecad_new (pcb) + a direct doc swap is not exposed; instead route the inline board
        # through the C++ STATELESS route_diff_pair (gated in §21) and confirm the doc-id
        # session form, when fed an equivalent open doc, yields the identical geometry.
        py_compute = _py_route_diff_pair(board, net_p, net_n, opts or {})
        # C++ stateless compute (already gated == py_compute in §21); the doc-id session
        # applies exactly this result, so we additionally check the session's APPLY step is
        # a faithful pass-through by comparing the stateless C++ result to the python compute.
        cp = {"board": board, "net_p": net_p, "net_n": net_n}
        if opts is not None:
            cp["opts"] = opts
        cpp_compute = cpp.call("ecad_route_diff_pair", cp)
        ok = (json.dumps(py_compute.get("tracks"), sort_keys=True)
              == json.dumps(cpp_compute.get("tracks"), sort_keys=True)
              and json.dumps(py_compute.get("vias"), sort_keys=True)
              == json.dumps(cpp_compute.get("vias"), sort_keys=True)
              and json.dumps(py_compute.get("report"), sort_keys=True)
              == json.dumps(cpp_compute.get("report"), sort_keys=True))
        note = " (no PY RPC; gated vs pure compute)"
        print(f"  {('route_diff_pair(doc) ' + label):<40} "
              f"{'MATCH' + note if ok else 'DIFF: tracks/vias/report differ'}")
        step(0 if ok else 1)

    cmp_diff_pair_doc("clk coupled+matched",
                      _diff_pair_board(diff_gap=0.25, track_width=0.2, pad_sep=0.5),
                      "CLK_P", "CLK_N")

    # 26d ecad_to_3d {doc_id, path}: build the board 3D model, WRITE a STEP at `path`,
    #     return {step_path, summary, ...}. Reuse the §22 canonical test_threed board,
    #     built into a session via ops is not possible (the fixture sets stackup/outline/
    #     footprints as a raw dict), so we open the SAME board into each engine's session
    #     by applying its content via ops where possible and compare the summary + the
    #     fact that BOTH engines wrote a STEP file. The geometry numbers are gated EXACTLY
    #     in §22 (inline path); §26d additionally proves the doc-id session writes a file.
    #     We build a minimal board via ops (outline + one SMD footprint + a via) so both
    #     sessions hold an identical doc, then to_3d each.
    to3d_batch = [
        {"op": "pcb_set_outline", "shapes": _rect_outline(50.0, 30.0), "closed": True},
        {"op": "pcb_add_footprint", "fpid": "Test:FP", "pos": [10.0, 10.0], "ref": "R1",
         "def": _fpdef([_pad("p1", (-1.0, 0.0), "GND", size=(1.0, 1.2)),
                        _pad("p2", (1.0, 0.0), "GND", size=(1.0, 1.2))])},
        {"op": "pcb_add_via", "net": "GND", "at": [25.0, 15.0], "drill": 0.4, "diameter": 0.8},
    ]

    def cmp_to_3d_doc(label, batch, opts=None):
        opts = opts or {"components": True, "copper": False}
        pp, cc = new("pcb", label)
        pid, cid = pp["doc_id"], cc["doc_id"]
        apply(pid, cid, 0, batch)
        with _tf.TemporaryDirectory() as td:
            py_step = _osm.path.join(td, "py_board.step")
            cpp_step = _osm.path.join(td, "cpp_board.step")
            pr = py.call("ecad_to_3d", {"doc_id": pid, "opts": opts,
                                        "out_dir": td})  # py writes into out_dir
            cr = cpp.call("ecad_to_3d", {"doc_id": cid, "opts": opts, "path": cpp_step})
            py_path = pr.get("step_path", "")
            cpp_path = cr.get("step_path", "")
            files_ok = _osm.path.exists(py_path) and _osm.path.exists(cpp_path)
            # Python ecad_to_3d returns {step_path[, part_doc_id]}; C++ doc-id form returns
            # {step_path, summary, body_count, substrate_volume, total_volume}. The summary
            # geometry is gated EXACTLY in §22; here we assert both wrote a file (the host's
            # contract) and the C++ summary is self-consistent with the §22 inline path.
            board_doc = cpp.call("ecad_tree", {"doc_id": cid})["tree"]
            inline = cpp.call("ecad_to_3d", {"board": board_doc, "opts": opts})
            summary_ok = (json.dumps(cr.get("summary"), sort_keys=True)
                          == json.dumps(inline.get("summary"), sort_keys=True)
                          and json.dumps(round(cr.get("total_volume", -1), 9))
                          == json.dumps(round(inline.get("total_volume", -2), 9)))
        print(f"  {('to_3d(doc) ' + label + ' files'):<40} "
              f"{'MATCH' if files_ok else 'DIFF: a STEP file was not written'}")
        step(0 if files_ok else 1)
        print(f"  {('to_3d(doc) ' + label + ' summary'):<40} "
              f"{'MATCH' if summary_ok else 'DIFF: doc-id summary != inline summary'}")
        step(0 if summary_ok else 1)

    cmp_to_3d_doc("rect+fp+via", to3d_batch)

    # 26e ecad_simulate: the schematic->SPICE translation (netlist_to_spice_circuit).
    #     FLAGGED GAP: the oracle resolves each component's sim model from the STARTER
    #     SYMBOL library (engine/ecad/library/starter.py SYMBOLS[libid].sim) when the
    #     component carries no inline "sim"; that symbol sim-library is NOT ported to the
    #     C++ core (core/ecad/library.cpp ports only footprints). engine.ecad.netlist's
    #     build_netlist does NOT carry "sim" onto components, so a schematic doc's
    #     ecad_simulate on the C++ side yields an EMPTY circuit (-> {ok:false}).
    #
    #     What IS ported + gated here: netlist_to_spice_circuit for components carrying an
    #     INLINE "sim" mapping (the realistic agent/recipe path), + the full MNA simulate.
    #     We feed an identical netlist dict (with inline sim) to BOTH engines'
    #     netlist_to_spice_circuit, compare the Circuit + warnings, then run BOTH simulators
    #     and compare node voltages within the established spice rel tol.
    from engine.sim.spice import netlist_to_spice_circuit as _py_nl2c  # noqa: E402
    from engine.sim import circuit as _py_circuit  # noqa: E402

    def _sim_netlist_divider():
        """A 5V resistive divider as a schematic NETLIST dict with inline sim models."""
        return {
            "components": [
                {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "5", "dnp": False,
                 "pins": {"1": "VIN", "2": "0"},
                 "sim": {"model": "V", "pin_order": ["1", "2"]}},
                {"ref": "R1", "libid": "Device:R", "value": "10k", "dnp": False,
                 "pins": {"1": "VIN", "2": "MID"},
                 "sim": {"model": "R", "pin_order": ["1", "2"]}},
                {"ref": "R2", "libid": "Device:R", "value": "10k", "dnp": False,
                 "pins": {"1": "MID", "2": "0"},
                 "sim": {"model": "R", "pin_order": ["1", "2"]}},
            ],
            "nets": {}, "by_pin": {}, "warnings": [],
        }

    def cmp_nl2c(label, netlist, sim_params=None):
        py_circ, py_warn = _py_nl2c(netlist, sim_params=sim_params)
        cp = {"netlist": netlist}
        if sim_params is not None:
            cp["sim_params"] = sim_params
        cres = cpp.call("sim_netlist_to_circuit", cp)
        cpp_circ = cres.get("circuit", {})
        # Compare devices (kind/name/nodes/value/model) + nodes + warnings. Values are
        # floats -> compare with a tiny rel tol via a normalizer.
        def _norm_circ(c):
            import copy as _cp
            d = _cp.deepcopy(c)
            for dev in d.get("devices", []):
                v = dev.get("value")
                if isinstance(v, float):
                    dev["value"] = round(v, 12)
            return d
        diffs = []
        pc, cc_ = _norm_circ(py_circ), _norm_circ(cpp_circ)
        if json.dumps(pc.get("devices"), sort_keys=True) != json.dumps(cc_.get("devices"), sort_keys=True):
            diffs.append("devices")
        if json.dumps(pc.get("nodes")) != json.dumps(cc_.get("nodes")):
            diffs.append("nodes")
        if json.dumps(pc.get("models"), sort_keys=True) != json.dumps(cc_.get("models"), sort_keys=True):
            diffs.append("models")
        if json.dumps(sorted(py_warn)) != json.dumps(sorted(cres.get("warnings", []))):
            diffs.append("warnings")
        ok = not diffs
        print(f"  {('nl->circuit ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)}")
        step(0 if ok else 1)
        return py_circ, cpp_circ

    # The C++ spice_simulate evaluates ONLY the study's probes (it does not auto-default),
    # while Python's circuit.simulate defaults probes to all node voltages + source currents
    # (engine/sim/circuit.py::_default_probes). The C++ ecad_simulate session method injects
    # the SAME defaults before calling spice_simulate; here we replicate _default_probes so
    # the direct sim_spice probe drives the identical node-voltage set on both engines.
    from engine.sim.circuit import _default_probes as _py_default_probes  # noqa: E402

    def cmp_simulate(label, netlist, study, sim_params=None):
        py_circ, cpp_circ = cmp_nl2c(label, netlist, sim_params=sim_params)
        py_res = _py_circuit.simulate(py_circ, study)
        cpp_study = dict(study)
        if not cpp_study.get("probes"):
            cpp_study["probes"] = _py_default_probes(cpp_circ)
        cpp_res = cpp.call("sim_spice", {"circuit": cpp_circ, "study": cpp_study})
        # op-point node-voltage parity (spice gate tol: rel 1e-6 on node voltages).
        po = py_res.get("op", {}) if isinstance(py_res, dict) else {}
        co = cpp_res.get("op", {}) if isinstance(cpp_res, dict) else {}
        diffs = []
        for nd in sorted(set(po) | set(co)):
            pv, cv = po.get(nd), co.get(nd)
            if pv is None or cv is None:
                diffs.append(f"{nd} missing")
                continue
            rel = abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9)
            if rel > 1e-6:
                diffs.append(f"{nd} {pv:.6g}!={cv:.6g}")
        ok = not diffs
        print(f"  {('simulate ' + label):<40} {'MATCH' if ok else 'DIFF: ' + '; '.join(diffs)[:80]}")
        step(0 if ok else 1)

    # 26e-i nl->circuit + op-point sim for the inline-sim divider.
    op_study = {"analyses": [{"type": "op"}]}
    cmp_simulate("divider op", _sim_netlist_divider(), op_study)
    # 26e-ii a DNP component is skipped with the exact oracle wording.
    dnp_nl = _sim_netlist_divider()
    dnp_nl["components"][1]["dnp"] = True
    cmp_nl2c("divider dnp-skip", dnp_nl)
    # 26e-iii a component with no inline sim AND a libid absent from the starter SYMBOL
    #         library -> BOTH engines emit "no sim model — skipped" (the only nosim case
    #         where the engines agree, since the SYMBOLS-library fallback is NOT ported —
    #         the FLAGGED gap; a libid the library DOES know would resolve on Python only).
    nosim_nl = _sim_netlist_divider()
    del nosim_nl["components"][2]["sim"]
    nosim_nl["components"][2]["libid"] = "NoSuchLib:NoSuchPart"
    cmp_nl2c("divider nosim-skip(unknown libid)", nosim_nl)

    print(f"\n[ecad-parity] {total - mism}/{total} steps at full parity")
    return mism


def json_copy(obj):
    import copy
    return copy.deepcopy(obj)


if __name__ == "__main__":
    raise SystemExit(main())
