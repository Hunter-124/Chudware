"""Tests for services/mcp/sketch3d_tools.py — ENGINE-BACKED sketch3d tools.

The sketch3d_* tools drive the real doc feature `sketch3d` (doc_schema §3B,
contracts/sketch3d_gui_contract.md): create/edit via doc_apply_ops, solve via the
stateless sketch3d_health RPC, sweep consumption via the sweep path/guides refs.

Pure-Python helpers (curve mapping, arc tessellation, path sampling, narration) are
tested without the engine; everything else is skip-guarded on libchudware availability.

Run:
  cd <repo root>
  python3 -m pytest services/mcp/test_sketch3d_tools.py -q
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys

# ── import-shadow repair (same as test_gsl_tools) ────────────────────────────
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _unshadow_mcp_sdk() -> None:
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile:
        return
    if os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init,
                submodule_search_locations=[os.path.dirname(init), _HERE],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return


_unshadow_mcp_sdk()

import pytest  # noqa: E402

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp.sketch3d_tools import (  # noqa: E402
    _arc_polyline,
    _curves_to_entities,
    _free_narration,
    _polyline_length,
    _resolve_pipe_mode,
    _sample_polyline,
)

_engine_available = cb.available()


# ─────────────────────────────────────────────── helpers ──────────────────────

def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, raw_content)."""
    from services.mcp import server
    result = asyncio.run(server.mcp.call_tool(name, args))
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        text = structured.get("result") if isinstance(structured, dict) else None
    else:
        content = result
        text = None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    if parsed is None and isinstance(content, (list, tuple)):
        for item in content:
            raw = getattr(item, "text", None)
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    break
                except (json.JSONDecodeError, Exception):
                    pass
    return parsed, content


# ─────────────────────────────────────── pure-Python unit tests ───────────────


class TestCurveMapping:
    """_curves_to_entities: legacy curve specs AND raw engine entities."""

    def test_line(self):
        ents = _curves_to_entities([{"kind": "line", "pts": [[0, 0, 0], [0, 0, 80]]}])
        assert ents == [{"id": "e1", "type": "line", "construction": False,
                         "p1": [0, 0, 0], "p2": [0, 0, 80]}]

    def test_polyline_expands_to_lines(self):
        ents = _curves_to_entities(
            [{"kind": "polyline", "pts": [[0, 0, 0], [10, 0, 0], [10, 5, 0]]}])
        assert [e["type"] for e in ents] == ["line", "line"]
        assert ents[0]["p2"] == ents[1]["p1"] == [10, 0, 0]

    def test_arc_three_point(self):
        ents = _curves_to_entities(
            [{"kind": "arc", "pts": [[0, 0, 0], [5, 5, 0], [10, 0, 0]]}])
        assert ents[0]["type"] == "arc"
        assert ents[0]["pm"] == [5, 5, 0]

    def test_spline_and_point(self):
        ents = _curves_to_entities([
            {"kind": "spline", "pts": [[0, 0, 0], [1, 2, 3], [2, 0, 6]]},
            {"kind": "point", "pts": [[9, 9, 9]]},
        ])
        assert ents[0]["type"] == "spline" and len(ents[0]["points"]) == 3
        assert ents[1]["type"] == "point" and ents[1]["p"] == [9, 9, 9]

    def test_raw_engine_entity_passthrough(self):
        ents = _curves_to_entities(
            [{"type": "line", "p1": [0, 0, 0], "p2": [1, 1, 1]}], start_n=4)
        assert ents[0]["id"] == "e5"
        assert ents[0]["construction"] is False

    def test_id_continuation(self):
        ents = _curves_to_entities(
            [{"kind": "line", "pts": [[0, 0, 0], [1, 0, 0]]}], start_n=7)
        assert ents[0]["id"] == "e8"

    def test_bad_kind_raises(self):
        with pytest.raises(ValueError):
            _curves_to_entities([{"kind": "bezier", "pts": [[0, 0, 0], [1, 0, 0]]}])

    def test_bad_pts_raise(self):
        with pytest.raises(ValueError):
            _curves_to_entities([{"kind": "line", "pts": [[0, 0], [1, 0]]}])
        with pytest.raises(ValueError):
            _curves_to_entities([{"kind": "arc", "pts": [[0, 0, 0], [1, 0, 0]]}])


class TestArcPolyline:
    def test_semicircle_length(self):
        # Half circle radius 5 in XY: (0,0,0) → (5,5,0) → (10,0,0).
        pts = _arc_polyline([0, 0, 0], [5, 5, 0], [10, 0, 0], n=64)
        length = _polyline_length([pts])
        assert abs(length - math.pi * 5.0) < 0.05
        # Passes through the mid point.
        assert any(abs(p[0] - 5) < 0.3 and abs(p[1] - 5) < 0.3 for p in pts)

    def test_out_of_plane_arc(self):
        pts = _arc_polyline([0, 0, 0], [5, 0, 5], [10, 0, 0], n=64)
        assert abs(_polyline_length([pts]) - math.pi * 5.0) < 0.05

    def test_collinear_degenerates_to_polyline(self):
        pts = _arc_polyline([0, 0, 0], [5, 0, 0], [10, 0, 0])
        assert pts == [[0, 0, 0], [5, 0, 0], [10, 0, 0]]


class TestSamplePolyline:
    CHAINS = [[[0, 0, 0], [10, 0, 0]]]

    def test_midpoint(self):
        s = _sample_polyline(self.CHAINS, 0.5)
        assert abs(s["point"][0] - 5.0) < 1e-9
        assert abs(s["tangent"][0] - 1.0) < 1e-9

    def test_ends_and_clamp(self):
        assert _sample_polyline(self.CHAINS, 0.0)["point"][0] == 0.0
        assert abs(_sample_polyline(self.CHAINS, 1.0)["point"][0] - 10.0) < 1e-9
        assert abs(_sample_polyline(self.CHAINS, 2.5)["point"][0] - 10.0) < 1e-9

    def test_elbow_tangent_switch(self):
        chains = [[[0, 0, 0], [0, 0, 10], [10, 0, 10]]]
        s1 = _sample_polyline(chains, 0.25)
        s2 = _sample_polyline(chains, 0.75)
        assert abs(s1["tangent"][2] - 1.0) < 1e-9
        assert abs(s2["tangent"][0] - 1.0) < 1e-9

    def test_empty(self):
        assert _sample_polyline([], 0.5) is None


class TestFreeNarration:
    def test_groups_axes(self):
        free = [{"entity": "e2", "param": "p2 x", "var": 9},
                {"entity": "e2", "param": "p2 y", "var": 10},
                {"entity": "e2", "param": "p2 z", "var": 11}]
        txt = _free_narration(free)
        assert "e2" in txt and "p2 x/y/z" in txt and "loose" in txt

    def test_empty(self):
        assert _free_narration([]) == ""


class TestResolvePipeMode:
    """The sweep `mode` VALUE guard, without the engine.

    Vocabulary source: core/doc/document.cpp:4274-4304 — the only four values the sweep
    compile branch tests for. Everything else used to reach the engine and become a
    disjoint boss, so these are contract assertions, not spelling preferences."""

    def test_canonical_values_pass_through_byte_identical(self):
        for word in ("boss", "add", "cut", "new"):
            assert _resolve_pipe_mode(word, True) == word

    def test_cut_synonyms(self):
        for word in ("cut", "subtract", "remove", "difference", "pocket", "minus",
                     "cutout", "subtractive", "CUT", "  Remove  "):
            assert _resolve_pipe_mode(word, True) == "cut", word

    def test_boss_synonyms(self):
        for word in ("boss", "union", "join", "fuse", "merge", "weld", "pad",
                     "protrusion", "BOSS", " Union "):
            assert _resolve_pipe_mode(word, True) == "boss", word

    def test_new_body_synonyms(self):
        for word in ("new", "new_body", "newbody", "separate", "standalone", "none"):
            assert _resolve_pipe_mode(word, True) == "new", word

    def test_unknown_value_raises_naming_the_vocabulary(self):
        with pytest.raises(ValueError) as ei:
            _resolve_pipe_mode("obliterate", True)
        msg = str(ei.value)
        assert "obliterate" in msg
        for word in ("'boss'", "'cut'", "'new'"):
            assert word in msg, (word, msg)

    def test_empty_and_non_string_are_rejected_not_defaulted(self):
        # "" / None used to reach the engine and fall through to a disjoint body.
        for bad in ("", "   ", None, 0, ["cut"]):
            with pytest.raises(ValueError):
                _resolve_pipe_mode(bad, True)

    def test_cut_with_merge_false_is_a_contradiction(self):
        with pytest.raises(ValueError) as ei:
            _resolve_pipe_mode("cut", False)
        assert "merge" in str(ei.value)
        # boss/new are unaffected — merge=False is their normal "separate body" case.
        assert _resolve_pipe_mode("boss", False) == "boss"
        assert _resolve_pipe_mode("new", False) == "new"

    def test_alias_table_agrees_with_the_primitive_boolean_table(self):
        """cad_doc_tools maps the same words for primitives. Two tables that disagree on
        what "remove" means is the drift this asserts against."""
        from services.mcp.cad_doc_tools import _BOOLEAN_MODES, _BOOLEAN_NEW_BODY
        for word, want in _BOOLEAN_MODES.items():
            assert _resolve_pipe_mode(word, True) in (want, "add"), (word, want)
        for word in _BOOLEAN_NEW_BODY:
            assert _resolve_pipe_mode(word, True) == "new", word


# ─────────────────────────────── engine-backed MCP tool tests ─────────────────


@pytest.mark.skipif(
    not _engine_available,
    reason="libchudware not staged — C++ engine not available",
)
class TestSketch3dDocTools:
    """The re-pointed surface: sketch3d features live in the doc, solved by the engine."""

    def _new_part(self, name: str) -> str:
        r, _ = _call("new_part", {"name": name})
        assert r and r.get("ok"), f"new_part failed: {r}"
        return r["doc_id"]

    # ── CRUD ────────────────────────────────────────────────────────────────

    def test_new_creates_doc_feature(self):
        d = self._new_part("s3d_new")
        r, _ = _call("sketch3d_new", {"name": "route", "doc_id": d})
        assert r and r.get("ok"), r
        fid = r["id"]
        assert fid and fid.startswith("f")
        # It is a real feature in the doc tree.
        tree = cb.call("doc_tree", {"doc_id": d})
        feats = {f["id"]: f for f in tree["tree"]["features"]}
        assert feats[fid]["type"] == "sketch3d"

    def test_add_curves_and_health(self):
        d = self._new_part("s3d_curves")
        r0, _ = _call("sketch3d_new", {"name": "p", "doc_id": d})
        fid = r0["id"]
        r, _ = _call("sketch3d_add_curves", {"sketch3d_id": fid, "doc_id": d, "curves": [
            {"kind": "line", "pts": [[0, 0, 0], [0, 0, 40]]},
            {"kind": "arc", "pts": [[0, 0, 40], [2.93, 0, 47.07], [10, 0, 50]]},
        ]})
        assert r and r.get("ok"), r
        assert r["added"] == 2 and r["entity_count"] == 2
        assert r["health"]["status"] in ("under", "full")
        assert r["health"]["wires"], "touching line+arc should chain into a wire"
        assert r["health"]["wires"][0]["n_edges"] == 2

    def test_add_curves_unknown_feature_errors(self):
        d = self._new_part("s3d_missing")
        r, _ = _call("sketch3d_add_curves", {
            "sketch3d_id": "f999", "doc_id": d,
            "curves": [{"kind": "line", "pts": [[0, 0, 0], [1, 0, 0]]}]})
        assert r and r.get("ok") is False
        assert "f999" in r.get("error", "")

    def test_get_clear_list(self):
        d = self._new_part("s3d_get")
        r0, _ = _call("sketch3d_new", {"name": "g", "doc_id": d})
        fid = r0["id"]
        _call("sketch3d_add_curves", {"sketch3d_id": fid, "doc_id": d,
                                      "curves": [{"kind": "line",
                                                  "pts": [[0, 0, 0], [10, 0, 0]]}]})
        r, _ = _call("sketch3d_get", {"sketch3d_id": fid, "doc_id": d})
        assert r["ok"] and r["entities"][0]["type"] == "line"
        assert abs(r["total_length_mm"] - 10.0) < 1e-6
        assert r["wires"]

        rl, _ = _call("sketch3d_list", {"doc_id": d})
        assert rl["ok"] and any(s["id"] == fid for s in rl["sketches"])

        rc, _ = _call("sketch3d_clear", {"sketch3d_id": fid, "doc_id": d})
        assert rc["ok"]
        r2, _ = _call("sketch3d_get", {"sketch3d_id": fid, "doc_id": d})
        assert r2["ok"] and r2["entities"] == []

    # ── constraints + health narration ──────────────────────────────────────

    def test_constrain_to_fully_defined(self):
        d = self._new_part("s3d_constrain")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "L", "doc_id": d, "fit": "polyline",
            "points": [[0, 0, 0], [0, 0, 40], [30, 0, 40]]})
        fid = r0["id"]
        assert r0["health"]["status"] == "under"
        r, _ = _call("sketch3d_add_constraints", {"sketch3d_id": fid, "doc_id": d,
                                                  "constraints": [
            {"type": "fix", "refs": ["e1.p1"]},
            {"type": "along_axis", "refs": ["e1"], "axis": "z"},
            {"type": "length", "refs": ["e1"], "value": 40},
            {"type": "coincident", "refs": ["e1.p2", "e2.p1"]},
            {"type": "along_axis", "refs": ["e2"], "axis": "x"},
            {"type": "length", "refs": ["e2"], "value": 30},
        ]})
        assert r and r.get("ok"), r
        h = r["health"]
        # fix(3) + along_axis(2+2) + length(1+1) + coincident(3) = 12 dof of 12.
        assert h["dof"] == 0 and h["status"] == "full", h
        assert h["fully_defined"] is True
        assert "Fully defined" in h["summary"]

        rc, _ = _call("check_sketch3d", {"sketch3d_id": fid, "doc_id": d})
        assert rc["ok"] and rc["status"] == "full" and rc["dof"] == 0
        assert rc["wires"] and rc["wires"][0]["n_edges"] == 2

    def test_check_sketch3d_under_narrates_free_vars(self):
        d = self._new_part("s3d_under")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "seg", "doc_id": d, "points": [[0, 0, 0], [0, 0, 20]]})
        fid = r0["id"]
        _call("sketch3d_add_constraints", {"sketch3d_id": fid, "doc_id": d,
                                           "constraints": [{"type": "fix",
                                                            "refs": ["e1.p1"]}]})
        r, _ = _call("check_sketch3d", {"sketch3d_id": fid, "doc_id": d})
        assert r["ok"] and r["status"] == "under" and r["dof"] == 3
        assert "e1" in r["free_narration"] and "loose" in r["free_narration"]
        assert "Under-defined" in r["summary"]

    def test_bad_constraint_rejected_and_rolled_back(self):
        d = self._new_part("s3d_badcon")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "seg", "doc_id": d, "points": [[0, 0, 0], [0, 0, 20]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_add_constraints", {
            "sketch3d_id": fid, "doc_id": d,
            "constraints": [{"type": "tangent", "refs": ["e1"]}]})  # tangent needs 2 refs
        assert r and r.get("ok") is False
        # The doc keeps solving cleanly afterwards (rollback worked).
        rc, _ = _call("check_sketch3d", {"sketch3d_id": fid, "doc_id": d})
        assert rc["ok"]

    # ── one-call helpers ─────────────────────────────────────────────────────

    def test_path_from_points_polyline_and_closed(self):
        d = self._new_part("s3d_path")
        r, _ = _call("sketch3d_path_from_points", {
            "name": "tri", "doc_id": d, "close": True,
            "points": [[0, 0, 0], [20, 0, 0], [0, 20, 0]]})
        assert r["ok"], r
        assert r["entity_count"] == 3
        assert r["wires"] and r["wires"][0]["closed"] is True
        expected = 40 + math.hypot(20, 20)
        assert abs(r["total_length_mm"] - expected) < 1e-3

    def test_path_from_points_spline(self):
        d = self._new_part("s3d_spline")
        r, _ = _call("sketch3d_path_from_points", {
            "name": "smooth", "doc_id": d, "fit": "spline",
            "points": [[0, 0, 0], [10, 5, 10], [20, 0, 20]]})
        assert r["ok"] and r["entity_count"] == 1
        assert r["wires"] and r["wires"][0]["n_edges"] == 1

    def test_pipe_along_path_makes_solid(self):
        """The headline flow: 3D path → constraints → health → 12 mm tube along it."""
        d = self._new_part("s3d_pipe")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "route", "doc_id": d, "fit": "polyline",
            "points": [[0, 0, 0], [0, 0, 40], [30, 0, 40]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": fid, "diameter_mm": 12,
                                       "doc_id": d})
        assert r and r.get("ok"), r
        assert r["feature_ids"]["sweep"]
        assert r["feature_ids"]["plane"] and r["feature_ids"]["profile"]
        assert r.get("body_count") == 1
        # Volume ≈ π r² L (elbow eats a bit): sanity-band it.
        vol = r["volume_mm3"]
        nominal = math.pi * 6 * 6 * 70
        assert 0.8 * nominal < vol < 1.1 * nominal, (vol, nominal)
        assert r["start_point"] == [0, 0, 0]

    def test_pipe_with_existing_profile_sketch(self):
        d = self._new_part("s3d_pipe_prof")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "up", "doc_id": d, "points": [[5, 10, 0], [5, 10, 50]]})
        fid = r0["id"]
        # Author the profile manually on plane.top at the path start.
        ra, _ = _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {"type": "sketch", "name": "prof",
             "params": {"entities": [{"id": "e1", "type": "circle",
                                      "construction": False,
                                      "center": [5, 10], "radius": 4}],
                        "constraints": []},
             "refs": {"plane": "plane.top"}}}]})
        assert ra and ra.get("ok"), ra
        prof = ra["features"][-1]["id"]  # feature_status covers ALL features; last = the new sketch
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": fid, "profile_sketch": prof,
                                       "doc_id": d})
        assert r and r.get("ok"), r
        vol = r["volume_mm3"]
        nominal = math.pi * 16 * 50
        assert abs(vol - nominal) / nominal < 0.05, (vol, nominal)

    def test_pipe_requires_exactly_one_profile_spec(self):
        d = self._new_part("s3d_pipe_arg")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "p", "doc_id": d, "points": [[0, 0, 0], [0, 0, 10]]})
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": r0["id"], "doc_id": d})
        assert r and r.get("ok") is False and "exactly one" in r["error"]

    # ── sweep `mode` VALUE guard (silent-fall-through regression) ────────────
    #
    # The engine never validates a sweep's mode: core/doc/document.cpp:4274-4304 only
    # TESTS it ("boss"|"add"|"cut"), so an unrecognised value failed every test, nothing
    # merged, and the tube was committed as a second disjoint body — ok:true while ADDING
    # material a 'cut' asked it to remove. Measured before the fix on this fixture:
    #   mode="cut"    -5654.867 mm³   mode="remove"  +9047.787 mm³ (both ok:true)

    def _block_and_spine(self, name: str) -> tuple:
        """A 50 mm cube plus a Ø12-capable spine driven straight through it.

        Returns (doc_id, sketch3d_id, volume_before)."""
        d = self._new_part(name)
        rb, _ = _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "Block",
                "params": {"length": 50, "width": 50, "height": 50, "centered": True}}}]})
        assert rb and rb.get("ok"), rb
        m = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
        v0 = round(m["mass_g"] * 1000.0, 3)
        assert abs(v0 - 125000.0) < 1.0, v0
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "spine", "doc_id": d, "points": [[0, 0, -40], [0, 0, 40]]})
        assert r0 and r0.get("ok"), r0
        return d, r0["id"], v0

    def _pipe_delta(self, name: str, **kw) -> tuple:
        """(response, volume_delta_mm3, body_count_after) for one pipe call."""
        d, fid, v0 = self._block_and_spine(name)
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": fid, "diameter_mm": 12,
                                       "doc_id": d, **kw})
        m = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
        return r, round(m["mass_g"] * 1000.0 - v0, 3), m.get("body_count"), d

    def test_pipe_cut_removes_and_boss_adds_material(self):
        """The two canonical modes must move the volume in OPPOSITE directions."""
        rc, dcut, nc, _ = self._pipe_delta("s3d_pipe_mode_cut", mode="cut")
        assert rc and rc.get("ok"), rc
        rb, dboss, nb, _ = self._pipe_delta("s3d_pipe_mode_boss", mode="boss")
        assert rb and rb.get("ok"), rb
        # cut REMOVES (the Ø12 bore through a 50 cube), boss ADDS (the tube's exterior).
        assert dcut < -5000.0, dcut
        assert dboss > 3000.0, dboss
        # Both fuse into ONE body — the "no merge happened" signature is 2.
        assert nc == 1 and nb == 1, (nc, nb)

    def test_pipe_cut_synonyms_still_remove_material(self):
        """'remove'/'subtract'/'pocket' and a shouted/padded 'cut' all mean cut.

        Every one of these used to ADD ~+9048 mm³ as a disjoint body."""
        for i, word in enumerate(["remove", "subtract", "pocket", "CUT", "  cut  "]):
            r, delta, bodies, _ = self._pipe_delta(f"s3d_pipe_syn{i}", mode=word)
            assert r and r.get("ok"), (word, r)
            assert delta < -5000.0, (word, delta)
            assert bodies == 1, (word, bodies)

    def test_pipe_boss_synonyms_and_new_body_mode(self):
        r, delta, bodies, _ = self._pipe_delta("s3d_pipe_union", mode="union")
        assert r and r.get("ok") and delta > 3000.0 and bodies == 1, (r, delta, bodies)
        # "add" is the engine's own second spelling of boss — forwarded untouched.
        r, delta, bodies, _ = self._pipe_delta("s3d_pipe_add", mode="add")
        assert r and r.get("ok") and delta > 3000.0 and bodies == 1, (r, delta, bodies)
        # "new" deliberately leaves a SEPARATE body (the whole tube's volume).
        r, delta, bodies, _ = self._pipe_delta("s3d_pipe_newbody", mode="new")
        assert r and r.get("ok") and delta > 8000.0 and bodies == 2, (r, delta, bodies)

    def test_pipe_unknown_mode_errors_and_leaves_doc_untouched(self):
        """The negative case: reject, name the vocabulary, and build NOTHING."""
        d, fid, v0 = self._block_and_spine("s3d_pipe_badmode")
        feats0 = len(cb.call("doc_tree", {"doc_id": d})["tree"]["features"])
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": fid, "diameter_mm": 12,
                                       "mode": "obliterate", "doc_id": d})
        assert r and r.get("ok") is False, r
        err = r["error"]
        assert "obliterate" in err, err
        for word in ("boss", "cut", "new"):
            assert f"'{word}'" in err, (word, err)
        # No plane, no profile sketch, no sweep — and the volume is bit-for-bit the same.
        m = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
        assert round(m["mass_g"] * 1000.0, 3) == v0, (m, v0)
        assert len(cb.call("doc_tree", {"doc_id": d})["tree"]["features"]) == feats0

    def test_pipe_cut_with_merge_false_is_rejected_not_silently_added(self):
        """merge=False switches the engine's boolean off, so a 'cut' would land as a
        +9048 mm³ separate body. The contradiction must error instead."""
        d, fid, v0 = self._block_and_spine("s3d_pipe_cut_nomerge")
        r, _ = _call("sketch3d_pipe", {"sketch3d_id": fid, "diameter_mm": 12,
                                       "mode": "cut", "merge": False, "doc_id": d})
        assert r and r.get("ok") is False, r
        assert "merge" in r["error"] and "cut" in r["error"], r["error"]
        m = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": 1.0})
        assert round(m["mass_g"] * 1000.0, 3) == v0, (m, v0)

    # ── eq_curve3d ──────────────────────────────────────────────────────────

    def test_add_eq_curve3d_helix(self):
        d = self._new_part("s3d_eqcurve")
        r0, _ = _call("sketch3d_new", {"name": "helix", "doc_id": d})
        fid = r0["id"]
        r, _ = _call("sketch3d_add_curves", {"sketch3d_id": fid, "doc_id": d, "curves": [
            {"kind": "eq_curve3d", "xt": "10*cos(t)", "yt": "10*sin(t)", "zt": "2*t/360",
             "t1": 0, "t2": 720}]})
        assert r and r.get("ok"), r
        assert r["added"] == 1
        # 0 DOF — equation-fixed (mirrors the 2D eq_curve).
        assert r["health"]["dof"] == 0
        rg, _ = _call("sketch3d_get", {"sketch3d_id": fid, "doc_id": d})
        assert rg["ok"]
        ent = rg["entities"][0]
        assert ent["type"] == "eq_curve3d"
        assert abs(ent["p1"][0] - 10.0) < 1e-6 and abs(ent["p1"][2] - 0.0) < 1e-6
        assert abs(ent["p2"][0] - 10.0) < 1e-6 and abs(ent["p2"][2] - 4.0) < 1e-6

    def test_eq_curve3d_bad_expression_rejected(self):
        d = self._new_part("s3d_eqcurve_bad")
        r0, _ = _call("sketch3d_new", {"name": "bad", "doc_id": d})
        r, _ = _call("sketch3d_add_curves", {"sketch3d_id": r0["id"], "doc_id": d, "curves": [
            {"kind": "eq_curve3d", "xt": 5, "yt": "0", "zt": "0", "t1": 0, "t2": 1}]})
        assert r and r.get("ok") is False

    # ── external vertex references ────────────────────────────────────────

    def test_ref_vertex_grounds_a_coincident_constraint(self):
        d = self._new_part("s3d_xref")
        rb, _ = _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
             "params": {"length": 20, "width": 20, "height": 20}}}]})
        assert rb and rb.get("ok"), rb

        r0, _ = _call("sketch3d_new", {"name": "s", "doc_id": d})
        fid = r0["id"]
        _call("sketch3d_add_curves", {"sketch3d_id": fid, "doc_id": d, "curves": [
            {"kind": "line", "pts": [[0, 0, 0], [1, 1, 1]]}]})

        # Find a vertex pick id via doc_resolve_pick (mirrors the GUI id-pass).
        topo_id = None
        for tid in range(1, 65):
            pick = cb.call("doc_resolve_pick",
                           {"doc_id": d, "body_id": 1, "kind": "vertex", "topo_id": tid})
            sel = pick.get("selector")
            if sel and sel.get("centroid"):
                topo_id = tid
                break
        assert topo_id is not None, "expected at least one vertex on the box"

        r, _ = _call("sketch3d_ref_vertex", {"sketch3d_id": fid, "doc_id": d,
                                             "topo_id": topo_id})
        assert r and r.get("ok"), r
        ref_id = r["ref_id"]
        assert ref_id.startswith("x")
        assert r["point"] is not None and len(r["point"]) == 3
        assert r["stale"] is False

        rc, _ = _call("sketch3d_add_constraints", {"sketch3d_id": fid, "doc_id": d,
                                                    "constraints": [
                                                        {"type": "fix", "refs": ["e1.p1"]},
                                                        {"type": "coincident",
                                                         "refs": ["e1.p2", ref_id]}]})
        assert rc and rc.get("ok"), rc
        rg, _ = _call("sketch3d_get", {"sketch3d_id": fid, "doc_id": d})
        assert rg["ok"]
        e1 = next(e for e in rg["entities"] if e["id"] == "e1")
        p2 = e1["p2"]
        assert all(abs(p2[i] - r["point"][i]) < 1e-4 for i in range(3)), (p2, r["point"])

    def test_ref_vertex_bad_topo_id_errors(self):
        d = self._new_part("s3d_xref_bad")
        rb, _ = _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
             "params": {"length": 5, "width": 5, "height": 5}}}]})
        assert rb and rb.get("ok"), rb
        r0, _ = _call("sketch3d_new", {"name": "s", "doc_id": d})
        r, _ = _call("sketch3d_ref_vertex", {"sketch3d_id": r0["id"], "doc_id": d,
                                             "topo_id": 99999})
        assert r and r.get("ok") is False

    # ── sweep-ref helper / sampling / section ────────────────────────────────

    def test_as_sweep_guide_spine_and_guide(self):
        d = self._new_part("s3d_guide")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "spine", "doc_id": d, "points": [[0, 0, 0], [0, 0, 30]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_as_sweep_guide",
                     {"sketch3d_id": fid, "role": "spine", "doc_id": d})
        assert r["ok"] and r["guide_support"] is True
        assert r["ref"] == fid  # single chain → bare fid
        assert r["wires"] and "workflow_hint" in r and "coord_summary" in r
        rg, _ = _call("sketch3d_as_sweep_guide",
                      {"sketch3d_id": fid, "role": "guide", "doc_id": d})
        assert rg["ok"] and rg["ref"] == {"feature": fid, "wire": 0}

    def test_as_sweep_guide_empty_sketch_errors(self):
        d = self._new_part("s3d_guide_empty")
        r0, _ = _call("sketch3d_new", {"name": "empty", "doc_id": d})
        r, _ = _call("sketch3d_as_sweep_guide",
                     {"sketch3d_id": r0["id"], "doc_id": d})
        assert r and r.get("ok") is False

    def test_sample_at_solved_geometry(self):
        d = self._new_part("s3d_sample")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "seg", "doc_id": d, "points": [[0, 0, 0], [10, 0, 0]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_sample_at", {"sketch3d_id": fid, "t": 0.5, "doc_id": d})
        assert r["ok"] and abs(r["point"][0] - 5.0) < 0.01
        assert abs(r["tangent"][0] - 1.0) < 0.01
        r0b, _ = _call("sketch3d_sample_at", {"sketch3d_id": fid, "t": 0.0, "doc_id": d})
        r1b, _ = _call("sketch3d_sample_at", {"sketch3d_id": fid, "t": 1.0, "doc_id": d})
        assert r0b["point"][0] == 0.0 and abs(r1b["point"][0] - 10.0) < 0.01

    def test_sample_at_empty_sketch_errors(self):
        d = self._new_part("s3d_sample_empty")
        r0, _ = _call("sketch3d_new", {"name": "empty", "doc_id": d})
        r, _ = _call("sketch3d_sample_at", {"sketch3d_id": r0["id"], "doc_id": d})
        assert r and r.get("ok") is False

    def test_section_at_orientation_from_tangent(self):
        d = self._new_part("s3d_section")
        _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "b",
             "params": {"length": {"value": 30, "expr": None},
                        "width": {"value": 30, "expr": None},
                        "height": {"value": 50, "expr": None},
                        "centered": True}}}]})
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "path", "doc_id": d, "points": [[0, 0, 0], [0, 0, 50]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_section_at", {"sketch3d_id": fid, "t": 0.5, "doc_id": d})
        assert r and r.get("ok"), r
        assert r["plane_used"] == "top"
        assert abs(r["point"][2] - 25.0) < 0.1
        assert "area_mm2" in r and "origin" in r["summary"].lower()

    def test_section_at_open_body_refuses_by_name(self):
        # An open surf_extrude wall (no end caps) crosses the 'right' plane (x = 0). The
        # section has no area, so the tool must name the engine refusal, never report 0.
        d = self._new_part("s3d_section_open")
        built, _ = _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {
                "type": "sketch", "name": "Rect", "params": {"entities": [], "constraints": []},
                "refs": {"plane": "plane.top"}}},
            *[{"op": "sketch_add_entity", "sketch": "$0",
               "entity": {"type": "line", "p1": p1, "p2": p2}}
              for p1, p2 in (([-20, -15], [20, -15]), ([20, -15], [20, 15]),
                             ([20, 15], [-20, 15]), ([-20, 15], [-20, -15]))],
            {"op": "add_feature", "feature": {
                "type": "surf_extrude", "name": "Wall",
                "params": {"end": "blind", "depth": {"value": 20, "expr": None}},
                "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                      "centroid": [0, 0], "hint_index": 0}]}}},
        ]})
        assert built and built.get("ok") is True, built
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "path", "doc_id": d, "points": [[0, 0, 5], [10, 0, 5]]})
        r, _ = _call("sketch3d_section_at", {"sketch3d_id": r0["id"], "t": 0.5, "doc_id": d})
        assert r and r.get("ok") is False, r
        assert r.get("code") == "section_failed", r
        assert r["detail"]["reason"] == "body_not_closed_solid", r
        assert "area_mm2" not in r

    # ── preview ──────────────────────────────────────────────────────────────

    def test_preview_text_read(self):
        d = self._new_part("s3d_preview")
        r0, _ = _call("sketch3d_path_from_points", {
            "name": "route", "doc_id": d,
            "points": [[0, 0, 0], [0, 0, 40], [30, 0, 40]]})
        fid = r0["id"]
        r, _ = _call("sketch3d_preview", {"sketch3d_id": fid, "doc_id": d})
        assert r["ok"], r
        txt = r["summary_text"]
        assert fid in txt and "wire 0" in txt and "Health:" in txt
        assert "e1" in txt and "e2" in txt

    # ── 2D preview tools still work ──────────────────────────────────────────

    def test_doc_sketch_contact_sheet_lists_sketch3d(self):
        d = self._new_part("s3d_sheet")
        _call("add_features", {"doc_id": d, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "b",
             "params": {"length": {"value": 20, "expr": None},
                        "width": {"value": 20, "expr": None},
                        "height": {"value": 20, "expr": None}}}}]})
        _call("sketch3d_path_from_points", {
            "name": "p", "doc_id": d, "points": [[0, 0, 0], [0, 0, 30]]})
        r, content = _call("doc_sketch_contact_sheet", {"doc_id": d})
        # [Image, caption] return — caption text should mention the 3D sketch row.
        texts = [getattr(c, "text", "") for c in (content or [])]
        assert any("(3D)" in (t or "") for t in texts), texts


# ─────────────────────────────────────────────── __main__ runner ─────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
