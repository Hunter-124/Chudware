"""Tests for the ECAD schematic drawing-quality check (services/ecad/sch_qa.py +
``ecad_lint_schematic``).

ERC is electrical; this check is geometric. The tests are organised as matched PAIRS, because
the failure mode that would destroy the tool is a FALSE POSITIVE — an agent that learns to
ignore the check has lost the capability:

  * a DELIBERATELY broken sheet per category, asserting the specific offender is NAMED
    (refdes / wire-end coordinate / label text), and
  * a CLEAN sheet built with the normal tools (ecad_place_symbol + ecad_wire + ecad_add_power
    + ecad_label) asserting ZERO findings.

Plus the exclusions that make the clean case survive: a wire that terminates exactly on a pin
sitting ON the body boundary is not a crossing; a rail symbol whose body cannot be resolved is
skipped and NAMED rather than guessed at; two sheets are never compared.

``test_neuter_each_check`` proves each assertion above is actually load-bearing: with the check
function stubbed to return nothing, the matching broken-sheet assertions must fail.

Run:  .venv-mcp/bin/python -m pytest services/mcp/test_ecad_qa_tools.py -q
      .venv-mcp/bin/python -m services.mcp.test_ecad_qa_tools
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from services.ecad import sch_qa
from mcp_server.ecad_common import _ECAD, ecad_session
from mcp_server.ecad_qa_tools import register_ecad_qa_tools
from mcp_server.ecad_sch_tools import register_ecad_sch_tools

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- harness


class _StubMCP:
    """Collects @mcp.tool()-decorated functions by name (no FastMCP needed for tests)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _register() -> dict:
    stub = _StubMCP()
    register_ecad_sch_tools(stub)
    register_ecad_qa_tools(stub)
    return stub.tools


def _call(_tools: dict, _name: str, **kw) -> dict:
    out = _tools[_name](**kw)
    assert isinstance(out, str), f"{_name} must return a JSON string, got {type(out)}"
    return json.loads(out)


def _session():
    return ecad_session.get_session(_ECAD["current_sch"])


def _mutate(fn) -> None:
    """Edit the live schematic doc directly (the only way to author a BROKEN sheet the
    normal tools would never emit) and write it back to the C++ session."""
    sess = _session()
    doc = copy.deepcopy(sess.doc)
    fn(doc)
    ecad_session.swap_doc(sess, doc)


def _lint(tools: dict, **kw) -> dict:
    r = _call(tools, "ecad_lint_schematic", **kw)
    assert r["ok"], r
    for key in ("clean", "total", "counts", "findings", "checked", "hint", "next_actions",
                "warnings", "verdict"):
        assert key in r, f"ecad_lint_schematic payload missing {key!r}: {r}"
    return r


def _of(report: dict, kind: str) -> list:
    return [f for f in report["findings"] if f["kind"] == kind]


# Tool names of the FULL server, filled in by the registration test below (a subprocess, so
# importing server.py cannot disturb another test module's catalog snapshot).
_SERVER_NAMES: set = set()


def _run_probe(code: str) -> dict:
    out = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _server_names() -> set:
    if not _SERVER_NAMES:
        _SERVER_NAMES.update(_run_probe(
            "import sys, json; sys.path.insert(0, '.');"
            "from mcp_server import server;"
            "print(json.dumps({'names': sorted(server.mcp._tool_manager._tools)}))"
        )["names"])
    return _SERVER_NAMES


# --------------------------------------------------------------------------- fixtures (sheets)


def _build_clean(tools: dict) -> dict:
    """A realistic, geometrically clean sheet built ONLY with the normal tools.

    R1 - R2 divider, C1 hung off the midpoint, both rails on power ports, and a net label
    dropped ON the midpoint wire. This is the false-positive guard: any check that fires here
    is broken.

    Every coordinate that must LAND on something is read back from the tools (the placement's
    pin xy, the auto-router's own wire points) instead of being hardcoded — a hardcoded guess
    would make this guard pass or fail for the wrong reason.
    """
    _call(tools, "ecad_new_schematic", name="qa_clean")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20.32, y=0, ref="R2")
    c1 = _call(tools, "ecad_place_symbol", libid="Device:C", x=12.7, y=-20.32, ref="C1")
    c1_pins = {p["number"]: p["xy"] for p in c1["pins"]}
    _call(tools, "ecad_wire", from_ref="R1", from_pin="2", to_ref="R2", to_pin="1")
    _call(tools, "ecad_wire", from_ref="R2", from_pin="1", to_ref="C1", to_pin="1")
    _call(tools, "ecad_add_power", net="VCC", x=-5.08, y=0)
    _call(tools, "ecad_add_power", net="GND", x=25.4, y=0)
    # Midpoint of the LAST segment the auto-router actually emitted for the R2->C1 wire, so
    # the label provably sits on a wire.
    pts = _session().doc["wires"][-1]["pts"]
    a, b = pts[-2], pts[-1]
    mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]
    _call(tools, "ecad_label", text="MID", x=mid[0], y=mid[1])
    return {"c1_pins": c1_pins, "label_pos": mid}


def _test_lib(libid: str, pin_pos) -> dict:
    """A 4x4 mm rect-bodied part with ONE zero-length pin at ``pin_pos`` (local mm).

    ``pin_pos = (2, 0)`` puts the connection point exactly ON the body edge; ``(0, 0)`` puts it
    INSIDE the body. Both are legal symbol geometry and both must be excluded from the
    crossing check when a wire merely terminates there.
    """
    return {
        "libid": libid, "name": libid.split(":")[-1],
        "description": "test part with a controlled pin position",
        "keywords": "test", "reference_prefix": "U", "value": "BND",
        "footprint_filters": [], "units_count": 1, "power": False,
        "graphics": [{"type": "rect", "p1": [-2.0, -2.0], "p2": [2.0, 2.0],
                      "layer": "F.SilkS", "width": 0.15}],
        "pins": [{"number": "1", "name": "E",
                  "pos": [float(pin_pos[0]), float(pin_pos[1])], "angle": 0.0,
                  "length": 0.0, "type": "passive", "shape": "line", "unit": 1}],
    }


def _place_test_part(ref: str = "U9", pin_pos=(2.0, 0.0), pos=(0.0, 0.0),
                     libid: str = "Test:BoundaryPin") -> None:
    def edit(doc: dict) -> None:
        doc.setdefault("lib_cache", {})[libid] = _test_lib(libid, pin_pos)
        doc.setdefault("symbols", []).append({
            "id": f"s{len(doc.get('symbols', [])) + 90}", "ref": ref,
            "libid": libid, "pos": [float(pos[0]), float(pos[1])],
            "angle": 0.0, "mirror": "none", "unit": 1, "value": "BND",
            "sheet": "sh1", "fields": {}, "in_bom": True, "on_board": True, "dnp": False,
        })
    _mutate(edit)


def _add_wire(wid: str, pts: list, sheet: str = "sh1") -> None:
    def edit(doc: dict) -> None:
        doc.setdefault("wires", []).append({"id": wid, "pts": [list(map(float, p)) for p in pts],
                                            "bus": False, "sheet": sheet})
    _mutate(edit)


# --------------------------------------------------------------------------- registration


def test_register() -> dict:
    tools = _register()
    assert "ecad_lint_schematic" in tools, sorted(tools)
    import inspect
    sig = inspect.signature(tools["ecad_lint_schematic"])
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), \
        "ecad_lint_schematic must never declare **kwargs (it disables the arg reconciler)"
    assert set(sig.parameters) == {"doc_id", "max_per_kind"}, list(sig.parameters)
    print("  [register] PASS")
    return tools


def test_registered_under_family_ecad_and_count_went_up() -> None:
    """The tool is catalog-tagged family 'ecad' (Settings toggle + deny_reason depend on it)
    and the live registry gained exactly one tool.

    The baseline is measured by importing server.py with THIS module's register call stubbed
    out — the only honest way to show the count went UP rather than asserting a magic number.
    """
    probe = (
        "import sys, types, json; sys.path.insert(0, '.');"
        "from mcp_server import server;"
        "from services.mcp import tool_registry as reg;"
        "print(json.dumps({'names': sorted(server.mcp._tool_manager._tools),"
        " 'tag': reg.catalog().get('ecad_lint_schematic')}))"
    )
    neuter = (
        "import sys, types; sys.path.insert(0, '.');"
        "m = types.ModuleType('mcp_server.ecad_qa_tools');"
        "m.register_ecad_qa_tools = lambda mcp: None;"
        "sys.modules['mcp_server.ecad_qa_tools'] = m;"
    ) + probe

    live = _run_probe(probe)
    base = _run_probe(neuter)
    n_live, n_base = len(live["names"]), len(base["names"])
    assert "ecad_lint_schematic" in live["names"], n_live
    assert "ecad_lint_schematic" not in base["names"], n_base
    assert live["tag"] == {"family": "ecad", "risk": "write"}, live["tag"]
    assert n_live == n_base + 1, \
        f"registry count must go UP by exactly 1: {n_base} -> {n_live}"
    _SERVER_NAMES.update(live["names"])
    print(f"  [registered_under_family_ecad_and_count_went_up] PASS "
          f"({n_base} -> {n_live} tools)")


def test_reachable_from_the_general_lane() -> None:
    """The ecad category pack must actually resolve to the new name, or the general lane
    cannot call it however well it is registered."""
    from services.agent import tool_categories as tc
    resolved = tc.resolve_categories(["ecad"])
    assert "ecad_lint_schematic" in resolved, \
        ("ecad_lint_schematic is not reachable from the 'ecad' category pack — it needs an "
         "explicit entry in CATEGORY_PACKS['ecad'] (family:ecad expansion is capped at "
         f"{tc.CATEGORY_EXPAND_CAP})")
    print("  [reachable_from_the_general_lane] PASS")


# --------------------------------------------------------------------------- the clean sheet


def test_clean_schematic_has_zero_findings(tools: dict) -> None:
    """THE false-positive guard: a normal, tool-built sheet must produce nothing at all."""
    _build_clean(tools)
    r = _lint(tools)
    assert r["clean"] is True, f"clean sheet must report clean: {r['findings']}"
    assert r["total"] == 0, r["findings"]
    for kind, n in r["counts"].items():
        assert n == 0, f"{kind} fired on a clean sheet: {_of(r, kind)}"
    ch = r["checked"]
    assert ch["symbols"] == 3 and ch["wires"] == 2 and ch["labels"] == 1, ch
    assert ch["bodies_resolved"] == 3 and ch["symbols_without_body"] == [], ch
    assert r["warnings"] == [], r["warnings"]
    assert "clean" in r["verdict"], r["verdict"]
    print("  [clean_schematic_has_zero_findings] PASS")


# --------------------------------------------------------------------------- overlap


def _assert_overlap_reported(tools: dict) -> dict:
    r = _lint(tools)
    hits = _of(r, "symbol_overlap")
    assert len(hits) == 1, f"expected exactly one overlapping pair, got {hits}"
    h = hits[0]
    assert set(h["refs"]) == {"R1", "R2"}, h
    assert "R1" in h["message"] and "R2" in h["message"], h["message"]
    x0, y0, x1, y1 = h["overlap_bbox"]
    assert x1 > x0 and y1 > y0, f"overlap box must have area: {h}"
    return h


def test_symbol_overlap_names_both_refs(tools: dict) -> None:
    """Two resistors 0.5 mm apart: bodies (2.03 x 1.27 mm) interpenetrate."""
    _call(tools, "ecad_new_schematic", name="qa_overlap")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0.5, y=0, ref="R2")
    h = _assert_overlap_reported(tools)
    # R1 body x in [-1.016, 1.016], R2 body x in [-0.516, 1.516] -> shared x in [-0.516, 1.016]
    assert abs(h["overlap_bbox"][0] - -0.516) < 1e-3, h
    assert abs(h["overlap_bbox"][2] - 1.016) < 1e-3, h
    print("  [symbol_overlap_names_both_refs] PASS")


def test_adjacent_symbols_that_only_touch_are_not_overlapping(tools: dict) -> None:
    """Bodies sharing exactly one edge is a tight layout, not a defect."""
    _call(tools, "ecad_new_schematic", name="qa_touch")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=2.032, y=0, ref="R2")
    r = _lint(tools)
    assert _of(r, "symbol_overlap") == [], r["findings"]
    print("  [adjacent_symbols_that_only_touch_are_not_overlapping] PASS")


def test_two_sheets_are_never_compared(tools: dict) -> None:
    """Identical positions on DIFFERENT sheets is not an overlap."""
    _call(tools, "ecad_new_schematic", name="qa_sheets")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R2")
    assert _of(_lint(tools), "symbol_overlap"), "same-sheet coincident bodies must overlap"

    def edit(doc: dict) -> None:
        doc["sheets"].append({"id": "sh2", "name": "page2", "parent": "sh1",
                              "size": [297, 210], "ports": []})
        for s in doc["symbols"]:
            if s.get("ref") == "R2":
                s["sheet"] = "sh2"
    _mutate(edit)
    r = _lint(tools)
    assert _of(r, "symbol_overlap") == [], \
        f"cross-sheet pairs must never be compared: {r['findings']}"
    print("  [two_sheets_are_never_compared] PASS")


# --------------------------------------------------------------------------- dangling wires


def _assert_dangling_reported(tools: dict) -> list:
    r = _lint(tools)
    hits = _of(r, "dangling_wire")
    assert len(hits) == 2, f"both ends of an orphan wire must be named: {hits}"
    ends = {tuple(h["pos"]) for h in hits}
    assert ends == {(40.0, 40.0), (50.0, 40.0)}, ends
    for h in hits:
        assert h["wire_id"] == "w90", h
        assert h["end"] in ("start", "end"), h
        assert "touches nothing" in h["message"], h["message"]
    return hits


def test_dangling_wire_names_the_endpoint_coordinates(tools: dict) -> None:
    _build_clean(tools)
    _add_wire("w90", [[40.0, 40.0], [50.0, 40.0]])
    _assert_dangling_reported(tools)
    print("  [dangling_wire_names_the_endpoint_coordinates] PASS")


def test_near_miss_stub_is_dangling_and_reports_the_gap(tools: dict) -> None:
    """A wire end 0.25 mm off a pin is NOT connected (netlist snaps at 1e-6 mm) — report it
    with the measured miss distance, which is what makes it fixable."""
    _build_clean(tools)
    _add_wire("w91", [[-5.08, 30.0], [-5.33, 0.0]])   # R1 pin 1 tip is at (-5.08, 0)
    r = _lint(tools)
    near = [h for h in _of(r, "dangling_wire") if tuple(h["pos"]) == (-5.33, 0.0)]
    assert len(near) == 1, _of(r, "dangling_wire")
    n = near[0]["nearest"]
    assert "R1.1" in n["what"], n
    assert abs(n["distance_mm"] - 0.25) < 1e-3, n
    assert "near miss" in near[0]["message"], near[0]["message"]
    print("  [near_miss_stub_is_dangling_and_reports_the_gap] PASS")


def test_wire_ended_by_a_label_or_junction_is_not_dangling(tools: dict) -> None:
    """A named-net stub (wire + label on its end) and a junction both terminate a wire."""
    _build_clean(tools)
    _add_wire("w92", [[-5.08, 0.0], [-5.08, 12.7]])   # starts on R1.1's tip
    _call(tools, "ecad_label", text="VIN", x=-5.08, y=12.7)
    r = _lint(tools)
    assert [h for h in _of(r, "dangling_wire") if h["wire_id"] == "w92"] == [], r["findings"]
    assert _of(r, "floating_label") == [], r["findings"]

    _add_wire("w93", [[25.4, 0.0], [25.4, -12.7]])    # starts on the GND port
    def edit(doc: dict) -> None:
        doc.setdefault("junctions", []).append({"id": "j90", "pos": [25.4, -12.7],
                                                "sheet": "sh1"})
    _mutate(edit)
    r = _lint(tools)
    assert [h for h in _of(r, "dangling_wire") if h["wire_id"] == "w93"] == [], r["findings"]
    print("  [wire_ended_by_a_label_or_junction_is_not_dangling] PASS")


# --------------------------------------------------------------------------- crossings


def _assert_crossing_reported(tools: dict) -> dict:
    r = _lint(tools)
    hits = [h for h in _of(r, "wire_crosses_symbol") if h["wire_id"] == "w94"]
    assert len(hits) == 1, f"expected the wire through R1's body to be named: {r['findings']}"
    h = hits[0]
    assert h["ref"] == "R1", h
    assert "R1" in h["message"], h["message"]
    # R1's body is 2.032 mm wide; the 0.05 mm inset trims a little off each side.
    assert 1.8 < h["inside_mm"] <= 2.032, h
    assert h["enter"][0] < h["exit"][0], h
    return h


def test_wire_through_a_body_names_the_refdes_and_the_span(tools: dict) -> None:
    _build_clean(tools)
    _add_wire("w94", [[-12.7, 0.0], [12.7, 0.0]])     # straight through R1 at the origin
    _assert_crossing_reported(tools)
    print("  [wire_through_a_body_names_the_refdes_and_the_span] PASS")


def test_wire_crossing_a_pin_lead_is_not_crossing_the_body(tools: dict) -> None:
    """A pin's LEAD is not the body. Device:R draws its lead as a graphic line from the pin root
    (x = 2.54 mm) to the body edge (x = 1.016 mm), so a wire running vertically at x = 1.905 mm
    crosses the LEAD and nothing else. Legal — and the exact case that fires if lead graphics
    are folded into the body box.
    """
    _call(tools, "ecad_new_schematic", name="qa_lead")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _add_wire("w82", [[1.905, -7.62], [1.905, 7.62]])
    r = _lint(tools)
    assert _of(r, "wire_crosses_symbol") == [], \
        f"a wire over a pin lead must not be a body crossing: {r['findings']}"
    print("  [wire_crossing_a_pin_lead_is_not_crossing_the_body] PASS")


def test_wire_terminating_on_a_boundary_pin_is_not_a_crossing(tools: dict) -> None:
    """THE exclusion that keeps this check usable: a pin can sit ON the body edge, and a wire
    ending there touches the boundary without entering the body."""
    _call(tools, "ecad_new_schematic", name="qa_boundary")
    _place_test_part("U9", pin_pos=(2.0, 0.0))
    _add_wire("w95", [[12.7, 0.0], [2.0, 0.0]])       # ends exactly on U9 pin 1 at (2, 0)
    r = _lint(tools)
    assert _of(r, "wire_crosses_symbol") == [], \
        f"a wire ending on a boundary pin must not be a crossing: {r['findings']}"
    # ...and the end that lands on the pin is not dangling either.
    assert [h for h in _of(r, "dangling_wire") if tuple(h["pos"]) == (2.0, 0.0)] == [], \
        r["findings"]
    print("  [wire_terminating_on_a_boundary_pin_is_not_a_crossing] PASS")


def test_wire_through_the_same_boundary_part_is_still_reported(tools: dict) -> None:
    """Contrast for the test above: the exclusion is about TERMINATING, not about the part."""
    _call(tools, "ecad_new_schematic", name="qa_boundary_cross")
    _place_test_part("U9", pin_pos=(2.0, 0.0))
    _add_wire("w96", [[12.7, 0.0], [-12.7, 0.0]])     # right through the 4 mm body
    hits = _of(_lint(tools), "wire_crosses_symbol")
    assert len(hits) == 1 and hits[0]["ref"] == "U9", hits
    assert 3.8 < hits[0]["inside_mm"] <= 4.0, hits[0]
    print("  [wire_through_the_same_boundary_part_is_still_reported] PASS")


def test_wire_ending_on_a_pin_inside_its_own_body_is_not_a_crossing(tools: dict) -> None:
    """Some symbols put the connection point INSIDE the body outline (exposed pads, centre
    pins). Reaching that pin necessarily enters the body — that is the symbol definition's
    geometry, not a routing defect, so it must not be reported."""
    _call(tools, "ecad_new_schematic", name="qa_interior")
    _place_test_part("U8", pin_pos=(0.0, 0.0), libid="Test:InteriorPin")
    _add_wire("w97", [[12.7, 0.0], [0.0, 0.0]])       # terminates on the centre pin
    r = _lint(tools)
    assert _of(r, "wire_crosses_symbol") == [], \
        f"reaching a pin inside its own body must not be a crossing: {r['findings']}"
    assert [h for h in _of(r, "dangling_wire") if tuple(h["pos"]) == (0.0, 0.0)] == [], \
        r["findings"]
    print("  [wire_ending_on_a_pin_inside_its_own_body_is_not_a_crossing] PASS")


def test_wire_passing_over_an_interior_pin_part_is_still_reported(tools: dict) -> None:
    """Contrast: the same part, but the wire passes straight over it instead of terminating."""
    _call(tools, "ecad_new_schematic", name="qa_interior_cross")
    _place_test_part("U8", pin_pos=(0.0, 0.0), libid="Test:InteriorPin")
    _add_wire("w98", [[12.7, 1.0], [-12.7, 1.0]])     # over the body, ends nowhere near the pin
    hits = _of(_lint(tools), "wire_crosses_symbol")
    assert len(hits) == 1 and hits[0]["ref"] == "U8", hits
    print("  [wire_passing_over_an_interior_pin_part_is_still_reported] PASS")


def test_rotated_symbol_body_follows_the_transform(tools: dict) -> None:
    """The body is an ORIENTED quad, not a static bbox: rotating a resistor 90 deg must swap
    which wires cross it, or every rotated part on a sheet is judged against the wrong shape."""
    _call(tools, "ecad_new_schematic", name="qa_rotated")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1", angle=90)
    quad = sch_qa.symbol_body_quad(_session().doc, _session().doc["symbols"][0])
    xs = sorted(round(p[0], 3) for p in quad)
    ys = sorted(round(p[1], 3) for p in quad)
    assert (xs[0], xs[-1]) == (-0.635, 0.635), quad   # the 1.27 mm side is now horizontal
    assert (ys[0], ys[-1]) == (-1.016, 1.016), quad
    _add_wire("w80", [[0.0, -12.7], [0.0, 12.7]])     # along the rotated part's long axis
    _add_wire("w81", [[-12.7, 3.0], [12.7, 3.0]])     # clear of it
    hits = _of(_lint(tools), "wire_crosses_symbol")
    assert [h["wire_id"] for h in hits] == ["w80"], hits
    assert 1.8 < hits[0]["inside_mm"] <= 2.032, hits[0]
    print("  [rotated_symbol_body_follows_the_transform] PASS")


def test_rail_symbol_without_a_resolvable_body_is_skipped_and_named(tools: dict) -> None:
    """power:VCC is drawn as a single bar + lead: there is no body to test. Skip it and SAY
    so — never guess a bbox and then flag the wire that legitimately reaches its pin."""
    _call(tools, "ecad_new_schematic", name="qa_rail")
    _call(tools, "ecad_place_symbol", libid="power:VCC", x=0, y=0, ref="#PWR01")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=-10.16, ref="R1")
    r = _lint(tools)
    assert _of(r, "wire_crosses_symbol") == [], r["findings"]
    assert _of(r, "symbol_overlap") == [], r["findings"]
    assert "#PWR01" in r["checked"]["symbols_without_body"], r["checked"]
    assert any("#PWR01" in w for w in r["warnings"]), r["warnings"]
    print("  [rail_symbol_without_a_resolvable_body_is_skipped_and_named] PASS")


def test_pin_lead_graphics_are_not_part_of_the_body() -> None:
    """The body of Device:R is its 2.03 x 1.27 mm rect, NOT its 5.08 mm pin span — otherwise
    every wire reaching a pin would be a 'crossing'. Measured over the whole starter library."""
    from services.ecad.library import starter
    assert sch_qa.body_local_rect(starter.SYMBOLS["Device:R"]) == \
        (-1.016, -0.635, 1.016, 0.635)
    no_body = [lid for lid, s in starter.SYMBOLS.items()
               if sch_qa.body_local_rect(s) is None]
    assert sorted(no_body) == ["power:+3V3", "power:+5V", "power:-5V", "power:VCC"], no_body
    print(f"  [pin_lead_graphics_are_not_part_of_the_body] PASS "
          f"({len(starter.SYMBOLS) - len(no_body)}/{len(starter.SYMBOLS)} bodies resolved)")


# --------------------------------------------------------------------------- labels


def _assert_floating_label_reported(tools: dict) -> dict:
    r = _lint(tools)
    hits = _of(r, "floating_label")
    assert len(hits) == 1, f"expected the orphan label to be named: {r['findings']}"
    h = hits[0]
    assert h["text"] == "NOWHERE", h
    assert "NOWHERE" in h["message"], h["message"]
    assert h["pos"] == [80.0, 80.0], h
    return h


def test_floating_label_names_its_text_and_position(tools: dict) -> None:
    _build_clean(tools)
    _call(tools, "ecad_label", text="NOWHERE", x=80.0, y=80.0)
    _assert_floating_label_reported(tools)
    print("  [floating_label_names_its_text_and_position] PASS")


def test_label_on_a_pin_is_not_floating(tools: dict) -> None:
    """A label placed on a pin's world xy names that pin's net — the documented workflow."""
    info = _build_clean(tools)
    x, y = info["c1_pins"]["1"]                                 # C1 pin 1 world xy
    _call(tools, "ecad_label", text="CTOP", x=x, y=y)
    r = _lint(tools)
    assert _of(r, "floating_label") == [], r["findings"]
    print("  [label_on_a_pin_is_not_floating] PASS")


# --------------------------------------------------------------------------- report shape


def test_finding_messages_only_name_real_tools(tools: dict) -> None:
    """Every fix a finding recommends must be a tool that EXISTS. A message naming
    'ecad_move_symbol' (there is no such tool) teaches the agent a dead end."""
    import re
    _call(tools, "ecad_new_schematic", name="qa_messages")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0.5, y=0, ref="R2")
    _add_wire("w80", [[-12.7, 0.0], [12.7, 0.0]])
    _call(tools, "ecad_label", text="SDA", x=80.0, y=80.0)
    r = _lint(tools)
    assert r["total"] >= 4, r["counts"]
    text = " ".join([f["message"] for f in r["findings"]] + r["next_actions"] + [r["verdict"]])
    named = set(re.findall(r"\b(?:ecad|apply|render)_[a-z0-9_]+\b", text))
    assert named, text
    missing = sorted(named - _server_names())
    assert not missing, f"findings recommend tools that do not exist: {missing}"
    print(f"  [finding_messages_only_name_real_tools] PASS ({len(named)} tools named)")


def test_truncation_is_reported_never_silent(tools: dict) -> None:
    _build_clean(tools)
    _add_wire("w90", [[40.0, 40.0], [50.0, 40.0]])
    r = _lint(tools, max_per_kind=1)
    assert r["counts"]["dangling_wire"] == 2, r["counts"]
    assert len(_of(r, "dangling_wire")) == 1, r["findings"]
    assert r["truncated"] == {"dangling_wire": 1}, r["truncated"]
    assert any("max_per_kind" in w for w in r["warnings"]), r["warnings"]
    print("  [truncation_is_reported_never_silent] PASS")


def test_board_document_is_refused_not_silently_checked() -> None:
    """sch_qa is schematic-only; handing it a board must raise, not return 'clean'."""
    from services.ecad.schema import new_board
    try:
        sch_qa.check_schematic_geometry(new_board("b"))
    except ValueError as e:
        assert "not a schematic" in str(e), str(e)
    else:
        raise AssertionError("a board doc must be refused, not reported clean")
    print("  [board_document_is_refused_not_silently_checked] PASS")


# --------------------------------------------------------------------------- neuter check


def test_neuter_each_check_makes_its_assertions_fail(tools: dict) -> None:
    """Disable each check in turn and confirm the matching broken-sheet assertions FAIL.

    Without this, a check that silently degraded to ``return []`` would leave every test above
    passing on its negative half alone.
    """
    cases = (
        ("check_symbol_overlaps", _assert_overlap_reported, _setup_overlap),
        ("check_dangling_wires", _assert_dangling_reported, _setup_dangling),
        ("check_wires_crossing_symbols", _assert_crossing_reported, _setup_crossing),
        ("check_floating_labels", _assert_floating_label_reported, _setup_floating_label),
    )
    for fname, assertion, setup in cases:
        setup(tools)
        assertion(tools)                      # live code finds it
        real = getattr(sch_qa, fname)
        setattr(sch_qa, fname, lambda doc: [])
        try:
            try:
                assertion(tools)
            except AssertionError:
                pass
            else:
                raise AssertionError(
                    f"neutering sch_qa.{fname} did NOT break its test — the assertion is not "
                    f"actually exercising that check")
        finally:
            setattr(sch_qa, fname, real)
        assertion(tools)                      # restored
    print("  [neuter_each_check_makes_its_assertions_fail] PASS")


def _setup_overlap(tools: dict) -> None:
    _call(tools, "ecad_new_schematic", name="qa_neuter_overlap")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0.5, y=0, ref="R2")


def _setup_dangling(tools: dict) -> None:
    _build_clean(tools)
    _add_wire("w90", [[40.0, 40.0], [50.0, 40.0]])


def _setup_crossing(tools: dict) -> None:
    _build_clean(tools)
    _add_wire("w94", [[-12.7, 0.0], [12.7, 0.0]])


def _setup_floating_label(tools: dict) -> None:
    _build_clean(tools)
    _call(tools, "ecad_label", text="NOWHERE", x=80.0, y=80.0)


# --------------------------------------------------------------------------- pytest / main


try:
    import pytest
except ImportError:  # pragma: no cover — pytest absent when run via main()
    pytest = None  # type: ignore


if pytest is not None:
    @pytest.fixture(scope="module")
    def tools() -> dict:
        return _register()


def main() -> None:
    print("ECAD schematic drawing-quality (sch_qa) tests")
    t = test_register()
    test_registered_under_family_ecad_and_count_went_up()
    test_reachable_from_the_general_lane()
    test_clean_schematic_has_zero_findings(t)
    test_symbol_overlap_names_both_refs(t)
    test_adjacent_symbols_that_only_touch_are_not_overlapping(t)
    test_two_sheets_are_never_compared(t)
    test_dangling_wire_names_the_endpoint_coordinates(t)
    test_near_miss_stub_is_dangling_and_reports_the_gap(t)
    test_wire_ended_by_a_label_or_junction_is_not_dangling(t)
    test_wire_through_a_body_names_the_refdes_and_the_span(t)
    test_wire_crossing_a_pin_lead_is_not_crossing_the_body(t)
    test_wire_terminating_on_a_boundary_pin_is_not_a_crossing(t)
    test_wire_through_the_same_boundary_part_is_still_reported(t)
    test_wire_ending_on_a_pin_inside_its_own_body_is_not_a_crossing(t)
    test_wire_passing_over_an_interior_pin_part_is_still_reported(t)
    test_rotated_symbol_body_follows_the_transform(t)
    test_rail_symbol_without_a_resolvable_body_is_skipped_and_named(t)
    test_pin_lead_graphics_are_not_part_of_the_body()
    test_floating_label_names_its_text_and_position(t)
    test_label_on_a_pin_is_not_floating(t)
    test_finding_messages_only_name_real_tools(t)
    test_truncation_is_reported_never_silent(t)
    test_board_document_is_refused_not_silently_checked()
    test_neuter_each_check_makes_its_assertions_fail(t)
    print("ALL PASS")


if __name__ == "__main__":
    main()
