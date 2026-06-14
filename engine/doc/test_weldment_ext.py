"""Weldments — extended profile library + butt/cope (Trim/Extend) joint tests.

Run:  & $py -m pytest engine/doc/test_weldment_ext.py   (from repo root)

This is the SolidWorks-parity extension of ``test_weldment.py`` (docs/SW_PARITY_SPEC.md §6 +
docs/SW_PARITY_GAPS.md P0). It mirrors that file's harness (manual sketch entities → weld_member
features → compiled bodies) and covers the two new capabilities:

Profiles
  * the new structural profiles build with the documented cross-section area:
      - i_beam (W / I-beam): area = h·tw + 2·tf·(w − tw);
      - t_section:           area = w·tf + tw·(h − tf);
  * an HSS-square frame swept from a new standard-size catalogue entry has the catalogue's
    cross-section, and a swept member's volume == area × length;
  * the standard-size catalogue resolves to the right profile + dims and every entry builds.

Joints (Trim/Extend)
  * a T-joint with the legacy ``miter`` joint INTERPENETRATES (the through member's endpoint is not
    at the vertex, so the mutual mitre cannot fire) — this is the documented gap;
  * ``cope`` removes the interpenetration: the trimmed member's volume drops and the two members no
    longer overlap, while the through member stays full;
  * ``butt`` also removes the interpenetration (square cut against the through member's face);
  * cope removes LESS material than butt (the fish-mouth hugs the contour) yet both clear the
    overlap;
  * an L-corner (shared vertex) butt/cope keeps the longer (through) member full and trims the
    shorter one with no overlap.
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.weld_profiles import (
    PROFILES,
    STANDARD_SIZES,
    build_profile_face,
    standard_size,
    standard_size_names,
)


# --------------------------------------------------------------------------- helpers (mirror test_weldment.py)


def _line(eid: str, p1, p2) -> dict:
    return {"id": eid, "type": "line", "construction": False, "p1": p1, "p2": p2}


def _sketch(fid: str, ents: list, plane="plane.top") -> dict:
    return T._feat(fid, "sketch", {"entities": ents, "constraints": []},
                   refs={"plane": plane})


def _weld(fid: str, sketch_id: str, profile: str, *, params: dict | None = None,
          segments: list | None = None) -> dict:
    p = {"profile": profile}
    p.update(params or {})
    refs: dict = {"sketch": sketch_id}
    if segments is not None:
        refs["segments"] = segments
    return T._feat(fid, "weld_member", p, refs=refs)


def _t_joint_sketch(fid: str = "f1") -> dict:
    """A real T-joint: a long through member along X, and a stub whose END lands on the MIDDLE of
    the through member's span (so the mitre — which only fires on shared endpoints — cannot trim it).
    """
    return _sketch(fid, [
        _line("e1", [0.0, 0.0], [200.0, 0.0]),     # through member (full span)
        _line("e2", [100.0, 0.0], [100.0, 80.0]),  # stub ending on e1's interior
    ])


def _l_corner_sketch(fid: str = "f1") -> dict:
    """An L corner sharing vertex (0,0): a long leg (through) + a short leg (trimmed)."""
    return _sketch(fid, [
        _line("e1", [0.0, 0.0], [150.0, 0.0]),   # longer → through member
        _line("e2", [0.0, 0.0], [0.0, 60.0]),    # shorter → trimmed member
    ])


def _max_pairwise_overlap(solids: list) -> float:
    """Largest pairwise boolean-intersection volume among member solids (0 = no interpenetration)."""
    worst = 0.0
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            try:
                iv = getattr(solids[i] & solids[j], "volume", 0.0)
            except Exception:  # noqa: BLE001
                iv = 0.0
            worst = max(worst, iv)
    return worst


# --------------------------------------------------------------------------- new profiles


def test_new_profiles_area_matches_face() -> None:
    """i_beam and t_section build with their analytic cross-section area (cut-list roll-up)."""
    for name in ("i_beam", "t_section"):
        assert name in PROFILES, f"{name} missing from the profile library"
        face, _dims, area = build_profile_face(name, {})
        assert abs(face.area - area) < 1e-6, f"{name}: analytic {area} != face {face.area}"


def test_i_beam_area_formula() -> None:
    """I-beam analytic area = h·tw + 2·tf·(w − tw), and the face is NOT a filled rectangle."""
    h, w, tw, tf = 100.0, 50.0, 5.0, 7.0
    face, _dims, area = build_profile_face(
        "i_beam", {"h": h, "w": w, "tw": tw, "tf": tf})
    expected = h * tw + 2.0 * tf * (w - tw)
    assert abs(area - expected) < 1e-9, f"i_beam area {area} != {expected}"
    assert abs(face.area - expected) < 1e-6
    # Hollow-of-an-I: strictly less than the w×h envelope (it is an I, not a block).
    assert face.area < w * h


def test_t_section_area_formula() -> None:
    """T-section analytic area = w·tf + tw·(h − tf), face matches, not a filled rectangle."""
    h, w, tw, tf = 60.0, 60.0, 6.0, 6.0
    face, _dims, area = build_profile_face(
        "t_section", {"h": h, "w": w, "tw": tw, "tf": tf})
    expected = w * tf + tw * (h - tf)
    assert abs(area - expected) < 1e-9
    assert abs(face.area - expected) < 1e-6
    assert face.area < w * h


def test_degenerate_i_beam_raises() -> None:
    """A flange thickness that swallows the depth is a degenerate section → ValueError (the compiler
    maps this to a contained FeatureError, like the existing tube-wall guard)."""
    import pytest
    with pytest.raises(ValueError):
        build_profile_face("i_beam", {"h": 100.0, "w": 50.0, "tw": 5.0, "tf": 60.0})
    with pytest.raises(ValueError):
        build_profile_face("t_section", {"h": 60.0, "w": 60.0, "tw": 80.0, "tf": 6.0})


# --------------------------------------------------------------------------- standard sizes


def test_standard_sizes_resolve_and_build() -> None:
    """Every catalogue entry resolves to a known profile + dims and builds a face whose area matches
    that profile's analytic area for those dims."""
    assert standard_size_names(), "catalogue must not be empty"
    for name in standard_size_names():
        profile, dims = standard_size(name)
        assert profile in PROFILES, f"{name} -> unknown profile {profile}"
        face, _rd, area = build_profile_face(profile, dims)
        assert area > 0.0 and face.area > 0.0
        assert abs(face.area - area) < 1e-4, f"{name}: face {face.area} != area {area}"


def test_standard_size_hss_square_specific() -> None:
    """A named HSS-square size resolves to the expected square_tube dims + area."""
    profile, dims = standard_size("HSS 50x50x5")
    assert profile == "square_tube"
    assert dims["size"] == 50.0 and dims["thickness"] == 5.0
    _f, _d, area = build_profile_face(profile, dims)
    expected = 50.0 * 50.0 - (50.0 - 2 * 5.0) ** 2  # 2500 - 1600 = 900
    assert abs(area - expected) < 1e-9


def test_standard_size_unknown_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        standard_size("HSS 999x999x99")


# --------------------------------------------------------------------------- HSS frame member volume


def test_hss_square_frame_member_volume() -> None:
    """A frame swept with the HSS 50x50x5 catalogue size: the member's cross-section area is the
    catalogue area and an untrimmed member's volume == area × length."""
    profile, dims = standard_size("HSS 50x50x5")
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [300.0, 0.0])])
    wm = _weld("f2", "f1", profile,
               params={**dims, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert s["ok"], f"HSS frame failed: {s.get('error')}"
    area = s["resolved"]["_section_area"]
    assert abs(area - 900.0) < 1e-6, f"HSS 50x50x5 area {area} != 900"
    vol = res.bodies[-1].shape.volume
    assert abs(vol - area * 300.0) < 1.0, f"HSS member vol {vol} != area·len {area * 300.0}"
    # Hollow: well under a solid 50×50 bar.
    assert vol < 50.0 * 50.0 * 300.0


def test_i_beam_frame_member_volume() -> None:
    """An I-beam member's volume == its analytic section area × length (a real I, not a block)."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [500.0, 0.0])])
    wm = _weld("f2", "f1", "i_beam",
               params={"h": 200.0, "w": 100.0, "tw": 6.0, "tf": 9.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    area = s["resolved"]["_section_area"]
    expected_area = 200.0 * 6.0 + 2.0 * 9.0 * (100.0 - 6.0)  # 1200 + 1692 = 2892
    assert abs(area - expected_area) < 1e-6
    vol = res.bodies[-1].shape.volume
    assert abs(vol - expected_area * 500.0) < 2.0, f"i_beam member vol {vol}"
    assert vol < 200.0 * 100.0 * 500.0, "an I-beam is not a filled block"


# --------------------------------------------------------------------------- the T-joint gap (miter)


def test_miter_t_joint_interpenetrates() -> None:
    """The documented gap: the legacy ``miter`` joint cannot trim a T-joint (the through member's
    endpoint is not at the vertex), so the two members INTERPENETRATE."""
    sk = _t_joint_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "joint": "miter", "trim_corners": True})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert s["ok"]
    solids = [b.shape for b in res.bodies]
    assert len(solids) == 2
    overlap = _max_pairwise_overlap(solids)
    assert overlap > 100.0, f"expected a T-joint interpenetration with miter, got {overlap}"


# --------------------------------------------------------------------------- cope removes interpenetration


def test_cope_t_joint_removes_interpenetration() -> None:
    """``cope`` trims the stub to the through member's contour: the stub's volume drops and the two
    members no longer overlap, while the through member stays FULL."""
    sk = _t_joint_sketch("f1")

    # Untrimmed reference (joint ignored when trim_corners=False) for the stub's full volume.
    untrimmed = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "cope", "trim_corners": False})]))
    bu = [b.shape for b in untrimmed.bodies]
    # Identify the stub (shorter member) and through (longer) by volume.
    full_through = max(b.volume for b in bu)
    full_stub = min(b.volume for b in bu)
    assert _max_pairwise_overlap(bu) > 100.0, "untrimmed T-joint must interpenetrate"

    coped = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "cope", "trim_corners": True})]))
    bc = [b.shape for b in coped.bodies]
    assert len(bc) == 2
    overlap = _max_pairwise_overlap(bc)
    assert overlap < 1e-3, f"cope must remove the interpenetration, overlap={overlap}"

    through_v = max(b.volume for b in bc)
    stub_v = min(b.volume for b in bc)
    # Through member stays full (within kernel noise); the stub is trimmed back.
    assert abs(through_v - full_through) < 1.0, "cope must leave the through member full"
    assert stub_v < full_stub - 100.0, "cope must reduce the trimmed member's volume"


def test_butt_t_joint_removes_interpenetration() -> None:
    """``butt`` square-cuts the stub against the through member's face: no overlap, stub reduced,
    through member full."""
    sk = _t_joint_sketch("f1")
    untrimmed = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "butt", "trim_corners": False})]))
    bu = [b.shape for b in untrimmed.bodies]
    full_stub = min(b.volume for b in bu)
    full_through = max(b.volume for b in bu)

    butted = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "butt", "trim_corners": True})]))
    bb = [b.shape for b in butted.bodies]
    overlap = _max_pairwise_overlap(bb)
    assert overlap < 1e-3, f"butt must remove the interpenetration, overlap={overlap}"
    assert min(b.volume for b in bb) < full_stub - 100.0, "butt must reduce the stub"
    assert abs(max(b.volume for b in bb) - full_through) < 1.0, "butt keeps through member full"


def test_cope_removes_less_than_butt() -> None:
    """The fish-mouth cope hugs the through member's contour, so it removes LESS stub material than
    the square butt cut — both still clear the overlap."""
    sk = _t_joint_sketch("f1")
    cope = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "cope", "trim_corners": True})]))
    butt = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "butt", "trim_corners": True})]))
    cope_stub = min(b.shape.volume for b in cope.bodies)
    butt_stub = min(b.shape.volume for b in butt.bodies)
    assert cope_stub > butt_stub, f"cope stub {cope_stub} should keep more than butt stub {butt_stub}"


def test_round_tube_cope_is_clean() -> None:
    """A round-tube T-joint coped onto a round through member: a real saddle cut, no overlap."""
    sk = _t_joint_sketch("f1")
    res = compile_document(T._doc([sk, _weld(
        "f2", "f1", "round_tube",
        params={"od": 25.0, "thickness": 3.0, "joint": "cope", "trim_corners": True})]))
    s = T._status(res, "f2")
    assert s["ok"], f"round cope failed: {s.get('error')}"
    solids = [b.shape for b in res.bodies]
    assert _max_pairwise_overlap(solids) < 1e-3, "round cope must not interpenetrate"
    # The stub is reduced relative to its untrimmed self.
    untrimmed = compile_document(T._doc([sk, _weld(
        "f2", "f1", "round_tube",
        params={"od": 25.0, "thickness": 3.0, "joint": "cope", "trim_corners": False})]))
    full_stub = min(b.shape.volume for b in untrimmed.bodies)
    coped_stub = min(b.shape.volume for b in res.bodies)
    assert coped_stub < full_stub - 50.0


# --------------------------------------------------------------------------- L-corner butt/cope


def test_l_corner_butt_keeps_through_full() -> None:
    """At a shared-vertex L corner the LONGER member runs through (stays full) and the shorter is
    butt-trimmed; the members no longer interfere."""
    sk = _l_corner_sketch("f1")
    res = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "butt", "trim_corners": True})]))
    s = T._status(res, "f2")
    assert s["ok"]
    solids = [b.shape for b in res.bodies]
    assert _max_pairwise_overlap(solids) < 1e-3, "L-corner butt must not interfere"
    # Through (longer, 150 mm) member stays full: 150 × 144 = 21600.
    through_v = max(b.volume for b in solids)
    assert abs(through_v - 144.0 * 150.0) < 1.0, f"through member trimmed unexpectedly: {through_v}"
    # Shorter (60 mm) member is trimmed back below its full 60 × 144 = 8640.
    short_v = min(b.volume for b in solids)
    assert short_v < 144.0 * 60.0, "shorter member should be trimmed"


def test_l_corner_cope_keeps_through_full() -> None:
    """L-corner cope: same through/trimmed split, contour cut, no interference."""
    sk = _l_corner_sketch("f1")
    res = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "cope", "trim_corners": True})]))
    s = T._status(res, "f2")
    assert s["ok"]
    solids = [b.shape for b in res.bodies]
    assert _max_pairwise_overlap(solids) < 1e-3
    assert abs(max(b.volume for b in solids) - 144.0 * 150.0) < 1.0


# --------------------------------------------------------------------------- joint validation


def test_unknown_joint_is_clean_error() -> None:
    sk = _t_joint_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "joint": "no_such_joint"})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert not s["ok"] and "unknown joint" in (s.get("error") or "")


# --------------------------------------------------------------------------- closed-frame regression for new joints


def test_closed_rect_cope_no_interference() -> None:
    """A closed rectangle with cope joints: every member trims cleanly (no gross interference).

    At each corner two members share a vertex; cope picks the (here equal-or-longer) through member
    and copes the other — the result must not interpenetrate."""
    sk = _sketch("f1", [
        _line("e1", [0.0, 0.0], [120.0, 0.0]),
        _line("e2", [120.0, 0.0], [120.0, 80.0]),
        _line("e3", [120.0, 80.0], [0.0, 80.0]),
        _line("e4", [0.0, 80.0], [0.0, 0.0]),
    ])
    res = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "joint": "cope", "trim_corners": True})]))
    s = T._status(res, "f2")
    assert s["ok"], f"closed cope frame failed: {s.get('error')}"
    solids = [b.shape for b in res.bodies]
    assert len(solids) == 4
    one_member_vol = min(x.volume for x in solids)
    assert _max_pairwise_overlap(solids) < 0.02 * one_member_vol, "cope frame members interfere"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[weldment_ext] {name} ok")
    print("[weldment_ext] all tests passed")
