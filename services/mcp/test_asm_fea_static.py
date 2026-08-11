"""Assembly / multi-body FEA regression suite.

The engine resolves document selectors across every body, meshes imported assemblies
per solid, and requires contact to be declared explicitly. Meshing disconnected solids
must remain reliable without silently fusing them; a static solve spanning bodies must
set ``bond_gap_mm`` only when bonded contact is physically intended.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed MCP tools unavailable",
)


def _has_fea() -> bool:
    try:
        return bool(cb.call("sim_capabilities", {}).get("fea_static", True))
    except Exception:  # noqa: BLE001 — older engines: probe by attempting the RPC later
        return True


def _two_body_doc():
    """Two 20mm boxes overlapping 5mm in X: body A at x[0,20], body B at x[15,35].

    Distinct bodies (transform-copy then move), NOT booleaned — exactly the shape of an
    imported assembly. Returns doc_id.
    """
    r = cb.call("doc_new", {"name": "asm_fea_two_boxes"})
    doc = r["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {
            "type": "box", "name": "A",
            "params": {"length": {"value": 20}, "width": {"value": 20},
                       "height": {"value": 20}}}},
        {"op": "add_feature", "feature": {
            "type": "transform", "name": "B",
            "params": {"translate": [15.0, 0.0, 0.0], "copy": True}}},
    ]
    rr = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    assert rr.get("revision") is not None, rr
    return doc


@pytest.fixture(scope="module")
def two_body():
    return _two_body_doc()


def test_doc_query_spans_all_bodies(two_body):
    """side=bottom matches BOTH bodies' bottom faces (last-body-only saw one).

    Box A is origin-CENTERED (spans x[-10,10]); the copy B is translated +15
    (spans x[5,25]) — a 5mm overlap band, bottoms centred at x=0 and x=15.
    """
    r = cb.call("doc_query", {"doc_id": two_body,
                              "query": {"of": "face", "side": "bottom"}})
    ms = [m for m in r.get("matches", []) if m.get("kind") == "face"]
    assert len(ms) == 2, ms
    xs = sorted(m["centroid"][0] for m in ms)
    assert abs(xs[0] - 0.0) < 1e-6 and abs(xs[1] - 15.0) < 1e-6, xs


def test_face_points_resolves_first_body_face(two_body):
    """A match from body A (the FIRST body) yields its own points — the old code
    re-resolved against body B only and dangled or sampled the wrong face."""
    r = cb.call("doc_query", {"doc_id": two_body,
                              "query": {"of": "face", "side": "bottom"}})
    ms = sorted((m for m in r.get("matches", []) if m.get("kind") == "face"),
                key=lambda m: m["centroid"][0])
    sel = ms[0]  # body A's bottom (centroid x=0)
    fp = cb.call("doc_face_points", {"doc_id": two_body, "selector": sel})
    pts = fp.get("points", [])
    assert pts, fp
    # Every sample lies ON the matched face: same z-plane, within A's 20mm x-span
    # around the recorded centroid (so a wrong-body face fails loudly).
    cz, cx = sel["centroid"][2], sel["centroid"][0]
    assert all(abs(p[2] - cz) < 1e-6 for p in pts), pts[:5]
    assert all(abs(p[0] - cx) <= 10.0 + 1e-6 for p in pts), pts[:5]


def test_exact_selector_not_ambiguous_among_siblings(two_body):
    """The exact-snapshot fast path: re-resolving a recorded match must not dangle as
    'symmetric ambiguity' just because a near-identical sibling face exists."""
    r = cb.call("doc_query", {"doc_id": two_body,
                              "query": {"of": "face", "side": "top"}})
    for m in r.get("matches", []):
        fp = cb.call("doc_face_points", {"doc_id": two_body, "selector": m})
        assert fp.get("points"), (m.get("centroid"), fp)


def test_mesh_multi_solid_step_preserves_separate_parts(two_body, tmp_path):
    """The exported 2-solid STEP meshes each part without inventing contact semantics."""
    step = str(tmp_path / "two_body.step")
    cb.call("doc_export_step", {"doc_id": two_body, "path": step})
    m = cb.call("sim_mesh", {"step_path": step, "fineness": 0.4, "order": 1})
    assert m.get("ok"), m.get("error")
    assert m.get("n_elems", 0) > 0 and m.get("n_nodes", 0) > 0, \
        {k: m.get(k) for k in ("n_elems", "n_nodes", "note")}
    assert "separate solid bodies" in (m.get("note") or ""), m.get("note")


def test_static_solve_across_explicit_bonded_interface(two_body, tmp_path):
    """An explicit bond transfers load between the overlapping imported bodies."""
    if not _has_fea():
        pytest.skip("engine lacks FEA static RPCs")
    step = str(tmp_path / "two_body_solve.step")
    cb.call("doc_export_step", {"doc_id": two_body, "path": step})

    def faceset(query):
        r = cb.call("doc_query", {"doc_id": two_body, "query": query})
        ms = [m for m in r.get("matches", []) if m.get("kind") == "face"]
        assert ms, query
        fp = cb.call("doc_face_points", {"doc_id": two_body, "selector": ms[0]})
        assert fp.get("points"), (query, fp)
        return fp

    fix = faceset({"of": "face", "side": "left"})    # body A x=0 face
    load = faceset({"of": "face", "side": "right"})  # body B x=35 face
    study = {
        "step_path": step,
        "material": {"E_mpa": 70000.0, "nu": 0.33, "density_g_cm3": 2.7,
                     "yield_mpa": 95.0},
        "fixtures": [fix],
        "loads": [{"type": "force", "vector": [0.0, 0.0, -200.0], "faces": load}],
        "mesh_order": 1,
        "fineness": 0.4,
        "bond_gap_mm": 5.1,
    }
    res = cb.call("sim_run_static", {"study": study})
    assert res.get("ok"), res.get("error")
    summ = res.get("summary", {})
    disp = summ.get("max_disp_mm", 0.0)
    vm = summ.get("max_von_mises_mpa", 0.0)
    # Load crossed the bonded interface: finite nonzero deflection + stress. Bounds are
    # deliberately loose (mesh-coarse smoke), but a NON-bonded pair gives a singular
    # system (solve error) or a free-floating body B (garbage/huge displacement).
    assert 0.0 < disp < 10.0, summ
    assert 0.0 < vm < 1000.0, summ


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
