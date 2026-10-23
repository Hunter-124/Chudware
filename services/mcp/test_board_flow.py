"""Wave 2b board-flow tests — schematic -> preliminary PCB + standalone tools.

Uses the _StubMCP harness (same pattern as scripts/demo_sensor_node.py /
services/mcp/test_ecad_tools.py) so we exercise the real register_ecad_tools
wiring without FastMCP/stdio.

Coverage
--------
(a) board_from_schematic yields a board with footprints placed + tracks routed
    (or residual nets named); DRC residuals explicitly listed.
(b) The flow reuses place_board + optimize_traces (report carries ratsnest_after
    + corners_rounded keys; monkeypatch asserts both are CALLED).
(c) Determinism: two runs on the same schematic -> identical placement + routing.
(d) ecad_autoplace + ecad_optimize_traces standalone tools work on an active board.

Run::

    python -X utf8 \\
        -m pytest services/mcp/test_board_flow.py -q
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import pytest

from services.mcp.ecad_tools import register_ecad_tools


# --------------------------------------------------------------------------- stub mcp


class _StubMCP:
    """Collects @mcp.tool()-decorated functions (no FastMCP / stdio needed)."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _call(_tools: dict, _name: str, **kwargs: Any) -> dict:
    """Invoke a tool by name; underscore params avoid kwarg collisions (e.g. name=)."""
    fn = _tools.get(_name)
    assert fn is not None, f"tool {_name!r} not registered"
    raw = fn(**kwargs)
    if isinstance(raw, str):
        return json.loads(raw)
    assert isinstance(raw, dict), f"{_name} returned {type(raw)}"
    return raw


def _tools() -> dict:
    stub = _StubMCP()
    register_ecad_tools(stub)
    return stub.tools


# --------------------------------------------------------------------------- fixtures


def _build_minimal_board_schematic(tools: dict, name: str = "bf_div") -> str:
    """Minimal board-ready schematic: footprinted divider (starter R_0603).

    V1 is sim-only (no footprint); R1/R2 sync to the board. Connectivity uses
    power ports + labels seated on exact pin tips so the netlist has multi-pad
    nets that the autorouter can actually route. Returns schematic doc_id.
    """
    return _build_divider_schematic(tools, name=name)


def _build_divider_schematic(tools: dict, name: str = "bf_div",
                             sch_id: str | None = None) -> str:
    """Footprinted resistive divider (always works with starter library).

    Layout (pin tips must land on power/label positions)::

        V1 at (0, 0):  + tip ~(0, 5.08),  - tip ~(0, -5.08)
        R1 at (20, 5): pin1 tip ~(14.92, 5), pin2 tip ~(25.08, 5)
        R2 at (40, 5): pin1 tip ~(34.92, 5), pin2 tip ~(45.08, 5)

    Nets: +5V = V1.+ — R1.1; MID = R1.2 — R2.1; GND = R2.2 (power port).
    """
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    if not sch_id:
        r = _call(tools, "ecad_new_schematic", name=name)
        sch_id = r.get("doc_id") or _ECAD.get("current_sch")
        assert sch_id, f"failed to create schematic: {r}"

    v1 = _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source",
               x=0, y=0, value="5", ref="V1", doc_id=sch_id)
    r1 = _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=5,
               value="10k", ref="R1", footprint="Resistor_SMD:R_0603",
               doc_id=sch_id)
    r2 = _call(tools, "ecad_place_symbol", libid="Device:R", x=40, y=5,
               value="10k", ref="R2", footprint="Resistor_SMD:R_0603",
               doc_id=sch_id)
    assert r1.get("ok") and r2.get("ok"), f"place failed: v1={v1} r1={r1} r2={r2}"

    s = ecad_session.get_session(sch_id)
    by_ref = {sy.get("ref"): sy for sy in s.doc.get("symbols", [])}
    ops: list[dict] = []
    for ref in ("R1", "R2"):
        sy = by_ref.get(ref)
        if sy is not None and not sy.get("footprint"):
            ops.append({"op": "sch_edit_symbol", "id": sy["id"],
                        "footprint": "Resistor_SMD:R_0603"})
    # Exact pin-tip power/labels (matches test_ecad_tools._build_footprinted_divider).
    ops.extend([
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0, 5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_label", "text": "MID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "MID", "pos": [34.92, 5.0], "scope": "local"},
        {"op": "sch_add_power", "net": "GND", "pos": [45.08, 5.0]},
    ])
    ecad_session.apply_ops(s, s.revision, ops)
    return sch_id

# --------------------------------------------------------------------------- tests


def test_tools_registered() -> None:
    t0 = time.perf_counter()
    tools = _tools()
    for name in ("board_from_schematic", "ecad_autoplace", "ecad_optimize_traces"):
        assert name in tools, f"{name} not registered in register_ecad_tools"
    dt = (time.perf_counter() - t0) * 1000.0
    print(f"  [register] PASS  wall={dt:.1f} ms  tools={len(tools)}")


def test_board_from_schematic_places_and_routes() -> None:
    """(a) board_from_schematic -> placed footprints + routed tracks (or residual nets)."""
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    tools = _tools()
    t0 = time.perf_counter()
    sch_id = _build_minimal_board_schematic(tools, name="bf_a")
    r = _call(tools, "board_from_schematic", sch_doc_id=sch_id, optimize=True)
    dt = (time.perf_counter() - t0) * 1000.0

    assert r.get("ok") is True or r.get("board_doc_id"), f"flow failed: {r}"
    board_id = r.get("board_doc_id") or r.get("doc_id")
    assert board_id, f"no board_doc_id: {r}"
    assert _ECAD.get("current_pcb") == board_id, "tool must set current_pcb"

    pcb = ecad_session.get_session(board_id)
    n_fp = len(pcb.doc.get("footprints") or [])
    n_tracks = len(pcb.doc.get("tracks") or [])
    assert n_fp >= 2, f"expected >=2 footprints, got {n_fp}"
    # Either tracks exist OR residual nets are named honestly.
    failed = r.get("failed_nets") or []
    assert n_tracks > 0 or failed, \
        f"expected tracks or named residual nets; tracks={n_tracks} failed={failed}"

    # Residual DRC is always a list (may be empty) — never hidden.
    assert "residual_drc" in r and isinstance(r["residual_drc"], list), \
        f"residual_drc missing or not a list: {r.keys()}"
    rep = r.get("report") or {}
    assert "ratsnest_before" in rep and "ratsnest_after" in rep, rep
    assert "corners_rounded" in rep, rep  # optimize path ran (or reported 0)
    assert "drc_errors" in rep, rep

    print(f"  [board_from_schematic] PASS  wall={dt:.1f} ms  "
          f"fp={n_fp} tracks={n_tracks} failed={failed} "
          f"drc_errors={r.get('drc_errors')} "
          f"corners={rep.get('corners_rounded')} teardrops={rep.get('teardrops')}")
    if r["residual_drc"]:
        print(f"    residual_drc ({len(r['residual_drc'])}): "
              + "; ".join(
                  f"{v.get('rule')}:{v.get('severity')}" for v in r["residual_drc"][:6]))


def test_reuses_place_and_optimize(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) place_board + optimize_traces are CALLED; report carries their keys."""
    from services.mcp import board_flow as bf
    from services.ecad import place as place_mod
    from services.ecad import trace_opt as trace_opt_mod

    tools = _tools()
    sch_id = _build_minimal_board_schematic(tools, name="bf_b")

    place_calls: list = []
    opt_calls: list = []

    real_place = place_mod.place_board
    real_opt = trace_opt_mod.optimize_traces

    def _spy_place(doc, opts=None):
        place_calls.append({"opts": dict(opts or {}), "n_fp": len(doc.get("footprints") or [])})
        return real_place(doc, opts)

    def _spy_opt(doc, opts=None):
        opt_calls.append({"opts": dict(opts or {}), "n_tracks": len(doc.get("tracks") or [])})
        return real_opt(doc, opts)

    monkeypatch.setattr(place_mod, "place_board", _spy_place)
    monkeypatch.setattr(trace_opt_mod, "optimize_traces", _spy_opt)
    # board_flow imported them by module attribute — patch there too.
    monkeypatch.setattr(bf.place_mod, "place_board", _spy_place)
    monkeypatch.setattr(bf.trace_opt_mod, "optimize_traces", _spy_opt)

    t0 = time.perf_counter()
    # Call the engine directly so the spy is unambiguous (tools also call it).
    from services.mcp.ecad_common import ecad_session
    result = bf.board_from_schematic(ecad_session.get_session(sch_id),
                                     {"optimize": True, "seed": 0})
    dt = (time.perf_counter() - t0) * 1000.0

    assert place_calls, "place_board was never called"
    assert opt_calls, "optimize_traces was never called"
    rep = result.get("report") or {}
    assert "ratsnest_after" in rep, rep
    assert "corners_rounded" in rep, rep
    print(f"  [reuses_e1_e2] PASS  wall={dt:.1f} ms  "
          f"place_calls={len(place_calls)} opt_calls={len(opt_calls)} "
          f"ratsnest_after={rep.get('ratsnest_after')} "
          f"corners_rounded={rep.get('corners_rounded')}")


def test_determinism_two_runs() -> None:
    """(c) two runs on the same schematic -> identical placement + routing."""
    from services.mcp import board_flow as bf
    from services.mcp.ecad_common import ecad_session

    tools = _tools()
    sch_id = _build_minimal_board_schematic(tools, name="bf_c")
    sch = ecad_session.get_session(sch_id)

    t0 = time.perf_counter()
    r1 = bf.board_from_schematic(sch, {"optimize": True, "seed": 0, "pcb_name": "det1"})
    r2 = bf.board_from_schematic(sch, {"optimize": True, "seed": 0, "pcb_name": "det2"})
    dt = (time.perf_counter() - t0) * 1000.0

    assert r1.get("placement") == r2.get("placement"), \
        f"placement diverged:\n  {r1.get('placement')}\n  {r2.get('placement')}"
    assert r1.get("routing") == r2.get("routing"), \
        f"routing diverged:\n  {r1.get('routing')}\n  {r2.get('routing')}"
    # Report keys that should also be stable.
    for key in ("ratsnest_before", "ratsnest_after", "corners_rounded",
                "teardrops", "nets_tuned"):
        assert (r1.get("report") or {}).get(key) == (r2.get("report") or {}).get(key), \
            f"report[{key}] diverged: {r1.get('report')} vs {r2.get('report')}"
    print(f"  [determinism] PASS  wall={dt:.1f} ms  "
          f"fp={r1.get('placed')} tracks={r1.get('n_tracks')} "
          f"placement_rows={len(r1.get('placement') or [])}")


def test_standalone_autoplace_and_optimize() -> None:
    """(d) ecad_autoplace + ecad_optimize_traces work on an active board."""
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    tools = _tools()
    sch_id = _build_minimal_board_schematic(tools, name="bf_d")

    # Build a board via the classical sync path (no place/opt yet).
    t0 = time.perf_counter()
    _call(tools, "ecad_new_pcb", name="standalone_board")
    pcb_id = _ECAD["current_pcb"]
    up = _call(tools, "ecad_update_from_sch", sch_doc_id=sch_id, pcb_doc_id=pcb_id)
    assert up.get("ok"), f"update_from_sch failed: {up}"

    # Give the board a bit of outline so placement has a bbox.
    pcb = ecad_session.get_session(pcb_id)
    if not (pcb.doc.get("outline") or {}).get("shapes"):
        ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": [
            {"type": "line", "p1": [0, 0], "p2": [60, 0]},
            {"type": "line", "p1": [60, 0], "p2": [60, 40]},
            {"type": "line", "p1": [60, 40], "p2": [0, 40]},
            {"type": "line", "p1": [0, 40], "p2": [0, 0]},
        ], "closed": True}])

    ap = _call(tools, "ecad_autoplace", doc_id=pcb_id)
    assert ap.get("ok"), f"ecad_autoplace failed: {ap}"
    assert "report" in ap and "moved" in (ap.get("report") or ap), ap
    assert ap.get("placed", 0) >= 1 or (ap.get("report") or {}).get("n_footprints", 0) >= 1

    # Route so optimize has tracks to polish.
    ar = _call(tools, "ecad_autoroute", doc_id=pcb_id)
    assert ar.get("ok"), f"ecad_autoroute failed: {ar}"

    opt = _call(tools, "ecad_optimize_traces", doc_id=pcb_id)
    assert opt.get("ok"), f"ecad_optimize_traces failed: {opt}"
    assert "corners_rounded" in opt, opt
    assert "teardrops" in opt, opt
    assert "residual_drc" in opt and isinstance(opt["residual_drc"], list), opt
    assert "next_actions" in opt and "hint" in opt, "envelope incomplete"

    dt = (time.perf_counter() - t0) * 1000.0
    print(f"  [standalone tools] PASS  wall={dt:.1f} ms  "
          f"autoplace moved={ap.get('moved')}  "
          f"opt corners={opt.get('corners_rounded')} teardrops={opt.get('teardrops')} "
          f"drc_errors={opt.get('drc_errors')}")
    if opt["residual_drc"]:
        print(f"    residual_drc: "
              + "; ".join(f"{v.get('rule')}" for v in opt["residual_drc"][:6]))


def test_board_from_schematic_tool_envelope() -> None:
    """MCP tool envelope carries ok/summary/next_actions + residual_drc."""
    tools = _tools()
    sch_id = _build_minimal_board_schematic(tools, name="bf_env")
    t0 = time.perf_counter()
    r = _call(tools, "board_from_schematic", sch_doc_id=sch_id)
    dt = (time.perf_counter() - t0) * 1000.0
    for key in ("ok", "hint", "next_actions", "warnings", "summary",
                "residual_drc", "report", "board_doc_id"):
        assert key in r, f"missing envelope key {key!r}: {list(r.keys())}"
    assert isinstance(r["next_actions"], list) and r["next_actions"]
    assert isinstance(r["residual_drc"], list)
    print(f"  [envelope] PASS  wall={dt:.1f} ms  summary={r.get('summary', '')[:100]}")


if __name__ == "__main__":
    # Allow `python -m` style without pytest for a quick smoke.
    test_tools_registered()
    test_board_from_schematic_places_and_routes()
    # monkeypatch-less smoke for reuse: just check report keys
    tools = _tools()
    sch_id = _build_minimal_board_schematic(tools, name="bf_main")
    from services.mcp import board_flow as bf
    from services.mcp.ecad_common import ecad_session
    r = bf.board_from_schematic(ecad_session.get_session(sch_id), {"optimize": True})
    assert "ratsnest_after" in (r.get("report") or {})
    assert "corners_rounded" in (r.get("report") or {})
    print("  [main smoke] PASS")
    test_determinism_two_runs()
    test_standalone_autoplace_and_optimize()
    test_board_from_schematic_tool_envelope()
    print("ALL PASS")


def test_a_symbol_sync_cannot_place_is_named_never_silently_dropped() -> None:
    """Live 2026-07-30 (ESP32 board flow): a nine-part schematic synced as FIVE
    footprints and still reported "5 footprint(s) added ... Next: ecad_autoroute".

    core/ecad/sync.cpp::placeable() rejects a component carrying no fpid and the loop
    `continue`s BEFORE anything is written to report["unresolved"], so those parts left
    no trace at all — not placed, not listed, not warned about. A board silently missing
    a third of its components is the worst kind of green.

    The contract this pins: every non-power, non-DNP component either lands on the board
    or is NAMED in the reply. Never neither.
    """
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    tools = _tools()
    sch_id = _build_divider_schematic(tools, name="bf_nofp")

    # A real starter symbol that is a PHYSICAL part yet carries no default footprint —
    # exactly the shape that used to vanish. (The starter library has three such:
    # Device:Switch, Device:Zener and FPGA:iCE40UP5K-SG48. Simulation_SPICE:V_source and
    # I_source are also footprint-less but are legitimately sim-only, so they must NOT be
    # reported — the divider above carries V1 and this test would trip on it otherwise.)
    ghost = _call(tools, "ecad_place_symbol", libid="Device:Switch", x=60, y=5,
                  value="SW1", ref="S1", doc_id=sch_id)
    assert ghost.get("ok"), f"place failed: {ghost}"

    _call(tools, "ecad_new_pcb", name="nofp_board")
    pcb_id = _ECAD["current_pcb"]
    up = _call(tools, "ecad_update_from_sch", sch_doc_id=sch_id, pcb_doc_id=pcb_id)
    assert up.get("ok"), f"update_from_sch failed: {up}"

    placed = {fp.get("ref") for fp in
              (ecad_session.get_session(pcb_id).doc.get("footprints") or [])}
    blob = json.dumps(up)
    if "S1" not in placed:
        assert "S1" in blob, (
            "S1 was neither placed nor named anywhere in the reply — it vanished:\n"
            f"placed={sorted(placed)}\nreply={blob[:800]}")

    # The footprinted parts must still land, i.e. the guard did not cost the happy path.
    assert {"R1", "R2"} <= placed, f"footprinted parts missing: {sorted(placed)}"


def test_a_header_symbol_now_has_a_default_footprint_and_lands() -> None:
    """Conn_2/Conn_3/Conn_4/Header_1x4 are starter SYMBOLS that ecad_find_part("header")
    offers, but they had no entry in _DEFAULT_FOOTPRINTS, so every header the model added
    was dropped off the board by the same silent path."""
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    tools = _tools()
    sch_id = _build_divider_schematic(tools, name="bf_hdr")
    hdr = _call(tools, "ecad_place_symbol", libid="Connector:Conn_2", x=60, y=5,
                value="J1", ref="J1", doc_id=sch_id)
    assert hdr.get("ok"), f"place failed: {hdr}"

    _call(tools, "ecad_new_pcb", name="hdr_board")
    pcb_id = _ECAD["current_pcb"]
    up = _call(tools, "ecad_update_from_sch", sch_doc_id=sch_id, pcb_doc_id=pcb_id)
    assert up.get("ok"), f"update_from_sch failed: {up}"

    placed = {fp.get("ref") for fp in
              (ecad_session.get_session(pcb_id).doc.get("footprints") or [])}
    assert "J1" in placed, f"the header did not reach the board: {sorted(placed)}"
