"""Smoke tests for the C++ feature-engine MCP tools (mcp_server/cad_tools.py).

These drive the registered MCP tools end-to-end against the STAGED libaicad.dll (the same
engine the app loads) — build a part, render it, run FEA + CFD. They are skipped cleanly
when the DLL isn't built/staged, so a checkout without a build still passes.

Run:  python -m pytest mcp_server/test_cad_tools.py -v
      python mcp_server/test_cad_tools.py        # also works (prints PASS/FAIL)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# Allow `python mcp_server/test_cad_tools.py` (script dir, not repo root, lands on path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from mcp_server import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad.dll not staged (run build.bat); engine-backed MCP tools unavailable",
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


def _new_plate_with_hole() -> str:
    r, _ = _call("new_part", {"name": "plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all", "diameter": cb.dim(8)},
                {"face": {"of": "face", "side": "top"}}),
    ]
    r, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert r["ok"], r
    assert r["body_count"] == 1, r
    # Drilled: volume strictly below the solid 60*40*10 = 24000.
    assert 0 < r["volume_mm3"] < 24000, r
    return doc


def test_cad_guide_returns_vocabulary():
    r, content = _call("cad_guide", {"topic": "overview"})
    text = content[0].text
    assert "add_feature" in text and "revolve" in text.lower(), text[:200]


def test_build_plate_with_hole():
    _new_plate_with_hole()


def test_query_geometry_top_face():
    doc = _new_plate_with_hole()
    r, _ = _call("query_geometry", {"query": "top", "doc_id": doc})
    assert r["count"] >= 1
    assert any(m.get("kind") == "face" for m in r["matches"])


def test_render_part_returns_image():
    doc = _new_plate_with_hole()
    from mcp_server import server

    # call_tool's return shape varies for image vs text tools; just hunt for the ImageContent.
    def _find_image(x):
        if getattr(x, "type", "") == "image":
            return x
        if isinstance(x, (list, tuple)):
            for e in x:
                found = _find_image(e)
                if found is not None:
                    return found
        return None

    result = asyncio.run(server.mcp.call_tool("render_part", {"doc_id": doc}))
    item = _find_image(result)
    assert item is not None, f"no ImageContent in {result!r}"
    assert getattr(item, "mimeType", "").startswith("image/"), item
    assert item.data, "empty image payload"


def test_measure_part():
    doc = _new_plate_with_hole()
    r, _ = _call("measure_part", {"doc_id": doc})
    assert r["ok"]
    assert r["mass"]["volume_mm3"] > 0


def test_fea_static_cantilever():
    doc = _new_plate_with_hole()
    r, _ = _call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -200]}],
        "material": "aluminum",
        "doc_id": doc,
    })
    assert r["ok"], r
    s = r["summary"]
    assert s["max_von_mises_mpa"] > 0
    assert "min_fos" in s and s["min_fos"] > 0


def test_cfd_flow_box():
    doc = _new_plate_with_hole()
    r, _ = _call("run_cfd_flow", {"velocity": 20.0, "doc_id": doc})
    assert r["ok"], r
    assert r["drag_n"] is not None
    assert r["reynolds"] > 0


def test_export_step():
    import os
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert r["ok"] and r["exists"], r
    os.unlink(r["path"])


def test_save_part_native_stepplus_roundtrips():
    """save_part writes the NATIVE .step+ and it reopens with the feature tree intact."""
    import os
    import tempfile

    doc = _new_plate_with_hole()
    out = os.path.join(tempfile.mkdtemp(), "delivered")  # no extension -> tool adds .step+
    r, _ = _call("save_part", {"path": out, "doc_id": doc})
    assert r["ok"] and r["native"] and r["format"] == "step+", r
    assert r["path"].lower().endswith(".step+") and r["bytes"] > 0, r

    # Round-trip: reopening the native file restores an EDITABLE feature tree (box + hole),
    # which a flat STEP export would not carry. Drives the engine directly (no MCP open tool).
    opened = cb.call("doc_open", {"path": r["path"]})
    tree = cb.call("doc_tree", {"doc_id": opened["doc_id"]})
    feats = (tree.get("tree") or {}).get("features", [])
    types = {f.get("type") for f in feats}
    assert {"box", "hole"} <= types, (types, feats)
    os.unlink(r["path"])


def test_export_step_plus_alias_is_native():
    """export_part('step+') is accepted as an alias and routed to the native saver."""
    import os
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step+", "doc_id": doc})
    assert r["ok"] and r.get("native") and r["format"] == "step+", r
    assert r["path"].lower().endswith(".step+") and r["exists"], r
    os.unlink(r["path"])


def test_transform_moves_a_body():
    """transform translates a non-primitive-addressable body (COM shifts by the vector)."""
    r, _ = _call("new_part", {"name": "mv"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    m0, _ = _call("measure_part", {"doc_id": doc})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Move", {"translate": [200, 0, 0]})]})
    m1, _ = _call("measure_part", {"doc_id": doc})
    assert abs(m1["mass"]["com"][0] - m0["mass"]["com"][0] - 200) < 1e-6


def test_transform_copy_makes_two_bodies():
    """transform copy:true keeps the source and adds a moved duplicate (assembly instancing)."""
    r, _ = _call("new_part", {"name": "cp"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C", {"radius": cb.dim(10), "height": cb.dim(30)})]})
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [50, 0, 0], "copy": True})]})
    assert r2["ok"] and r2["body_count"] == 2, r2


def test_measure_multibody_per_body_and_total():
    """A multi-body doc reports per-body breakdown + the combined total (not just one body)."""
    r, _ = _call("new_part", {"name": "asm"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C", {"radius": cb.dim(10), "height": cb.dim(30)})]})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [50, 0, 0], "copy": True})]})
    m, _ = _call("measure_part", {"doc_id": doc})
    mass = m["mass"]
    assert mass["body_count"] == 2 and len(mass.get("bodies", [])) == 2, mass
    per = sum(b["volume_mm3"] for b in mass["bodies"])
    assert abs(mass["volume_mm3"] - per) < 1e-3  # total == sum of bodies


def test_export_step_round_trip_validated():
    """export_part returns real metadata and only ok:True when the file actually re-reads."""
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert r["ok"] and r["validated"] is True, r
    assert r["solid_count"] == r["expected_solids"] == 1 and r["bytes"] > 0, r
    os.unlink(r["path"])


def test_batch_atomicity_rolls_back_on_failure():
    """A batch with a feature that fails to COMPILE is rejected whole — nothing persists."""
    r, _ = _call("new_part", {"name": "atom"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    before, _ = _call("get_part", {"doc_id": doc})
    n_before = len((before.get("tree") or {}).get("features", []))
    bad, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B2", {"length": cb.dim(5), "width": cb.dim(5), "height": cb.dim(5)}),
        cb.feat("fillet", "F", {"radius": cb.dim(9999)}, {"edges": {"of": "edge"}})]})
    after, _ = _call("get_part", {"doc_id": doc})
    n_after = len((after.get("tree") or {}).get("features", []))
    assert bad["ok"] is False and bad.get("rolled_back") is True, bad
    assert n_after == n_before, (n_before, n_after)  # the whole batch was rolled back


def test_cfd_diagnostics_and_v2_scaling():
    """CFD reports resolution + convergence diagnostics, and drag now scales ~V^2 (not V^1)."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sphere", "S", {"radius": cb.dim(50)})]})
    r1, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 10.0, "cells": 64, "n_steps": 1200})
    r2, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 20.0, "cells": 64, "n_steps": 1200})
    for rr in (r1, r2):
        assert rr["ok"] and "voxel_mm" in rr and "converged" in rr and "residual" in rr, rr
        assert "ref_length_mm" in rr
    # Doubling velocity ~quadruples drag (V^2), and Cd is ~velocity-independent (was Cd∝1/V).
    ratio = r2["drag_n"] / r1["drag_n"]
    assert 3.2 < ratio < 4.8, (ratio, r1["drag_n"], r2["drag_n"])
    assert abs(r1["cd"] - r2["cd"]) / max(abs(r1["cd"]), 1e-9) < 0.05, (r1["cd"], r2["cd"])


def test_background_job_and_get_job():
    """A background CFD returns a job_id immediately; get_job polls it to completion."""
    import time
    r, _ = _call("new_part", {"name": "bg"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sphere", "S", {"radius": cb.dim(40)})]})
    j, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 12.0, "cells": 48,
                                  "n_steps": 800, "background": True})
    assert j["ok"] and j.get("job_id") and j["status"] == "running", j
    jid = j["job_id"]
    for _ in range(120):
        g, _ = _call("get_job", {"job_id": jid})
        if g.get("status") in ("done", "error"):
            break
        time.sleep(1)
    assert g["status"] == "done" and g["result"]["ok"], g


if __name__ == "__main__":
    import sys

    if not cb.available():
        print("SKIP: libaicad.dll not staged")
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
