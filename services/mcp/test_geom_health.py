"""Solid-health verification tool (services/mcp/cad_eval_tools.py::check_geometry_health).

WHY THIS TOOL EXISTS: nothing in the 400-odd tool registry answered "is this body actually a
sound closed solid?" — zero tools matched watertight|manifold|mesh_qual|repair — so an agent
committed to an FEA mesh, a CAM toolpath or a STEP/STL export without ever checking, and the
first symptom of a bad body was a confusing downstream failure or a silently wrong export.

EVERY BODY BELOW IS REAL. The good one is a box built through add_features; the unsound one is
an open surf_extrude wall (a surface body with NO end caps — the cheatsheet's own description),
also built through add_features. Nothing here is monkeypatched, so a regression in the engine
census (core/doc/session.cpp::doc_eval_solid_health) fails these tests rather than sliding past
a stubbed response.

Run: .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_geom_health.py -q
"""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn as at

ALIASES = ("is_watertight", "assess_mesh_quality", "analyze_stl_geometry",
           "health_check", "design_audit")


def _dispatch(tool: str, /, **kwargs):
    """Exactly what the built-in agent does. Returns (payload, reconciler_error)."""
    fn = at._bind_tool(tool)
    assert fn is not None, f"{tool} does not resolve in the MCP registry"
    bound, _notes, err = at._reconcile_kwargs(tool, fn, kwargs)
    if err is not None:
        return None, str(err.get("error", ""))
    out = fn(**bound)
    return (json.loads(out) if isinstance(out, str) else out), None


def _call(tool: str, /, **kwargs) -> dict:
    payload, err = _dispatch(tool, **kwargs)
    assert err is None, f"{tool} rejected {sorted(kwargs)} before calling: {err}"
    return payload


def _new_doc(name: str, ops: list) -> str:
    doc_id = _call("new_part", name=name)["doc_id"]
    built = _call("add_features", doc_id=doc_id, ops=ops)
    assert built.get("ok") is True, built
    assert all(f["ok"] for f in built["features"]), built
    return doc_id


# 80 x 60 x 10 mm — volume 48000 mm^3 by hand, 6 faces, 12 edges, 8 vertices, 1 shell.
BOX = [{"op": "add_feature", "feature": {
    "type": "box", "name": "Plate",
    "params": {"length": 80, "width": 60, "height": 10}}}]

# An OPEN wall: a 40 x 30 rectangle extruded 20 mm as a SURFACE. surf_extrude makes side
# walls with NO end caps (services/agent/prompts/shared/cheatsheets/surfacing.md), so the
# result is a genuinely unsound body — not a solid, open at both rims.
OPEN_WALL = [
    {"op": "add_feature", "feature": {
        "type": "sketch", "name": "Rect", "params": {"entities": [], "constraints": []},
        "refs": {"plane": "plane.top"}}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [-20, -15], "p2": [20, -15]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [20, -15], "p2": [20, 15]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [20, 15], "p2": [-20, 15]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [-20, 15], "p2": [-20, -15]}},
    {"op": "add_feature", "feature": {
        "type": "surf_extrude", "name": "Wall",
        "params": {"end": "blind", "depth": {"value": 20, "expr": None}},
        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                              "centroid": [0, 0], "hint_index": 0}]}}},
]

# A bare planar FACE (surf_planar) — no shell at all, so the orientation check has nothing
# to run on and must say so rather than reporting "consistent".
PLANAR_FACE = OPEN_WALL[:5] + [
    {"op": "add_feature", "feature": {
        "type": "surf_planar", "name": "Panel", "params": {},
        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                              "centroid": [0, 0], "hint_index": 0}]}}},
]

# Two flat panels that CROSS each other, sewn into ONE body. surf_planar on plane.top spans
# x,y in [-20,20]; surf_planar on plane.front spans x,z in [-10,10]; they pass through each
# other along y=0,z=0. surf_knit sews them into a single surface body, so the result is one
# body whose own skin passes through itself — the case a watertight/manifold census is blind
# to by construction. No feature op will emit a self-intersecting SOLID (extrude rejects a
# self-intersecting profile, sweep rejects a self-intersecting sweep, surf_thicken fails on
# a folded wall), so the solid case lives in core/tests/test_document.cpp.
CROSSED_PANELS = [
    {"op": "add_feature", "feature": {
        "type": "sketch", "name": "Flat", "params": {"entities": [], "constraints": []},
        "refs": {"plane": "plane.top"}}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [-20, -20], "p2": [20, -20]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [20, -20], "p2": [20, 20]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [20, 20], "p2": [-20, 20]}},
    {"op": "sketch_add_entity", "sketch": "$0",
     "entity": {"type": "line", "p1": [-20, 20], "p2": [-20, -20]}},
    {"op": "add_feature", "feature": {
        "type": "surf_planar", "name": "PanelA", "params": {},
        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                              "centroid": [0, 0], "hint_index": 0}]}}},
    {"op": "add_feature", "feature": {
        "type": "sketch", "name": "Up", "params": {"entities": [], "constraints": []},
        "refs": {"plane": "plane.front"}}},
    {"op": "sketch_add_entity", "sketch": "$6",
     "entity": {"type": "line", "p1": [-10, -10], "p2": [10, -10]}},
    {"op": "sketch_add_entity", "sketch": "$6",
     "entity": {"type": "line", "p1": [10, -10], "p2": [10, 10]}},
    {"op": "sketch_add_entity", "sketch": "$6",
     "entity": {"type": "line", "p1": [10, 10], "p2": [-10, 10]}},
    {"op": "sketch_add_entity", "sketch": "$6",
     "entity": {"type": "line", "p1": [-10, 10], "p2": [-10, -10]}},
    {"op": "add_feature", "feature": {
        "type": "surf_planar", "name": "PanelB", "params": {},
        "refs": {"profile": [{"kind": "region", "sketch": "$6",
                              "centroid": [0, 0], "hint_index": 0}]}}},
    {"op": "add_feature", "feature": {
        "type": "surf_knit", "name": "Knit", "params": {},
        "refs": {"surfaces": [{"kind": "body", "owner_feature": "$5"},
                              {"kind": "body", "owner_feature": "$11"}]}}},
]


@pytest.fixture(scope="module")
def box() -> str:
    return _new_doc("health_box", BOX)


@pytest.fixture(scope="module")
def open_wall() -> str:
    return _new_doc("health_open_wall", OPEN_WALL)


@pytest.fixture(scope="module")
def crossed_panels() -> str:
    return _new_doc("health_crossed_panels", CROSSED_PANELS)


# --------------------------------------------------------------------------
# a KNOWN-GOOD solid
# --------------------------------------------------------------------------

def test_a_plain_box_is_watertight_with_the_counts_that_prove_it(box):
    """The verdict AND its justification, on a body whose numbers are hand-computable."""
    out = _call("check_geometry_health", doc_id=box)
    assert out["ok"] is True, out
    assert out["watertight"] is True and out["is_watertight"] is True, out
    assert out["sound"] is True, out
    assert out["body_count"] == 1, out
    b = out["bodies"][0]
    assert b["free_edges"]["count"] == 0 and b["free_edges"]["sids"] == [], b
    assert b["non_manifold_edges"]["count"] == 0, b
    assert b["shells"] == 1 and b["solids"] == 1, b
    assert (b["faces"], b["edges"], b["vertices"]) == (6, 12, 8), b
    # 80 * 60 * 10 = 48000 mm^3, and POSITIVE (a negative volume is the inverted-solid tell).
    assert b["volume_positive"] is True, b
    assert abs(b["volume_mm3"] - 48000.0) < 1e-6, b
    # 2*(80*60) + 2*(80*10) + 2*(60*10) = 12400 mm^2.
    assert abs(b["area_mm2"] - 12400.0) < 1e-6, b
    assert b["orientation"] == {"checked": True, "consistent": True, "problems": []}, b
    assert b["brep_valid"] is True, b
    assert b["findings"] == [], b
    assert "CLOSED MANIFOLD SOLID" in b["verdict"], b


def test_a_curved_body_is_watertight_despite_its_seam_and_pole_edges():
    """A naive "<2 incident faces == free edge" census calls every round body an open shell.

    Two exceptions make the census correct, and they are NOT equally load-bearing here —
    measured by disabling each in core/doc/session.cpp and rebuilding:
      * DEGENERATE edges (a sphere's two poles): removing the exception makes a plain sphere
        report free_edges=2 and watertight=False. Load-bearing; this test fails without it.
      * SEAM edges (the 0/2pi closure): in this OCCT build MapShapesAndAncestors lists a
        seam's parent face TWICE, so the >=2-incidence rule already covers it and removing
        the seam exception changed nothing. It is kept because it mirrors the invariant
        doc/document.cpp::dress_is_closed_solid relies on; the assertion below therefore
        pins that the seam is COUNTED as a seam, not that the exception is what saves us.
    """
    cyl = _new_doc("health_cyl", [{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Rod", "params": {"radius": 5, "height": 20}}}])
    c = _call("check_geometry_health", doc_id=cyl)["bodies"][0]
    assert c["seam_edges"] >= 1, c            # the seam really is there
    assert c["free_edges"]["count"] == 0, c   # and it is NOT counted as a hole
    assert c["watertight"] is True, c
    assert abs(c["volume_mm3"] - 3.141592653589793 * 25 * 20) < 1e-3, c

    sph = _new_doc("health_sph", [{"op": "add_feature", "feature": {
        "type": "sphere", "name": "Ball", "params": {"radius": 10}}}])
    s = _call("check_geometry_health", doc_id=sph)["bodies"][0]
    assert s["degenerate_edges"] == 2, s      # the two poles
    assert s["free_edges"]["count"] == 0, s   # neither pole is a hole
    assert s["watertight"] is True and s["sound"] is True, s
    assert abs(s["volume_mm3"] - 4.0 / 3.0 * 3.141592653589793 * 1000) < 1e-2, s


# --------------------------------------------------------------------------
# a genuinely UNSOUND body — built through the real tools, not faked
# --------------------------------------------------------------------------

def test_an_open_surface_wall_flips_the_verdict_and_the_counts_explain_why(open_wall):
    """surf_extrude makes side walls with no end caps: an open shell, not a solid."""
    out = _call("check_geometry_health", doc_id=open_wall)
    assert out["ok"] is True, out
    assert out["watertight"] is False and out["is_watertight"] is False, out
    assert out["sound"] is False, out
    b = out["bodies"][0]
    assert b["solids"] == 0, b
    # 4 side faces; the 8 rim edges (4 top + 4 bottom) bound ONE face each -> free.
    assert b["faces"] == 4, b
    assert b["free_edges"]["count"] == 8, b
    assert len(b["free_edges"]["sids"]) == 8, b
    assert all(s.startswith("E") for s in b["free_edges"]["sids"]), b
    assert b["non_manifold_edges"]["count"] == 0, b
    assert b["volume_mm3"] is None and b["volume_positive"] is False, b
    # Every finding names something the agent can act on.
    joined = " ".join(b["findings"])
    assert "FREE (boundary) edge" in joined, b["findings"]
    assert b["free_edges"]["sids"][0] in joined, b["findings"]
    assert "no SOLID was produced" in joined, b["findings"]
    assert "volume was NOT computed" in joined, b["findings"]
    assert "NOT SOUND" in b["verdict"], b


def test_a_bare_surface_has_no_volume_reported_rather_than_a_bogus_number():
    """BRepGProp will integrate a "volume" over an open skin — a confident, meaningless
    number. The census must report volume as NOT computed instead of quoting it."""
    doc = _new_doc("health_planar", PLANAR_FACE)
    out = _call("check_geometry_health", doc_id=doc)
    b = out["bodies"][0]
    assert b["solids"] == 0 and b["volume_mm3"] is None, b
    assert "no SOLID" in b["volume_reason"], b
    assert any(x.startswith("volume ") for x in out["not_checked"]), out


def test_the_good_and_the_bad_body_disagree_on_every_load_bearing_field(box, open_wall):
    """Guards against a verdict that is constant regardless of the geometry."""
    good = _call("check_geometry_health", doc_id=box)["bodies"][0]
    bad = _call("check_geometry_health", doc_id=open_wall)["bodies"][0]
    assert (good["watertight"], good["solids"], good["free_edges"]["count"]) == (True, 1, 0)
    assert (bad["watertight"], bad["solids"], bad["free_edges"]["count"]) == (False, 0, 8)
    assert good["volume_mm3"] == 48000.0 and bad["volume_mm3"] is None


# --------------------------------------------------------------------------
# contract rule 1: never report a verdict we did not compute
# --------------------------------------------------------------------------


def test_self_intersection_is_a_verdict_the_engine_actually_computed(box):
    """The check RUNS, and on a clean box it answers `no`.

    This field was a hard-coded {checked:false, "this engine has no such test"} disclaimer.
    It is now a BOPAlgo_CheckerSI run, so a plain box must come back with a real verdict —
    and nothing may be advertised in the top-level not_checked list for it.
    """
    out = _call("check_geometry_health", doc_id=box)
    si = out["bodies"][0]["self_intersection"]
    assert si["checked"] is True, si
    assert si["self_intersecting"] is False, si
    assert si["interference_count"] == 0, si
    assert si["kinds"] == [], si
    assert isinstance(si["elapsed_ms"], (int, float)) and si["elapsed_ms"] >= 0.0, si
    assert not [x for x in out["not_checked"] if x.startswith("self_intersection")], out
    assert out["watertight"] is True and out["sound"] is True, out


def test_two_crossing_panels_knit_into_one_body_are_reported_self_intersecting(
        crossed_panels):
    """A body whose own skin passes through itself — and a census that can see it.

    The counts a watertight verdict is built from cannot detect this: both panels are
    perfectly valid faces and neither one is torn. The kernel check must find the
    face/face interference, the body must not read as sound, and the report must carry an
    actionable finding naming the defect (an interference_count with no sentence beside it
    is not an answer an agent can act on).
    """
    out = _call("check_geometry_health", doc_id=crossed_panels)
    assert out["body_count"] == 1, out
    b = out["bodies"][0]
    assert b["faces"] == 2, b                 # ONE body, two crossing panels
    si = b["self_intersection"]
    assert si["checked"] is True, si
    assert si["self_intersecting"] is True, si
    assert si["interference_count"] >= 1, si
    assert "face_face" in si["kinds"], si
    assert b["sound"] is False and out["sound"] is False, out
    joined = " ".join(b["findings"])
    assert "SELF-INTERSECTS" in joined, b["findings"]
    assert str(si["interference_count"]) in joined, b["findings"]
    assert "NOT SOUND" in b["verdict"], b
    # The check RAN, so it must not ALSO be advertised as not-checked.
    assert not [x for x in out["not_checked"] if x.startswith("self_intersection")], out


def test_orientation_is_not_checked_when_there_is_no_shell_to_orient():
    """A second not-checked case, on a DIFFERENT check.

    surf_planar makes a bare TopoDS_Face: there is no SHELL, and shell orientation is the
    only normal-consistency test the kernel exposes. The report must say the check did not
    run — reporting `consistent: true` for a body nothing was checked on is exactly the
    confidently-wrong output this tool exists to avoid.
    """
    doc = _new_doc("health_planar_orient", PLANAR_FACE)
    out = _call("check_geometry_health", doc_id=doc)
    orient = out["bodies"][0]["orientation"]
    assert out["bodies"][0]["shells"] == 0, out["bodies"][0]
    assert orient["checked"] is False, orient
    assert orient["consistent"] is None, orient
    assert "not checked" in orient["reason"] and "SHELL" in orient["reason"], orient
    assert any(x.startswith("orientation") for x in out["not_checked"]), out


# --------------------------------------------------------------------------
# repair is refused, not stubbed
# --------------------------------------------------------------------------

def test_repair_is_refused_and_names_what_to_do_instead(open_wall):
    out = _call("check_geometry_health", doc_id=open_wall, repair=True)
    assert out["repair"]["performed"] is False, out["repair"]
    assert "NOT done" in out["repair"]["refused"], out["repair"]
    assert "feature" in out["repair"]["refused"].lower(), out["repair"]
    assert "repair REFUSED" in out["summary"], out["summary"]
    # The diagnosis is still delivered — a refusal is not a dead end.
    assert out["bodies"][0]["free_edges"]["count"] == 8, out


@pytest.mark.parametrize("knob", ["fill_holes", "fix_normals", "fix_winding"])
def test_every_trimesh_repair_verb_is_refused_by_name(open_wall, knob):
    out = _call("check_geometry_health", doc_id=open_wall, **{knob: True})
    assert out["repair"]["performed"] is False, out["repair"]
    assert knob in out["repair"]["refused"], out["repair"]


def test_no_repair_argument_means_no_repair_key(box):
    assert "repair" not in _call("check_geometry_health", doc_id=box)


# --------------------------------------------------------------------------
# the alias spellings models actually write
# --------------------------------------------------------------------------

def _same_answer(a: dict) -> dict:
    """The payload minus its one non-deterministic field.

    `self_intersection.elapsed_ms` is a wall-clock measurement of the BOPAlgo_CheckerSI run,
    so two calls on the same body legitimately differ by a fraction of a millisecond. Every
    other field is a verdict and must match exactly.
    """
    out = json.loads(json.dumps(a))
    for b in out.get("bodies") or []:
        (b.get("self_intersection") or {}).pop("elapsed_ms", None)
    return out


@pytest.mark.parametrize("alias", ALIASES)
def test_each_alias_resolves_to_the_canonical_tool(box, alias):
    canonical = _call("check_geometry_health", doc_id=box)
    out = _call(alias, doc_id=box)
    assert _same_answer(out) == _same_answer(canonical), \
        f"{alias} answered differently from check_geometry_health"


@pytest.mark.parametrize("alias", ALIASES + ("check_geometry_health",))
def test_every_spelling_is_registered_on_the_mcp_surface(alias):
    """External MCP clients resolve by NAME, so an alias must be a real registered tool."""
    from services.mcp import server
    assert alias in server.mcp._tool_manager._tools


def test_body_id_is_accepted_as_a_spelling_of_body(box):
    assert _same_answer(_call("check_geometry_health", doc_id=box, body=1)) == \
           _same_answer(_call("check_geometry_health", doc_id=box, body_id=1))


def test_an_out_of_range_body_is_an_error_not_a_verdict(box):
    out = _call("check_geometry_health", doc_id=box, body=7)
    assert out.get("ok") is not True, out
    assert "body_id 7" in json.dumps(out), out


def test_an_unknown_argument_is_refused_loudly(box):
    """No **kwargs here on purpose: an unknown key must be a named error, never a silent drop."""
    _payload, err = _dispatch("check_geometry_health", doc_id=box, tolerance=0.1)
    assert err is not None and "tolerance" in err, err
