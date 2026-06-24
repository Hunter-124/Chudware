"""ECAD document ops + session tests (house-style plain runner, NOT pytest).

Run:  & $py -m engine.ecad.test_ops   (from repo root)

Self-contained: every schematic fixture seeds an inline ``lib_cache`` (a minimal SymbolDef with two
pins) so these tests do NOT depend on the parallel library module. Every document an op batch
produces is asserted to pass ``schema.validate`` (empty error list).

Covers the card's obligations:
  * placeholder batch: add a symbol ($0) + a wire/connect referencing $0 in one batch
  * atomic validation: one bad op rejects the whole batch, NO mutation
  * patch-edit semantics (present keys replace; null clears)
  * delete (symbol / wire)
  * undo / redo via the session layer
  * stale-revision guard
  * auto-ref assignment (next free <prefix><n>)
  * a PCB batch (set outline, add footprint, add track) round-trips + validates via validate_board
"""

from __future__ import annotations

import copy

from engine.ecad import schema
from engine.ecad.ops import OpError, apply_ops
from engine.ecad.session import (
    apply_ops as session_apply_ops, new_session, undo as session_undo, redo as session_redo,
)


# --------------------------------------------------------------------------- fixtures


def _res_def() -> dict:
    """A minimal 2-pin passive SymbolDef (a resistor), self-contained for lib_cache."""
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


def _blank_sch() -> dict:
    doc = schema.new_schematic("Test")
    doc["lib_cache"]["Device:R"] = _res_def()
    return doc


def _blank_pcb() -> dict:
    return schema.new_board("TestBoard")


def _add_R(pos, ref=None):
    op = {"op": "sch_add_symbol", "libid": "Device:R", "pos": pos}
    if ref is not None:
        op["ref"] = ref
    return op


# --------------------------------------------------------------------------- placeholder batch


def test_placeholder_batch_symbol_and_wire() -> None:
    """One batch: add a symbol (op 0), then auto-route a wire to its pin via $0 (op 1). The $0
    placeholder must resolve to the just-created symbol id, and the resulting doc must validate."""
    doc = _blank_sch()
    batch = [
        _add_R([10.0, 10.0]),
        {"op": "sch_connect",
         "from": {"sym": "$0", "pin": "1"},
         "to": {"sym": "$0", "pin": "2"}},
    ]
    res = apply_ops(doc, batch)
    new_doc = res["doc"]
    assert schema.validate(new_doc) == [], schema.validate(new_doc)
    assert res["id_map"]["$0"] == "s1", res["id_map"]
    assert res["id_map"]["$1"] == "w1", res["id_map"]
    sym = new_doc["symbols"][0]
    assert sym["id"] == "s1" and sym["libid"] == "Device:R"
    wire = new_doc["wires"][0]
    assert wire["id"] == "w1" and len(wire["pts"]) >= 2
    # endpoints must equal the two pins' world coords (schema.pin_world)
    p1 = schema.pin_world(sym, _res_def()["pins"][0])
    assert abs(wire["pts"][0][0] - p1[0]) < 1e-6 and abs(wire["pts"][0][1] - p1[1]) < 1e-6
    print(f"[ecad-ops] placeholder batch: symbol s1 + auto-routed wire w1 ($0 -> s1), "
          f"pts={wire['pts']}")


def test_placeholder_forward_ref_rejected() -> None:
    """A placeholder that refers to the CURRENT op (or a later op) is an invalid_op."""
    doc = _blank_sch()
    try:
        apply_ops(doc, [{"op": "sch_edit_symbol", "id": "$0", "value": "1k"}])
        raise AssertionError("forward/self placeholder must be rejected")
    except OpError as e:
        assert e.code == "invalid_op", e.to_envelope()
    print("[ecad-ops] forward placeholder $0 referenced before creation -> invalid_op")


# --------------------------------------------------------------------------- atomic validation


def test_atomic_validation_one_bad_op_rejects_batch() -> None:
    """A batch with one bad op (unknown symbol edit) must reject the WHOLE batch and leave the
    original doc untouched (no partial mutation)."""
    doc = _blank_sch()
    before = copy.deepcopy(doc)
    batch = [
        _add_R([0.0, 0.0]),                                   # good
        {"op": "sch_edit_symbol", "id": "s999", "value": "x"},  # bad: unknown object
        _add_R([5.0, 0.0]),                                   # good (never reached)
    ]
    try:
        apply_ops(doc, batch)
        raise AssertionError("batch with a bad op must raise")
    except OpError as e:
        assert e.code == "unknown_object", e.to_envelope()
    assert doc == before, "the input doc must NOT be mutated when a batch fails"
    print("[ecad-ops] atomic: bad op rejects the whole batch (unknown_object), no mutation")


def test_atomic_validation_bad_result_doc() -> None:
    """An op that would produce a schema-invalid doc (bad mirror) is rejected before any swap."""
    doc = _blank_sch()
    before = copy.deepcopy(doc)
    try:
        apply_ops(doc, [{"op": "sch_add_symbol", "libid": "Device:R",
                         "pos": [0.0, 0.0], "mirror": "diagonal"}])
        raise AssertionError("bad mirror must be rejected")
    except OpError as e:
        assert e.code == "invalid_op", e.to_envelope()
    assert doc == before
    print("[ecad-ops] atomic: invalid field (mirror) rejected, doc unchanged")


# --------------------------------------------------------------------------- patch edit


def test_patch_edit_replaces_and_clears() -> None:
    """sch_edit_symbol patch: present keys replace; a null value clears the optional key."""
    doc = _blank_sch()
    res = apply_ops(doc, [_add_R([0.0, 0.0])])
    doc = res["doc"]
    # set a footprint + value
    doc = apply_ops(doc, [{"op": "sch_edit_symbol", "id": "s1",
                           "value": "10k", "footprint": "Resistor_SMD:R_0603"}])["doc"]
    s = doc["symbols"][0]
    assert s["value"] == "10k" and s["footprint"] == "Resistor_SMD:R_0603"
    # null clears the optional footprint key; value untouched
    doc = apply_ops(doc, [{"op": "sch_edit_symbol", "id": "s1", "footprint": None}])["doc"]
    s = doc["symbols"][0]
    assert "footprint" not in s, "null must clear the optional key"
    assert s["value"] == "10k", "untouched keys remain"
    assert schema.validate(doc) == [], schema.validate(doc)
    print("[ecad-ops] patch edit: present keys replace, null clears, untouched keys remain")


# --------------------------------------------------------------------------- delete


def test_delete_symbol_and_wire() -> None:
    doc = _blank_sch()
    doc = apply_ops(doc, [_add_R([0.0, 0.0]),
                          {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]}])["doc"]
    assert len(doc["symbols"]) == 1 and len(doc["wires"]) == 1
    doc = apply_ops(doc, [{"op": "sch_delete_symbol", "id": "s1"}])["doc"]
    assert doc["symbols"] == [], "symbol deleted"
    assert len(doc["wires"]) == 1, "wire left dangling (re-netlisted), not deleted"
    doc = apply_ops(doc, [{"op": "sch_delete_wire", "id": "w1"}])["doc"]
    assert doc["wires"] == []
    # deleting a missing object -> unknown_object
    try:
        apply_ops(doc, [{"op": "sch_delete_wire", "id": "w1"}])
        raise AssertionError("delete of missing wire must raise")
    except OpError as e:
        assert e.code == "unknown_object", e.to_envelope()
    print("[ecad-ops] delete: symbol cascades (wire left dangling); delete missing -> unknown_object")


# --------------------------------------------------------------------------- auto-ref


def test_auto_ref_assignment() -> None:
    """Omitting ref auto-assigns the next free <prefix><n>; an explicit ref is honored and the
    auto-numberer skips it."""
    doc = _blank_sch()
    doc = apply_ops(doc, [_add_R([0.0, 0.0])])["doc"]           # R1 (auto)
    assert doc["symbols"][0]["ref"] == "R1", doc["symbols"][0]["ref"]
    doc = apply_ops(doc, [_add_R([10.0, 0.0], ref="R5")])["doc"]  # explicit R5
    doc = apply_ops(doc, [_add_R([20.0, 0.0])])["doc"]          # next free auto -> R2
    refs = [s["ref"] for s in doc["symbols"]]
    assert refs == ["R1", "R5", "R2"], refs
    doc = apply_ops(doc, [_add_R([30.0, 0.0])])["doc"]          # R3
    assert doc["symbols"][-1]["ref"] == "R3"
    print(f"[ecad-ops] auto-ref: {[s['ref'] for s in doc['symbols']]} (skips explicit R5)")


def test_sch_annotate_reset() -> None:
    """sch_annotate mode=reset renumbers every symbol deterministically by position."""
    doc = _blank_sch()
    doc = apply_ops(doc, [_add_R([30.0, 0.0], ref="R9"),
                          _add_R([10.0, 0.0], ref="R7"),
                          _add_R([20.0, 0.0], ref="R8")])["doc"]
    doc = apply_ops(doc, [{"op": "sch_annotate", "scope": "all", "mode": "reset"}])["doc"]
    # sorted by x -> 10,20,30 -> R1,R2,R3
    by_pos = sorted(doc["symbols"], key=lambda s: s["pos"][0])
    assert [s["ref"] for s in by_pos] == ["R1", "R2", "R3"], [s["ref"] for s in by_pos]
    print("[ecad-ops] sch_annotate reset: renumbered R1..R3 by position")


# --------------------------------------------------------------------------- session: undo/redo + stale


def test_session_undo_redo() -> None:
    s = new_session("sch", "UndoTest")
    s.doc["lib_cache"]["Device:R"] = _res_def()
    r1 = session_apply_ops(s, 0, [_add_R([0.0, 0.0])])
    assert r1["revision"] == 1 and r1["kind"] == "sch"
    assert "compile" in r1 and "erc" in r1["compile"]
    r2 = session_apply_ops(s, 1, [_add_R([10.0, 0.0])])
    assert r2["revision"] == 2 and len(s.doc["symbols"]) == 2
    # undo removes the 2nd symbol
    ru = session_undo(s)
    assert ru["revision"] == 3 and len(s.doc["symbols"]) == 1, s.doc["symbols"]
    # redo restores it
    rr = session_redo(s)
    assert rr["revision"] == 4 and len(s.doc["symbols"]) == 2
    assert schema.validate(s.doc) == [], schema.validate(s.doc)
    print("[ecad-session] undo/redo: 2 symbols -> undo (1) -> redo (2); compile field present")


def test_session_stale_revision() -> None:
    s = new_session("sch", "Stale")
    s.doc["lib_cache"]["Device:R"] = _res_def()
    r1 = session_apply_ops(s, 0, [_add_R([0.0, 0.0])])
    assert r1["revision"] == 1
    try:
        session_apply_ops(s, 0, [_add_R([10.0, 0.0])])
        raise AssertionError("stale base_revision must raise")
    except OpError as e:
        assert e.code == "stale_revision" and e.detail["current_revision"] == 1, e.to_envelope()
    # current revision works
    r2 = session_apply_ops(s, 1, [_add_R([10.0, 0.0])])
    assert r2["revision"] == 2
    print("[ecad-session] stale_revision: base!=current refused (current echoed); current accepted")


# --------------------------------------------------------------------------- PCB batch round-trip


def test_pcb_batch_round_trip() -> None:
    """A PCB batch: set a closed square outline, register a net, add a footprint (with an inline
    pad def), then add a track on that net. The result must validate via schema.validate_board."""
    s = new_session("pcb", "PCBTest")
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
    batch = [
        {"op": "pcb_set_outline", "shapes": outline, "closed": True},
        {"op": "pcb_add_footprint", "fpid": "R_0603", "pos": [10.0, 10.0], "ref": "R1",
         "def": fp_def},
        {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu",
         "p1": [10.0, 10.0], "p2": [20.0, 10.0]},
    ]
    res = session_apply_ops(s, 0, batch)
    assert res["revision"] == 1 and res["kind"] == "pcb"
    assert res["id_map"]["$1"] == "fp1" and res["id_map"]["$2"] == "t1", res["id_map"]
    assert schema.validate_board(s.doc) == [], schema.validate_board(s.doc)
    assert s.doc["outline"]["closed"] is True
    fp = s.doc["footprints"][0]
    assert fp["ref"] == "R1" and len(fp["pads"]) == 2
    tr = s.doc["tracks"][0]
    assert tr["net"] == "GND" and tr["layer"] == "F.Cu" and tr["width"] > 0
    assert "drc" in res["compile"] and "ratsnest" in res["compile"]
    print(f"[ecad-session] pcb batch: outline(closed) + fp1(R1,2 pads) + t1(GND,F.Cu,"
          f"w={tr['width']}) validates")


def test_pcb_outline_not_closed_rejected() -> None:
    """An open outline marked closed -> outline_not_closed."""
    s = new_session("pcb", "OpenOutline")
    try:
        session_apply_ops(s, 0, [{"op": "pcb_set_outline", "closed": True, "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"type": "line", "p1": [10.0, 0.0], "p2": [10.0, 10.0]},
        ]}])
        raise AssertionError("open outline marked closed must raise")
    except OpError as e:
        assert e.code == "outline_not_closed", e.to_envelope()
    print("[ecad-session] pcb outline: open loop marked closed -> outline_not_closed")


def test_pcb_track_bad_layer_and_net() -> None:
    """Track on a non-copper layer -> layer_not_in_stackup; track on an unknown net -> net_not_found."""
    s = new_session("pcb", "BadTrack")
    try:
        session_apply_ops(s, 0, [{"op": "pcb_add_track", "net": "GND", "layer": "F.SilkS",
                                  "p1": [0.0, 0.0], "p2": [1.0, 0.0]}])
        raise AssertionError("track on silk layer must raise")
    except OpError as e:
        assert e.code == "layer_not_in_stackup", e.to_envelope()
    try:
        session_apply_ops(s, 0, [{"op": "pcb_add_track", "net": "NOPE", "layer": "F.Cu",
                                  "p1": [0.0, 0.0], "p2": [1.0, 0.0]}])
        raise AssertionError("track on unknown net must raise")
    except OpError as e:
        assert e.code == "net_not_found", e.to_envelope()
    print("[ecad-session] pcb track: bad layer -> layer_not_in_stackup; bad net -> net_not_found")


def test_pcb_add_tracks_polyline() -> None:
    """pcb_add_tracks expands a polyline into consecutive segments, ids in $idx / $idx#k."""
    s = new_session("pcb", "Poly")
    res = session_apply_ops(s, 0, [{"op": "pcb_add_tracks", "net": "GND", "layer": "F.Cu",
                                    "pts": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]}])
    assert res["id_map"]["$0"] == "t1" and res["id_map"]["$0#1"] == "t2", res["id_map"]
    assert len(s.doc["tracks"]) == 2
    assert schema.validate_board(s.doc) == []
    print("[ecad-session] pcb_add_tracks: 3-pt polyline -> t1,t2 ($0,$0#1)")


# --------------------------------------------------------------------------- shared ops


def test_rename_and_set_field() -> None:
    doc = _blank_sch()
    doc = apply_ops(doc, [_add_R([0.0, 0.0])])["doc"]
    doc = apply_ops(doc, [{"op": "rename_doc", "name": "Renamed"},
                          {"op": "set_field", "target": {"kind": "symbols", "id": "s1"},
                           "key": "MPN", "value": "RC0603FR-0710KL"}])["doc"]
    assert doc["name"] == "Renamed"
    assert doc["symbols"][0]["fields"]["MPN"] == "RC0603FR-0710KL"
    # nop is a valid transactional no-op
    doc = apply_ops(doc, [{"op": "nop"}])["doc"]
    assert schema.validate(doc) == []
    print("[ecad-ops] shared: rename_doc + set_field(MPN) + nop")


def test_ac_magnitude_propagates_to_source() -> None:
    """E-1: a source carrying ac_mag yields a device with an AC stimulus (not a flat zero)."""
    from engine.sim.spice import netlist_to_spice_circuit
    nl = {"components": [
        {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "0", "ac_mag": 1.0,
         "sim": {"model": "V", "pin_order": ["+", "-"]},
         "pins": {"+": "IN", "-": "GND"}},
    ], "nets": {}}
    circ, _ = netlist_to_spice_circuit(nl)
    vs = [d for d in circ["devices"] if d.get("kind") == "V"]
    assert vs, "expected a V device"
    ac = (vs[0].get("params") or {}).get("ac") or {}
    assert ac.get("mag") == 1.0, f"AC magnitude not propagated: {vs[0]}"
    print("[ecad-ops] E-1: source ac_mag -> device AC stimulus")


def test_drc_warns_no_outline_then_clears() -> None:
    """E-3: a board with no Edge.Cuts outline warns; setting one clears the warning."""
    from engine.ecad import drc as drc_mod
    s = new_session("pcb", "ob")
    session_apply_ops(s, s.revision, [
        {"op": "pcb_add_footprint", "fpid": "R_0805", "ref": "R1", "pos": [5, 5],
         "pads": [{"pos": [-1, 0]}, {"pos": [1, 0]}]}])
    v = drc_mod.run_drc(s.doc)
    assert any(x["rule"] == "no_outline" for x in v), "expected a no_outline warning"
    session_apply_ops(s, s.revision, [{"op": "pcb_set_outline", "closed": True, "shapes": [
        {"type": "line", "p1": [0, 0], "p2": [20, 0]},
        {"type": "line", "p1": [20, 0], "p2": [20, 20]},
        {"type": "line", "p1": [20, 20], "p2": [0, 20]},
        {"type": "line", "p1": [0, 20], "p2": [0, 0]}]}])
    v2 = drc_mod.run_drc(s.doc)
    assert not any(x["rule"] == "no_outline" for x in v2), "no_outline should clear once an outline is set"
    print("[ecad-ops] E-3: no_outline DRC warning fires then clears")


def main() -> None:
    test_placeholder_batch_symbol_and_wire()
    test_placeholder_forward_ref_rejected()
    test_atomic_validation_one_bad_op_rejects_batch()
    test_atomic_validation_bad_result_doc()
    test_patch_edit_replaces_and_clears()
    test_delete_symbol_and_wire()
    test_auto_ref_assignment()
    test_sch_annotate_reset()
    test_session_undo_redo()
    test_session_stale_revision()
    test_pcb_batch_round_trip()
    test_pcb_outline_not_closed_rejected()
    test_pcb_track_bad_layer_and_net()
    test_pcb_add_tracks_polyline()
    test_rename_and_set_field()
    test_ac_magnitude_propagates_to_source()
    test_drc_warns_no_outline_then_clears()
    print("\nECAD OPS + SESSION TESTS PASSED")


if __name__ == "__main__":
    main()
