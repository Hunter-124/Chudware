"""Manual (pad-to-pad) PCB routing tests — ecad_route_pad_to_pad.

``ecad_autoroute`` reports failed nets and tells the agent they "need manual routing". Before
this tool there was nothing to do that with: the harness narrated a next step the agent could
not take. These tests pin the four behaviours that make the new tool safe to advertise:

  1. the segment lands on the two pads' ACTUAL world coordinates and its length is the
     hand-computed distance between them (no snapping, no invented waypoints);
  2. routing two pads on DIFFERENT nets is REFUSED and the board is byte-identical afterwards
     (a silent short is catastrophic, so it must never be a warning);
  3. the trace goes into the board's own ``tracks`` list, so ``ecad_run_drc`` catches a hand
     route that violates clearance — this is what proves it is not a side structure;
  4. with no explicit ``width`` the net class's SOLVED ``track_width`` (what
     ``ecad_stackup_solve`` writes) is what actually ends up on the track.

Self-contained: tools are registered against a stub mcp (no FastMCP / stdio server needed).
Run:  .venv-mcp/bin/python -m pytest services/mcp/test_ecad_pcb_tools.py -q
"""
from __future__ import annotations

import json
import math

from services.mcp.ecad_common import ecad_session
from services.mcp.ecad_tools import _ECAD, register_ecad_tools
from services.ecad import schema
from services.mcp import ecad_pcb_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools() -> dict:
    mcp = _StubMCP()
    register_ecad_tools(mcp)
    return mcp.tools


def _call(_tools_: dict, _name_: str, **kw) -> dict:
    out = _tools_[_name_](**kw)
    assert isinstance(out, str), f"{_name_} must return a JSON string, got {type(out)}"
    return json.loads(out)


# --------------------------------------------------------------------------- board fixture


def _divider_board(tools: dict, r1_pos=(20.0, 10.0), r2_pos=(40.0, 10.0)) -> dict:
    """+5V — R1 — MID — R2 — GND, synced onto a board with R1/R2 at KNOWN positions.

    Returns the board session. Positions are forced with pcb_edit_footprint so the pad
    coordinates the routing test asserts against are deterministic, not placer-dependent.
    """
    _call(tools, "ecad_new_schematic", name="manual_route_sch")
    r1 = _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=5, value="10k", ref="R1")
    r2 = _call(tools, "ecad_place_symbol", libid="Device:R", x=40, y=5, value="10k", ref="R2")
    s = ecad_session.get_session(_ECAD["current_sch"])
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_edit_symbol", "id": r1["id"], "footprint": "Resistor_SMD:R_0603"},
        {"op": "sch_edit_symbol", "id": r2["id"], "footprint": "Resistor_SMD:R_0603"},
    ])
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_label", "text": "MID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "MID", "pos": [34.92, 5.0], "scope": "local"},
        {"op": "sch_add_power", "net": "GND", "pos": [45.08, 5.0]},
    ])

    _call(tools, "ecad_new_pcb", name="manual_route_pcb")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": [
        {"type": "line", "p1": [0, 0],   "p2": [60, 0]},
        {"type": "line", "p1": [60, 0],  "p2": [60, 30]},
        {"type": "line", "p1": [60, 30], "p2": [0, 30]},
        {"type": "line", "p1": [0, 30],  "p2": [0, 0]},
    ], "closed": True}])

    up = _call(tools, "ecad_update_from_sch")
    assert up["ok"], f"update_from_sch failed: {up.get('error')}"
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    moves = []
    for fp in pcb.doc["footprints"]:
        pos = r1_pos if fp.get("ref") == "R1" else r2_pos
        moves.append({"op": "pcb_edit_footprint", "id": fp["id"],
                      "pos": [float(pos[0]), float(pos[1])], "angle": 0.0})
    ecad_session.apply_ops(pcb, pcb.revision, moves)
    return ecad_session.get_session(_ECAD["current_pcb"])


def _pad(pcb, ref: str, *, net: str = "", not_net: str = "") -> dict:
    """The pad of ``ref`` on ``net`` (or the one NOT on ``not_net``), with its world xy."""
    fp = next(f for f in pcb.doc["footprints"] if f.get("ref") == ref)
    for p in fp["pads"]:
        if net and p.get("net") != net:
            continue
        if not_net and p.get("net") == not_net:
            continue
        return {"number": str(p["number"]), "net": p.get("net"),
                "xy": tuple(float(v) for v in schema.pad_world(fp, p))}
    raise AssertionError(f"{ref}: no pad matching net={net!r} not_net={not_net!r} "
                         f"(pads: {[(p.get('number'), p.get('net')) for p in fp['pads']]})")


# --------------------------------------------------------------------------- tests


def test_route_pad_to_pad_lands_on_the_pads() -> None:
    """AC1: endpoints ARE the pad world coordinates and length IS their distance."""
    tools = _tools()
    pcb = _divider_board(tools)
    a = _pad(pcb, "R1", net="MID")
    b = _pad(pcb, "R2", net="MID")
    expect_len = math.hypot(b["xy"][0] - a["xy"][0], b["xy"][1] - a["xy"][1])
    assert expect_len > 1.0, "fixture must separate the two MID pads"

    r = _call(tools, "ecad_route_pad_to_pad",
              from_ref="R1", from_pad=a["number"], to_ref="R2", to_pad=b["number"])
    assert r["ok"], f"manual route failed: {r.get('error')}"
    assert r["net"] == "MID", f"net must be DETECTED as MID, got {r['net']!r}"
    assert len(r["segments"]) == 1, f"one straight segment expected, got {r['segments']}"
    seg = r["segments"][0]
    for got, want, what in ((seg["p1"], a["xy"], "p1/from-pad"), (seg["p2"], b["xy"], "p2/to-pad")):
        assert abs(got[0] - want[0]) < 1e-6 and abs(got[1] - want[1]) < 1e-6, \
            f"{what}: track {got} is not on the pad at {list(want)}"
    assert abs(seg["length_mm"] - expect_len) < 1e-6, \
        f"length {seg['length_mm']} != hand-computed {expect_len}"
    assert abs(r["length_mm"] - expect_len) < 1e-6, "total length must match too"
    assert seg["layer"] == "F.Cu" and seg["width_mm"] > 0.0

    # The track is really in the board doc, with the same id the response reported.
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    ids = [t["id"] for t in pcb.doc["tracks"]]
    assert seg["id"] in ids, f"track {seg['id']} is not in board tracks {ids}"
    print(f"  [route_pad_to_pad_lands_on_the_pads] PASS  {a['xy']} -> {b['xy']} "
          f"= {seg['length_mm']} mm")


def test_route_across_two_nets_is_refused_and_board_unchanged() -> None:
    """AC2: a pad pair on different nets is a SHORT — refuse, and change nothing."""
    tools = _tools()
    pcb = _divider_board(tools)
    a = _pad(pcb, "R1", net="+5V")
    b = _pad(pcb, "R2", net="GND")
    before_tracks = json.dumps(pcb.doc.get("tracks", []), sort_keys=True)
    before_vias = json.dumps(pcb.doc.get("vias", []), sort_keys=True)
    before_rev = pcb.revision

    r = _call(tools, "ecad_route_pad_to_pad",
              from_ref="R1", from_pad=a["number"], to_ref="R2", to_pad=b["number"])
    assert not r["ok"], "routing +5V to GND must be refused, not routed"
    assert "short" in json.dumps(r).lower(), f"the refusal must say why: {r}"
    assert r["from_net"] == "+5V" and r["to_net"] == "GND", \
        f"the refusal must name both nets, got {r.get('from_net')}/{r.get('to_net')}"
    assert r["next_actions"], "a refusal must name the concrete next call"

    after = ecad_session.get_session(_ECAD["current_pcb"])
    assert after.revision == before_rev, \
        f"refusal bumped the revision {before_rev} -> {after.revision}"
    assert json.dumps(after.doc.get("tracks", []), sort_keys=True) == before_tracks, \
        "refusal must not add copper"
    assert json.dumps(after.doc.get("vias", []), sort_keys=True) == before_vias, \
        "refusal must not add vias"
    print("  [route_across_two_nets_is_refused_and_board_unchanged] PASS  "
          f"+5V/GND refused, revision still {before_rev}")


def test_manual_trace_is_visible_to_drc() -> None:
    """AC3: a hand route that violates clearance IS reported by ecad_run_drc.

    R2 is placed on the far side of R1, so the straight MID-to-MID trace runs directly over
    R1's OTHER pad (+5V) — zero clearance between two different nets. If the tool wrote into
    a side structure instead of board['tracks'], DRC would stay clean and this fails.
    """
    tools = _tools()
    pcb = _divider_board(tools, r1_pos=(20.0, 10.0), r2_pos=(10.0, 10.0))
    a = _pad(pcb, "R1", net="MID")
    other = _pad(pcb, "R1", not_net="MID")   # the +5V pad the trace will run across
    b = _pad(pcb, "R2", net="MID")
    assert (a["xy"][0] - other["xy"][0]) * (a["xy"][0] - b["xy"][0]) > 0, \
        "fixture must put R1's other pad BETWEEN the two MID pads"

    clean = _call(tools, "ecad_run_drc")
    base_errors = clean["errors"]

    r = _call(tools, "ecad_route_pad_to_pad",
              from_ref="R1", from_pad=a["number"], to_ref="R2", to_pad=b["number"])
    assert r["ok"], f"route failed: {r.get('error')}"
    track_id = r["segments"][0]["id"]

    drc = _call(tools, "ecad_run_drc")
    assert drc["errors"] + len(drc["violations"]) > base_errors, \
        f"DRC did not see the hand trace (before={base_errors}, after={drc})"
    blob = json.dumps(drc)
    assert "clearance" in blob.lower(), f"expected a clearance violation, got {drc['by_rule']}"
    assert track_id in blob, \
        f"the violation must name the manual track {track_id}: {drc['violations']}"
    print(f"  [manual_trace_is_visible_to_drc] PASS  {track_id} flagged: {drc['violations'][:1]}")


def test_solved_net_class_width_is_used_when_width_omitted() -> None:
    """AC4: no explicit width -> the width ecad_stackup_solve solved for the class, exactly."""
    tools = _tools()
    pcb = _divider_board(tools)
    sol = _call(tools, "ecad_stackup_solve", copper_layers=4,
                targets={"se_50": 50}, net_map={"se_50": ["MID"]})
    assert sol["ok"], f"stackup_solve failed: {sol.get('error')}"
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    solved_w = float(pcb.doc["net_classes"]["se_50"]["track_width"])
    assert solved_w > 0.0 and abs(solved_w - 0.25) > 1e-9, \
        f"solved width {solved_w} must differ from the 0.25 board default to be a real test"

    a = _pad(pcb, "R1", net="MID")
    b = _pad(pcb, "R2", net="MID")
    r = _call(tools, "ecad_route_pad_to_pad",
              from_ref="R1", from_pad=a["number"], to_ref="R2", to_pad=b["number"])
    assert r["ok"], f"route failed: {r.get('error')}"
    assert r["net_class"] == "se_50", f"MID should be in se_50, got {r['net_class']!r}"
    assert abs(r["width_mm"] - solved_w) < 1e-9, \
        f"track width {r['width_mm']} != solved class width {solved_w}"
    assert r["width_source"] == "net_class:se_50", \
        f"precedence must be reported, got {r['width_source']!r}"
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    on_board = next(t for t in pcb.doc["tracks"] if t["id"] == r["segments"][0]["id"])
    assert abs(float(on_board["width"]) - solved_w) < 1e-9, \
        f"the BOARD track is {on_board['width']} mm, not the solved {solved_w} mm"

    # ...and an explicit width still wins.
    r2 = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad=a["number"],
               to_ref="R2", to_pad=b["number"], width=0.6)
    assert r2["ok"] and abs(r2["width_mm"] - 0.6) < 1e-9 and r2["width_source"] == "explicit", \
        f"explicit width must override the class: {r2.get('width_mm')} / {r2.get('width_source')}"
    print(f"  [solved_net_class_width_is_used_when_width_omitted] PASS  "
          f"solved={solved_w} mm used, explicit 0.6 mm overrides")


def test_layer_change_inserts_a_via() -> None:
    """A to_layer route becomes track -> through via -> track, all on the detected net."""
    tools = _tools()
    pcb = _divider_board(tools)
    a = _pad(pcb, "R1", net="MID")
    b = _pad(pcb, "R2", net="MID")
    r = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad=a["number"],
              to_ref="R2", to_pad=b["number"], layer="F.Cu", to_layer="B.Cu")
    assert not r["ok"], "an SMD pad has copper on one side only — B.Cu finish must be refused"
    assert "B.Cu" in json.dumps(r), f"the refusal must name the layer: {r}"

    # A tht pad spans both layers, so the layer change is legal there: fake one by widening
    # R2's MID pad layer list the way a through-hole footprint declares it.
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    doc = json.loads(json.dumps(pcb.doc))
    for fp in doc["footprints"]:
        if fp.get("ref") != "R2":
            continue
        for p in fp["pads"]:
            if p.get("net") == "MID":
                p["type"] = "tht"
                p["layers"] = ["F.Cu", "B.Cu"]
                p["drill"] = {"diameter": 0.6, "shape": "round", "offset": [0, 0]}
    ecad_session.swap_doc(pcb, doc)
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    a = _pad(pcb, "R1", net="MID")
    b = _pad(pcb, "R2", net="MID")

    r = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad=a["number"],
              to_ref="R2", to_pad=b["number"], layer="F.Cu", to_layer="B.Cu")
    assert r["ok"], f"layer-change route failed: {r.get('error')}"
    assert len(r["segments"]) == 2, f"expected two segments around the via: {r['segments']}"
    assert [s["layer"] for s in r["segments"]] == ["F.Cu", "B.Cu"], r["segments"]
    assert len(r["vias"]) == 1, f"expected exactly one via: {r['vias']}"
    via = r["vias"][0]
    assert via["layers"] == ["F.Cu", "B.Cu"], via
    mid = [(a["xy"][0] + b["xy"][0]) / 2.0, (a["xy"][1] + b["xy"][1]) / 2.0]
    assert abs(via["at"][0] - mid[0]) < 1e-6 and abs(via["at"][1] - mid[1]) < 1e-6, \
        f"default via should sit at the midpoint {mid}, got {via['at']}"
    expect = math.hypot(b["xy"][0] - a["xy"][0], b["xy"][1] - a["xy"][1])
    assert abs(r["length_mm"] - expect) < 1e-6, \
        f"the two halves must still total the pad distance ({r['length_mm']} vs {expect})"
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    assert any(v["id"] == via["id"] for v in pcb.doc["vias"]), "via must be on the board"
    print(f"  [layer_change_inserts_a_via] PASS  via {via['id']} at {via['at']}")


def test_unknown_endpoints_teach_instead_of_substituting() -> None:
    """An unknown ref/pad must NAME the real ones — never silently pick a nearby pad."""
    tools = _tools()
    pcb = _divider_board(tools)
    a = _pad(pcb, "R1", net="MID")
    before = json.dumps(pcb.doc.get("tracks", []), sort_keys=True)

    r = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad=a["number"],
              to_ref="U99", to_pad="1")
    assert not r["ok"] and "R1" in r["board_refs"] and "R2" in r["board_refs"], r
    r = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad="47",
              to_ref="R2", to_pad="1")
    assert not r["ok"] and sorted(r["pads"]) == ["1", "2"], r
    r = _call(tools, "ecad_route_pad_to_pad", from_ref="R1", from_pad=a["number"],
              to_ref="R2", to_pad="1", layer="In7.Cu")
    assert not r["ok"] and r["copper_layers"] == ["F.Cu", "B.Cu"], r

    after = ecad_session.get_session(_ECAD["current_pcb"])
    assert json.dumps(after.doc.get("tracks", []), sort_keys=True) == before, \
        "no refusal may leave copper behind"
    print("  [unknown_endpoints_teach_instead_of_substituting] PASS")


def test_tool_declares_real_parameters_not_kwargs() -> None:
    """A **kwargs tool disables the shared arg reconciler and makes FastMCP drop unknown keys
    silently — turning a loud unknown_argument into a silent wrong call. Never allowed."""
    import inspect

    sig = inspect.signature(_tools()["ecad_route_pad_to_pad"])
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), \
        "ecad_route_pad_to_pad must never declare **kwargs"
    for need in ("from_ref", "from_pad", "to_ref", "to_pad", "layer", "width", "doc_id"):
        assert need in sig.parameters, f"missing declared parameter {need}"
    print("  [tool_declares_real_parameters_not_kwargs] PASS")


def test_route_diff_pair_is_registered_and_forwards_rpc(monkeypatch) -> None:
    """The surgical pair tool is callable and forwards the resolved doc plus exact pair."""
    tools = _tools()
    assert "ecad_route_diff_pair" in tools
    pcb = _divider_board(tools)
    seen = {}

    def fake_rpc(method, params):
        seen.update(method=method, params=params)
        return {
            "revision": pcb.revision + 1,
            "report": {
                "routed": True, "length_p": 12.4, "length_n": 12.35,
                "skew_mm": 0.05, "coupled_pct": 96.0,
            },
        }

    monkeypatch.setattr(ecad_pcb_tools, "_rpc", fake_rpc)
    r = _call(tools, "ecad_route_diff_pair", net_p="USB_D+", net_n="USB_D-",
              opts={"diff_gap": 0.18, "length_tol": 0.1})

    assert seen == {
        "method": "ecad_route_diff_pair",
        "params": {
            "doc_id": pcb.doc_id, "net_p": "USB_D+", "net_n": "USB_D-",
            "opts": {"diff_gap": 0.18, "length_tol": 0.1},
        },
    }
    assert r["ok"] and r["routed"]
    assert r["skew_mm"] == 0.05 and r["coupled_pct"] == 96.0
    assert "skew=0.050mm" in r["hint"] and "coupled=96%" in r["hint"]


def test_route_diff_pair_coerces_json_string_opts(monkeypatch) -> None:
    """Stringified objects from tolerant model calls reach the RPC as real mappings."""
    tools = _tools()
    pcb = _divider_board(tools)
    seen = {}

    def fake_rpc(method, params):
        seen.update(method=method, params=params)
        return {"revision": pcb.revision + 1,
                "report": {"routed": True, "skew_mm": 0.08, "coupled_pct": 90.0}}

    monkeypatch.setattr(ecad_pcb_tools, "_rpc", fake_rpc)
    r = _call(tools, "ecad_route_diff_pair", net_p="CLK_P", net_n="CLK_N",
              opts='{"pitch": 0.15, "diff_gap": 0.2}')

    assert r["ok"]
    assert seen["params"]["opts"] == {"pitch": 0.15, "diff_gap": 0.2}


def test_route_diff_pair_missing_or_unknown_net_teaches(monkeypatch) -> None:
    """Missing names are rejected locally; an engine unknown-net error becomes guidance."""
    tools = _tools()
    _divider_board(tools)
    called = False

    def fake_rpc(method, params):
        nonlocal called
        called = True
        raise ecad_pcb_tools.OpError(
            "not_found", "unknown net 'NO_SUCH_NET'", {"net": "NO_SUCH_NET"})

    monkeypatch.setattr(ecad_pcb_tools, "_rpc", fake_rpc)
    missing = _call(tools, "ecad_route_diff_pair", net_p="", net_n="USB_D-")
    assert not missing["ok"] and "required" in missing["error"].lower()
    assert not called, "a missing required net must not reach the RPC"

    unknown = _call(tools, "ecad_route_diff_pair",
                    net_p="NO_SUCH_NET", net_n="USB_D-")
    assert called and not unknown["ok"]
    assert "unknown net" in unknown["error"].lower()
    assert "exact" in unknown["hint"].lower()
    assert unknown["next_actions"]


def main() -> None:
    test_route_pad_to_pad_lands_on_the_pads()
    test_route_across_two_nets_is_refused_and_board_unchanged()
    test_manual_trace_is_visible_to_drc()
    test_solved_net_class_width_is_used_when_width_omitted()
    test_layer_change_inserts_a_via()
    test_unknown_endpoints_teach_instead_of_substituting()
    test_tool_declares_real_parameters_not_kwargs()
    print("ALL PASS")


if __name__ == "__main__":
    main()
