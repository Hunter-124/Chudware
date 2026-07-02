"""Tests for the reverse-engineering MCP tools (services/mcp/recon_tools.py).

The RE round-trip from the handoff spec, end-to-end through the registered MCP tools
against the STAGED libaicad:

  - plate 100x60x8 + r3 vertical-edge fillets + 4x d6 bolt-circle (pcd 50) -> STEP ->
    reverse_engineer recovers base extrude + seed hole + circular pattern (+ fillets
    applied or reported as candidates) at >= 97% boolean match;
  - the recovered hole is EDITABLE: d6 -> d8 recompiles and the match drops
    (compare_parts proves the rebuild is live parametric geometry, not a dumb copy);
  - a turned stepped shaft round-trips >= 95% via an analytic revolve;
  - STL (facet) input completes HONESTLY: 0% match, the facet-yield note, every source
    face reported unclaimed, and no features invented.

Skipped cleanly when the core library isn't built/staged.

Run:  python3 -m pytest services/mcp/test_recon_tools.py -q
      python3 services/mcp/test_recon_tools.py         # also works (prints PASS/FAIL)
"""
from __future__ import annotations

# --- pytest import-shadow repair --------------------------------------------------------
# Under `python3 -m pytest services/mcp/test_recon_tools.py` pytest's default (prepend)
# import mode names this module `mcp.test_recon_tools` — this directory has an
# __init__.py, so the PACKAGE `mcp` becomes services/mcp, shadowing the `mcp` SDK that
# mcp_server's tool modules import (`from mcp.server.fastmcp import FastMCP`). Repair
# sys.modules: load the real SDK package as `mcp`, keeping this directory on its
# __path__ so pytest can still import sibling test modules as `mcp.test_*`.
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)  # `mcp_server` (repo-root name of services/mcp)


def _unshadow_mcp_sdk() -> None:
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return  # `mcp` is absent or already the real SDK — nothing to repair
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module  # must be visible to the SDK's own imports
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from mcp_server import core_bridge as cb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad not staged (build the C++ core); engine-backed MCP tools unavailable",
)


def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, content_list)."""
    from mcp_server import server

    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return parsed, content


def _export(doc: str, fmt: str) -> str:
    r, _ = _call("export_part", {"fmt": fmt, "doc_id": doc})
    assert r["ok"], r
    return r["path"]


# ---------------------------------------------------------------- the acceptance plate
# Built + reverse-engineered ONCE (it is the expensive step: import, 5-pass recognition,
# batched rebuild with an eval_compare after every batch); both plate tests read the
# cached result. The editability test then mutates the REBUILT doc only.
_PLATE: dict = {}


def _plate_round_trip() -> dict:
    if _PLATE:
        return _PLATE
    r, _ = _call("new_part", {"name": "re_plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate",
                {"length": cb.dim(100), "width": cb.dim(60), "height": cb.dim(8)}),
        cb.feat("fillet", "CornerFillets", {"radius": cb.dim(3)},
                {"edges": {"of": "edge", "parallel": "z"}}),
        cb.feat("bolt_circle", "Bolts",
                {"count": 4, "pcd": cb.dim(50), "diameter": cb.dim(6),
                 "style": "simple", "end": "through_all"},
                {"face": {"of": "face", "side": "top"}}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"], rb
    path = _export(doc, "step")
    try:
        re_res, _ = _call("reverse_engineer", {"source": path})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    _PLATE.update({"result": re_res, "source_volume_mm3": rb["volume_mm3"]})
    return _PLATE


def test_reverse_engineer_plate_recovers_hole_pattern_and_base():
    """STEP of the acceptance plate -> an editable tree: base extrude + ONE seed hole +
    a 4x circular pattern, with the r3 vertical-edge fillets applied (or, at minimum,
    reported under `candidates` — the spec's fillet gate), at >= 97% boolean match."""
    re_res = _plate_round_trip()["result"]
    assert re_res["ok"], re_res
    assert re_res["doc_id"] and re_res["ref_doc_id"], re_res
    assert re_res["match_pct"] is not None and re_res["match_pct"] >= 97.0, re_res

    feats = re_res["features"]
    bases = [f for f in feats if f["role"] == "base"]
    assert len(bases) == 1 and bases[0]["type"] == "extrude", feats
    assert abs(bases[0]["depth_mm"] - 8.0) < 0.05, bases

    holes = [f for f in feats if f["role"] == "hole"]
    assert len(holes) == 1, feats  # ONE seed; the ring comes from the pattern
    assert abs(holes[0]["diameter_mm"] - 6.0) < 0.01, holes
    assert holes[0]["through"] is True, holes

    pats = [f for f in feats if f["role"] == "pattern"]
    assert len(pats) == 1 and pats[0]["type"] == "circular_pattern", feats
    assert pats[0]["count"] == 4 and pats[0]["seed"] == holes[0]["id"], pats

    # The 4 corner fillets: applied features + fillet candidates must account for all 4.
    fillets = [f for f in feats if f["type"] == "fillet"]
    fillet_cands = [c for c in re_res["candidates"] if c.get("kind") == "fillet"]
    assert len(fillets) + len(fillet_cands) >= 4, (fillets, fillet_cands)
    for f in fillets:
        assert abs(f["radius"] - 3.0) < 0.01, f

    # Acceptance: nothing rejected, and everything the recognizer saw was explained.
    assert re_res["rejected"] == [], re_res["rejected"]
    assert re_res["recognition"]["kind"] == "prismatic", re_res["recognition"]
    assert re_res["recognition"]["unclaimed"] == [], re_res["recognition"]["unclaimed"]

    # The residual localization fields exist (near-zero on a 97%+ match).
    unex = re_res["unexplained"]
    assert set(unex) == {"missing_mm3", "extra_mm3", "regions"}, unex


def test_recovered_hole_is_editable_and_match_drops():
    """Editability proof: edit the RECOVERED seed hole d6 -> d8; the tree recompiles
    (pattern included) and compare_parts against the source shows the match DROP —
    the rebuild is live parametric geometry, not a frozen copy."""
    plate = _plate_round_trip()
    re_res = plate["result"]
    assert re_res["ok"], re_res
    match_before = re_res["match_pct"]
    new_doc, ref_doc = re_res["doc_id"], re_res["ref_doc_id"]
    hole = next(f for f in re_res["features"] if f["role"] == "hole")

    ed, _ = _call("add_features", {"doc_id": new_doc, "ops": [
        {"op": "edit_feature", "id": hole["id"], "params": {"diameter": cb.dim(8)}}]})
    assert ed["ok"], ed  # the edited tree recompiles
    # 4 holes went d6 -> d8: material was removed relative to the recovered part.
    assert ed["volume_mm3"] < plate["source_volume_mm3"], ed

    cp, _ = _call("compare_parts", {"doc_a": ref_doc, "doc_b": new_doc})
    assert cp["ok"], cp
    assert cp["match_pct"] is not None, cp
    assert cp["match_pct"] < match_before - 0.5, (cp["match_pct"], match_before)
    assert cp["match_pct"] > 90.0, cp  # still recognizably the same part


# ---------------------------------------------------------------- turned shaft


def test_reverse_engineer_turned_shaft_via_revolve():
    """A stepped shaft (r15/r10/r5 coaxial steps) round-trips through STEP at >= 95%
    match as ONE analytic revolve — the turned-body pass, no sectioning."""
    r, _ = _call("new_part", {"name": "re_shaft"})
    doc = r["doc_id"]
    ops = [
        cb.feat("cylinder", "S1", {"radius": cb.dim(15), "height": cb.dim(20)}),
        cb.feat("cylinder", "S2", {"radius": cb.dim(10), "height": cb.dim(40)}),
        cb.feat("cylinder", "S3", {"radius": cb.dim(5), "height": cb.dim(60)}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"] and rb["body_count"] == 1, rb
    path = _export(doc, "step")
    try:
        re_res, _ = _call("reverse_engineer", {"source": path})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert re_res["ok"], re_res
    assert re_res["match_pct"] is not None and re_res["match_pct"] >= 95.0, re_res
    assert re_res["recognition"]["kind"] == "turned", re_res["recognition"]
    bases = [f for f in re_res["features"] if f["role"] == "base"]
    assert len(bases) == 1 and bases[0]["type"] == "revolve", re_res["features"]
    assert re_res["rejected"] == [], re_res["rejected"]


# ---------------------------------------------------------------- STL smoke


def test_reverse_engineer_stl_smoke_is_honest():
    """Facet (STL) input completes without inventing anything: ok:true with a 0% match,
    the facet-yield note, EVERY source face reported unclaimed (the §3.4 rule: heuristics
    never suppress the raw face list), and a clean 'no features rebuilt' summary instead
    of a compare error."""
    r, _ = _call("new_part", {"name": "re_stl"})
    doc = r["doc_id"]
    rb, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(30), "width": cb.dim(20),
                             "height": cb.dim(10)})]})
    assert rb["ok"], rb
    path = _export(doc, "stl")
    try:
        re_res, _ = _call("reverse_engineer", {"source": path})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert re_res["ok"], re_res  # honest completion, not an error
    assert re_res["match_pct"] == 0.0, re_res
    notes = " ".join(re_res["notes"])
    assert "facet geometry: expect low deterministic yield" in notes, notes
    assert re_res["features"] == [], re_res["features"]
    assert "no features rebuilt" in re_res["summary"], re_res["summary"]
    rec = re_res["recognition"]
    assert rec["face_count"] > 0, rec
    assert len(rec["unclaimed"]) == rec["face_count"], (len(rec["unclaimed"]),
                                                        rec["face_count"])


if __name__ == "__main__":
    if not cb.available():
        print("SKIP: libaicad not staged")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
