"""Connectivity-aware placer tests.

Builds a >=15-component board (MCU + decoupling caps + 2 connectors + passives)
from starter footprints, then asserts:

  (a) each decoupling cap centre is within 5 mm of its IC power pad
  (b) zero courtyard overlaps (via DFM)
  (c) connectors sit on an outer board edge
  (d) place_board is deterministic (two calls → identical positions)
  (e) total ratsnest length is strictly LESS than the sync-style grid placement

Prints wall-times via time.perf_counter.

Run:
  python -X utf8 -m pytest services/ecad/test_place.py -q
"""

from __future__ import annotations

import copy
import math
import time

import pytest

from services.mcp import core_bridge as _cb
from services.ecad import schema
from services.ecad.library.starter import FOOTPRINTS
from services.ecad.place import (
    _DECAP_MAX_RADIUS_MM,
    autoplace,
    grid_place_board,
    place_board,
)


def run_dfm(board):
    """Run native inline-board DFM through the ``ecad_dfm`` RPC."""
    return _cb.call("ecad_dfm", {"board": board}).get("violations", [])


# ── fixture builders ───────────────────────────────────────────────────────────

def _pad_net(pad: dict, net: str | None) -> dict:
    p = copy.deepcopy(pad)
    p["net"] = net
    return p


def _fp_from_lib(fpid: str, ref: str, value: str, libid: str,
                 pad_nets: dict[str, str | None],
                 pos: list[float] | None = None,
                 **extra) -> dict:
    """Instantiate a starter footprint with pad nets bound."""
    fpdef = FOOTPRINTS[fpid]
    pads = []
    for pad in fpdef.get("pads") or []:
        num = str(pad.get("number") or "")
        net = pad_nets.get(num)
        pads.append(_pad_net(pad, net))
    # schema requires id prefix ``fp`` + integer (``fp1``, ``fp2``, …).
    n = len(getattr(_fp_from_lib, "_n", []) or [])
    if not hasattr(_fp_from_lib, "_n"):
        _fp_from_lib._n = []  # type: ignore[attr-defined]
    _fp_from_lib._n.append(1)  # type: ignore[attr-defined]
    fid = f"fp{len(_fp_from_lib._n)}"  # type: ignore[attr-defined]
    fp = {
        "id": fid,
        "fpid": fpid,
        "libid": libid,
        "ref": ref,
        "value": value,
        "pos": list(pos) if pos is not None else [0.0, 0.0],
        "angle": 0.0,
        "side": "top",
        "pads": pads,
        "graphics": copy.deepcopy(fpdef.get("graphics") or []),
        "attr": fpdef.get("attr", "smd"),
        "linked_symbol": None,
        "fields": {},
    }
    fp.update(extra)
    return fp


def _add_nets(board: dict, *names: str) -> None:
    have = {n.get("name") for n in board.get("nets") or []}
    for name in names:
        if name not in have:
            board.setdefault("nets", []).append({"name": name, "class": "default"})
            have.add(name)


def build_mcu_board() -> dict:
    """MCU host board: LQFP-48 + 6 decaps + bulk + 2 connectors + passives (>=15 parts).

    Connectivity (pads numbered as in starter LQFP-48 / C_0603 / headers / USB):
      U1.13,U1.1  → 3V3   (power rails)
      U1.12,U1.8  → GND
      U1.9        → VDDA
      C1..C4      3V3↔GND  (decoupling near VDD pins)
      C5          VDDA↔GND
      C6          3V3↔GND  (bulk, still treated as decap by rail+GND rule)
      R1          NRST pull-up 3V3↔NRST
      R2          BOOT strap  BOOT↔GND
      Y1          crystal XIN↔XOUT  (as 2-pin C footprint stand-in if no crystal fp)
      J1          USB-C on 3V3/GND/D+/D-
      J2          SWD 2x5 header
      R3,R4,R5    signal passives on GPIO nets
      C7,C8       extra rail decaps
    """
    b = schema.new_board("place_mcu_board")
    # Fresh footprint-id sequence for schema validation (fp1, fp2, …).
    _fp_from_lib._n = []  # type: ignore[attr-defined]

    # Explicit outline so edge placement has a known perimeter.
    b["outline"] = {
        "closed": True,
        "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
            {"type": "line", "p1": [80.0, 0.0], "p2": [80.0, 60.0]},
            {"type": "line", "p1": [80.0, 60.0], "p2": [0.0, 60.0]},
            {"type": "line", "p1": [0.0, 60.0], "p2": [0.0, 0.0]},
        ],
    }
    _add_nets(b, "3V3", "GND", "VDDA", "NRST", "BOOT", "XIN", "XOUT",
              "PA0", "PA1", "PA2", "D+", "D-", "SWDIO", "SWCLK")

    fps = b["footprints"]

    # MCU — LQFP-48. Power pins from starter MCU symbol map:
    #   1=VBAT, 8=VSSA, 9=VDDA, 12=VSS, 13=VDD, 23=VSS, 24=VDD, 36=VSS, 37=VDD, 48=VSS
    mcu_nets = {str(i): None for i in range(1, 49)}
    mcu_nets["1"] = "3V3"     # VBAT tied to 3V3 for this fixture
    mcu_nets["8"] = "GND"     # VSSA
    mcu_nets["9"] = "VDDA"
    mcu_nets["12"] = "GND"    # VSS
    mcu_nets["13"] = "3V3"    # VDD
    mcu_nets["23"] = "GND"
    mcu_nets["24"] = "3V3"
    mcu_nets["36"] = "GND"
    mcu_nets["37"] = "3V3"
    mcu_nets["48"] = "GND"
    mcu_nets["7"] = "NRST"
    mcu_nets["10"] = "XIN"
    mcu_nets["11"] = "XOUT"
    mcu_nets["14"] = "PA0"
    mcu_nets["15"] = "PA1"
    mcu_nets["16"] = "PA2"
    mcu_nets["44"] = "BOOT"
    fps.append(_fp_from_lib(
        "Package_QFP:LQFP-48_7x7mm_P0.5mm", "U1", "MCU", "MCU:Generic_QFP48",
        mcu_nets, pos=[0.0, 0.0],
    ))

    # Decoupling caps on 3V3 (one per VDD pin affinity) + VDDA + bulk.
    for i, (ref, rail) in enumerate([
        ("C1", "3V3"), ("C2", "3V3"), ("C3", "3V3"), ("C4", "3V3"),
        ("C5", "VDDA"), ("C6", "3V3"), ("C7", "3V3"), ("C8", "3V3"),
    ]):
        fps.append(_fp_from_lib(
            "Capacitor_SMD:C_0603", ref, "100n" if ref != "C6" else "10u",
            "Device:C", {"1": rail, "2": "GND"}, pos=[0.0, 0.0],
        ))

    # Passives
    fps.append(_fp_from_lib(
        "Resistor_SMD:R_0603", "R1", "10k", "Device:R",
        {"1": "NRST", "2": "3V3"}, pos=[0.0, 0.0],
    ))
    fps.append(_fp_from_lib(
        "Resistor_SMD:R_0603", "R2", "10k", "Device:R",
        {"1": "BOOT", "2": "GND"}, pos=[0.0, 0.0],
    ))
    fps.append(_fp_from_lib(
        "Resistor_SMD:R_0603", "R3", "100", "Device:R",
        {"1": "PA0", "2": "PA1"}, pos=[0.0, 0.0],
    ))
    fps.append(_fp_from_lib(
        "Resistor_SMD:R_0603", "R4", "4k7", "Device:R",
        {"1": "PA1", "2": "PA2"}, pos=[0.0, 0.0],
    ))
    fps.append(_fp_from_lib(
        "Resistor_SMD:R_0603", "R5", "22", "Device:R",
        {"1": "D+", "2": "PA2"}, pos=[0.0, 0.0],
    ))

    # Crystal stand-in: use C_0603 pads as a 2-pin device on XIN/XOUT.
    fps.append(_fp_from_lib(
        "Capacitor_SMD:C_0603", "Y1", "8MHz", "Device:Crystal_HSE",
        {"1": "XIN", "2": "XOUT"}, pos=[0.0, 0.0],
    ))

    # Connectors
    usbc_nets = {str(i): None for i in range(1, 17)}
    usbc_nets["1"] = "GND"
    usbc_nets["4"] = "3V3"   # VBUS → 3V3 rail for this fixture
    usbc_nets["6"] = "D+"
    usbc_nets["7"] = "D-"
    usbc_nets["12"] = "GND"
    fps.append(_fp_from_lib(
        "Connector_USB:USB_C", "J1", "USB_C", "Connector:USB_C",
        usbc_nets, pos=[0.0, 0.0],
    ))

    swd_nets = {str(i): None for i in range(1, 11)}
    swd_nets["1"] = "3V3"
    swd_nets["2"] = "SWDIO"
    swd_nets["3"] = "GND"
    swd_nets["4"] = "SWCLK"
    swd_nets["9"] = "GND"
    swd_nets["10"] = "GND"
    # Header_2x05 has 10 pads numbered 1..10
    fps.append(_fp_from_lib(
        "Connector_PinHeader_2.54mm:Header_2x05", "J2", "SWD", "Connector:SWD_2x5",
        swd_nets, pos=[0.0, 0.0],
    ))

    assert len(fps) >= 15, f"fixture must have >=15 components, got {len(fps)}"
    return b


def _fp_by_ref(board: dict, ref: str) -> dict:
    return next(f for f in board["footprints"] if f.get("ref") == ref)


def _pad_world_for_net(fp: dict, net: str) -> list[tuple[float, float]]:
    out = []
    for pad in fp.get("pads") or []:
        if pad.get("net") == net:
            out.append(schema.pad_world(fp, pad))
    return out


def _on_edge(pos: list[float], bbox: list[float], margin: float = 8.0) -> bool:
    x, y = float(pos[0]), float(pos[1])
    xmin, ymin, xmax, ymax = bbox
    return (
        abs(x - xmin) <= margin or abs(x - xmax) <= margin
        or abs(y - ymin) <= margin or abs(y - ymax) <= margin
    )


# ── tests ──────────────────────────────────────────────────────────────────────

def test_place_board_decap_proximity_and_edge_and_overlaps():
    """(a) decaps near IC power pins, (b) zero courtyard overlaps, (c) connectors on edge."""
    board = build_mcu_board()
    t0 = time.perf_counter()
    out = place_board(board, {"seed": 0})
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"place_board wall-time: {ms:.2f} ms  "
          f"(n={out['report']['n_footprints']}, ratsnest={out['report']['ratsnest_len_mm']:.2f} mm)")

    placed = out["doc"]
    report = out["report"]

    # Input not mutated.
    assert all(
        (fp.get("pos") or [0, 0]) == [0.0, 0.0] or (fp.get("pos") or [0, 0]) == [0, 0]
        for fp in board["footprints"]
    ), "place_board must not mutate the input doc"

    assert len(placed["footprints"]) >= 15
    assert report["n_decaps"] >= 6
    assert report["n_connectors"] >= 2
    assert report["n_ics"] >= 1

    # (a) each decoupling cap centre within 5 mm of its IC power pad.
    u1 = _fp_by_ref(placed, "U1")
    for ref in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"):
        cap = _fp_by_ref(placed, ref)
        rail = None
        for p in cap["pads"]:
            n = p.get("net")
            if n and n != "GND":
                rail = n
                break
        assert rail, f"{ref} has no rail net"
        power_pads = _pad_world_for_net(u1, rail)
        assert power_pads, f"U1 has no pad on rail {rail}"
        cx, cy = float(cap["pos"][0]), float(cap["pos"][1])
        dmin = min(math.hypot(cx - px, cy - py) for px, py in power_pads)
        print(f"  decap {ref} rail={rail} dist_to_IC_pad={dmin:.3f} mm")
        assert dmin <= _DECAP_MAX_RADIUS_MM, (
            f"{ref} centre is {dmin:.3f} mm from nearest U1 {rail} pad "
            f"(limit {_DECAP_MAX_RADIUS_MM} mm); pos={cap['pos']}"
        )

    # (b) zero courtyard overlaps via DFM.
    viols = run_dfm(placed)
    cy = [v for v in viols if v.get("rule") == "courtyard_overlap"]
    print(f"  courtyard_overlap count={len(cy)}  report.overlaps={report['overlaps']}")
    assert len(cy) == 0, f"expected 0 courtyard overlaps, got {cy}"
    assert report["overlaps"] == 0

    # (c) connectors on outer edge.
    bbox = report["bbox"]
    for ref in ("J1", "J2"):
        fp = _fp_by_ref(placed, ref)
        assert _on_edge(fp["pos"], bbox, margin=10.0), (
            f"connector {ref} at {fp['pos']} not on board edge bbox={bbox}"
        )
        print(f"  connector {ref} pos={fp['pos']} on-edge OK")


def test_place_board_deterministic():
    """(d) same doc+seed → identical positions."""
    board = build_mcu_board()
    t0 = time.perf_counter()
    a = place_board(board, {"seed": 0})
    b = place_board(board, {"seed": 0})
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"determinism (2× place_board) wall-time: {ms:.2f} ms")

    pos_a = {fp["ref"]: list(fp["pos"]) for fp in a["doc"]["footprints"]}
    pos_b = {fp["ref"]: list(fp["pos"]) for fp in b["doc"]["footprints"]}
    assert pos_a == pos_b, f"positions differ:\n  A={pos_a}\n  B={pos_b}"
    assert a["report"]["ratsnest_len_mm"] == b["report"]["ratsnest_len_mm"]
    assert a["report"]["cluster_order"] == b["report"]["cluster_order"]

    # Different seed may differ (not required, but report should carry seed).
    c = place_board(board, {"seed": 1})
    assert c["report"]["seed"] == 1


def test_place_board_beats_grid_ratsnest():
    """(e) connectivity-aware ratsnest length strictly less than grid placement."""
    board = build_mcu_board()

    t0 = time.perf_counter()
    smart = place_board(board, {"seed": 0})
    t1 = time.perf_counter()
    grid = grid_place_board(board)
    t2 = time.perf_counter()

    smart_len = smart["report"]["ratsnest_len_mm"]
    grid_len = grid["report"]["ratsnest_len_mm"]
    print(f"place_board wall-time: {(t1 - t0) * 1000.0:.2f} ms  ratsnest={smart_len:.2f} mm")
    print(f"grid_place_board wall-time: {(t2 - t1) * 1000.0:.2f} ms  ratsnest={grid_len:.2f} mm")
    print(f"improvement: {grid_len - smart_len:.2f} mm "
          f"({100.0 * (1.0 - smart_len / grid_len):.1f}% shorter)" if grid_len > 0 else "")

    assert smart_len < grid_len, (
        f"expected connectivity-aware ratsnest ({smart_len:.2f}) < "
        f"grid ({grid_len:.2f})"
    )
    assert smart_len > 0.0  # board is unrouted; some airwire length remains


def test_locked_and_hint_honoured():
    """Locked / place_hint positions are preserved."""
    board = build_mcu_board()
    u1 = _fp_by_ref(board, "U1")
    u1["locked"] = True
    u1["pos"] = [40.0, 30.0]
    j1 = _fp_by_ref(board, "J1")
    j1["place_hint"] = [5.0, 5.0]

    t0 = time.perf_counter()
    out = place_board(board, {"seed": 0})
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"locked/hint place_board wall-time: {ms:.2f} ms")

    pu1 = _fp_by_ref(out["doc"], "U1")
    pj1 = _fp_by_ref(out["doc"], "J1")
    assert pu1["pos"] == [40.0, 30.0], f"locked U1 moved to {pu1['pos']}"
    assert pj1["pos"] == [5.0, 5.0], f"hinted J1 moved to {pj1['pos']}"
    assert out["report"]["n_locked"] >= 2


def test_autoplace_session_writeback():
    """autoplace swaps the placed doc into a live EcadSession via swap_doc."""
    from services.mcp.ecad_common import ecad_session as esess

    board = build_mcu_board()
    sess = esess.open_session(board, path=None)
    rev0 = sess.revision

    t0 = time.perf_counter()
    resp = autoplace(sess, {"seed": 0})
    ms = (time.perf_counter() - t0) * 1000.0
    print(f"autoplace wall-time: {ms:.2f} ms  revision {rev0}→{sess.revision}")

    assert sess.revision == rev0 + 1
    assert "place_report" in resp
    assert resp["place_report"]["n_footprints"] >= 15
    # Positions actually landed on the session doc.
    u1 = _fp_by_ref(sess.doc, "U1")
    assert u1["pos"] != [0.0, 0.0]
    # Courtyard-clean after writeback.
    cy = [v for v in run_dfm(sess.doc) if v.get("rule") == "courtyard_overlap"]
    assert cy == []
    esess.close_session(sess.doc_id)


def test_public_signature():
    """Public contract: place_board(doc, opts=None) -> {doc, report}."""
    import inspect
    from services.ecad.place import place_board as pb

    sig = inspect.signature(pb)
    params = list(sig.parameters)
    assert params[0] == "doc"
    assert params[1] == "opts"
    assert sig.parameters["opts"].default is None

    board = build_mcu_board()
    out = pb(board)
    assert set(out) == {"doc", "report"}
    assert "ratsnest_len_mm" in out["report"]
    assert "moved" in out["report"]
    assert "overlaps" in out["report"]
