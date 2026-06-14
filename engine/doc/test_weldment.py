"""Weldments — `weld_member` feature: structural profile swept along sketch segments.

Run:  & $py -m pytest engine/doc/test_weldment.py   (from repo root)

Covers:
  * an open 3-line frame → 3 square_tube members (separate bodies);
  * total volume ≈ Σ(cross-section area × member length) within the corner-trim tolerance;
  * each member's stored cut-list length == its segment length (±trim);
  * hollow sections are genuinely hollow (member vol < a solid bar of the same envelope);
  * a closed 4-segment rectangle → 4 mitre-trimmed members with no gross interference;
  * angle_L yields a true L cross-section (area = 2·leg·t − t²);
  * error cases: unknown profile, sketch with no usable segments.
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document, CompileContext
from engine.doc.features import get_compiler
from engine.doc.features.datum import reserved_datums
from engine.doc.schema import validate_document
from engine.doc.weld_profiles import PROFILES, build_profile_face


# --------------------------------------------------------------------------- helpers


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


def _open_frame_sketch(fid: str = "f1") -> dict:
    """3 connected lines (an open U-frame): 100 + 80 + 100 mm."""
    return _sketch(fid, [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [100.0, 0.0], [100.0, 80.0]),
        _line("e3", [100.0, 80.0], [0.0, 80.0]),
    ])


def _closed_rect_sketch(fid: str = "f1") -> dict:
    """4 connected lines forming a closed rectangle: 100 × 80 mm."""
    return _sketch(fid, [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [100.0, 0.0], [100.0, 80.0]),
        _line("e3", [100.0, 80.0], [0.0, 80.0]),
        _line("e4", [0.0, 80.0], [0.0, 0.0]),
    ])


def _member_store(sketch: dict, weld: dict) -> dict:
    """Drive the sketch + weld compilers manually and return the stashed cut-list store."""
    datums = reserved_datums()
    ctx = CompileContext(equations_env={}, bodies=[], datums=datums)
    get_compiler("sketch")(sketch, ctx)
    get_compiler("weld_member")(weld, ctx)
    return ctx.datums["_weld_members"].obj[weld["id"]]


# --------------------------------------------------------------------------- profile library


def test_profile_areas_match_faces() -> None:
    """Every library profile's analytic cross-section area matches the built face area exactly
    (the cut-list mass roll-up depends on this)."""
    for name in PROFILES:
        face, _dims, area = build_profile_face(name, {})
        assert abs(face.area - area) < 1e-6, f"{name}: analytic {area} != face {face.area}"


def test_hollow_sections_are_hollow() -> None:
    """A square_tube / round_tube face area is strictly less than its solid envelope area."""
    sq, _d, sq_area = build_profile_face("square_tube", {"size": 20.0, "thickness": 2.0})
    assert sq_area < 20.0 * 20.0, "square_tube must be hollow"
    rt, _d, rt_area = build_profile_face("round_tube", {"od": 20.0, "thickness": 2.0})
    assert rt_area < math.pi * 100.0, "round_tube must be hollow"


# --------------------------------------------------------------------------- members


def test_open_frame_three_members() -> None:
    """An open 3-line frame → 3 square_tube members, each a SEPARATE body."""
    sk = _open_frame_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": True})
    doc = T._doc([sk, wm])
    assert validate_document(doc) == [], "weld_member doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["ok"], f"weld_member failed: {s.get('error')}"
    assert s["resolved"]["_member_count"] == 3.0
    # 3 members = 3 distinct bodies in the rolling body list.
    assert len(res.bodies) == 3, f"expected 3 member bodies, got {len(res.bodies)}"
    for b in res.bodies:
        assert b.shape.volume > 0.0


def test_total_volume_matches_area_times_length() -> None:
    """Σ member volume ≈ section_area × Σ length, within the corner-trim tolerance.

    The mitre trim removes a little corner material, so the trimmed total is slightly UNDER the
    ideal Σ(area·length); the untrimmed build must match exactly."""
    sk = _open_frame_sketch("f1")
    # Untrimmed: each member is a clean prism → exact area·length.
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    area = s["resolved"]["_section_area"]
    total_len = s["resolved"]["_total_length"]
    total_vol = s["resolved"]["_total_volume"]
    assert abs(total_len - 280.0) < 1e-6
    assert abs(total_vol - area * total_len) < 1.0, \
        f"untrimmed Σvol {total_vol} != area·len {area * total_len}"

    # Trimmed: total is within a small corner allowance below the ideal (never above).
    wm_t = _weld("f3", "f1", "square_tube",
                 params={"size": 20.0, "thickness": 2.0, "trim_corners": True})
    res_t = compile_document(T._doc([sk, wm_t]))
    trimmed_vol = T._status(res_t, "f3")["resolved"]["_total_volume"]
    ideal = area * total_len
    # Two mitred corners; each removes < one section envelope · section size of material.
    corner_allow = 2.0 * (20.0 ** 3)
    assert ideal - corner_allow < trimmed_vol <= ideal + 1.0, \
        f"trimmed Σvol {trimmed_vol} outside [{ideal - corner_allow}, {ideal}]"


def test_member_length_matches_segment_length() -> None:
    """Each stored cut-list length equals its sketch segment length (untrimmed members)."""
    sk = _open_frame_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    store = _member_store(sk, wm)
    by_seg = {m["segment_id"]: m for m in store["members"]}
    assert abs(by_seg["e1"]["length"] - 100.0) < 1e-6
    assert abs(by_seg["e2"]["length"] - 80.0) < 1e-6
    assert abs(by_seg["e3"]["length"] - 100.0) < 1e-6
    # Endpoints round-trip the sketch coordinates.
    assert by_seg["e1"]["start"][:2] == [0.0, 0.0]
    assert by_seg["e1"]["end"][:2] == [100.0, 0.0]
    # Each record carries the cut-list payload.
    for m in store["members"]:
        assert set(m) >= {"segment_id", "profile", "length", "start", "end", "area", "volume"}
        assert m["profile"] == "square_tube"


def test_member_hollow_volume_less_than_solid_bar() -> None:
    """A square_tube member's volume is well under a solid bar of the same 20×20 envelope."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    vol = res.bodies[-1].shape.volume
    solid_bar = 20.0 * 20.0 * 100.0
    assert vol < solid_bar, f"hollow member {vol} should be < solid bar {solid_bar}"
    assert abs(vol - 144.0 * 100.0) < 1.0, f"square_tube member vol {vol} != 14400"


def test_round_tube_member_hollow() -> None:
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "round_tube",
               params={"od": 20.0, "thickness": 2.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    vol = res.bodies[-1].shape.volume
    assert vol < math.pi * 100.0 * 100.0, "round_tube member must be hollow"


def test_angle_L_cross_section() -> None:
    """angle_L produces an L cross-section: area = 2·leg·t − t² (not a filled square)."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "angle_L",
               params={"leg": 30.0, "thickness": 3.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    expected_area = 2 * 30.0 * 3.0 - 3.0 * 3.0  # = 171
    assert abs(s["resolved"]["_section_area"] - expected_area) < 1e-6
    vol = res.bodies[-1].shape.volume
    assert abs(vol - expected_area * 100.0) < 1.0, f"angle_L member vol {vol}"
    # An L is NOT the filled leg×leg square.
    assert vol < 30.0 * 30.0 * 100.0


# --------------------------------------------------------------------------- corner trim


def test_closed_loop_four_members_no_interference() -> None:
    """A closed rectangle (4 segments) → 4 mitre-trimmed members with no gross interference."""
    sk = _closed_rect_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": True})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert s["ok"], f"closed-loop weld failed: {s.get('error')}"
    assert s["resolved"]["_member_count"] == 4.0
    solids = [b.shape for b in res.bodies]
    assert len(solids) == 4
    # Pairwise intersection volume must be tiny (a clean mitre touches but does not interpenetrate).
    one_member_vol = min(x.volume for x in solids)
    tol = 0.01 * one_member_vol
    max_overlap = 0.0
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            try:
                iv = getattr(solids[i] & solids[j], "volume", 0.0)
            except Exception:  # noqa: BLE001
                iv = 0.0
            max_overlap = max(max_overlap, iv)
    assert max_overlap < tol, f"members interfere: max overlap {max_overlap} > {tol}"


def test_trim_reduces_volume_vs_untrimmed() -> None:
    """Mitre trimming removes corner material → trimmed Σvol < untrimmed Σvol."""
    sk = _closed_rect_sketch("f1")
    untrimmed = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "trim_corners": False})]))
    trimmed = compile_document(T._doc([sk, _weld(
        "f2", "f1", "square_tube",
        params={"size": 20.0, "thickness": 2.0, "trim_corners": True})]))
    vu = T._status(untrimmed, "f2")["resolved"]["_total_volume"]
    vt = T._status(trimmed, "f2")["resolved"]["_total_volume"]
    assert vt < vu, f"trimmed {vt} should be < untrimmed {vu}"


# --------------------------------------------------------------------------- segment selection


def test_segments_subset_selects_members() -> None:
    """refs.segments limits the build to the named segments."""
    sk = _open_frame_sketch("f1")
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False},
               segments=["e1", "e3"])
    doc = T._doc([sk, wm])
    assert validate_document(doc) == [], "segment-subset doc must validate"
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["resolved"]["_member_count"] == 2.0
    assert len(res.bodies) == 2


# --------------------------------------------------------------------------- error cases


def test_unknown_profile_is_clean_error() -> None:
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "no_such_profile")
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert not s["ok"] and "unknown profile" in (s.get("error") or "")


def test_no_segments_is_clean_error() -> None:
    """A sketch with only a closed circle (no open segment) → contained error, not a crash."""
    sk = _sketch("f1", [{"id": "e1", "type": "circle", "construction": False,
                         "center": [0.0, 0.0], "radius": 10.0}])
    wm = _weld("f2", "f1", "square_tube")
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert not s["ok"] and "no usable segments" in (s.get("error") or "")


def test_missing_sketch_ref_is_clean_error() -> None:
    """A weld_member pointing at a non-compiled sketch id reports a contained error."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    # refs.sketch must be an existing feature id; point at f9 (never built).
    wm = T._feat("f2", "weld_member", {"profile": "square_tube"}, refs={"sketch": "f9"})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert not s["ok"], "weld_member with a dangling sketch ref must fail cleanly"


def test_degenerate_profile_dims_is_clean_error() -> None:
    """A wall thicker than half the tube is a degenerate cross-section → contained error."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "square_tube", params={"size": 20.0, "thickness": 12.0})
    res = compile_document(T._doc([sk, wm]))
    s = T._status(res, "f2")
    assert not s["ok"] and "degenerate profile" in (s.get("error") or "")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[weldment] {name} ok")
    print("[weldment] all tests passed")
