r"""SW-parity extensions to Rib + Draft (SW_PARITY_SPEC §2 — Rib, Draft).

Run:  & $py -m pytest engine\doc\test_ribdraft_ext.py -q   (from repo root, -X utf8)

Pytest-style (also importable as plain functions). Harness mirrors test_extrude_ends.py /
test_rib_split.py (the existing rib/draft harnesses, NOT edited here).

RIB extensions (rib.py):
1.  Explicit drop direction (params.drop_dir) lands the wall where the pick points — a rib dropped
    DOWN (-Z) reaches the channel base; the resolved drop echoes the pick. (vs. inferred default.)
2.  drop_flip reverses an explicit drop direction.
3.  thicken='parallel' (legacy) and thicken='normal' produce DIFFERENT geometry — parallel grows the
    rib in-plane (drop ‖ sketch), normal grows it off the sketch plane (drop ‖ sketch-normal). Both
    stay one connected solid; their drop directions are orthogonal and rib volumes differ.
4.  Legacy rib (no new options) is byte-for-byte unchanged (same volume as the bare rib).
5.  Error cases: a bad drop_dir literal; drop_dir given in 'normal' mode — both contained.

DRAFT extensions (draft.py):
6.  Parting-line draft tapers the two sides of the parting line OPPOSITELY — the two drafted
    fragments of the picked wall tilt by +angle and −angle about the parting line (the parting line
    is the maximal silhouette). Asserts the two face tilt signs + magnitudes, the untouched opposite
    wall, and one connected body.
7.  Step draft produces a stepped riser at the parting line — a NEW horizontal step face appears and
    the two regions are offset by the step distance; one connected body.
8.  Legacy neutral-plane draft (no mode) is byte-for-byte unchanged (same ±wedge volume as before).
9.  Error cases: parting_line mode with no params.parting; a parting line that does not split the
    body along pull — both contained (ok:false), never crashing the rebuild.
"""

from __future__ import annotations

import math

from build123d import Solid, Vector

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- harness

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-ribdraft-ext",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _line(eid: str, p1, p2) -> dict:
    return {"id": eid, "type": "line", "construction": False, "p1": p1, "p2": p2}


def _has_material(shape, lo: tuple[float, float, float], hi: tuple[float, float, float]) -> bool:
    """True if the shape has solid material inside the AABB [lo, hi] (probe-box intersection)."""
    box = Solid.make_box(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]).translate(Vector(*lo))
    try:
        return shape.intersect(box).volume > 1e-6
    except Exception:  # noqa: BLE001
        return False


def _x_extent_at(shape, z: float) -> tuple[float, float]:
    """(min X, max X) of the thin z-slab through the shape (None,None if empty)."""
    slab = Solid.make_box(400.0, 400.0, 0.4).translate(Vector(-200.0, -200.0, z - 0.2))
    inter = shape.intersect(slab)
    if inter.volume <= 1e-6:
        return (math.nan, math.nan)
    bb = inter.bounding_box()
    return (bb.min.X, bb.max.X)


# --------------------------------------------------------------------------- U-channel fixture (rib)

# A U cross-section on plane.right (local u=Y, v=Z): base z 0..4 over y -20..20, two upright walls
# (4 wide) at the y-ends rising to z=30. Extruded mid-plane along X to total length 60 (x -30..30).
# (Identical to test_rib_split's fixture so the legacy-rib regression compares against the same part.)
_U_PTS = [[-20, 0], [20, 0], [20, 30], [16, 30], [16, 4], [-16, 4], [-16, 30], [-20, 30], [-20, 0]]
_U_VOL = 22080.0


def _u_channel() -> list[dict]:
    edges = [_line(f"e{i + 1}", _U_PTS[i], _U_PTS[i + 1]) for i in range(len(_U_PTS) - 1)]
    return [
        _feat("f1", "sketch", {"entities": edges, "constraints": []},
              refs={"plane": "plane.right"}),
        _feat("f2", "extrude", {"mode": "boss", "end": "mid_plane", "depth": _dim(60.0)},
              refs={"profile": [{"kind": "region", "sketch": "f1"}]}),
    ]


def _rib_sketch() -> dict:
    """An OPEN line bridging the wall tops (z=24), spanning the gap (y -18..18), on plane.right."""
    return _feat("f3", "sketch",
                 {"entities": [_line("e1", [-18, 24], [18, 24])], "constraints": []},
                 refs={"plane": "plane.right"})


def _compile_rib(rib_params: dict):
    rib = _feat("f4", "rib", rib_params,
                refs={"profile": [{"kind": "region", "sketch": "f3"}]})
    doc = _doc(_u_channel() + [_rib_sketch(), rib])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    return res, _status(res, "f4")


# --------------------------------------------------------------------------- test 1: explicit drop

def test_rib_explicit_drop_direction_lands_wall() -> None:
    """An explicit drop direction (params.drop_dir = [0,0,-1], straight DOWN) drops the rib toward
    the channel base — material appears in the lower band (z 2..6) reaching the base, the resolved
    drop echoes the pick (0,0,-1), and the feature flags it as an explicit pick. One connected body.
    """
    res, s = _compile_rib({"thickness": _dim(3.0), "drop_dir": [0.0, 0.0, -1.0]})
    assert s["ok"], f"explicit-drop rib failed: {s['error']}"
    body = res.bodies[-1].shape
    assert len(body.solids()) == 1, f"rib must be one connected body; got {len(body.solids())}"
    assert body.volume > _U_VOL + 1.0, "rib must add material"

    r = s["resolved"]
    assert abs(r["_drop_explicit"] - 1.0) < 1e-9, "drop must be flagged explicit"
    # Resolved drop direction is the (in-plane-projected, normalized) pick: straight down.
    assert abs(r["_drop_x"]) < 1e-6 and abs(r["_drop_y"]) < 1e-6 and abs(r["_drop_z"] + 1.0) < 1e-6, \
        f"resolved drop should be (0,0,-1); got ({r['_drop_x']},{r['_drop_y']},{r['_drop_z']})"
    # The rib material reaches DOWN into the base band (z 2..6) across the gap centre.
    assert _has_material(body, (-1.0, -2.0, 2.0), (1.0, 2.0, 6.0)), \
        "a downward-dropped rib must reach the channel base band (z~2..6)"
    print(f"[rib-explicit-drop] one body, +{r['_rib_volume']:.1f} mm^3, drop=(0,0,-1) reaches base")


# --------------------------------------------------------------------------- test 2: drop flip

def test_rib_drop_flip_reverses_explicit_drop() -> None:
    """drop_flip negates an explicit drop: drop_dir=[0,0,-1] with drop_flip=True resolves to
    (0,0,+1) (the opposite of the un-flipped rib)."""
    _, s_down = _compile_rib({"thickness": _dim(3.0), "drop_dir": [0.0, 0.0, -1.0]})
    _, s_up = _compile_rib({"thickness": _dim(3.0), "drop_dir": [0.0, 0.0, -1.0],
                            "drop_flip": True})
    assert s_down["ok"] and s_up["ok"], (s_down.get("error"), s_up.get("error"))
    assert abs(s_down["resolved"]["_drop_z"] + 1.0) < 1e-6, "un-flipped drop is -Z"
    assert abs(s_up["resolved"]["_drop_z"] - 1.0) < 1e-6, "flipped drop must be +Z"
    print(f"[rib-drop-flip] -Z -> +Z (resolved _drop_z {s_down['resolved']['_drop_z']:.0f} -> "
          f"{s_up['resolved']['_drop_z']:.0f})")


# --------------------------------------------------------------------------- test 3: thicken modes

def test_rib_normal_vs_parallel_thicken_differ() -> None:
    """thicken='parallel' (drop IN-PLANE, ‖ sketch) vs thicken='normal' (drop along the sketch
    NORMAL, off the sketch plane) produce DIFFERENT geometry: orthogonal drop directions and
    different rib volumes. Both stay one connected solid. The sketch is on plane.right (normal = X),
    so parallel drops in the Y-Z plane (here -Z) and normal drops along ±X."""
    res_p, sp = _compile_rib({"thickness": _dim(3.0), "thicken": "parallel"})
    res_n, sn = _compile_rib({"thickness": _dim(3.0), "thicken": "normal"})
    assert sp["ok"], f"parallel thicken failed: {sp['error']}"
    assert sn["ok"], f"normal thicken failed: {sn['error']}"

    assert len(res_p.bodies[-1].shape.solids()) == 1, "parallel rib must be one body"
    assert len(res_n.bodies[-1].shape.solids()) == 1, "normal rib must be one body"

    rp, rn = sp["resolved"], sn["resolved"]
    assert abs(rp["_thicken_normal"]) < 1e-9, "parallel must flag _thicken_normal=0"
    assert abs(rn["_thicken_normal"] - 1.0) < 1e-9, "normal must flag _thicken_normal=1"

    dp = Vector(rp["_drop_x"], rp["_drop_y"], rp["_drop_z"])
    dn = Vector(rn["_drop_x"], rn["_drop_y"], rn["_drop_z"])
    # parallel drops IN the sketch plane (no X-component); normal drops ALONG the sketch normal (X).
    assert abs(dp.X) < 1e-6, f"parallel drop must be in-plane (X~0); got {dp}"
    assert abs(abs(dn.X) - 1.0) < 1e-6, f"normal drop must be along the sketch normal X; got {dn}"
    # Orthogonal drops + different added volume = genuinely different ribs.
    assert abs(dp.dot(dn)) < 1e-6, f"parallel & normal drop dirs must be orthogonal: {dp}·{dn}"
    assert abs(rp["_rib_volume"] - rn["_rib_volume"]) > 1.0, \
        f"parallel ({rp['_rib_volume']:.1f}) and normal ({rn['_rib_volume']:.1f}) ribs must differ"
    print(f"[rib-thicken] parallel drop={tuple(round(c,2) for c in (dp.X,dp.Y,dp.Z))} "
          f"vol +{rp['_rib_volume']:.1f}  |  normal drop="
          f"{tuple(round(c,2) for c in (dn.X,dn.Y,dn.Z))} vol +{rn['_rib_volume']:.1f}")


# --------------------------------------------------------------------------- test 4: legacy unchanged

def test_rib_legacy_unchanged_without_new_options() -> None:
    """A bare rib (no new options) is byte-for-byte the legacy rib: same added volume as a rib that
    only sets thickness/side — the new params default to the legacy inference + parallel thicken."""
    _, s_bare = _compile_rib({"thickness": _dim(3.0)})
    _, s_explicit_legacy = _compile_rib({"thickness": _dim(3.0), "side": "both",
                                         "thicken": "parallel"})
    assert s_bare["ok"] and s_explicit_legacy["ok"]
    assert abs(s_bare["resolved"]["_rib_volume"]
               - s_explicit_legacy["resolved"]["_rib_volume"]) < 1e-6, \
        "explicitly stating the legacy defaults must not change the rib"
    assert abs(s_bare["resolved"]["_drop_explicit"]) < 1e-9, "bare rib uses inferred (not explicit) drop"
    assert abs(s_bare["resolved"]["_thicken_normal"]) < 1e-9, "bare rib thickens parallel"
    print(f"[rib-legacy] bare rib +{s_bare['resolved']['_rib_volume']:.1f} mm^3 (unchanged)")


# --------------------------------------------------------------------------- test 5: rib errors

def test_rib_option_errors_are_contained() -> None:
    """A non-vector drop_dir, and drop_dir given in 'normal' mode (where the drop is fixed to the
    sketch normal) — both contained errors (ok:false), the U-channel body survives."""
    # (a) bad drop_dir literal
    res_a, s_a = _compile_rib({"thickness": _dim(3.0), "drop_dir": "not-a-vector"})
    assert not s_a["ok"], "a non-vector drop_dir must error"
    assert "drop_dir" in s_a["error"], s_a["error"]
    assert abs(res_a.bodies[-1].shape.volume - _U_VOL) < 1e-3, "body survives the contained error"

    # (b) drop_dir in normal mode
    _, s_b = _compile_rib({"thickness": _dim(3.0), "thicken": "normal",
                           "drop_dir": [0.0, 0.0, -1.0]})
    assert not s_b["ok"], "drop_dir in 'normal' mode must error"
    assert "parallel" in s_b["error"], s_b["error"]
    print(f"[rib-errors] bad drop_dir + normal-mode-drop both contained")


# --------------------------------------------------------------------------- draft fixtures

def _box(fid: str, L: float, W: float, H: float, centered: bool = False) -> dict:
    return _feat(fid, "box", {"length": _dim(L), "width": _dim(W), "height": _dim(H),
                              "centered": centered})


def _x_wall_sel() -> dict:
    """The +X side wall of a 40x40x40 corner box (x[0,40]) — centroid (40,20,20), normal +X."""
    return {"kind": "face", "surf": "plane", "centroid": [40.0, 20.0, 20.0],
            "axis": [1.0, 0.0, 0.0], "area_or_len": 1600.0,
            "owner_feature": "f1", "hint_index": 0}


def _parting_edge_sel() -> dict:
    """A vertical edge of the +X wall whose centroid sits at the parting level z=20 (it defines the
    parting plane's LEVEL — the parting plane is z=20, normal = pull). Selecting a model edge whose
    centroid is at mid-height is the parity for a parting line traced across the wall."""
    return {"kind": "edge", "surf": "line", "centroid": [40.0, 40.0, 20.0],
            "axis": [0.0, 0.0, 1.0], "area_or_len": 40.0,
            "owner_feature": "f1", "hint_index": 0}


# --------------------------------------------------------------------------- test 6: parting-line

def test_parting_line_draft_tapers_both_sides_oppositely() -> None:
    """Parting-line draft on the +X wall of a 40-cube, parting line at z=20: the wall splits into an
    UPPER region (z>20) and a LOWER region (z<20) drafted in OPPOSITE senses about the parting line.

    Acceptance: the two drafted fragments of the +X wall tilt by +angle and −angle (their normals
    pick up opposite pull-axis components of magnitude sin(angle)); the parting line is the maximal
    silhouette (x≈40 at z=20, smaller above and below); the opposite (−X) wall is untouched; the
    body stays one connected solid.
    """
    angle = 8.0
    doc = _doc([
        _box("f1", 40.0, 40.0, 40.0, centered=False),
        _feat("f2", "draft",
              {"angle": _dim(angle), "mode": "parting_line", "parting": [_parting_edge_sel()]},
              refs={"neutral": "plane.top", "pull": "axis.z", "faces": [_x_wall_sel()]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"parting-line draft failed: {s['error']}"
    assert abs(s["resolved"]["_mode_parting"] - 1.0) < 1e-9, "must flag parting mode"

    out = res.bodies[-1].shape
    assert len(out.solids()) == 1, f"parting-line draft must be one body; got {len(out.solids())}"

    pull = Vector(0.0, 0.0, 1.0)
    # The two +X wall fragments (normal mostly +X), high in x.
    frags = [f for f in out.faces()
             if Vector(f.normal_at()).X > 0.5 and f.center().X > 15.0]
    assert len(frags) >= 2, f"the +X wall must be split into >=2 drafted fragments; got {len(frags)}"
    tilts = {}
    for f in frags:
        n = Vector(f.normal_at())
        tilt = math.degrees(math.asin(max(-1.0, min(1.0, n.dot(pull)))))
        side = "upper" if f.center().Z > 20.0 else "lower"
        tilts[side] = tilt
    assert "upper" in tilts and "lower" in tilts, f"need an upper+lower fragment; got {tilts}"
    # Opposite signs, each ~= the draft angle.
    assert tilts["upper"] * tilts["lower"] < 0.0, \
        f"the two regions must tilt OPPOSITELY; got {tilts}"
    assert abs(abs(tilts["upper"]) - angle) < 0.5, f"upper tilt {tilts['upper']:.2f} != ±{angle}"
    assert abs(abs(tilts["lower"]) - angle) < 0.5, f"lower tilt {tilts['lower']:.2f} != ±{angle}"

    # The parting line is the maximal silhouette: x at z=20 > x above/below.
    x_part = _x_extent_at(out, 20.0)[1]
    x_top = _x_extent_at(out, 38.0)[1]
    x_bot = _x_extent_at(out, 2.0)[1]
    assert x_part > x_top + 1.0 and x_part > x_bot + 1.0, \
        f"parting line must be the widest: z20={x_part:.2f} top={x_top:.2f} bot={x_bot:.2f}"

    # The opposite (−X) wall is untouched (still at x=0 top and bottom).
    assert abs(_x_extent_at(out, 2.0)[0]) < 1e-3 and abs(_x_extent_at(out, 38.0)[0]) < 1e-3, \
        "the un-picked -X wall must stay at x=0"
    print(f"[draft-parting] one body; tilts upper={tilts['upper']:+.2f} lower={tilts['lower']:+.2f}; "
          f"parting widest (x {x_bot:.1f}<{x_part:.1f}>{x_top:.1f})")


def test_parting_line_draft_via_refs_slot() -> None:
    """Same parting-line draft as above, but the parting edges live in refs.parting (the
    schema-declared slot the PM form fills) instead of params.parting — refs is canonical,
    params is the back-compat fallback. The drafted result must still flag parting mode and
    stay one body."""
    doc = _doc([
        _box("f1", 40.0, 40.0, 40.0, centered=False),
        _feat("f2", "draft",
              {"angle": _dim(8.0), "mode": "parting_line"},
              refs={"neutral": "plane.top", "pull": "axis.z",
                    "faces": [_x_wall_sel()], "parting": [_parting_edge_sel()]}),
    ])
    _assert_valid(doc)  # refs.parting must validate as a _SEL_LIST
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"parting-line draft via refs.parting failed: {s['error']}"
    assert abs(s["resolved"]["_mode_parting"] - 1.0) < 1e-9, "must flag parting mode"
    assert len(res.bodies[-1].shape.solids()) == 1, "must stay one body"
    print("[draft-parting-refs] refs.parting drives parting mode; one body")


# --------------------------------------------------------------------------- test 7: step draft

def test_step_draft_produces_a_step() -> None:
    """Step draft on the same wall + parting line, with a step of 5mm: the two regions are drafted
    AND offset by the step, leaving a NEW horizontal step (riser) face at the parting line and a
    measurable in-plane offset between the upper and lower regions. One connected body."""
    step = 5.0
    # Baseline (no draft) face count to prove the step face is NEW.
    base_doc = _doc([_box("f1", 40.0, 40.0, 40.0, centered=False)])
    base_res = compile_document(base_doc)
    n_faces_base = len(base_res.bodies[-1].shape.faces())  # a plain box: 6

    doc = _doc([
        _box("f1", 40.0, 40.0, 40.0, centered=False),
        _feat("f2", "draft",
              {"angle": _dim(8.0), "mode": "step", "parting": [_parting_edge_sel()],
               "step": _dim(step)},
              refs={"neutral": "plane.top", "pull": "axis.z", "faces": [_x_wall_sel()]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"step draft failed: {s['error']}"
    assert abs(s["resolved"]["_mode_step"] - 1.0) < 1e-9, "must flag step mode"
    assert abs(s["resolved"]["_step"] - step) < 1e-6, f"resolved step should be {step}"

    out = res.bodies[-1].shape
    assert len(out.solids()) == 1, f"step draft must be one body; got {len(out.solids())}"

    # A NEW horizontal (normal ‖ Z) riser face near the parting level z=20, on the +X side.
    step_faces = [f for f in out.faces()
                  if abs(abs(Vector(f.normal_at()).Z) - 1.0) < 1e-2
                  and abs(f.center().Z - 20.0) < 1.0 and f.center().X > 5.0]
    assert step_faces, "step draft must create a horizontal riser face at the parting line"
    assert len(out.faces()) > n_faces_base, \
        f"step must add faces vs the plain box ({len(out.faces())} > {n_faces_base})"

    # The upper region is offset by the step relative to the lower region: the −X wall jumps by
    # ~step between below and above the parting line (the whole upper region shifted along +X).
    xmin_below = _x_extent_at(out, 5.0)[0]
    xmin_above = _x_extent_at(out, 35.0)[0]
    assert abs((xmin_above - xmin_below) - step) < 0.5, \
        f"upper region must be stepped by ~{step}: below xmin={xmin_below:.2f} " \
        f"above xmin={xmin_above:.2f}"
    print(f"[draft-step] one body; {len(step_faces)} riser face(s); region offset "
          f"{xmin_above - xmin_below:.2f} (step={step})")


# --------------------------------------------------------------------------- test 8: legacy draft

def test_neutral_plane_draft_unchanged() -> None:
    """The legacy neutral-plane draft (no mode) is byte-for-byte unchanged: drafting two opposite
    side faces of a 40x40x20 centred box about its top plane by 5° sweeps the SAME ±wedge volume
    the existing test_pattern_mirror_draft asserts (~1400 mm^3)."""
    side = lambda cx, cy, ax: {"kind": "face", "surf": "plane",
                               "centroid": [cx, cy, 0.0], "axis": ax,
                               "owner_feature": "f1"}
    doc = _doc([
        _feat("f1", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": True}),
        _feat("f2", "plane", {"method": "offset", "offset": _dim(10.0)},
              refs={"base": "plane.top"}),
        _feat("f3", "draft", {"angle": _dim(5.0)},
              refs={"neutral": {"kind": "datum", "feature": "f2"},
                    "faces": [side(20.0, 0.0, [1.0, 0.0, 0.0]),
                              side(-20.0, 0.0, [-1.0, 0.0, 0.0])]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"legacy neutral-plane draft failed: {s['error']}"
    assert abs(s["resolved"].get("_mode_parting", 0.0)) < 1e-9
    assert abs(s["resolved"].get("_mode_step", 0.0)) < 1e-9
    assert abs(s["resolved"]["angle"] - 5.0) < 1e-9
    vol = res.bodies[-1].shape.volume
    full = 40.0 * 40.0 * 20.0
    wedge = 2.0 * 0.5 * math.tan(math.radians(5.0)) * 20.0 * 20.0 * 40.0
    assert abs(abs(vol - full) - wedge) < 250.0, \
        f"legacy draft delta {vol - full:.1f} != ±{wedge:.0f} (changed vs prior behaviour)"
    assert len(res.bodies[-1].shape.solids()) == 1, "legacy draft is one body"
    print(f"[draft-legacy] delta {vol - full:.1f} ~ ±{wedge:.0f} (unchanged)")


# --------------------------------------------------------------------------- test 9: draft errors

def test_parting_draft_errors_are_contained() -> None:
    """parting_line mode with no params.parting -> clean 'required' error; and a parting line that
    does NOT split the body along pull (a plane that misses) -> clean 'does not split' error. Both
    contained (ok:false), the box survives unchanged."""
    # (a) no parting line supplied
    doc_a = _doc([
        _box("f1", 40.0, 40.0, 40.0, centered=False),
        _feat("f2", "draft", {"angle": _dim(8.0), "mode": "parting_line"},
              refs={"neutral": "plane.top", "pull": "axis.z", "faces": [_x_wall_sel()]}),
    ])
    _assert_valid(doc_a)
    res_a = compile_document(doc_a)
    s_a = _status(res_a, "f2")
    assert not s_a["ok"], "parting_line with no parting line must error"
    assert "parting" in s_a["error"], s_a["error"]
    assert abs(res_a.bodies[-1].shape.volume - 64000.0) < 1e-2, "box survives the contained error"

    # (b) a parting line at the TOP edge (z=40) — the plane is tangent to the top, splitting nothing.
    top_edge = {"kind": "edge", "surf": "line", "centroid": [40.0, 20.0, 40.0],
                "axis": [0.0, 1.0, 0.0], "area_or_len": 40.0,
                "owner_feature": "f1", "hint_index": 0}
    doc_b = _doc([
        _box("f1", 40.0, 40.0, 40.0, centered=False),
        _feat("f2", "draft",
              {"angle": _dim(8.0), "mode": "parting_line", "parting": [top_edge]},
              refs={"neutral": "plane.top", "pull": "axis.z", "faces": [_x_wall_sel()]}),
    ])
    _assert_valid(doc_b)
    res_b = compile_document(doc_b)
    s_b = _status(res_b, "f2")
    assert not s_b["ok"], "a parting line that doesn't split the body must error"
    assert "split" in s_b["error"].lower(), s_b["error"]
    assert abs(res_b.bodies[-1].shape.volume - 64000.0) < 1e-2, "box survives the contained error"
    print(f"[draft-errors] no-parting + non-splitting both contained")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_rib_explicit_drop_direction_lands_wall()
    test_rib_drop_flip_reverses_explicit_drop()
    test_rib_normal_vs_parallel_thicken_differ()
    test_rib_legacy_unchanged_without_new_options()
    test_rib_option_errors_are_contained()
    test_parting_line_draft_tapers_both_sides_oppositely()
    test_step_draft_produces_a_step()
    test_neutral_plane_draft_unchanged()
    test_parting_draft_errors_are_contained()
    print("\nRIB/DRAFT EXTENSION TESTS PASSED")


if __name__ == "__main__":
    main()
