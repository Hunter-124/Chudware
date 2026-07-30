"""Tests for RE-14: fitted-primitive HINTS on reverse_engineer (recon_tools.py).

Two layers of proof (per the RE-14 acceptance):

  1. PURE-PYTHON, engine-free — runs everywhere. Exercises `_fit_hint_specs`, the
     side-effect-free helper that turns an `aicad.reveng_fits/1` doc into authorable
     feature specs. Proves: None/empty hints are a no-op; a convex cylinder fit that the
     deterministic passes could NOT recover becomes a boss spec; a fit already coaxial
     with a recovered feature (hole/turned base) is dropped as a duplicate; the axis gate
     fires. Plus the two `fits_revision: 2` seams: acceptance is graded on FIT QUALITY
     (`residual_mm` / `inliers`), never on `confidence` (which segment_fit computes as a
     mesh-share ratio, ~0.03-0.31 for a genuine feature); and a `role: "hole"` fit is
     authored SUBTRACTIVELY while `unknown` / sphere / cone are reported, never guessed.

  2. ENGINE-BACKED — skips cleanly when libchudware isn't staged (mirrors
     test_recon_tools.py's skipif). A plate with a large convex Z cylinder boss: the boss
     radius > pass-5's fillet bound and it is not a bore, so every frozen pass leaves it
     `unclaimed`. Reverse-engineered WITHOUT hints it stays unrecovered; WITH the fit hint
     the additive boss batch recovers it, so hinted match_pct >= unhinted (by construction:
     hints route through the same eval_compare keep-or-revert gate). A second fixture adds
     a BLIND BORE down that boss and feeds a `fits_revision: 2` doc at a realistic 0.05
     mesh-share confidence: the boss is fused, the `role: "hole"` fit is CUT, and the
     rebuild reaches 100% — the whole scan -> editable, dimensionally-correct CAD claim.

Run:  python3 -m pytest services/mcp/test_recon_hints.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _unshadow_mcp_sdk() -> None:
    # Same repair as test_recon_tools.py: pytest's package import shadows the `mcp` SDK
    # with this directory. Load the real SDK back as `mcp`.
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()

import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402

import pytest  # noqa: E402

from mcp_server import core_bridge as cb  # noqa: E402
from mcp_server.recon_tools import (  # noqa: E402
    _HINT_MIN_CONFIDENCE,
    _HINT_MIN_INLIERS,
    _HINT_THROUGH_OVERSHOOT_FRAC,
    _HINT_THROUGH_OVERSHOOT_MM,
    _fit_hint_specs,
    _plane_hint_block,
)


# ---------------------------------------------------------------- pure-Python (always runs)


def _fits(*fits: dict, revision=None) -> dict:
    doc = {"schema": "aicad.reveng_fits/1", "session_id": "re_test", "fits": list(fits)}
    if revision is not None:
        doc["fits_revision"] = revision
    return doc


def _cyl(radius=8.0, height=12.0, axis=(0, 0, 1), origin=(0, 0, 0), conf=0.9):
    """A LEGACY (`fits_revision` 1) cylinder fit: no role, no residual_mm, no inliers."""
    return {"kind": "cylinder", "confidence": conf,
            "params_mm": {"radius": radius, "height": height,
                          "axis": list(axis), "origin": list(origin)},
            "source": "ransac", "face_ids": ["f1"]}


def _cyl2(role="boss", radius=8.0, height=12.0, axis=(0, 0, 1), origin=(0, 0, 0),
          base=None, conf=0.05, residual=0.04, inliers=900, through=False, depth=None):
    """A `fits_revision: 2` cylinder entry shaped exactly as segment_fit `_emit_cylinder`
    writes it: mesh-share `confidence`, quality via `residual_mm`/`inliers`, semantics via
    `role`/`through`/`depth_mm`, and `params_mm.base` as the axial start."""
    return {"kind": "cylinder", "confidence": conf, "source": "ransac",
            "params_mm": {"radius": radius, "height": height, "axis": list(axis),
                          "origin": list(origin),
                          "base": list(origin if base is None else base)},
            "inliers": inliers, "residual_mm": residual, "face_ids": ["cyl_0"],
            "role": role, "through": through,
            "depth_mm": height if depth is None else depth}


def _specs(*fits, claimed=(), revision=None):
    return _fit_hint_specs(_fits(*fits, revision=revision), list(claimed))[0]


def _skips(*fits, claimed=(), revision=None):
    return _fit_hint_specs(_fits(*fits, revision=revision), list(claimed))[1]


def test_no_hints_is_a_noop():
    assert _fit_hint_specs(None, []) == ([], [])
    assert _fit_hint_specs({}, []) == ([], [])
    assert _fit_hint_specs({"schema": "aicad.reveng_fits/1", "fits": []}, []) == ([], [])


def test_unrecovered_cylinder_becomes_a_boss_spec():
    specs = _specs(_cyl(radius=8.0, height=12.0))
    assert len(specs) == 1
    s = specs[0]
    assert abs(s["radius"] - 8.0) < 1e-9 and abs(s["height"] - 12.0) < 1e-9
    assert list(s["axis"]) == [0.0, 0.0, 1.0]
    assert list(s["origin"]) == [0.0, 0.0, 0.0]
    assert s["mode"] == "boss"


def test_only_cylinder_fits_are_used():
    box = {"kind": "box", "confidence": 0.9,
           "params_mm": {"dx": 40, "dy": 30, "dz": 10, "origin": [0, 0, 0]}}
    plane = {"kind": "plane", "confidence": 0.9,
             "params_mm": {"normal": [0, 0, 1], "origin": [0, 0, 0]}}
    assert _specs(box, plane) == []


def test_low_confidence_legacy_fit_is_dropped():
    # A revision-1 doc ships no residual_mm/inliers, so `confidence` is the ONLY signal and
    # v1 semantics (it was the quality score) still apply.
    assert _specs(_cyl(conf=0.2)) == []


def test_degenerate_fit_is_dropped():
    assert _specs(_cyl(radius=0.0)) == []
    assert _specs(_cyl(height=0.0)) == []


def test_fit_coaxial_with_a_recovered_feature_is_a_duplicate():
    # A hole/turned base the passes already own at axis Z through the origin: a coaxial,
    # on-axis cylinder fit describes the SAME geometry -> dropped so we never double-build.
    claimed = [([0.0, 0.0, 1.0], [0.0, 0.0, 0.0])]
    assert _specs(_cyl(axis=(0, 0, 1), origin=(0, 0, 0)), claimed=claimed) == []
    # ...but a cylinder OFF that axis (a boss elsewhere on the part) is still kept.
    off = _specs(_cyl(axis=(0, 0, 1), origin=(30, 0, 0)), claimed=claimed)
    assert len(off) == 1
    # ...and the duplicate is REPORTED, not silently swallowed.
    dup = _skips(_cyl(axis=(0, 0, 1), origin=(0, 0, 0)), claimed=claimed)
    assert len(dup) == 1 and dup[0]["code"] == "already_recovered"


# ---------------------------------------------------------------- defect 1: quality gate
# segment_fit sets `confidence = inliers / total_points`. On a real multi-feature scan one
# cylinder owns a small slice of the mesh, so a 0.5 confidence gate discarded EVERY genuine
# hint and kept only synthetic ~0.9 fixtures. Acceptance is now graded on fit quality.


def test_realistic_low_mesh_share_fit_reaches_reverse_engineer():
    """A 6 mm boss on a big scanned bracket: mesh share 0.05, residual 0.04 mm, 1200
    supporting points. That is an excellent fit and MUST become a hint."""
    specs = _specs(_cyl2(role="boss", radius=6.0, conf=0.05, residual=0.04, inliers=1200),
                   revision=2)
    assert len(specs) == 1, specs
    assert specs[0]["confidence"] == 0.05  # low mesh share, kept anyway
    assert specs[0]["mode"] == "boss"


def test_a_genuinely_bad_fit_is_still_rejected():
    """Same mesh share and point count, but the inliers are 2.5 mm off the cylinder — that
    surface is not a cylinder, so it must NOT be authored."""
    bad = _cyl2(role="boss", radius=6.0, conf=0.05, residual=2.5, inliers=1200)
    assert _specs(bad, revision=2) == []
    sk = _skips(bad, revision=2)
    assert len(sk) == 1 and sk[0]["code"] == "low_quality"
    assert "residual" in sk[0]["reason"]


def test_residual_is_graded_against_the_feature_size():
    # 0.45 mm is under the absolute cap but is >5% of a 4 mm bore -> reject...
    assert _specs(_cyl2(role="hole", radius=4.0, residual=0.45, inliers=400),
                  revision=2) == []
    # ...while the same 0.45 mm on a 40 mm boss is 1.1% -> keep.
    assert len(_specs(_cyl2(role="boss", radius=40.0, residual=0.45, inliers=400),
                      revision=2)) == 1


def test_too_few_inliers_is_rejected():
    thin = _cyl2(role="boss", residual=0.01, inliers=_HINT_MIN_INLIERS - 1)
    assert _specs(thin, revision=2) == []
    assert _skips(thin, revision=2)[0]["code"] == "low_quality"


def test_confidence_never_gates_a_fit_that_carries_quality_signals():
    """Belt and braces: mesh share 0.0 still passes when the fit itself is good."""
    assert len(_specs(_cyl2(role="boss", conf=0.0, residual=0.02, inliers=500),
                      revision=2)) == 1


# ---------------------------------------------------------------- defect 2: role routing
# Every cylinder used to become an ADDITIVE boss. Once segment_fit started classifying
# bores, that turned a hole in the scanned part into a protrusion — inverted geometry.


def test_hole_role_is_a_cut_and_never_an_additive_boss():
    specs = _specs(_cyl2(role="hole", radius=5.0, height=10.0, origin=(0, 0, 0),
                         base=(0, 0, -5.0), depth=10.0), revision=2)
    assert len(specs) == 1
    s = specs[0]
    assert s["mode"] == "cut", "a scanned bore must be subtractive"
    assert s["role"] == "hole"
    assert not any(x["mode"] == "boss" for x in specs)
    # placement anchors on params_mm.base (the axial START), NOT on the v1 origin.
    assert list(s["start"]) == [0.0, 0.0, -5.0]
    assert list(s["origin"]) == [0.0, 0.0, 0.0]
    # blind bore: exactly depth_mm deep, no overshoot (overshoot = a deeper, wrong hole).
    assert abs(s["height"] - 10.0) < 1e-9 and s["through"] is False


def test_through_bore_overshoots_both_ends_but_a_blind_bore_does_not():
    thru = _specs(_cyl2(role="hole", radius=5.0, height=9.5, base=(0, 0, 0),
                        through=True, depth=10.0), revision=2)[0]
    over = max(_HINT_THROUGH_OVERSHOOT_MM, _HINT_THROUGH_OVERSHOOT_FRAC * 10.0)
    assert abs(thru["height"] - (10.0 + 2.0 * over)) < 1e-9
    assert abs(thru["start"][2] - (-over)) < 1e-9
    assert thru["through"] is True
    blind = _specs(_cyl2(role="hole", radius=5.0, height=9.5, base=(0, 0, 0),
                         through=False, depth=9.5), revision=2)[0]
    assert abs(blind["height"] - 9.5) < 1e-9 and abs(blind["start"][2]) < 1e-9


def test_boss_and_shaft_roles_stay_additive():
    for role in ("boss", "shaft"):
        s = _specs(_cyl2(role=role, radius=10.0, height=50.0), revision=2)[0]
        assert s["mode"] == "boss" and s["role"] == role


def test_unknown_role_is_never_guessed_into_either_bucket():
    unk = _cyl2(role="unknown")
    assert _specs(unk, revision=2) == []
    sk = _skips(unk, revision=2)
    assert len(sk) == 1 and sk[0]["code"] == "role_unknown"
    assert sk[0]["role"] == "unknown"


def test_revision_2_entry_with_no_role_at_all_is_not_guessed_either():
    fit = _cyl2(role="boss")
    fit.pop("role")
    assert _specs(fit, revision=2) == []
    assert _skips(fit, revision=2)[0]["code"] == "role_unknown"


def test_sphere_and_cone_are_reported_not_dropped_and_never_cylinders():
    sphere = {"kind": "sphere", "confidence": 0.1, "inliers": 500, "residual_mm": 0.02,
              "params_mm": {"center": [1, 2, 3], "radius": 5.0, "origin": [1, 2, 3]},
              "source": "ransac", "face_ids": ["sphere_0"]}
    cone = {"kind": "cone", "confidence": 0.1, "inliers": 500, "residual_mm": 0.02,
            "params_mm": {"apex": [0, 0, 9], "axis": [0, 0, 1], "half_angle_rad": 0.5,
                          "radius_at_base": 4.0, "height": 8.0, "origin": [0, 0, 9]},
            "source": "lsq", "face_ids": ["cone_0"]}
    specs, skipped = _fit_hint_specs(_fits(sphere, cone, revision=2), [])
    assert specs == []
    assert [s["fit_kind"] for s in skipped] == ["sphere", "cone"]
    assert all(s["code"] == "kind_unsupported" for s in skipped)


# ---------------------------------------------------------------- plane hints -> a base
# On MESH input the deterministic prismatic pass is structurally dead: a sewn STL is
# per-TRIANGLE faces, so its "largest planar face + antiparallel partner + laterals >= 80%
# of the rest" test measures triangles. A 40x30x20 block with one 8 mm bore scored 73% and
# rebuilt ZERO features even though segment_fit had already recovered all six bounding
# planes and the bore. `_plane_hint_block` is the fallback base for exactly that case.


def _plane(normal, origin) -> dict:
    """A plane entry shaped as segment_fit `_emit_plane` writes it. Its normal is
    UNORIENTED — RANSAC fits the same patch as +n or -n — which is why the helper may not
    read a side out of the sign."""
    return {"kind": "plane", "confidence": 0.2, "source": "ransac",
            "params_mm": {"normal": list(normal), "origin": list(origin),
                          "offset": 0.0},
            "inliers": 2000, "residual_mm": 0.03, "face_ids": ["plane_0"],
            "role": "unknown"}


def _box_planes(lx=40.0, ly=30.0, lz=20.0) -> list:
    """The six bounding planes of an lx*ly*lz block at the origin, with the +Z normal on
    BOTH z faces — the unoriented-normal shape segment_fit actually emitted for the probe
    block."""
    return [_plane((0, 0, 1), (20.2, 14.7, lz)), _plane((0, 0, 1), (19.6, 15.0, 0.0)),
            _plane((0, -1, 0), (19.6, ly, 10.1)), _plane((0, 1, 0), (19.8, 0.0, 9.9)),
            _plane((-1, 0, 0), (0.0, 15.2, 10.2)), _plane((1, 0, 0), (lx, 15.2, 9.9))]


def test_no_plane_fits_is_a_noop():
    assert _plane_hint_block(None) == (None, None)
    assert _plane_hint_block({}) == (None, None)
    assert _plane_hint_block(_fits()) == (None, None)
    assert _plane_hint_block(_fits(_cyl2())) == (None, None)


def test_six_bounding_planes_become_a_prismatic_extrude_base():
    base, why = _plane_hint_block(_fits(*_box_planes(40.0, 30.0, 20.0)))
    assert why is None, why
    assert base["type"] == "extrude" and base["axis"] == "z", base
    assert base["plane"] == "plane.top", base
    assert base["source"] == "plane_fit_hint", base
    assert base["extents_mm"] == {"x": 40.0, "y": 30.0, "z": 20.0}, base
    assert abs(base["depth"] - 20.0) < 1e-6, base
    assert abs(base["offset"] - 0.0) < 1e-6, base  # sketch sits at the LOW z station
    # A closed 4-line rectangle spanning the x/y extents, in plane.top (u=X, v=Y).
    assert [e["type"] for e in base["entities"]] == ["line"] * 4, base["entities"]
    xs = {round(c[0], 6) for e in base["entities"] for c in (e["p1"], e["p2"])}
    ys = {round(c[1], 6) for e in base["entities"] for c in (e["p1"], e["p2"])}
    assert xs == {0.0, 40.0} and ys == {0.0, 30.0}, base["entities"]
    assert base["region_centroid"] == [20.0, 15.0], base


def test_offset_follows_the_low_station_not_the_origin():
    """The block need not sit at z=0 — the sketch must land on its LOW face or the extrude
    builds 15 mm away from the scan it claims to rebuild."""
    planes = [_plane((0, 0, 1), (0, 0, 35.0)), _plane((0, 0, 1), (0, 0, 15.0)),
              _plane((0, 1, 0), (0, 0.0, 0)), _plane((0, 1, 0), (0, 30.0, 0)),
              _plane((1, 0, 0), (0.0, 0, 0)), _plane((1, 0, 0), (40.0, 0, 0))]
    base, why = _plane_hint_block(_fits(*planes))
    assert why is None, why
    assert abs(base["offset"] - 15.0) < 1e-6, base
    assert abs(base["depth"] - 20.0) < 1e-6, base


def test_a_pocket_floor_does_not_shrink_the_block():
    """An interior plane (a pocket floor at z=12, same +Z normal as the top face) must not
    be mistaken for a bounding face — the block is bounded by the EXTREME stations."""
    planes = _box_planes() + [_plane((0, 0, 1), (20.0, 15.0, 12.0))]
    base, why = _plane_hint_block(_fits(*planes))
    assert why is None, why
    assert abs(base["depth"] - 20.0) < 1e-6, base


def test_a_turned_signature_is_refused_not_bent_into_a_block():
    """Two end-cap planes of a shaft bound ONE axis. Authoring a bounding block there
    would hand the user a cuboid for their cylinder, so it is refused with the reason."""
    planes = [_plane((0, 0, 1), (0, 0, 0.0)), _plane((0, 0, 1), (0, 0, 60.0))]
    base, why = _plane_hint_block(_fits(*planes, _cyl2(role="boss")))
    assert base is None
    assert why and "bound only axis z" in why, why


def test_a_rotated_scan_is_refused_and_says_to_align_it():
    """reverse_engineer authors in the source's own coordinates, so a scan sitting at an
    angle cannot be rebuilt from axis-aligned spans. Name that, do not approximate it."""
    planes = [_plane((0.7071, 0.7071, 0), (0, 0, 0)),
              _plane((0.7071, -0.7071, 0), (0, 0, 0)),
              _plane((0, 0, 1), (0, 0, 0)), _plane((0, 0, 1), (0, 0, 20.0))]
    base, why = _plane_hint_block(_fits(*planes))
    assert base is None
    assert why and "not world-axis-aligned" in why and "align the scan" in why, why


def test_the_axis_alignment_gate_brackets_the_stated_tolerance():
    """The extents are read as dot(plane_origin, axis), so a tilted face reports a station
    it does not have. 1 deg off is still that face; 5 deg is not, and must be refused
    rather than snapped — proving the gate is a real angle and not a rubber stamp."""
    def block_with_tilted_top(deg: float) -> tuple:
        s, c = math.sin(math.radians(deg)), math.cos(math.radians(deg))
        return _plane_hint_block(_fits(
            _plane((s, 0.0, c), (0, 0, 20.0)), _plane((0, 0, 1), (0, 0, 0.0)),
            _plane((0, 1, 0), (0, 0.0, 0)), _plane((0, 1, 0), (0, 30.0, 0)),
            _plane((1, 0, 0), (0.0, 0, 0)), _plane((1, 0, 0), (40.0, 0, 0))))

    base, why = block_with_tilted_top(1.0)
    assert why is None and base is not None, why
    assert abs(base["depth"] - 20.0) < 1e-6, base

    base, why = block_with_tilted_top(5.0)
    assert base is None, base  # z is then bounded by ONE plane — not a slab
    assert why and "not world-axis-aligned" in why, why


# ---------------------------------------------------------------- engine-backed (skips clean)
# NOTE: skipif is applied to the engine-backed test ONLY (via a decorator) — the pure-Python
# tests above must always run, so there is deliberately no module-level pytestmark.

_needs_engine = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged (build the C++ core); engine-backed hint test unavailable",
)


def _call(name: str, args: dict):
    from mcp_server import server
    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = json.loads(text) if isinstance(text, str) else None
    return parsed, content


def _boss_plate_step() -> str:
    """A plate + a big convex Z cylinder boss that falls into `unclaimed` (r above the
    fillet bound, not a bore). Returns an exported STEP path (caller unlinks)."""
    r, _ = _call("new_part", {"name": "re_boss_plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate",
                {"length": cb.dim(80), "width": cb.dim(60), "height": cb.dim(10)}),
        # Boss centered on the plate top: base at z=+5 (plate half-height), extends up 15.
        # r13 > pass-5's fillet bound (0.2*median(80,60,10)=12) and not a bore, so every
        # frozen pass leaves this boss in `unclaimed` — the gap the hint fills.
        cb.feat("cylinder", "Boss",
                {"radius": cb.dim(13), "height": cb.dim(15), "mode": "boss",
                 "at": [0.0, 0.0, 12.5]}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"], rb
    ex, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert ex["ok"], ex
    return ex["path"]


@_needs_engine
def test_hinted_match_pct_ge_unhinted_on_a_cylinder_boss():
    path = _boss_plate_step()
    try:
        unhinted, _ = _call("reverse_engineer", {"source": path})
        # The boss sits at z=+5..+20; fit origin = its base center, axis +Z.
        fits = {"schema": "aicad.reveng_fits/1", "session_id": "re_test",
                "fits": [{"kind": "cylinder", "confidence": 0.95,
                          "params_mm": {"radius": 13.0, "height": 15.0,
                                        "axis": [0, 0, 1], "origin": [0, 0, 0.0]},
                          "source": "ransac", "face_ids": []}]}
        hinted, _ = _call("reverse_engineer", {"source": path, "hints": fits})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    assert unhinted["ok"] and hinted["ok"], (unhinted, hinted)
    mu, mh = unhinted["match_pct"], hinted["match_pct"]
    assert mu is not None and mh is not None, (mu, mh)
    # By construction (hints route through eval_compare keep-or-revert): hinted never worse.
    assert mh >= mu - 1e-6, (mh, mu)
    # And on this fixture the boss is real unrecovered volume, so the hint should WIN.
    # Measured on the staged libchudware: unhinted 85.77 -> hinted 95.26 with segment_fit's
    # REAL origin=[0,0,0] convention (boss base defaults to the origin plane, so along-axis
    # placement is approximate — it still recovers most of the protrusion volume). A base-
    # aligned origin would reach 100.0; the approximation is a known, reported limitation.
    hint_feats = [f for f in hinted["features"] if f.get("role") == "hint"]
    assert hint_feats, hinted["features"]
    assert mh > mu, (mh, mu, "hint boss did not improve the match")


def _boss_bore_plate_step() -> str:
    """A plate + a big convex Z boss + a BLIND BORE down that boss — what a scanned part
    actually looks like. The deterministic hole pass RECOGNISES the bore but cannot drill
    it (its entry face lives on the boss, which no frozen pass rebuilds), so it lands in
    `candidates` and its axis stays unclaimed: exactly the gap a role-routed fit hint
    fills. Returns an exported STEP path (caller unlinks)."""
    r, _ = _call("new_part", {"name": "re_boss_bore_plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate",
                {"length": cb.dim(80), "width": cb.dim(60), "height": cb.dim(10)}),
        cb.feat("cylinder", "Boss",
                {"radius": cb.dim(13), "height": cb.dim(15), "mode": "boss",
                 "at": [0.0, 0.0, 12.5]}),
        # Blind bore z=+12..+20 — 8 mm down from the boss top face.
        cb.feat("cylinder", "Bore",
                {"radius": cb.dim(5), "height": cb.dim(8), "mode": "cut",
                 "at": [0.0, 0.0, 16.0]}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"], rb
    ex, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert ex["ok"], ex
    return ex["path"]


def _rev2_cyl(role, radius, height, base, depth, through=False, conf=0.05, fid="cyl_0"):
    """A `fits_revision: 2` entry shaped as segment_fit emits it: MESH-SHARE confidence,
    quality via residual_mm/inliers, semantics via role/through/depth_mm, axial start via
    `params_mm.base` (`origin` keeps its v1 meaning and is a different point)."""
    return {"kind": "cylinder", "confidence": conf, "source": "ransac",
            "params_mm": {"radius": radius, "height": height, "axis": [0, 0, 1],
                          "origin": [0, 0, 0], "base": list(base)},
            "inliers": 1400, "residual_mm": 0.05, "face_ids": [fid],
            "role": role, "through": through, "depth_mm": depth}


@_needs_engine
def test_hinted_role_hole_is_bored_never_bossed():
    """The scan->CAD point, end to end. Every cylinder carries a realistic mesh-share
    `confidence` of 0.05 — under the old 0.5 gate all of them were discarded and the
    feature did nothing on real data. The bore fit is `role: "hole"`; before role routing
    it would have been authored as an additive boss, turning a recess into a protrusion
    (an unusable printed part)."""
    path = _boss_bore_plate_step()
    try:
        unhinted, _ = _call("reverse_engineer", {"source": path})
        fits = {"schema": "aicad.reveng_fits/1", "fits_revision": 2,
                "session_id": "re_test", "sections": [], "notes": [],
                # Bore first on purpose: ordering boss-before-bore is the caller's job.
                "fits": [_rev2_cyl("hole", 5.0, 8.0, (0, 0, 12.0), 8.0, fid="cyl_1"),
                         _rev2_cyl("boss", 13.0, 15.0, (0, 0, 5.0), 15.0, fid="cyl_0")]}
        assert all(f["confidence"] < _HINT_MIN_CONFIDENCE for f in fits["fits"])
        hinted, _ = _call("reverse_engineer", {"source": path, "hints": fits})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    assert unhinted["ok"] and hinted["ok"], (unhinted, hinted)
    hint_feats = [f for f in hinted["features"] if f.get("role") == "hint"]
    # 1. the bore reached reverse_engineer despite its 0.05 mesh share, and was SUBTRACTIVE
    bores = [f for f in hint_feats if f.get("hint_role") == "hole"]
    assert bores and all(f["mode"] == "cut" for f in bores), hinted["features"]
    # 2. ...and never additive — the inverted-geometry regression.
    assert not any(f.get("hint_role") == "hole" and f.get("mode") == "boss"
                   for f in hint_feats), hinted["features"]
    # 3. the boss stayed additive.
    assert [f["mode"] for f in hint_feats if f.get("hint_role") == "boss"] == ["boss"]
    # 4. bosses are authored before bores, so the cut has a body to bore into.
    assert [f["name"] for f in hint_feats] == ["HintBoss1", "HintBore2"]
    # 5. the rebuild IS the source now. Measured on the staged libchudware: 86.743 -> 100.0
    mu, mh = unhinted["match_pct"], hinted["match_pct"]
    assert mh > mu, (mh, mu, "role-routed hints did not improve the match")
    assert mh > 99.5, (mh, "boss + bore should reconstruct the source exactly")

# ------------------------------------------------- STL scan -> parametric, vs GROUND TRUTH
# The whole product claim, measured. The mesh is GENERATED here (export a known solid to
# STL) rather than committed, so the numbers below are checked against dimensions this test
# itself authored: a 40 x 30 x 20 mm block with one d8 through bore.

_GT = {"x": 40.0, "y": 30.0, "z": 20.0, "bore_d": 8.0, "bore_x": 20.0, "bore_y": 15.0}
# Tessellation is the whole error budget here: a faceted bore is inscribed in the true
# circle and the outer faces land on vertex positions, so every recovered number is a few
# hundredths light. 0.1 mm is loose enough for any mesh density the exporter picks and
# tight enough that a wrong axis, a dropped hole or a half-pitch offset cannot hide.
_GT_TOL_MM = 0.1


def _fitter_available() -> bool:
    try:
        from services.vision.segment_fit import _backend_available
    except Exception:  # noqa: BLE001
        return False
    return bool(_backend_available())


_needs_fitter = pytest.mark.skipif(
    not _fitter_available(),
    reason="no mesh backend provisioned (pip install trimesh); segment_fit unavailable",
)


def _gt_block_stl(tmp_path) -> str:
    """Export the ground-truth block (40x30x20, d8 through bore at 20,15) as a mesh."""
    r, _ = _call("new_part", {"name": "re_gt_block"})
    doc = r["doc_id"]
    ops = [
        cb.feat("sketch", "S1", {"entities": [
            {"id": "e0", "type": "line", "p1": [0, 0], "p2": [40, 0]},
            {"id": "e1", "type": "line", "p1": [40, 0], "p2": [40, 30]},
            {"id": "e2", "type": "line", "p1": [40, 30], "p2": [0, 30]},
            {"id": "e3", "type": "line", "p1": [0, 30], "p2": [0, 0]}],
            "constraints": []}, {"plane": "plane.top"}),
        cb.feat("extrude", "Base", {"mode": "boss", "end": "blind", "depth": cb.dim(20)},
                {"profile": [{"kind": "region", "sketch": "$0", "centroid": [20, 15],
                              "hint_index": 0}]}),
        cb.feat("cylinder", "Bore", {"radius": cb.dim(4), "height": cb.dim(30),
                                     "mode": "cut", "at": [20.0, 15.0, 10.0]}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"] and rb["body_count"] == 1, rb
    out = str(tmp_path / "gt_block.stl")
    ex, _ = _call("export_part", {"fmt": "stl", "path": out, "doc_id": doc})
    assert ex["ok"] and os.path.isfile(ex["path"]), ex
    return ex["path"]


@_needs_engine
@_needs_fitter
def test_stl_scan_reaches_a_parametric_tree_within_tolerance_of_ground_truth(tmp_path):
    """`reverse engineer this STL into a parametric part`, driven for real.

    Before the plane-hint base this returned ok:true, match_pct 0.0 and features [] — the
    fitter had recovered all six planes and the bore, but the only hint the pipeline
    consumed was the bore, and a cut needs a body to cut into. Now the same fits rebuild an
    editable extrude + bore whose dimensions are checked against the block this test built.
    """
    from services.vision.segment_fit import segment_fit

    path = _gt_block_stl(tmp_path)
    fits = segment_fit(path, session_id="re_gt")
    assert fits.get("ok"), fits

    res, _ = _call("reverse_engineer", {"source": path, "hints": fits})
    assert res["ok"], res

    # 1. a real feature tree, not a mesh body: a parametric base plus the bore.
    kinds = [(f["type"], f.get("role")) for f in res["features"]]
    assert ("extrude", "base") in kinds, kinds
    bores = [f for f in res["features"] if f.get("hint_role") == "hole"]
    assert len(bores) == 1 and bores[0]["mode"] == "cut", res["features"]

    # 2. it is the source, boolean-verified.
    assert res["match_pct"] is not None and res["match_pct"] > 99.0, res["match_pct"]
    assert res["rejected"] == [], res["rejected"]

    # 3. the recovered DIMENSIONS are the ground truth, not merely a plausible solid.
    base = next(f for f in res["features"] if f.get("role") == "base")
    assert abs(base["depth_mm"] - _GT["z"]) < _GT_TOL_MM, base
    assert abs(bores[0]["radius_mm"] * 2.0 - _GT["bore_d"]) < _GT_TOL_MM, bores
    assert bores[0]["through"] is True, bores
    dp, _ = _call("describe_part", {"doc_id": res["doc_id"]})
    bbox = dp["digest"]["bbox_mm"]
    for got, want, axis in zip(bbox, (_GT["x"], _GT["y"], _GT["z"]), "xyz"):
        assert abs(got - want) < _GT_TOL_MM, (axis, got, want, bbox)

    # 4. and it is EDITABLE — the point of "parametric". Doubling the base depth must
    #    recompile and grow the part; a dumb mesh body could not.
    ed, _ = _call("add_features", {"doc_id": res["doc_id"], "ops": [
        {"op": "edit_feature", "id": base["id"], "params": {"depth": cb.dim(40)}}]})
    assert ed["ok"], ed
    dp2, _ = _call("describe_part", {"doc_id": res["doc_id"]})
    assert abs(dp2["digest"]["bbox_mm"][2] - 40.0) < _GT_TOL_MM, dp2["digest"]["bbox_mm"]


@_needs_engine
def test_an_empty_mesh_refuses_with_a_teaching_message(tmp_path):
    """A 0-triangle STL imports to a bodyless document, where doc_query RAISES rather than
    returning no matches — so the intended refusal was dead code and the caller got the raw
    `document has no bodies to query` / invalid_op envelope, naming neither the file nor a
    next step. It must never be ok, never crash, and must say what to do."""
    stl = tmp_path / "empty.stl"
    stl.write_bytes(b"empty".ljust(80, b"\0") + (0).to_bytes(4, "little"))
    res, _ = _call("reverse_engineer", {"source": str(stl)})
    assert res["ok"] is False, res
    err = res["error"]
    assert "empty.stl" in err, err
    assert "no triangles" in err and "point cloud" in err, err
    assert "reveng_fit_primitives" in err, err  # the still-usable next step
    assert "has no bodies to query" not in err, err  # the raw engine envelope


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        if not cb.available() and getattr(fn, "pytestmark", None):
            print(f"[SKIP] {fn.__name__} (libchudware not staged)")
            continue
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{'FAILED' if failed else 'OK'} ({failed} failed)")
    sys.exit(1 if failed else 0)
