"""Cross-document back-annotation tests (engine.ecad.sync + ecad_update_from_sch RPC).

House style: plain main() + asserts, NOT pytest.

Run:  & $py -m engine.ecad.test_sync   (from repo root)

Scenario: a tiny voltage-divider schematic
    V1 (V_source) ── +5V ── R1 ── MID ── R2 ── GND
The two resistors carry a real footprint (``Resistor_SMD:R_0603``); V1 (sim-only) and the GND power
symbol have none. After ``update_board_from_sch`` the board must have exactly the 2 resistor
footprints, every resistor pad bound to its netlist net, those nets present in ``board['nets']``, and
the board must validate. A second sync must be idempotent (0 added / 0 updated). Finally the
ratsnest must surface the now-populated, unrouted nets.
"""

from __future__ import annotations

import copy
import sys

from engine.ecad import schema, sync
from engine.ecad.netlist import build_netlist
from engine.ecad.drc import ratsnest
from engine.ecad.library.starter import SYMBOLS
from engine.ecad.ops import apply_ops, OpError
from engine.ecad import session as esess
from engine.ecad.rpc import METHODS


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"  ok  {msg}")


def _noop_emit(_data: dict) -> None:
    pass


# --------------------------------------------------------------------------- schematic fixture


def _build_divider_sch_session():
    """Build the V1/R1/MID/R2/GND divider in a fresh schematic session.

    R1 and R2 get a real footprint via sch_edit_symbol; V1 and GND stay footprint-less. Returns the
    schematic EcadSession.
    """
    s = esess.new_session("sch", "divider_sch")
    for libid in ("Simulation_SPICE:V_source", "Device:R", "power:GND"):
        s.doc["lib_cache"][libid] = copy.deepcopy(SYMBOLS[libid])

    # Place V1, R1, R2 (refs explicit so the netlist is deterministic).
    esess.apply_ops(s, s.revision, [
        {"op": "sch_add_symbol", "libid": "Simulation_SPICE:V_source",
         "pos": [0.0, 0.0], "ref": "V1", "value": "5"},
        {"op": "sch_add_symbol", "libid": "Device:R",
         "pos": [20.0, 5.0], "ref": "R1", "value": "10k"},
        {"op": "sch_add_symbol", "libid": "Device:R",
         "pos": [40.0, 5.0], "ref": "R2", "value": "10k"},
    ])

    # Give R1/R2 a real footprint (the back-annotation source of truth).
    esess.apply_ops(s, s.revision, [
        {"op": "sch_edit_symbol", "id": "s2", "footprint": "Resistor_SMD:R_0603"},
        {"op": "sch_edit_symbol", "id": "s3", "footprint": "Resistor_SMD:R_0603"},
    ])

    # Connectivity via power ports + labels (name-based — robust against coord fragility), mirroring
    # the integration test. Pin world tips:
    #   V1.+ (0, 5.08)   R1.pin1 (14.92, 5)  R1.pin2 (25.08, 5)
    #   R2.pin1 (34.92, 5) R2.pin2 (45.08, 5)  GND symbol pin tip placed onto (45.08, 5)
    esess.apply_ops(s, s.revision, [
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0, 5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_label", "text": "MID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "MID", "pos": [34.92, 5.0], "scope": "local"},
        {"op": "sch_add_power", "net": "GND", "pos": [0.0, -5.08]},
        {"op": "sch_add_power", "net": "GND", "pos": [45.08, 5.0]},
    ])

    # A real GND power symbol so the netlist has a "GND" component (which must be SKIPPED — no fp).
    # power:GND pin is at [0,0], angle 90, length 2.54 -> tip (0, 2.54); place so tip = (45.08, 5).
    esess.apply_ops(s, s.revision, [
        {"op": "sch_add_symbol", "libid": "power:GND", "pos": [45.08, 2.46]},
    ])

    return s


# --------------------------------------------------------------------------- tests


def test_netlist_shape() -> dict:
    """Build the netlist and confirm the resistors have footprints + the right nets, V1/GND don't."""
    print("\n--- netlist -------------------------------------------------------")
    s = _build_divider_sch_session()
    nl = build_netlist(s.doc)

    comps = {c["ref"]: c for c in nl["components"]}
    _assert("R1" in comps and "R2" in comps, f"R1+R2 in netlist (got {sorted(comps)})")
    _assert(comps["R1"]["footprint"] == "Resistor_SMD:R_0603",
            f"R1 footprint set (got {comps['R1']['footprint']!r})")
    _assert(comps["V1"]["footprint"] in ("", None),
            f"V1 has no footprint (got {comps['V1']['footprint']!r})")

    # R1.pin1 -> +5V, R1.pin2 -> MID ; R2.pin1 -> MID, R2.pin2 -> GND
    _assert(comps["R1"]["pins"].get("1") == "+5V" and comps["R1"]["pins"].get("2") == "MID",
            f"R1 pins bound (+5V/MID) (got {comps['R1']['pins']})")
    _assert(comps["R2"]["pins"].get("1") == "MID" and comps["R2"]["pins"].get("2") == "GND",
            f"R2 pins bound (MID/GND) (got {comps['R2']['pins']})")
    print(f"  R1.pins={comps['R1']['pins']}  R2.pins={comps['R2']['pins']}")
    return {"sch_session": s, "netlist": nl, "comps": comps}


def test_sync_populates_board(ctx: dict) -> dict:
    """update_board_from_sch on a blank board: 2 R footprints created, pads bound, nets added."""
    print("\n--- sync (pure function) ------------------------------------------")
    nl = ctx["netlist"]
    comps = ctx["comps"]
    board = schema.new_board("divider_pcb")

    out = sync.update_board_from_sch(board, dict(nl, id=ctx["sch_session"].doc.get("id")))
    new_board = out["mutated_board"]
    report = out["report"]

    # original board untouched (pure-ish)
    _assert(len(board.get("footprints", [])) == 0, "input board NOT mutated (still 0 footprints)")

    fps = {fp["ref"]: fp for fp in new_board["footprints"]}
    _assert(set(fps) == {"R1", "R2"},
            f"exactly R1+R2 footprints created — V1/GND skipped (got {sorted(fps)})")
    _assert(report["added"] == ["R1", "R2"], f"report.added == [R1,R2] (got {report['added']})")
    _assert(report["updated"] == [], f"report.updated empty on first run (got {report['updated']})")
    _assert(report["unresolved"] == [], f"no unresolved (got {report['unresolved']})")
    _assert(report["linked_sch"] == ctx["sch_session"].doc.get("id"),
            f"report.linked_sch recorded (got {report['linked_sch']!r})")
    _assert(new_board.get("linked_sch") == ctx["sch_session"].doc.get("id"),
            "board.linked_sch set on the board")

    # every resistor pad bound to the correct net (matching the netlist component pins)
    bindings: dict[str, dict] = {}
    for ref, fp in fps.items():
        for pad in fp["pads"]:
            expect = comps[ref]["pins"].get(str(pad.get("pin") or pad.get("number")))
            got = pad.get("net")
            _assert(got == expect,
                    f"{ref} pad {pad['number']} net == {expect!r} (got {got!r})")
            bindings[f"{ref}.{pad['number']}"] = got

    # board.nets includes the bound nets
    net_names = {n["name"] for n in new_board["nets"]}
    for net in ("+5V", "MID", "GND"):
        _assert(net in net_names, f"board.nets includes {net!r} (got {sorted(net_names)})")

    # nets_bound tally = 4 resistor pads (R1.1,R1.2,R2.1,R2.2)
    _assert(report["nets_bound"] == 4, f"report.nets_bound == 4 (got {report['nets_bound']})")

    # board validates
    errs = schema.validate_board(new_board)
    _assert(errs == [], f"board validates (got {errs[:3]})")

    print(f"  report={report}")
    print(f"  net bindings={bindings}")
    ctx["board"] = new_board
    return ctx


def test_idempotent(ctx: dict) -> dict:
    """Running sync AGAIN on the populated board -> 0 added / 0 updated, board unchanged."""
    print("\n--- idempotency ---------------------------------------------------")
    nl = ctx["netlist"]
    board = ctx["board"]
    before = copy.deepcopy(board)

    out = sync.update_board_from_sch(board, dict(nl, id=ctx["sch_session"].doc.get("id")))
    report = out["report"]
    _assert(report["added"] == [], f"2nd run adds nothing (got {report['added']})")
    _assert(report["updated"] == [], f"2nd run updates nothing (got {report['updated']})")
    _assert(report["removed"] == [], f"2nd run removes nothing (got {report['removed']})")

    # The board content (footprints + nets) is unchanged on the second pass.
    _assert(out["mutated_board"]["footprints"] == before["footprints"],
            "footprints byte-identical after 2nd sync (idempotent)")
    print(f"  2nd report={report}")
    return ctx


def test_ratsnest_reflects_population(ctx: dict) -> None:
    """The populated, unrouted board's ratsnest must surface the multi-pad nets (MID at least)."""
    print("\n--- ratsnest ------------------------------------------------------")
    board = ctx["board"]
    rats = ratsnest(board)
    nets = {r["net"] for r in rats}
    # MID connects R1.pad2 -> R2.pad1 (2 terminals, unrouted) -> must be an airwire.
    _assert("MID" in nets, f"MID is an unrouted airwire (got ratsnest nets {sorted(nets)})")
    # +5V and GND each have only ONE resistor pad on the board (the other terminal is V1/GND which
    # have no footprint) -> a single-terminal net has nothing to connect, so it is NOT in ratsnest.
    _assert(len(rats) >= 1, f"ratsnest non-empty for the populated board (got {len(rats)})")
    print(f"  ratsnest nets={sorted(nets)}  ({len(rats)} airwire(s))")


def test_rpc_update_from_sch() -> None:
    """End-to-end via the RPC method: both docs in the registry, one call back-annotates the board."""
    print("\n--- ecad_update_from_sch (RPC) ------------------------------------")
    sch_s = _build_divider_sch_session()

    new_fn = METHODS["ecad_new"]
    update_fn = METHODS["ecad_update_from_sch"]

    r0 = new_fn({"kind": "pcb", "name": "rpc_board"}, _noop_emit)
    board_doc_id = r0["doc_id"]
    base_rev = r0["revision"]

    resp = update_fn({"board_doc_id": board_doc_id, "sch_doc_id": sch_s.doc_id,
                      "base_revision": base_rev}, _noop_emit)

    _assert(resp["kind"] == "pcb", f"response kind pcb (got {resp['kind']})")
    _assert(resp["revision"] == base_rev + 1, f"revision bumped by 1 (got {resp['revision']})")
    _assert("report" in resp, "response carries a sync report")
    _assert(resp["report"]["added"] == ["R1", "R2"],
            f"RPC added R1+R2 (got {resp['report']['added']})")

    board = resp["tree"]
    _assert(len(board["footprints"]) == 2, f"board has 2 footprints (got {len(board['footprints'])})")
    _assert(board.get("linked_sch") == sch_s.doc.get("id"), "board linked back to the schematic")

    # compile field present + ratsnest computed on the freshly-populated board
    _assert("compile" in resp and "ratsnest" in resp["compile"], "ops-response compile.ratsnest present")
    rats_nets = {r["net"] for r in resp["compile"]["ratsnest"]}
    _assert("MID" in rats_nets, f"compile.ratsnest surfaces MID (got {sorted(rats_nets)})")

    # idempotent through the RPC too (apply against the new revision).
    resp2 = update_fn({"board_doc_id": board_doc_id, "sch_doc_id": sch_s.doc_id,
                       "base_revision": resp["revision"]}, _noop_emit)
    _assert(resp2["report"]["added"] == [] and resp2["report"]["updated"] == [],
            f"RPC 2nd call idempotent (got {resp2['report']})")
    print(f"  RPC report={resp['report']}  (rev {base_rev} -> {resp['revision']} -> {resp2['revision']})")


def test_dnp_and_unresolved() -> None:
    """A dnp resistor is skipped; a resistor with a bogus fpid lands in report.unresolved."""
    print("\n--- dnp + unresolved ----------------------------------------------")
    s = esess.new_session("sch", "edge_sch")
    s.doc["lib_cache"]["Device:R"] = copy.deepcopy(SYMBOLS["Device:R"])
    esess.apply_ops(s, s.revision, [
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [0.0, 0.0], "ref": "R1",
         "footprint": "Resistor_SMD:R_0603", "dnp": True},
        {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10.0, 0.0], "ref": "R2",
         "footprint": "NoSuchLib:NoSuchFp"},
    ])
    nl = build_netlist(s.doc)
    board = schema.new_board("edge_board")
    out = sync.update_board_from_sch(board, nl)
    report = out["report"]
    _assert(report["added"] == [], f"dnp + unresolved add nothing (got {report['added']})")
    _assert(report["unresolved"] == [["R2", "NoSuchLib:NoSuchFp"]],
            f"R2 unresolved (got {report['unresolved']})")
    _assert(len(out["mutated_board"]["footprints"]) == 0, "no footprints placed for dnp/unresolved")
    _assert(schema.validate_board(out["mutated_board"]) == [], "edge board still validates")
    print(f"  report={report}")


def main() -> None:
    print("=== AI-ECAD sync (back-annotation) tests ===")
    ctx = test_netlist_shape()
    ctx = test_sync_populates_board(ctx)
    ctx = test_idempotent(ctx)
    test_ratsnest_reflects_population(ctx)
    test_rpc_update_from_sch()
    test_dnp_and_unresolved()
    print("\n=== ALL SYNC ASSERTIONS PASSED ===")


if __name__ == "__main__":
    main()
