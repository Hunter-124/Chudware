"""Weldment cut list — aggregate weld_member structural members into a grouped cut-list table.

Run:  & $py -m pytest engine/doc/test_weld_cutlist.py   (from repo root)

Covers (cutlist.build_cut_list over a compiled doc's CompileResult.weld_members):
  * identical members (same profile + dims + length) group into ONE row with the right quantity;
  * distinct lengths get SEPARATE rows;
  * per-row total_length_mm == quantity * length, and the grand total == Σ per-member lengths;
  * rows are sorted by profile asc then length desc; item numbers are 1-based post-sort;
  * the description is a human profile label;
  * a doc with NO weld_member features -> empty rows, zero totals (not an error);
  * the side-channel survives onto CompileResult.weld_members after compile;
  * the session entry point (cut_list_for_session) builds the same table through a fresh compile.
"""

from __future__ import annotations

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.cutlist import build_cut_list, cut_list_for_session, _describe
from engine.doc.schema import validate_document


# --------------------------------------------------------------------------- helpers


def _line(eid: str, p1, p2) -> dict:
    return {"id": eid, "type": "line", "construction": False, "p1": p1, "p2": p2}


def _sketch(fid: str, ents: list, plane="plane.top") -> dict:
    return T._feat(fid, "sketch", {"entities": ents, "constraints": []}, refs={"plane": plane})


def _weld(fid: str, sketch_id: str, profile: str, *, params: dict | None = None,
          segments: list | None = None) -> dict:
    p = {"profile": profile}
    p.update(params or {})
    refs: dict = {"sketch": sketch_id}
    if segments is not None:
        refs["segments"] = segments
    return T._feat(fid, "weld_member", p, refs=refs)


def _cutlist_for(features: list[dict]) -> dict:
    """Compile a part doc and build the cut list straight off CompileResult.weld_members."""
    doc = T._doc(features)
    assert validate_document(doc) == [], "cut-list doc must validate"
    res = compile_document(doc)
    return build_cut_list(res.weld_members)


# --------------------------------------------------------------------------- grouping


def test_identical_members_group_into_one_row() -> None:
    """Two equal-length 100mm legs + one 80mm leg of one square_tube feature -> 2 rows.

    The 100mm pair collapses to a single row (quantity 2); the 80mm leg is its own row."""
    # Untrimmed so each member's stored length equals its exact segment length.
    sk = _sketch("f1", [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),     # 100 mm
        _line("e2", [100.0, 0.0], [100.0, 80.0]),  #  80 mm
        _line("e3", [100.0, 80.0], [0.0, 80.0]),   # 100 mm
    ])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    cl = _cutlist_for([sk, wm])

    assert cl["total_members"] == 3
    assert len(cl["rows"]) == 2, f"expected 2 grouped rows, got {cl['rows']}"

    by_len = {r["length_mm"]: r for r in cl["rows"]}
    assert by_len[100.0]["quantity"] == 2, "the two 100mm legs must group into one row"
    assert by_len[80.0]["quantity"] == 1
    # Per-row total = quantity * length.
    assert abs(by_len[100.0]["total_length_mm"] - 200.0) < 1e-6
    assert abs(by_len[80.0]["total_length_mm"] - 80.0) < 1e-6


def test_distinct_lengths_separate_rows_and_totals() -> None:
    """Three distinct lengths -> three rows; grand total == Σ per-member lengths."""
    sk = _sketch("f1", [
        _line("e1", [0.0, 0.0], [60.0, 0.0]),       # 60 mm
        _line("e2", [60.0, 0.0], [60.0, 90.0]),     # 90 mm
        _line("e3", [60.0, 90.0], [60.0, 210.0]),   # 120 mm (collinear-ish but distinct length)
    ])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    cl = _cutlist_for([sk, wm])

    assert cl["total_members"] == 3
    assert len(cl["rows"]) == 3, "three distinct lengths => three rows"
    lengths = sorted(r["length_mm"] for r in cl["rows"])
    assert lengths == [60.0, 90.0, 120.0]
    # Grand total == sum of the per-member lengths.
    assert abs(cl["total_length_mm"] - (60.0 + 90.0 + 120.0)) < 1e-6
    assert all(r["quantity"] == 1 for r in cl["rows"])


def test_sorted_by_profile_then_length_desc_with_item_numbers() -> None:
    """Two profiles: rows sort by profile asc then length desc; item numbers run 1..N."""
    # square_tube legs (100, 100) on one sketch; round_tube leg (50) on another.
    sk1 = _sketch("f1", [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [0.0, 50.0], [100.0, 50.0]),
    ])
    wm1 = _weld("f2", "f1", "square_tube",
                params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    sk2 = _sketch("f3", [_line("e1", [0.0, 0.0], [50.0, 0.0])])
    wm2 = _weld("f4", "f3", "round_tube",
                params={"od": 20.0, "thickness": 2.0, "trim_corners": False})
    cl = _cutlist_for([sk1, wm1, sk2, wm2])

    assert cl["total_members"] == 3
    assert cl["profiles"] == ["round_tube", "square_tube"]
    # Sorted: round_tube (50) first, then square_tube (100).
    assert [r["profile"] for r in cl["rows"]] == ["round_tube", "square_tube"]
    assert [r["item"] for r in cl["rows"]] == [1, 2]
    sq = next(r for r in cl["rows"] if r["profile"] == "square_tube")
    assert sq["quantity"] == 2 and abs(sq["total_length_mm"] - 200.0) < 1e-6


def test_two_profiles_length_desc_within_profile() -> None:
    """Within one profile, the longer cut sorts first (length descending)."""
    sk = _sketch("f1", [
        _line("e1", [0.0, 0.0], [40.0, 0.0]),     # 40 mm
        _line("e2", [0.0, 30.0], [160.0, 30.0]),  # 160 mm
    ])
    wm = _weld("f2", "f1", "flat_bar",
               params={"w": 40.0, "thickness": 6.0, "trim_corners": False})
    cl = _cutlist_for([sk, wm])
    lens = [r["length_mm"] for r in cl["rows"]]
    assert lens == sorted(lens, reverse=True), f"rows must be length-desc within a profile: {lens}"
    assert lens == [160.0, 40.0]


# --------------------------------------------------------------------------- descriptions


def test_description_is_human_label() -> None:
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    cl = _cutlist_for([sk, wm])
    assert cl["rows"][0]["description"] == "Square Tube 20 x 20 x 2"


def test_describe_covers_every_library_profile() -> None:
    """Every library profile yields a non-empty, human-ish description (no raw snake_case name)."""
    from engine.doc.weld_profiles import PROFILES
    for name, spec in PROFILES.items():
        desc = _describe(name, spec.params)
        assert desc and "_" not in desc, f"{name}: description '{desc}' should be human-readable"


# --------------------------------------------------------------------------- empty / edge cases


def test_no_weld_members_is_empty_not_error() -> None:
    """A plain part with NO weld_member features -> empty rows + zero totals (not an error)."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    box = T._feat("f2", "box", {"length": T._dim(20), "width": T._dim(20), "height": T._dim(20)})
    cl = _cutlist_for([sk, box])
    assert cl["rows"] == []
    assert cl["total_members"] == 0
    assert cl["total_length_mm"] == 0.0
    assert cl["profiles"] == []


def test_build_cut_list_empty_mapping() -> None:
    """The pure builder over an empty/None mapping returns the empty table."""
    for mapping in (None, {}, {"f2": {}}):
        cl = build_cut_list(mapping)
        assert cl["rows"] == [] and cl["total_members"] == 0 and cl["total_length_mm"] == 0.0


def test_side_channel_surfaces_on_compile_result() -> None:
    """weld_member's _weld_members datum is surfaced onto CompileResult.weld_members."""
    sk = _sketch("f1", [_line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    res = compile_document(T._doc([sk, wm]))
    assert "f2" in res.weld_members
    rec = res.weld_members["f2"]
    assert rec["profile"] == "square_tube"
    assert len(rec["members"]) == 1 and abs(rec["members"][0]["length"] - 100.0) < 1e-6


# --------------------------------------------------------------------------- session entry point


def test_cut_list_for_session_matches_pure_builder() -> None:
    """The session entry point (fresh compile + group) produces the same table as the pure path."""
    from engine.doc import session as docsess

    sk = _sketch("f1", [
        _line("e1", [0.0, 0.0], [100.0, 0.0]),
        _line("e2", [100.0, 0.0], [100.0, 80.0]),
        _line("e3", [100.0, 80.0], [0.0, 80.0]),
    ])
    wm = _weld("f2", "f1", "square_tube",
               params={"size": 20.0, "thickness": 2.0, "trim_corners": False})
    doc = T._doc([sk, wm])
    s = docsess.from_document(doc)
    try:
        cl = cut_list_for_session(s)
    finally:
        docsess.close_session(s.doc_id)

    assert cl["total_members"] == 3
    by_len = {r["length_mm"]: r for r in cl["rows"]}
    assert by_len[100.0]["quantity"] == 2
    assert by_len[80.0]["quantity"] == 1
    assert abs(cl["total_length_mm"] - 280.0) < 1e-6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[weld_cutlist] {name} ok")
    print("[weld_cutlist] all tests passed")
