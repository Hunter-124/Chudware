"""Smoke tests for the C++ feature-engine MCP tools (services/mcp/cad_tools.py).

These drive the registered MCP tools end-to-end against the STAGED libchudware.dll (the same
engine the app loads) — build a part, render it, run FEA + CFD. They are skipped cleanly
when the DLL isn't built/staged, so a checkout without a build still passes.

Run:  python -m pytest services/mcp/test_cad_tools.py -v
      python -m services.mcp.test_cad_tools        # also works (prints PASS/FAIL)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import threading
import time
import uuid
from pathlib import Path


import pytest

from services.mcp import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware.dll not staged (run build.bat); engine-backed MCP tools unavailable",
)


def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, content_list)."""
    from services.mcp import server

    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return parsed, content


def _solve(name: str, args: dict, timeout_s: float = 300.0):
    """Call a heavy solve tool and honor its async contract.

    A solve (run_fea_*/run_cfd_*) runs on a worker thread and AUTO-HANDS-OFF to the
    background if it overruns CHUDWARE_MCP_SYNC_BUDGET_S (default 45s) — a small plate
    FEA sits right on that edge, so the reply is non-deterministically either the
    inline result or {status:"running", auto_backgrounded:True, job_id}. When it's
    backgrounded we poll get_job() until it completes and return the solve `result`,
    exactly as the tool docstring instructs the agent to. Returns the final solve dict.
    """
    r, _ = _call(name, args)
    if not (isinstance(r, dict) and r.get("auto_backgrounded") and r.get("job_id")):
        return r  # finished inline within the sync budget
    jid = r["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        j, _ = _call("get_job", {"job_id": jid})
        status = j.get("status") if isinstance(j, dict) else None
        if status == "done":
            return j.get("result")
        if status == "error":
            return {"ok": False, "error": j.get("error", "solve failed")}
        time.sleep(0.5)
    raise AssertionError(f"solve {name} did not finish within {timeout_s:.0f}s (job {jid})")


_SIM_AVAILABLE: bool | None = None


def _require_sim() -> None:
    """Skip when the loaded engine was built without OCCT/Polyhedral-FEA.

    An unknown-method response is a build-configuration state rather than a solver
    regression. The main gate selects the full build; this remains a defensive skip for
    deliberately slim local builds.
    """
    global _SIM_AVAILABLE
    if _SIM_AVAILABLE is None:
        # cb.call RAISES CoreError("unknown method: ...") on a sim-less build; a
        # dispatched-but-invalid-study reply means the RPC IS compiled in (sim present).
        try:
            cb.call("sim_run_static", {})
            _SIM_AVAILABLE = True
        except cb.CoreError as exc:
            _SIM_AVAILABLE = "unknown method" not in str(exc).lower()
    if not _SIM_AVAILABLE:
        pytest.skip(
            "engine has no FEA solver; rebuild with CHUDWARE_WITH_POLYMESH=ON or "
            "point CHUDWARE_LIBCHUDWARE at core/build-occt-linux/bin/libchudware.so"
        )


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


# ---- task 06: sketch relations + check_sketch -----------------------------------------------
# Sketch entity/constraint shape mirrors the proven C++ DSL (core/tests/test_document.cpp::
# rect_entities): 4 lines closed by coincident + h/v relations + a fixed corner + 2 driving
# distance dims == a FULLY DEFINED rectangle (dof 0). Drop the dims -> under-defined.
def _line(eid, x1, y1, x2, y2):
    return {"id": eid, "type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}


def _rel(cid, ctype, refs):
    return {"id": cid, "type": ctype, "refs": refs}


def _dim(cid, ctype, refs, value):
    return {"id": cid, "type": ctype, "refs": refs, "value": {"value": value, "expr": None},
            "driven": False}


def _rect_sketch(name, w, h, with_dims=True):
    """A fully/under-constrained rectangle as a CANONICAL add_feature op.

    Entities + constraints live under `params`; the sketch plane is a `refs.plane`
    datum ("plane.top"). This is the shape core/doc/validate.cpp enforces for a
    sketch feature — the old flat shape ({id,type,plane,entities,constraints} at
    top level) is rejected by validation before it can land (normalize_feature
    accepts it, but validate runs first). The engine assigns the feature id (f1,
    f2, …) and keeps `name` as the caller's label, so callers reference the sketch
    by the id returned from add_features, not by `name` (see _add_rect_sketch).
    """
    cons = [
        _rel("c1", "coincident", ["e1.p2", "e2.p1"]),
        _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
        _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
        _rel("c4", "coincident", ["e4.p2", "e1.p1"]),
        _rel("c5", "horizontal", ["e1"]),
        _rel("c6", "horizontal", ["e3"]),
        _rel("c7", "vertical", ["e2"]),
        _rel("c8", "vertical", ["e4"]),
        _rel("c9", "fix", ["e1.p1"]),
    ]
    if with_dims:
        cons += [
            _dim("c10", "distance", ["e1.p1", "e1.p2"], w),
            _dim("c11", "distance", ["e2.p1", "e2.p2"], h),
        ]
    entities = [
        _line("e1", 0, 0, w, 0), _line("e2", w, 0, w, h),
        _line("e3", w, h, 0, h), _line("e4", 0, h, 0, 0),
    ]
    return cb.feat("sketch", name, {"entities": entities, "constraints": cons},
                   {"plane": "plane.top"})


def _add_rect_sketch(doc, w, h, with_dims=True, name="s1"):
    """Add a rectangle sketch and return the engine-assigned feature id (e.g. "f1")."""
    r, _ = _call("add_features",
                 {"ops": [_rect_sketch(name, w, h, with_dims=with_dims)], "doc_id": doc,
                  "auto_define": False})
    assert r["ok"], r
    return r["features"][0]["id"]


def test_check_sketch_fully_defined():
    r, _ = _call("new_part", {"name": "sk"})
    doc = r["doc_id"]
    sid = _add_rect_sketch(doc, 40, 24, with_dims=True)
    r, _ = _call("check_sketch", {"sketch": sid, "doc_id": doc})
    assert r["ok"], r
    assert r["status"] == "full" and r["dof"] == 0, r
    assert r["fully_defined"] is True, r
    assert not r["conflicting"], r
    assert "Fully defined" in r["summary"], r


def test_check_sketch_under_defined():
    r, _ = _call("new_part", {"name": "sk2"})
    doc = r["doc_id"]
    sid = _add_rect_sketch(doc, 40, 24, with_dims=False)
    r, _ = _call("check_sketch", {"sketch": sid, "doc_id": doc})
    assert r["ok"], r
    assert r["status"] == "under" and r["dof"] >= 1, r
    assert r["fully_defined"] is False, r
    assert "Under-defined" in r["summary"], r


def test_render_part_returns_image():
    doc = _new_plate_with_hole()
    from services.mcp import server

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


def _find_image(x):
    if getattr(x, "type", "") == "image":
        return x
    if isinstance(x, (list, tuple)):
        for e in x:
            found = _find_image(e)
            if found is not None:
                return found
    return None


def test_render_part_custom_angle_returns_image():
    # Task 03: render_part now takes an on-request view (named or {azimuth,elevation}). Each yields
    # a real PNG, and the core echoes the resolved camera, proving the angle actually applied.
    doc = _new_plate_with_hole()
    from services.mcp import server

    for view in ("right", {"azimuth": 30, "elevation": 20}):
        result = asyncio.run(
            server.mcp.call_tool("render_part", {"doc_id": doc, "view": view}))
        item = _find_image(result)
        assert item is not None, f"no ImageContent for view={view!r} in {result!r}"
        assert getattr(item, "mimeType", "").startswith("image/"), item
        assert item.data, "empty image payload"

    # azimuth 90 / elevation 0 resolves to the same camera as the named "right" view.
    import os
    png = _tmp_png()
    r = cb.call("doc_render_view",
                {"doc_id": doc, "out_path": png, "view": {"azimuth": 90, "elevation": 0}})
    assert r.get("ok"), r
    assert os.path.exists(png)
    assert abs(r["dir"][0] - 1.0) < 1e-6 and abs(r["dir"][1]) < 1e-6, r  # looks down +X


def test_contact_sheet_returns_image():
    # Task 03: contact_sheet tiles several labelled views into one PNG.
    doc = _new_plate_with_hole()
    from services.mcp import server

    result = asyncio.run(server.mcp.call_tool("contact_sheet", {"doc_id": doc}))
    item = _find_image(result)
    assert item is not None, f"no ImageContent in {result!r}"
    assert getattr(item, "mimeType", "").startswith("image/"), item
    assert item.data, "empty image payload"

    # Explicit view list is honoured; the core reports the tiled view names.
    import os
    png = _tmp_png()
    r = cb.call("doc_render_contact_sheet",
                {"doc_id": doc, "out_path": png, "views": ["front", "top"]})
    assert r.get("ok") and os.path.exists(png), r
    assert [v.upper() for v in r["views"]] == ["FRONT", "TOP"], r


def _tmp_png():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    return path


def test_measure_part():
    doc = _new_plate_with_hole()
    r, _ = _call("measure_part", {"doc_id": doc})
    assert r["ok"]
    assert r["mass"]["volume_mm3"] > 0


def test_set_material_preset_and_explicit():
    # A named material stores both engineering identity and rendered appearance. An explicit
    # appearance dict deliberately changes visuals only.
    doc = _new_plate_with_hole()

    r, _ = _call("set_material", {"material": "brass", "doc_id": doc})
    assert r["ok"], r
    assert r["bodies"] == ["b1"], r
    assert r["appearance"]["metallic"] > 0.5  # brass is metallic
    assert r["engineering_material_set"] and r["material_key"], r

    # The op landed on the doc and the tessellated body carries the brass colour/material.
    tree = cb.call("doc_tree", {"doc_id": doc})
    ap = (tree.get("tree") or {}).get("appearances", {})
    assert "b1" in ap, ap
    assert (tree.get("tree") or {})["material"]["key"] == r["material_key"], tree
    # The baked body colour/material is reported in the apply-ops tess metadata.
    info = cb.call("doc_apply_ops", {"doc_id": doc, "ops": []})
    b0 = info["tess"]["bodies"][0]
    assert b0["metallic"] > 0.5 and b0["color"][0] > 0.5, b0  # warm metallic

    # Explicit dict {color, roughness} also works.
    r2, _ = _call("set_material",
                  {"material": {"color": [0.1, 0.2, 0.3], "roughness": 0.2}, "doc_id": doc})
    assert r2["ok"], r2
    assert r2["engineering_material_set"] is False and r2["material_key"] is None, r2
    info2 = cb.call("doc_apply_ops", {"doc_id": doc, "ops": []})
    assert info2["tess"]["bodies"][0]["color"][2] > 0.25  # ~0.3 blue channel


def test_set_material_body_identity_survives_save_reopen(tmp_path):
    doc = _new_plate_with_hole()
    reopened = None
    try:
        cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            cb.feat("box", "SeparateBody", {
                "length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10),
                "at": [100, 0, 0], "mode": "new", "merge": False}),
        ]})
        whole, _ = _call("set_material", {"material": "6061-T6", "doc_id": doc})
        default_material = cb.call("doc_tree", {"doc_id": doc})["tree"]["material"]
        selected, _ = _call("set_material", {
            "material": "A36", "body": "b2", "doc_id": doc})
        assert whole["ok"] and selected["ok"], (whole, selected)
        path = tmp_path / "mixed_materials.step+"
        cb.call("doc_save", {"doc_id": doc, "path": str(path)})
        cb.call("doc_close", {"doc_id": doc})
        reopened = cb.call("doc_open", {"path": str(path)})["doc_id"]
        state = cb.call("doc_tree", {"doc_id": reopened})
        tree = state["tree"]
        assert tree["material"] == default_material
        assert tree["body_materials"]["b2"]["key"] == "steel_a36"
        assert "b1" not in tree["body_materials"]

        missing, _ = _call("set_material", {
            "material": "brass", "body": "b99", "doc_id": reopened})
        assert not missing.get("ok"), missing
        after_refusal = cb.call("doc_tree", {"doc_id": reopened})
        assert after_refusal["revision"] == state["revision"]
        tree = after_refusal["tree"]
        assert tree["material"] == default_material
        assert tree["body_materials"]["b2"]["key"] == "steel_a36"

        look, _ = _call("set_material", {
            "material": {"color": [0.1, 0.2, 0.3], "metallic": 0.0},
            "body": "b2", "doc_id": reopened})
        assert look["ok"], look
        tree = cb.call("doc_tree", {"doc_id": reopened})["tree"]
        assert tree["body_materials"]["b2"]["key"] == "steel_a36"

        replaced, _ = _call("set_material", {
            "material": "brass", "doc_id": reopened})
        assert replaced["ok"], replaced
        tree = cb.call("doc_tree", {"doc_id": reopened})["tree"]
        assert tree["material"]["key"] == replaced["material_key"]
        assert not tree.get("body_materials")
    finally:
        cb.call("doc_close", {"doc_id": reopened or doc})


def test_set_material_selected_faces_preserves_physics_and_reopens(tmp_path):
    doc = _new_plate_with_hole()
    reopened = None
    try:
        named, _ = _call("set_material", {"material": "A36", "doc_id": doc})
        assert named["ok"], named
        before = cb.call("doc_tree", {"doc_id": doc})
        queried, _ = _call("query_geometry", {"query": "top", "doc_id": doc})
        selected = queried["matches"][0]
        painted, _ = _call("set_material", {
            "material": {"color": [0.9, 0.1, 0.05], "metallic": 0.8, "roughness": 0.2},
            "body": "b1", "doc_id": doc, "faces": [selected]})
        assert painted["ok"] and not painted["engineering_material_set"], painted
        state, _ = _call("get_part", {"doc_id": doc})
        assert state["tree"]["material"] == before["tree"]["material"]
        assert state["tree"]["appearances"] == before["tree"]["appearances"]
        for args in (
            {"material": "brass", "body": "b1", "faces": [selected]},
            {"material": [1, 0, 0], "faces": [selected]},
            {"material": [1, 0, 0], "body": "b1", "faces": []},
            {"material": [1, 0, 0], "body": "b1", "faces": [selected, "Fffffff"]},
        ):
            rejected, _ = _call("set_material", {**args, "doc_id": doc})
            assert not rejected.get("ok"), rejected
            assert cb.call("doc_tree", {"doc_id": doc})["revision"] == state["revision"]
        path = tmp_path / "selected_face_finish.step+"
        cb.call("doc_save", {"doc_id": doc, "path": str(path)})
        cb.call("doc_close", {"doc_id": doc})
        reopened = cb.call("doc_open", {"path": str(path)})["doc_id"]
        loaded, _ = _call("get_part", {"doc_id": reopened})
        assert loaded["tree"]["face_appearances"] == state["tree"]["face_appearances"]
        cleared, _ = _call("set_material", {
            "material": {"color": None}, "body": 1, "doc_id": reopened, "faces": [selected["sid"]]})
        assert cleared["ok"], cleared
        tree = cb.call("doc_tree", {"doc_id": reopened})["tree"]
        assert not tree.get("face_appearances")
        assert tree["material"] == before["tree"]["material"]
        assert tree["appearances"] == before["tree"]["appearances"]
    finally:
        cb.call("doc_close", {"doc_id": reopened or doc})


def test_set_material_unknown_name_errors():
    doc = _new_plate_with_hole()
    r, _ = _call("set_material", {"material": "unobtanium", "doc_id": doc})
    assert not r.get("ok"), r  # _err envelope on an unknown preset


def test_fea_static_cantilever():
    _require_sim()
    doc = _new_plate_with_hole()
    r = _solve("run_fea_static", {
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
    r = _solve("run_cfd_flow", {"velocity": 20.0, "doc_id": doc})
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


def test_saved_relative_assembly_survives_batch_autosave_and_reopen(tmp_path, monkeypatch):
    """Editing a delivered bundle must not relocate its relative child-source base."""
    import zipfile
    from services.mcp import cad_common as C

    project = tmp_path / "project"
    bundle = project / "verification" / "teammate bundle"
    bundle.mkdir(parents=True)
    destination = bundle / "chosen assembly.step+"
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(project))
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("CHUDWARE_HANDOFF_DIR", str(tmp_path / "handoff"))
    # Exercise the real standalone publisher synchronously; never leave a timer
    # that could mutate another test's engine or outlive tmp_path.
    monkeypatch.setattr(C, "_PUBLISH_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(C, "_PUB_THROTTLE", {})
    monkeypatch.setattr(C, "_PUB", {"rev": None, "docs": {}, "active": None})
    docs = []
    children = []
    try:
        for name, size in (("first", (10, 4, 3)), ("second", (6, 5, 2))):
            part = cb.call("doc_new", {"name": name, "resume_if_open": False})["doc_id"]
            docs.append(part)
            built = cb.call("doc_apply_ops", {"doc_id": part, "ops": [
                cb.feat("box", name, dict(zip(("length", "width", "height"), size)))
            ]})
            assert all(s["ok"] for s in built["rebuild"]["feature_status"]), built
            child = bundle / f"{name}.step+"
            cb.call("doc_save", {"doc_id": part, "path": str(child)})
            children.append(child)
        child_bytes = {child: child.read_bytes() for child in children}
        asm = cb.call("doc_new", {"name": "relative handoff", "kind": "assembly",
                                   "resume_if_open": False})["doc_id"]
        docs.append(asm)
        inserted = cb.call("doc_apply_ops", {"doc_id": asm, "ops": [
            {"op": "asm_insert_component", "name": child.stem, "fixed": True,
             "source": {"path": str(child)},
             "transform": [1, 0, 0, i * 30, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}
            for i, child in enumerate(children)
        ]})
        components = [inserted["id_map"][f"${i}"] for i in range(2)]
        saved, _ = _call("save_part", {"doc_id": asm, "path": str(destination)})
        assert saved["ok"], saved

        edited, _ = _call("add_features", {"doc_id": asm, "ops": [
            {"op": "asm_edit_component", "id": component,
             "source": {"path": child.name}}
            for component, child in zip(components, children)
        ]})
        assert edited["ok"], edited
        assert edited["saved_path"] == str(destination)
        assert len(cb.call("asm_world_bodies", {"doc_id": asm})["bodies"]) == 2
        assert cb.call("eval_mass", {"doc_id": asm})["volume_mm3"] == pytest.approx(180)
        live = next(d for d in cb.call("doc_list", {})["docs"] if d["doc_id"] == asm)
        assert live["path"] == str(destination)
        saved, _ = _call("save_part", {"doc_id": asm, "path": str(destination)})
        assert saved["ok"], saved
        assert not (project / "relative_handoff.step+").exists()
        with zipfile.ZipFile(destination) as archive:
            tree = json.loads(archive.read("document.json"))
        assert [c["source"]["path"] for c in tree["components"]] == [
            child.name for child in children]

        cb.call("doc_close", {"doc_id": asm})
        docs.remove(asm)
        reopened = cb.call("doc_open", {"path": str(destination)})["doc_id"]
        docs.append(reopened)
        # A real post-open edit forces a rebuild rather than accepting display cache.
        edited, _ = _call("add_features", {"doc_id": reopened, "ops": [
            {"op": "asm_edit_component", "id": components[1],
             "transform": [1, 0, 0, 40, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}
        ]})
        assert edited["ok"], edited
        assert edited["saved_path"] == str(destination)
        assert len(cb.call("asm_world_bodies", {"doc_id": reopened})["bodies"]) == 2
        assert cb.call("eval_mass", {"doc_id": reopened})["volume_mm3"] == pytest.approx(180)
        saved, _ = _call("save_part", {"doc_id": reopened, "path": str(destination)})
        assert saved["ok"], saved
        assert {child: child.read_bytes() for child in children} == child_bytes
    finally:
        for doc in reversed(docs):
            cb.call("doc_close", {"doc_id": doc})


def test_save_part_replaces_a_cad_extension_instead_of_stacking_one():
    """save_part("x.step") must write x.step+, never x.step.step+.

    Measured live 2026-07-29 (parts sweep): save_part(path="m4_hex_nut_1.step") wrote
    m4_hex_nut_1.step.step+ because the tool appended unconditionally. A real user session
    left a lexus_f_keychain.step.step+ in the repo root the same way. A doubled extension
    makes one part look like two nested formats in a file browser, and the caller who typed
    ".step" has no way to predict the name they get back.
    """
    import os
    import tempfile

    doc = _new_plate_with_hole()
    d = tempfile.mkdtemp()

    for asked, expect in (("bracket.step", "bracket.step+"),
                          ("bracket.stl", "bracket.step+"),
                          ("bracket.STEP", "bracket.step+")):
        r, _ = _call("save_part", {"path": os.path.join(d, asked), "doc_id": doc})
        assert r["ok"], r
        assert os.path.basename(r["path"]) == expect, (asked, r["path"])
        # The substitution is stated, not silent — the caller asked for another format.
        assert "note" in r and ".step+" in r["note"], r
        os.unlink(r["path"])


def test_save_part_appends_when_there_is_no_cad_extension_to_replace():
    """A dotted BASE name is not an extension — "v1.2_bracket" must survive intact."""
    import os
    import tempfile

    doc = _new_plate_with_hole()
    d = tempfile.mkdtemp()

    for asked, expect in (("bracket", "bracket.step+"),
                          ("v1.2_bracket", "v1.2_bracket.step+"),
                          ("bracket.step+", "bracket.step+")):
        r, _ = _call("save_part", {"path": os.path.join(d, asked), "doc_id": doc})
        assert r["ok"], r
        assert os.path.basename(r["path"]) == expect, (asked, r["path"])
        # Nothing was rewritten, so no note should be invented.
        assert "note" not in r, r
        os.unlink(r["path"])


def test_save_part_written_file_reopens_after_extension_rewrite():
    """The rewritten path must be the file that actually exists and reopens."""
    import os
    import tempfile

    doc = _new_plate_with_hole()
    out = os.path.join(tempfile.mkdtemp(), "rewritten.step")
    r, _ = _call("save_part", {"path": out, "doc_id": doc})
    assert r["ok"] and os.path.exists(r["path"]), r
    assert not os.path.exists(out + ".step+"), "the stacked name was written after all"
    opened = cb.call("doc_open", {"path": r["path"]})
    tree = cb.call("doc_tree", {"doc_id": opened["doc_id"]})
    types = {f.get("type") for f in (tree.get("tree") or {}).get("features", [])}
    assert {"box", "hole"} <= types, types
    os.unlink(r["path"])


def test_close_part_warns_on_unsaved_changes():
    """close_part on a dirty doc refuses (unsaved_changes) so the AI saves deliverables —
    the user is never nagged about the AI's docs, so the discipline lives here."""
    doc = _new_plate_with_hole()  # has edits -> dirty
    from services.mcp import cad_common
    assert doc in cad_common._LAST_VOLUME
    assert doc in cad_common._PROCESS_DOCS
    assert doc in cad_common._TOUCHED_DOCS
    volume_before = cad_common._LAST_VOLUME[doc]
    r, _ = _call("close_part", {"doc_id": doc})
    assert r.get("ok") is False and r.get("code") == "unsaved_changes", r
    assert r.get("dirty") is True, r
    assert cad_common._LAST_VOLUME.get(doc) == volume_before, \
        "refused close must preserve the volume baseline"
    assert doc in cad_common._PROCESS_DOCS, "refused close must preserve process ownership"
    assert doc in cad_common._TOUCHED_DOCS, "refused close must preserve touched-doc state"
    # The doc is untouched — still listable and closable.
    ids = {e.get("doc_id") for e in cb.call("doc_list", {}).get("docs", [])}
    assert doc in ids, "refused close must not have dropped the doc"
    assert _call("close_part", {"doc_id": doc, "discard": True})[0].get("ok") is True


def test_close_part_discard_drops_scratch():
    """close_part(discard=True) closes a dirty scratch doc without saving."""
    doc = _new_plate_with_hole()
    from services.mcp import cad_common
    assert doc in cad_common._LAST_VOLUME
    assert doc in cad_common._PROCESS_DOCS
    assert doc in cad_common._TOUCHED_DOCS
    r, _ = _call("close_part", {"doc_id": doc, "discard": True})
    assert r.get("ok") is True, r
    ids = {e.get("doc_id") for e in cb.call("doc_list", {}).get("docs", [])}
    assert doc not in ids, "discard must actually close the doc"
    assert doc not in cad_common._LAST_VOLUME, "discard must evict the volume baseline"
    assert doc not in cad_common._PROCESS_DOCS, "discard must evict process ownership"
    assert doc not in cad_common._TOUCHED_DOCS, "discard must evict touched-doc state"


def test_close_part_clean_after_save():
    """A saved (clean) doc closes without the unsaved-changes guard tripping."""
    import os
    import tempfile
    doc = _new_plate_with_hole()
    out = os.path.join(tempfile.mkdtemp(), "clean")
    s, _ = _call("save_part", {"path": out, "doc_id": doc})
    assert s["ok"], s
    r, _ = _call("close_part", {"doc_id": doc})
    assert r.get("ok") is True and r.get("code") != "unsaved_changes", r
    os.unlink(s["path"])


def test_export_step_plus_alias_is_native():
    """export_part('step+') is accepted as an alias and routed to the native saver."""
    import os
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step+", "doc_id": doc})
    assert r["ok"] and r.get("native") and r["format"] == "step+", r
    assert r["path"].lower().endswith(".step+") and r["exists"], r
    os.unlink(r["path"])


def test_drawing_overhaul_gdt_section_export():
    """The drawing overhaul end-to-end via the MCP tools: make a drawing with an auto half
    section, add a GD&T frame + datum + surface finish, render it, and export DXF (the GD&T
    strokes must be pooled into the export, not just the projected geometry)."""
    import os
    doc = _new_plate_with_hole()
    mk, _ = _call("make_drawing", {
        "doc_id": doc,
        "views": ["front", "top", "right", "iso"],
        "sections": [{"parent": "front", "axis": "vertical", "mode": "half"}],
        "title": {"title": "PLATE", "material": "AL 6061"},
    })
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    assert dwg

    # The drawing has a section view with hatch (engine-computed).
    full = cb.call("drawing_get", {"doc_id": dwg})
    views = full["sheets"][0]["views"]
    base = next(v for v in views if v.get("type") == "base")
    sec = next((v for v in views if v.get("type") == "section"), None)
    assert sec is not None, "auto section not created"
    assert len(sec.get("section_faces", [])) > 0
    assert len(sec.get("hatch", [])) > 0
    bb = base["bbox"]
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2

    g, _ = _call("add_gdt", {
        "doc_id": dwg, "view_id": base["view_id"], "symbol": "position",
        "tolerance": "0.1", "diameter": True, "modifier": "mmc",
        "datums": ["A", "B"], "a": [cx, cy], "pos": [bb[2] + 15, bb[3]],
    })
    assert g["ok"], g
    d2, _ = _call("add_datum", {"doc_id": dwg, "view_id": base["view_id"], "label": "A",
                                "a": [bb[0], bb[1]], "pos": [bb[0], bb[1] - 12]})
    assert d2["ok"], d2
    sf, _ = _call("add_surface_finish", {"doc_id": dwg, "view_id": base["view_id"], "ra": "1.6",
                                         "a": [bb[2], cy], "pos": [bb[2] + 10, cy + 8]})
    assert sf["ok"], sf

    # set_view / set_titleblock must report the RESULTING state, not a bare ok (audit fix).
    sv, _ = _call("set_view", {"doc_id": dwg, "view_id": base["view_id"], "x": 77.0, "y": 88.0,
                               "scale": 0.5})
    assert sv["ok"] and sv["position"] == [77.0, 88.0] and sv["scale"] == 0.5, sv

    tb, _ = _call("set_titleblock", {"doc_id": dwg, "fields": {"rev": "B", "drawn_by": "AI"}})
    assert tb["ok"] and tb["fields_set"] == ["drawn_by", "rev"], tb
    assert tb["title_block"]["rev"] == "B" and tb["title_block"]["drawn_by"] == "AI", tb
    # prior title field (set by make_drawing's title=) must survive — full state, not a diff.
    assert tb["title_block"].get("title") == "PLATE", tb

    ex, _ = _call("export_drawing", {"fmt": "dxf", "doc_id": dwg})
    assert ex["ok"], ex
    assert ex["entities"] > 0
    assert os.path.exists(ex["path"])
    os.unlink(ex["path"])


# ── add_section_view's axis/mode discriminators ──────────────────────────────────────
#
# The defect: `cut = horizontal_line if axis == "horizontal" else vertical_line`. Every
# value that was not the exact string "horizontal" — "vert", "y", even "x" and "diagonal" —
# produced a VERTICAL cut, and a section on the wrong plane looks entirely normal on the
# sheet. `mode` was the same class: core/doc/drawing_session.cpp:926 honours only
# "full"/"half" and silently drops anything else back to a full section.

def test_section_axis_and_mode_vocabulary_is_total():
    """Pure vocabulary check — no engine. Every accepted spelling maps to a REAL cut, and
    nothing falls through to a default."""
    from services.mcp.cad_drawing_tools import _canon_section_axis, _canon_section_mode

    for spelling in ("horizontal", "Horizontal", " HORIZ ", "horiz", "hor", "h",
                     "horizontally", "horizontal-cut"):
        assert _canon_section_axis(spelling) == ("horizontal", None), spelling
    for spelling in ("vertical", "VERTICAL", "vert", "v", "vertically", "vertical cut"):
        assert _canon_section_axis(spelling) == ("vertical", None), spelling

    # Axis LETTERS are refused, not guessed: "along x" and "normal to x" are opposite cuts,
    # and the cut line lives in the parent view's sheet frame, not model space.
    for letter in ("x", "y", "+z", "-y"):
        val, bad = _canon_section_axis(letter)
        assert val is None, letter
        assert "ambiguous" in bad["error"] and letter in bad["error"], bad
        assert bad["valid_axis"] == ["horizontal", "vertical"], bad

    for junk in ("diagonal", "", None, "auxiliary", "42"):
        val, bad = _canon_section_axis(junk)
        assert val is None, junk
        assert bad["valid_axis"] == ["horizontal", "vertical"], bad
        assert "horizontal" in bad["hint"] and "vertical" in bad["hint"], bad

    for spelling in ("full", "Full", "full_section", "FULLSECTION", "whole"):
        assert _canon_section_mode(spelling) == ("full", None), spelling
    for spelling in ("half", "Half Section", "half-section", "halfsection"):
        assert _canon_section_mode(spelling) == ("half", None), spelling
    # "partial" is a DIFFERENT drafting construct (broken-out) — mapping it onto half would
    # be the same silent-wrong-answer bug wearing a synonym.
    for junk in ("partial", "quarter", "", None, "broken_out"):
        val, bad = _canon_section_mode(junk)
        assert val is None, junk
        assert bad["valid_mode"] == ["full", "half"], bad


def _plate_drawing() -> str:
    doc = _new_plate_with_hole()
    mk, _ = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                   "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    return mk["drawing_doc_id"]


def _section_views(dwg: str) -> list:
    full = cb.call("drawing_get", {"doc_id": dwg})
    return [v for v in full["sheets"][0]["views"] if v.get("type") == "section"]


def test_section_view_axis_selects_a_real_and_DIFFERENT_cut():
    """Geometry proof, not ok:true — the two axes must produce different cut lines AND
    different projected section geometry, so 'which axis' is an answer that matters."""
    dwg = _plate_drawing()
    h, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front", "axis": "horizontal"})
    v, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front", "axis": "vertical"})
    assert h["ok"] and v["ok"], (h, v)
    assert h["axis"] == "horizontal" and v["axis"] == "vertical", (h, v)

    # The cut line itself: constant y spanning x, vs constant x spanning y.
    (hx0, hy0), (hx1, hy1) = h["cut_line"]
    (vx0, vy0), (vx1, vy1) = v["cut_line"]
    assert hy0 == hy1 and hx0 < hx1, h["cut_line"]   # horizontal cutting LINE
    assert vx0 == vx1 and vy0 < vy1, v["cut_line"]   # vertical cutting LINE
    assert h["cut_line"] != v["cut_line"]

    secs = {s["view_id"]: s for s in _section_views(dwg)}
    sh, sv = secs[h["view_id"]], secs[v["view_id"]]
    # …and the resulting projected sections genuinely differ (a 60x40x10 plate cut the two
    # ways gives different cut faces / hatch / extents).
    assert sh["bbox"] != sv["bbox"], (sh["bbox"], sv["bbox"])
    assert (len(sh.get("section_faces", [])), len(sh.get("hatch", []))) != \
           (len(sv.get("section_faces", [])), len(sv.get("hatch", []))), (sh, sv)

    # Spellings a model actually writes land on the same real cut, not on a default.
    hh, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front", "axis": "HORIZ"})
    assert hh["ok"] and hh["cut_line"] == h["cut_line"], hh


def test_section_view_rejects_bad_axis_without_adding_a_view():
    """An out-of-vocabulary axis errors NAMING the valid set, and adds nothing. 'y' — which
    the affix rule reaches via section_axis='y' — used to silently yield the vertical cut."""
    dwg = _plate_drawing()
    before = len(_section_views(dwg))
    for bad_axis in ("diagonal", "y", "x"):
        r, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front", "axis": bad_axis})
        assert r["ok"] is False, (bad_axis, r)
        assert r["valid_axis"] == ["horizontal", "vertical"], r
        assert bad_axis in r["error"], r
        assert len(_section_views(dwg)) == before, (bad_axis, "a view was added anyway")


def test_section_view_mode_is_honoured_and_validated():
    """full vs half produce different section geometry; an unknown mode errors instead of
    silently degrading to full."""
    dwg = _plate_drawing()
    f, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front",
                                      "axis": "vertical", "mode": "full"})
    hf, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front",
                                       "axis": "vertical", "mode": "Half Section"})
    assert f["ok"] and hf["ok"], (f, hf)
    assert f["mode"] == "full" and hf["mode"] == "half", (f, hf)
    secs = {s["view_id"]: s for s in _section_views(dwg)}
    sf, sh_ = secs[f["view_id"]], secs[hf["view_id"]]
    # Same cut line, different result: the half section leaves half the part unsectioned.
    assert f["cut_line"] == hf["cut_line"], (f, hf)
    assert len(sf.get("hatch", [])) != len(sh_.get("hatch", [])), (sf, sh_)

    before = len(_section_views(dwg))
    for bad_mode in ("quarter", "partial"):
        r, _ = _call("add_section_view", {"doc_id": dwg, "parent": "front",
                                          "axis": "vertical", "mode": bad_mode})
        assert r["ok"] is False, (bad_mode, r)
        assert r["valid_mode"] == ["full", "half"], r
        assert bad_mode in r["error"], r
        assert len(_section_views(dwg)) == before, (bad_mode, "a view was added anyway")


def test_make_drawing_rejects_a_bad_section_discriminator():
    """The engine's auto-section path (drawing_session.cpp:2048) has the identical
    fall-through, so make_drawing's `sections` spec is validated the same way."""
    doc = _new_plate_with_hole()
    r, _ = _call("make_drawing", {"doc_id": doc, "views": ["front"],
                                  "sections": [{"parent": "front", "axis": "diagonal"}]})
    assert r["ok"] is False and "sections[0]" in r["error"], r
    assert r["valid_axis"] == ["horizontal", "vertical"], r
    # A good spec still builds, with the spelling canonicalised on the way in.
    ok, _ = _call("make_drawing", {"doc_id": doc, "views": ["front"], "model_items": False,
                                   "hole_callouts": False,
                                   "sections": [{"parent": "front", "axis": "vert",
                                                 "mode": "half"}]})
    assert ok["ok"], ok
    assert len(_section_views(ok["drawing_doc_id"])) == 1, ok


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


def test_transform_targets_named_non_last_body():
    """transform with a body SELECTOR OBJECT moves THAT named body, not the current/last one (T-1).

    Regression for the assembly bug: a canonical body selector object
    {"kind":"body","owner_feature":"fN"} was not resolved by body_ref (it only honored a
    bare string), so transform silently fell back to the last-imported body — every
    batched per-body move stacked onto one body. Here we lift the FIRST (non-last) body
    and confirm *it* moved while the later copy stayed put.
    """
    r, _ = _call("new_part", {"name": "tgt"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B1", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    first_id = r1["features"][0]["id"]
    # A moved copy off in +X becomes the current/last body (the wrong target if the bug bites).
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [100, 0, 0], "copy": True})]})
    # Target the FIRST body by a canonical selector OBJECT (top-level body field) and lift it +1000 Z.
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "LiftFirst", {"translate": [0, 0, 1000]},
                body={"kind": "body", "owner_feature": first_id})]})
    assert r3["ok"], r3
    m, _ = _call("measure_part", {"doc_id": doc})
    bodies = m["mass"]["bodies"]
    assert len(bodies) == 2, m
    near0 = next(b for b in bodies if abs(b["com"][0]) < 50)     # the original first body
    near100 = next(b for b in bodies if b["com"][0] > 50)        # the moved copy
    assert near0["com"][2] > 900, ("the named first body should have lifted", bodies)
    assert near100["com"][2] < 100, ("the copy must NOT have moved", bodies)


def test_transform_targets_named_body_string():
    """Same T-1 guard via a plain top-level body id STRING (the documented cheatsheet form)."""
    r, _ = _call("new_part", {"name": "tgt2"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B1", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    first_id = r1["features"][0]["id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [100, 0, 0], "copy": True})]})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "LiftFirst", {"translate": [0, 0, 1000]}, body=first_id)]})
    m, _ = _call("measure_part", {"doc_id": doc})
    bodies = m["mass"]["bodies"]
    near0 = next(b for b in bodies if abs(b["com"][0]) < 50)
    near100 = next(b for b in bodies if b["com"][0] > 50)
    assert near0["com"][2] > 900 and near100["com"][2] < 100, bodies


def test_second_transform_composes_not_duplicates():
    """Issue 4: a second copy:false transform that targets a body a PRIOR transform already moved
    must compose on the placed body — not re-read the original and spawn a duplicate."""
    r, _ = _call("new_part", {"name": "compose"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]
    m0, _ = _call("measure_part", {"doc_id": doc})
    vol0 = m0["mass"]["volume_mm3"]
    # First in-place move (consumes fid, produces a placed body).
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Place", {"translate": [100, 0, 0]}, body=fid)]})
    # Second transform STILL targeting the ORIGINAL fid, copy:false -> must compose on the placed
    # body. body_count stays 1, volume unchanged (no duplicate), and both moves accumulate.
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Nudge", {"translate": [0, 0, 50]}, body=fid)]})
    assert r3["ok"] and r3["body_count"] == 1, r3
    m1, _ = _call("measure_part", {"doc_id": doc})
    assert abs(m1["mass"]["volume_mm3"] - vol0) < 1e-6, (m1["mass"]["volume_mm3"], vol0)
    com = m1["mass"]["com"]
    assert com[0] > 90 and com[2] > 40, com  # composed both translations onto one body


def test_config_override_accepts_a_vector():
    """Issue 5: set_config_override accepts a LIST value (a transform translate) so one file can
    hold configs differing only in a placement vector."""
    r, _ = _call("new_part", {"name": "cfgvec"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)})]})
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Place", {"translate": [0, 0, 0]})]})
    tid = r2["features"][-1]["id"]
    _call("add_configuration", {"name": "Stowed", "doc_id": doc})
    ov, _ = _call("set_config_override", {"config": "Stowed", "feature_id": tid,
                                          "param": "translate", "value": [0, 0, 50], "doc_id": doc})
    assert ov["ok"] and ov["value"] == [0.0, 0.0, 50.0], ov
    act, _ = _call("activate_configuration", {"name": "Stowed", "doc_id": doc})
    assert act["ok"], act
    m, _ = _call("measure_part", {"doc_id": doc})
    assert m["mass"]["com"][2] > 40, m  # the body lifted in the Stowed config


def test_get_part_summary_is_lightweight_and_shares_the_full_layout():
    """Issue 8: get_part(summary=True) lists feature id/name/type/params with no tessellation.
    Both forms nest the list at tree.features: a summary read taught `["features"]` and the
    next full read raised KeyError (2026-10-10)."""
    doc = _new_plate_with_hole()
    summ, _ = _call("get_part", {"doc_id": doc, "summary": True})
    full, _ = _call("get_part", {"doc_id": doc})
    assert summ["ok"] and summ["summary"] is True, summ
    assert full["ok"] and full["doc_id"] == summ["doc_id"] == doc, full
    assert summ["feature_count"] >= 1, summ
    assert ([f["id"] for f in summ["tree"]["features"]]
            == [f["id"] for f in full["tree"]["features"]]), (summ, full)
    f0 = summ["tree"]["features"][0]
    assert set(("id", "name", "type", "params")).issubset(f0), f0


def test_get_part_summary_keeps_a_long_list_a_list_and_names_the_cut():
    """c243: a summary turned a 70-entity sketch into the string "<list of 70 elided>", and
    `for e in params["entities"]` iterated characters. The list must stay a list, and the
    feature must say how much of it the summary dropped."""
    r, _ = _call("new_part", {"name": "summary_long_list"})
    doc = r["doc_id"]
    n = 70
    pts = [[20 * math.cos(2 * math.pi * i / n), 20 * math.sin(2 * math.pi * i / n)]
           for i in range(n)]
    built, _ = _call("add_features", {"doc_id": doc, "ops": [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Ring", "refs": {"plane": "plane.top"}, "params": {
            "entities": [{"id": f"e{i + 1}", "type": "line", "construction": False,
                          "p1": pts[i], "p2": pts[(i + 1) % n]} for i in range(n)],
            "constraints": []}}}]})
    assert built["ok"], built
    summ, _ = _call("get_part", {"doc_id": doc, "summary": True})
    sketch = summ["tree"]["features"][0]
    entities = sketch["params"]["entities"]
    assert isinstance(entities, list) and all(isinstance(e, dict) for e in entities), entities
    assert sketch["truncated_lists"]["params.entities"] == {"kept": len(entities), "total": n}
    assert 0 < len(entities) < n
    full, _ = _call("get_part", {"doc_id": doc})
    assert len(full["tree"]["features"][0]["params"]["entities"]) == n


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
    r1 = _solve("run_cfd_flow", {"doc_id": doc, "velocity": 10.0, "cells": 64, "n_steps": 1200})
    r2 = _solve("run_cfd_flow", {"doc_id": doc, "velocity": 20.0, "cells": 64, "n_steps": 1200})
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


def test_run_cfd_accurate_dispatches():
    """run_cfd_accurate either starts an OpenFOAM job (returns a job_id) or reports it's
    unavailable — both are valid; we don't wait for the multi-minute solve here."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [cb.feat("sphere", "S", {"radius": cb.dim(40)})]})
    out, _ = _call("run_cfd_accurate", {"doc_id": doc, "velocity": 15.0, "preset": "coarse"})
    if not out["ok"]:
        assert "OpenFOAM" in out.get("error", ""), out  # graceful "not available"
        return
    assert out.get("job_id") and out["status"] == "running", out
    g, _ = _call("get_job", {"job_id": out["job_id"]})
    assert g["status"] in ("running", "done", "error"), g


def test_fea_static_material_scaling_f4():
    """F4: aluminum must deflect ~2.86x MORE than steel under the same load (E_steel/E_Al ≈ 200/70).

    Bug F4: non-steel materials were 3.6x too stiff — aluminum deflected LESS than steel because
    the material name was not being correctly resolved to a properties dict before the solve.
    Fix: _resolve_material always returns a fresh copy of the _MATERIALS dict and validates that
    E_mpa > 0 so a bad entry is caught in Python rather than as a cryptic C++ error.

    This test catches any regression where the wrong E (e.g. steel's 200 GPa) is used for
    aluminum instead of the correct 69 GPa.  The cantilever is the same geometry and load for
    both solves; the deflection ratio must equal E_steel / E_Al to within 15% (the tolerance
    accommodates the coarse mesh and boundary-layer effects, but 3.6x too stiff collapses the
    ratio to ~1 and would fail instantly).
    """
    _require_sim()
    r, _ = _call("new_part", {"name": "mat_scale"})
    doc = r["doc_id"]
    # A simple 80×20×10 mm cantilever beam.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Beam",
                {"length": cb.dim(80), "width": cb.dim(20), "height": cb.dim(10)})]})

    common = {
        "doc_id": doc,
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -300]}],
    }
    rs = _solve("run_fea_static", {**common, "material": "steel"})
    ra = _solve("run_fea_static", {**common, "material": "aluminum"})
    assert rs["ok"], f"steel solve failed: {rs}"
    assert ra["ok"], f"aluminum solve failed: {ra}"

    delta_steel = rs["summary"]["max_disp_mm"]
    delta_al = ra["summary"]["max_disp_mm"]
    assert delta_steel > 0, f"steel deflection is zero: {rs}"
    assert delta_al > 0, f"aluminum deflection is zero: {ra}"

    # Aluminum (E≈69 GPa) must deflect MORE than steel (E≈200 GPa) — the bug caused it to
    # deflect LESS (backwards), which maps to a ratio < 1.
    assert delta_al > delta_steel, (
        f"F4 regression: aluminum deflects LESS than steel (delta_al={delta_al:.4f} mm < "
        f"delta_steel={delta_steel:.4f} mm). Material resolution is broken — check _MATERIALS "
        f"and _resolve_material in cad_tools.py."
    )

    # The ratio must be close to E_steel / E_Al = 200000 / 69000 ≈ 2.90.
    E_steel, E_al = 200000.0, 69000.0
    expected_ratio = E_steel / E_al  # ≈ 2.90
    actual_ratio = delta_al / delta_steel
    assert abs(actual_ratio - expected_ratio) / expected_ratio < 0.15, (
        f"F4: deflection ratio {actual_ratio:.3f} differs from expected {expected_ratio:.3f} "
        f"by more than 15%  (delta_steel={delta_steel:.4f}, delta_al={delta_al:.4f}). "
        f"A ratio ~1 means both materials are being solved with the SAME (steel) E."
    )


def test_fea_modal_free_free_f6():
    """F6: run_fea_modal(fixtures=[]) must NOT return 'no fixture faces resolved' — it should
    call the C++ solver with no constraints and return rigid-body + elastic modes.

    Bug F6: the Python guard `if not fixture_sets: return error` fired for empty-fixture calls
    even though the C++ fea_modal_solve handles unconstrained (free-free) problems correctly.
    Fix: only reject when fixtures were PROVIDED but none resolved; skip the guard when
    fixtures=[] (free-free is intentional).

    Verified behavior from the live C++ solver on a 60×20×10 mm box:
    - ok:True (not an error)
    - Returns the requested n_modes frequencies
    - At least the first few modes are near 0 Hz (rigid-body; the shift-invert solver may
      collapse all 6 near-zero modes into fewer numerical zeros depending on spectral shift)
    - All remaining (elastic) modes are strictly positive
    """
    _require_sim()
    r, _ = _call("new_part", {"name": "free_free"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block",
                {"length": cb.dim(60), "width": cb.dim(20), "height": cb.dim(10)})]})

    # Free-free: no fixture faces.  Before the F6 fix this returned {"ok":False,
    # "error":"no fixture faces resolved"} without ever calling the C++ solver.
    rf = _solve("run_fea_modal", {"doc_id": doc, "fixtures": [], "n_modes": 12,
                                  "material": "steel"})
    assert rf["ok"], (
        f"F6 regression: free-free modal returned an error — the Python guard is still "
        f"blocking unconstrained solves.  Error: {rf.get('error', rf)}"
    )
    freqs = rf.get("freqs_hz", [])
    assert len(freqs) >= 6, (
        f"expected at least 6 modes from a free-free solve, got {len(freqs)}: {freqs}"
    )

    # At least the first mode must be near 0 Hz (a rigid-body mode).  We use a generous
    # 1 Hz threshold to accommodate numerical noise; the exact count of near-zero modes
    # depends on the spectral shift the solver uses.
    assert abs(freqs[0]) < 1.0, (
        f"F6: mode 0 has frequency {freqs[0]:.4f} Hz — expected a rigid-body mode near 0 Hz. "
        f"All modes: {freqs}"
    )

    # Count near-zero (rigid-body) modes and confirm the rest are positive elastic modes.
    near_zero = [f for f in freqs if abs(f) < 1.0]
    elastic = [f for f in freqs if abs(f) >= 1.0]
    assert len(near_zero) >= 1, f"no near-zero rigid-body modes found: {freqs}"
    for f in elastic:
        assert f > 0, (
            f"elastic mode has non-positive frequency {f} Hz — expected all elastic "
            f"modes to be > 0. Rigid-body modes: {near_zero}, elastic: {elastic}"
        )


def test_measure_between_single_face_area():
    """measure_between(one face query) returns that face's area."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "doc_id": doc})
    assert r["ok"], r
    assert r["kind"] == "area"
    # The plate top is 60x40 minus an 8mm hole's footprint; area is just under 2400.
    assert 2000 < r["area"] < 2400, r
    assert "mm^2" in r["summary"], r


def test_measure_between_two_faces_distance():
    """measure_between(top, bottom) returns the through-thickness distance (= plate height 10)."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "b_query": "bottom", "doc_id": doc})
    assert r["ok"], r
    assert abs(r["distance"] - 10.0) < 1e-6, r
    assert "distance" in r["summary"], r


def test_measure_between_two_faces_angle():
    """Adjacent orthogonal faces measure a 90-degree angle."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "b_query": "front", "doc_id": doc})
    assert r["ok"], r
    assert abs(r["angle"] - 90.0) < 1e-6, r


def test_measure_between_ambiguous_query_is_rejected():
    """A query matching several faces/edges is rejected with a helpful message (not a wrong number)."""
    doc = _new_plate_with_hole()
    # "all the vertical edges" matches 4 -> must not silently measure one.
    r, _ = _call("measure_between", {"a_query": {"of": "edge", "parallel": "z"}, "doc_id": doc})
    assert r["ok"] is False, r
    assert "matched" in r["error"].lower() or "candidate" in r["error"].lower(), r


def test_check_interference_single_part_clean():
    """A single-body part trivially reports no interference."""
    doc = _new_plate_with_hole()
    r, _ = _call("check_interference", {"doc_id": doc})
    assert r["ok"] is True and r["clear"] is True, r
    assert r["interferences"] == [], r
    assert len(r["parts"]) == 1, r


def test_check_interference_overlapping_bodies():
    """Two overlapping copies of a body are flagged as an interference with an overlap volume."""
    r, _ = _call("new_part", {"name": "clash"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    # A copy shifted only 10mm in X overlaps the original.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [10, 0, 0], "copy": True})]})
    r2, _ = _call("check_interference", {"doc_id": doc})
    # The check completed: interference is a finding (clear=false), not a tool failure.
    assert r2["ok"] is True and r2["complete"] is True and r2["clear"] is False, r2
    assert r2["summary"].startswith("Interference found"), r2
    assert len(r2["interferences"]) == 1, r2
    assert r2["interferences"][0]["volume"] > 0, r2


def test_check_interference_incomplete_check_is_a_failure_not_clear(monkeypatch):
    """A pair that failed to evaluate makes the check incomplete: ok=false and never clear."""
    r, _ = _call("new_part", {"name": "clash_incomplete"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Far", {"translate": [100, 0, 0], "copy": True})]})
    native_call = cb.call

    def fail_pair(method, params=None, **kwargs):
        report = native_call(method, params, **kwargs)
        if method == "eval_interference":
            pair = report["clearances"].pop()
            report.update(ok=False, complete=False, pairs_checked=0, pair_failures=[
                {"a": pair["a"], "b": pair["b"], "reason": "boolean_failed"}],
                summary="Interference check incomplete: 1 failed pair(s).")
        return report

    monkeypatch.setattr(cb, "call", fail_pair)
    r2, _ = _call("check_interference", {"doc_id": doc})
    assert r2["ok"] is False and r2["clear"] is False, r2
    assert r2["interferences"] == [] and "incomplete" in r2["error"], r2


def test_inspect_model_integrity_empty_document_is_not_complete_evidence():
    """A census with no solids must not masquerade as a complete clean model."""
    created, _ = _call("new_part", {"name": "IntegrityEmpty"})
    doc = created["doc_id"]
    try:
        out, _ = _call("inspect_model_integrity", {"doc_id": doc})
        assert out["ok"] is False and out["complete"] is False, out
        assert out["reason"] == "no_solids", out
        assert out["solids"] == [] and out["pairs"] == [], out
    finally:
        _call("close_part", {"doc_id": doc})


@pytest.mark.parametrize("invalid", [
    {"proximity_mm": float("nan")},
    {"proximity_mm": -0.01},
    {"pair_budget_s": 0.0},
    {"pair_budget_s": float("inf")},
])
def test_inspect_model_integrity_refuses_invalid_search_limits(invalid):
    """Invalid limits must refuse, never become an empty successful pair search."""
    doc = _new_plate_with_hole()
    try:
        out, _ = _call("inspect_model_integrity", {"doc_id": doc, **invalid})
        assert out["ok"] is False, out
        assert "error" in out, out
    finally:
        _call("close_part", {"doc_id": doc})


def test_inspect_model_integrity_sees_disconnected_solids_inside_component(tmp_path):
    """One fixed component can contain two gapped solids; a positive gap is not contact."""
    created, _ = _call("new_part", {"name": "IntegrityCompound"})
    part = created["doc_id"]
    asm = None
    try:
        built, _ = _call("add_features", {"doc_id": part, "ops": [
            cb.feat("box", "Block", {
                "length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)}),
            cb.feat("transform", "GappedCopy", {
                "translate": [10.02, 0, 0], "copy": True}),
        ]})
        assert built["ok"], built
        path = str(tmp_path / "integrity_compound.step+")
        cb.call("doc_save", {"doc_id": part, "path": path})
        asm = cb.call("doc_new", {"name": "IntegrityAssembly", "kind": "assembly"})["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
            {"op": "asm_insert_component", "source": {"path": path},
             "name": "FixedCompound", "fixed": True},
        ]})
        near, _ = _call("inspect_model_integrity", {
            "doc_id": asm, "proximity_mm": 0.05,
        })
        assert near["ok"] is True and near["complete"] is True, near
        assert len(near["solids"]) == 2, near
        assert len({s["component_id"] for s in near["solids"]}) == 1, near
        assert len(near["pairs"]) == 1, near
        pair = near["pairs"][0]
        assert pair["relation"] == "proximity", pair
        assert pair["distance_mm"] == pytest.approx(0.02, abs=1e-6), pair
        assert pair["overlap_volume_mm3"] == pytest.approx(0.0, abs=1e-9), pair
        ids = {s["id"] for s in near["solids"]}
        assert {pair["a"], pair["b"]} == ids, near
        assert len(near["islands"]) == 1, near

        separated, _ = _call("inspect_model_integrity", {
            "doc_id": asm, "proximity_mm": 0.01,
        })
        assert separated["ok"] is True and separated["complete"] is True, separated
        assert separated["pairs"] == [], separated
        assert set(separated["isolated_solids"]) == ids, separated
        assert len(separated["islands"]) == 2, separated
        assert {row["solid_id"] for row in separated["nearest"]} == ids, separated
        for row in separated["nearest"]:
            assert row["status"] == "exact", row
            assert {row["solid_id"], row["nearest_id"]} == ids, row
            assert row["distance_mm"] == pytest.approx(0.02, abs=1e-6), row
    finally:
        if asm is not None:
            _call("close_part", {"doc_id": asm})
        _call("close_part", {"doc_id": part})


def test_check_symmetry_centered_box():
    """A box centered on the origin is symmetric about all three principal planes."""
    r, _ = _call("new_part", {"name": "sym"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    for plane in ("top", "front", "right"):
        rr, _ = _call("check_symmetry", {"plane": plane, "doc_id": doc})
        assert rr["ok"] and rr["symmetric"] is True, (plane, rr)
        assert rr["deviation_mm3"] < 1e-3, (plane, rr)


def test_configurations_variant_changes_volume():
    """A configuration with a parameter override + a suppression yields a different built variant."""
    r, _ = _call("new_part", {"name": "variants"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]

    base, _ = _call("measure_part", {"doc_id": doc})
    base_vol = base["mass"]["volume_mm3"]
    assert abs(base_vol - 24000) < 1e-3, base

    # Default + a "Big" variant that doubles the length.
    lst, _ = _call("list_configurations", {"doc_id": doc})
    assert lst["configurations"] == ["Default"] and lst["active"] == "Default", lst

    a, _ = _call("add_configuration", {"name": "Big", "doc_id": doc})
    assert a["ok"] and "Big" in a["configurations"], a

    o, _ = _call("set_config_override", {"config": "Big", "feature_id": fid,
                                         "param": "length", "value": 80, "doc_id": doc})
    assert o["ok"], o

    act, _ = _call("activate_configuration", {"name": "Big", "doc_id": doc})
    assert act["ok"] and act["active"] == "Big" and act["body_count"] == 1, act

    big, _ = _call("measure_part", {"doc_id": doc})
    assert abs(big["mass"]["volume_mm3"] - 48000) < 1e-3, big  # length doubled -> volume doubled

    # Switch back to Default -> original volume restored.
    _call("activate_configuration", {"name": "Default", "doc_id": doc})
    back, _ = _call("measure_part", {"doc_id": doc})
    assert abs(back["mass"]["volume_mm3"] - base_vol) < 1e-3, back


def test_config_suppression_drops_a_feature():
    """Suppressing the only feature in a config removes the body in that variant."""
    r, _ = _call("new_part", {"name": "supp"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]
    _call("add_configuration", {"name": "Empty", "doc_id": doc})
    s, _ = _call("set_config_suppression", {"config": "Empty", "feature_id": fid,
                                            "suppressed": True, "doc_id": doc})
    assert s["ok"], s
    act, _ = _call("activate_configuration", {"name": "Empty", "doc_id": doc})
    assert act["ok"] and act["body_count"] == 0, act  # the box is suppressed -> no body


def test_solve_mates_rejects_a_part_but_world_bodies_lists_it():
    """solve_mates is assembly-only (clear error on a part); list_world_bodies now ALSO works on
    a multi-body part (Issue 9) — it reports each body's world bbox so an import_part-assembled
    part can be inspected without promoting it to a formal assembly doc."""
    doc = _new_plate_with_hole()
    sm, _ = _call("solve_mates", {"doc_id": doc})
    assert sm["ok"] is False and "assembl" in sm["error"].lower(), sm
    wb, _ = _call("list_world_bodies", {"doc_id": doc})
    assert wb["ok"] is True and wb.get("multibody_part") is True, wb
    assert wb["body_count"] >= 1 and wb["bodies"], wb
    assert wb["bodies"][0].get("bbox"), wb  # each body carries a world bbox


# ---------------------------------------------------------------- inspection suite
# These exercise the three INSPECTION RPCs that were filled in C++ (section area+moments,
# numeric wall thickness, moldability draft). They need the REBUILT DLL — the math was 0/empty
# before. After `build.bat` rebuilds + stages libchudware.dll they pass.

def test_inspect_section_real_area():
    """inspect_section cuts the part and returns the REAL cross-section area (not 0).

    A 60x40x20 box cut by the 'right' plane (YZ, through the origin -> mid-length) exposes a
    40(width) x 20(height) rectangle = 800 mm^2. Before the C++ fix this was hard-coded to 0.
    """
    r, _ = _call("new_part", {"name": "sec"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    s, _ = _call("inspect_section", {"plane": "right", "doc_id": doc})
    assert s["ok"], s
    # Right plane (YZ) slices the 40x20 face: 800 mm^2 (the box is centred on the origin).
    assert abs(s["area_mm2"] - 800.0) < 1.0, s
    assert "mm^2" in s["summary"], s
    # Second moments of a 40(b) x 20(h) rect about its centroid in the plane frame are nonzero.
    assert s["Ixx"] > 0 and s["Iyy"] > 0, s
    # The live run run-19345781609857 used the natural plane+offset spelling.  The
    # wrapper must forward that as the engine's arbitrary origin/normal plane instead of
    # rejecting a complete request at the tool boundary.
    shifted, _ = _call("inspect_section", {
        "plane": "right", "offset": 10, "doc_id": doc,
    })
    assert shifted["ok"] and abs(shifted["area_mm2"] - 800.0) < 1.0, shifted
    assert shifted["plane"]["origin"] == [10.0, 0.0, 0.0], shifted
    assert shifted["plane"]["normal"] == [1.0, 0.0, 0.0], shifted

    custom, _ = _call("inspect_section", {
        "origin": [0, 5, 0], "normal": [0, 1, 0], "doc_id": doc,
    })
    assert custom["ok"] and abs(custom["area_mm2"] - 1200.0) < 1.0, custom


def test_wall_thickness_numeric_on_shell():
    """wall_thickness returns a numeric min/mean/max — the thinnest wall of a shelled box.

    A 40x40x40 box shelled to 2 mm walls should report a min wall thickness ~2 mm. The inward
    ray-cast (Moller-Trumbore, two-sided) stops at the NEAREST opposite wall, so the thinnest
    wall is found regardless of triangle winding. Before the C++ fix only an aicm_path came
    back (no numbers).

    NOTE: the body must actually HAVE walls — i.e. an open shell with a removed face. A
    "closed" shell with NO removed face is, in the current engine, a solid shrunk block (the
    shell op needs at least one open face to leave a cavity), and a solid block's "thickness"
    is correctly its full span (~36 mm), not 2. So we remove the top face to make a real
    2 mm-walled bucket and assert the algorithm finds the 2 mm wall."""
    r, _ = _call("new_part", {"name": "wt"})
    doc = r["doc_id"]
    # Open hollow shell, 2 mm walls: remove the top face so there is a genuine cavity with
    # real inner + outer walls. thickness+outward are the documented params (see
    # cheatsheets/shell.md); outward:false hollows inward.
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)}),
        cb.feat("shell", "Hollow", {"thickness": cb.dim(2), "outward": False},
                {"remove": {"of": "face", "side": "top"}})]})
    # ASSERT THE SETUP. This test used to discard the batch result, so when the shell op
    # started dangling ("remove[0]: no candidates of matching kind" -> whole batch rolled
    # back -> 0 features) the only symptom was wall_thickness complaining about an EMPTY
    # document, which reads like a wall_thickness bug and is not one.
    assert a["ok"], a
    assert [f["ok"] for f in a["features"]] == [True, True], a
    # HAND-CHECKED GEOMETRY. Outer block 40^3 = 64000 mm^3. Shelling inward by t = 2 with
    # the +Z face removed leaves a cavity spanning 36 x 36 in x/y (40 - 2*2) and 38 in z
    # (40 - 2, open at the top) = 36*36*38 = 49248 mm^3. Wall volume = 64000 - 49248
    # = 14752 mm^3 exactly. This identity is what proves the walls really are 2 mm and the
    # top really is open -- without it, a shrunken solid block would also "have a volume".
    assert abs(a["volume_mm3"] - 14752.0) < 1.0, a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    assert w["ok"], w
    assert w["min_thickness_mm"] is not None, w
    # Every wall of that bucket (4 sides + floor) is exactly 2 mm, so the MINIMUM is 2.0.
    # The raycast runs on a tessellation, so allow 2% either way -- but no more: a band wide
    # enough to also admit the 36 mm interior span would prove nothing.
    assert abs(w["min_thickness_mm"] - 2.0) < 0.04, w
    # The thickest ray crosses the full 40 mm outer span of the block.
    assert abs(w["max_thickness_mm"] - 40.0) < 0.8, w
    assert w["mean_thickness_mm"] >= w["min_thickness_mm"], w
    assert w["max_thickness_mm"] >= w["mean_thickness_mm"], w
    assert "wall" in w["summary"].lower(), w


def _shelled_enclosure_ops(rounds: bool, bosses: bool = False) -> list[dict]:
    """120x80x40 box, outer vertical edges R5 (inner R2.5 after the shell), shelled 2.5 mm
    open-top; optionally four Ø6 bosses 30 tall on the floor with Ø2.5 x 25 pilot holes."""
    def f(typ, name, params, **kw):
        return {"op": "add_feature", "feature": {"type": typ, "name": name, "params": params, **kw}}
    ops = [f("box", "Body", {"length": 120, "width": 80, "height": 40}, at=[0, 0, 0],
             centered=False)]
    if rounds:
        ops.append(f("fillet", "CornerRounds", {"radius": 5},
                     refs={"edges": {"of": "edge", "surf": "line", "parallel": "z"}}))
    ops.append(f("shell", "Shell", {"thickness": 2.5},
                 refs={"remove": {"of": "face", "side": "top"}}))
    if bosses:
        pts = [(60 + sx * 50, 40 + sy * 30) for sx in (1, -1) for sy in (1, -1)]
        n = len(ops)
        ops += [f("sketch", "BossSk", {"entities": [{"type": "circle", "center": [x, y],
                                                    "radius": 3} for x, y in pts],
                                       "offset": 2.5}, refs={"plane": "plane.top"}),
                f("extrude", "Bosses", {"mode": "boss", "end": "blind", "depth": 30},
                  refs={"profile": [{"kind": "region", "sketch": f"${n}"}]})]
        ops += [f("hole", f"Pilot{i}", {"style": "simple", "diameter": 2.5, "end": "blind",
                                        "depth": 25, "world_points": [[x, y, 32.5]]},
                  refs={"face": {"of": "face", "surf": "flat", "near": [x, y, 32.5],
                                 "count": 1}})
                for i, (x, y) in enumerate(pts)]
    return ops


@pytest.mark.parametrize("rounds", [False, True], ids=["sharp", "R5_corners"])
def test_wall_thickness_of_a_uniform_shell_is_the_shell_thickness(rounds):
    """A uniform 2.5 mm shell reads 2.5 — with or without R5/R2.5 corner rounds. The ray
    origin sits 1e-3 inside the surface, so the wall is hit distance PLUS that offset (the
    engine once subtracted it, reading every wall 2e-3 mm thin)."""
    r, _ = _call("new_part", {"name": f"wt_shell_{int(rounds)}"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc, "ops": _shelled_enclosure_ops(rounds)})
    assert a["ok"], a
    th = cb.call("eval_thickness", {"doc_id": doc})
    # Planar walls are exact on the tessellation; the fillet strips carry chord error.
    assert th["min_thickness_mm"] == pytest.approx(2.5, abs=0.05 if rounds else 1e-3), th
    _call("close_part", {"doc_id": doc})


def test_wall_thickness_reports_a_genuine_thin_spot():
    """The same 2.5 mm shell with a 1.5 mm deep pocket cut into the floor from below: the
    floor there is 1.0 mm, and that is the minimum."""
    r, _ = _call("new_part", {"name": "wt_pocket"})
    doc = r["doc_id"]
    ops = _shelled_enclosure_ops(False) + [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "PocketSk", "params": {
            "entities": [{"type": "rectangle", "center": [60, 40], "width": 20,
                          "height": 10}]}, "refs": {"plane": "plane.top"}}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Pocket", "params": {
            "mode": "cut", "end": "blind", "depth": 1.5},
            "refs": {"profile": [{"kind": "region", "sketch": "$2"}]}}}]
    a, _ = _call("add_features", {"doc_id": doc, "ops": ops})
    assert a["ok"], a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    assert w["min_thickness_mm"] == pytest.approx(1.0, abs=1e-3), w
    assert w["thinnest"][0]["built_by"]["name"] == "Pocket", w
    sample = w["minimum_sample"]
    basis = w["measurement_basis"]
    # Boundary vertices on a real thin pocket still measure its floor. Boundary evidence
    # must not become a reason to drop this minimum or replace it with a percentile.
    assert math.dist(sample["ray_origin"], sample["exit_at"]) + basis["surface_offset_mm"] \
        == pytest.approx(1.0, abs=1e-3), w
    assert sample["face"] == w["thinnest"][0]["face"], w
    _call("close_part", {"doc_id": doc})


def test_wall_thickness_finds_the_boss_wall_around_a_pilot_hole():
    """The H6 enclosure: its shell is 2.5 mm, but a Ø6 boss bored Ø2.5 has a (6-2.5)/2 =
    1.75 mm radial wall — a real thinner wall than the shell, so it IS the minimum."""
    r, _ = _call("new_part", {"name": "wt_bosses"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc,
                                  "ops": _shelled_enclosure_ops(True, bosses=True)})
    assert a["ok"], a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    assert w["min_thickness_mm"] == pytest.approx(1.75, abs=0.02), w
    assert w["thinnest"][0]["built_by"]["name"] in {"Bosses", "Pilot0", "Pilot1", "Pilot2",
                                                    "Pilot3"}, w
    _call("close_part", {"doc_id": doc})


def test_wall_thickness_says_where_the_thinnest_wall_is():
    """c219: three sub-agents got only `min_thickness_mm` and bisected the tree —
    suppressing features one by one, ~25 round trips — to find which face held it.
    A 60x40x10 plate with a pocket cut to leave a 0.6 mm floor and a boss on top: the
    thinnest face must be the pocket floor, located on it and credited to the pocket."""
    r, _ = _call("new_part", {"name": "wt_where"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Plate",
                                          "params": {"length": 60, "width": 40, "height": 10}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Boss",
                                          "params": {"radius": 6, "height": 8},
                                          "at": [-20, 0, 9]}},
        {"op": "add_feature", "feature": {"type": "box", "name": "Pocket",
                                          "params": {"length": 20, "width": 10, "height": 9.4},
                                          "at": [10, 0, 0.3], "mode": "cut"}}]})
    assert a["ok"] and [f["ok"] for f in a["features"]] == [True, True, True], a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    spots = w["thinnest"]
    assert 1 <= len(spots) <= 3, w
    assert [s["thickness_mm"] for s in spots] == sorted(s["thickness_mm"] for s in spots)
    floor = spots[0]
    assert abs(floor["thickness_mm"] - 0.6) < 0.02, w
    assert abs(floor["thickness_mm"] - w["min_thickness_mm"]) < 1e-3, w
    assert floor["surf"] == "plane" and floor["built_by"]["name"] == "Pocket", w
    x, y, z = floor["at"]
    assert 0 - 1e-3 <= x <= 20 + 1e-3 and abs(y) <= 5 + 1e-3 and abs(z + 4.4) < 1e-3, w
    # Every other reported face is a different face and no thinner.
    assert len({s["face"] for s in spots}) == len(spots), w


def test_wall_thickness_locates_the_thin_wall_on_a_second_body():
    """A part with two solids (a plate and a separate 0.8 mm shim). eval_thickness
    measures one body per call, so every body must be measured, and the shim's face must
    be named and credited on ITS body, not aliased onto the plate's first faces."""
    r, _ = _call("new_part", {"name": "wt_two_bodies"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Plate",
                                          "params": {"length": 60, "width": 40, "height": 10}}},
        {"op": "add_feature", "feature": {"type": "box", "name": "Shim",
                                          "params": {"length": 20, "width": 20, "height": 0.8},
                                          "at": [100, 0, 0], "merge": False}}]})
    assert a["ok"] and a["body_count"] == 2, a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    shim = w["thinnest"][0]
    assert abs(shim["thickness_mm"] - 0.8) < 0.02, w
    assert shim["at"][0] > 80, w
    assert shim["built_by"]["name"] == "Shim" and shim["face"] and shim["surf"] == "plane", w
    assert shim["body"] == 2, w
    by_body = {b["body"]: b["min_thickness_mm"] for b in w["bodies"]}
    assert set(by_body) == {1, 2} and abs(by_body[2] - 0.8) < 0.02 and by_body[1] > 9, w
    sample = w["minimum_sample"]
    assert sample["body"] == 2 and sample["at"][0] > 80, w
    basis = w["measurement_basis"]
    assert math.dist(sample["ray_origin"], sample["exit_at"]) + basis["surface_offset_mm"] \
        == pytest.approx(0.8, abs=0.02), w
    assert basis["n_vertices"] == w["n_sampled"] + basis["n_unmeasured"], w


def test_wall_thickness_ignores_a_rim_ray_that_runs_along_a_ledge():
    """GripperGears (2026-10-10): a Ø8 pin end carrying a coaxial Ø7 collar read 0.5 mm.
    Each vertex on the Ø8 rim cast its ray IN the plane of the 0.5 mm ledge, on the solid
    boundary, and stopped at the collar foot (an ENTRY face). The ray origin now moves into
    its own face and only exit faces count, so the thinnest wall is the Ø7 collar itself."""
    r, _ = _call("new_part", {"name": "wt_pin_rim"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Pin",
                                          "params": {"radius": 4, "height": 10}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Collar",
                                          "params": {"radius": 3.5, "height": 5},
                                          "at": [0, 0, 7.5]}}]})
    assert a["ok"] and a["body_count"] == 1, a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    # The ledge is 0.5 mm wide, never 0.5 mm thick: the thinnest wall is the Ø7 collar
    # (7 mm across, less a small chord error).
    assert abs(w["min_thickness_mm"] - 7.0) < 0.15, w
    assert w["thinnest"][0]["built_by"]["name"] == "Collar", w
    _call("close_part", {"doc_id": doc})


@pytest.mark.parametrize("angle_deg", [65, 77])
def test_wall_thickness_retains_an_acute_edge_span_with_its_actual_ray(angle_deg):
    """A moderate acute edge has a genuine micron-scale EXIT, not a source-face self-hit.

    The triangular prism is millimetres thick away from its tapered edge. Keep that edge
    minimum, but expose enough evidence to distinguish it from an interior wall claim.
    """
    r, _ = _call("new_part", {"name": f"wt_edge_{angle_deg}"})
    doc = r["doc_id"]
    slope = math.tan(math.radians(angle_deg))
    points = [[0, 0], [100, 0], [100, 100 * slope]]
    entities = [_line(f"e{i + 1}", *points[i], *points[(i + 1) % 3]) for i in range(3)]
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Triangle",
            "params": {"entities": entities,
                       "constraints": [_rel(f"c{i + 1}", "fix", [f"e{i + 1}"])
                                       for i in range(3)]},
            "refs": {"plane": "plane.top"}}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "Wedge",
            "params": {"mode": "boss", "end": "blind", "depth": 4},
            "refs": {"profile": [{"kind": "region", "sketch": "$0"}]}}}]})
    assert a["ok"] and a["body_count"] == 1, a
    w, _ = _call("wall_thickness", {"doc_id": doc})
    assert w["ok"] and 0.002 < w["min_thickness_mm"] < 0.005, w
    sample, basis = w["minimum_sample"], w["measurement_basis"]
    assert sample["sample_location"] == "face_mesh_boundary", w
    assert sample["face"] == w["thinnest"][0]["face"], w
    assert math.hypot(*sample["at"][:2]) < 1e-5, w
    origin, exit_at, direction = (sample[key] for key in
                                  ("ray_origin", "exit_at", "inward_direction"))
    assert math.dist([0, 0, 0], direction) == pytest.approx(1), w
    distance = math.dist(origin, exit_at)
    assert distance + basis["surface_offset_mm"] \
        == pytest.approx(w["min_thickness_mm"], abs=1e-8), w
    assert [exit_at[k] - origin[k] for k in range(3)] \
        == pytest.approx([distance * direction[k] for k in range(3)], abs=1e-8), w
    # Origin and midpoint are strictly in the triangular material; exit lands on the
    # OTHER wedge face. A self-hit on the starting face cannot satisfy these inequalities.
    for fraction in (0, 0.5):
        x, y = [origin[k] + fraction * (exit_at[k] - origin[k]) for k in (0, 1)]
        assert 0 < x < 100 and 0 < y < slope * x, w
    x, y = exit_at[:2]
    assert min(abs(y), abs(slope * x - y)) < 1e-7, w
    assert abs(direction[2]) < 1e-8, w
    assert math.dist([0, 0, 0], sample["tangent_nudge"]) \
        == pytest.approx(basis["tangent_nudge_mm"], abs=1e-8), w
    _call("close_part", {"doc_id": doc})


def test_one_element_query_list_still_builds_a_shell():
    """A geometry query in a ONE-ELEMENT LIST must build the same geometry as the bare query.

    Regression pin for the cause of the failure above, which was NOT in wall_thickness. The
    two shapes reach two different engine resolvers: a bare {"of":"face","side":"top"} goes
    to resolve_geom_query (plain-English side/surf/nearest filters), a LIST goes to the
    topology-selector resolver, which answers "no candidates of matching kind". cad_doc_tools'
    ref normalizer wraps a bare selector-ish dict handed to a LIST slot (shell.remove,
    draft.faces) into a one-element list, so every agent-authored shell arrives in the shape
    the selector resolver rejects; add_features is atomic, so the batch rolled back and the
    document came out with ZERO features. What makes it work is the engine unwrapping that
    one-element query list back to the bare query in
    core/doc/validate.cpp::normalize_feature_shape -- and that unwrap is exactly what was
    MISSING from the staged libchudware.so while this test failed (source dated 2026-07-28,
    library linked 2026-07-27). So this pins the Python-wrap / C++-unwrap pair end to end, in
    the shape the normalizer actually produces, and fails loudly on a stale engine instead of
    surfacing as "wall_thickness says the document is empty".
    """
    r, _ = _call("new_part", {"name": "qlist"})
    doc = r["doc_id"]
    a, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)}),
        # Explicitly pre-wrapped: this is what cad_doc_tools hands the engine either way.
        cb.feat("shell", "Hollow", {"thickness": cb.dim(2), "outward": False},
                {"remove": [{"of": "face", "side": "top"}]})]})
    assert a["ok"], a
    assert [f["ok"] for f in a["features"]] == [True, True], a
    # Same hand-checked wall volume as the bare query above: 40^3 - 36*36*38 = 14752 mm^3.
    # An unwrap that resolved a DIFFERENT face (or none) would not land on this number.
    assert abs(a["volume_mm3"] - 14752.0) < 1.0, a
    # draft.faces routes through the same resolver pair, so pin it too.
    r2, _ = _call("new_part", {"name": "qlist2"})
    doc2 = r2["doc_id"]
    b, _ = _call("add_features", {"doc_id": doc2, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)}),
        cb.feat("draft", "Taper", {"angle": cb.dim(5)},
                {"faces": [{"of": "face", "side": "front"}], "neutral": "plane.top"})]})
    assert b["ok"], b
    assert [f["ok"] for f in b["features"]] == [True, True], b


def test_draft_analysis_flags_vertical_walls():
    """draft_analysis flags the 4 vertical walls of a box pulled along +Z (0deg draft < 3deg).

    A plain box's side walls are parallel to a +Z pull -> 0deg draft -> below the 3deg minimum.
    The top/bottom faces are perpendicular (90deg) -> fine. So exactly the 4 sides are flagged.
    """
    r, _ = _call("new_part", {"name": "dr"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    d, _ = _call("draft_analysis", {"pull_dir": [0, 0, 1], "min_draft_deg": 3.0, "doc_id": doc})
    assert d["ok"], d
    # The four vertical side walls have ~0deg draft -> below threshold; top+bottom are fine.
    assert d["n_faces_below_threshold"] == 4, d
    assert len(d["faces_below_threshold"]) == 4, d
    for f in d["faces_below_threshold"]:
        assert f["area_mm2"] > 0 and len(f["centroid"]) == 3, f
        assert f["min_draft_deg"] < 3.0, f
    assert "draft" in d["summary"].lower(), d


def test_draft_analysis_sphere_is_moldable():
    """A sphere has draft everywhere except a single silhouette ring; pulled along Z almost
    all faces clear a small threshold. We just assert the summary + fields are well-formed and
    that min/max draft span roughly -90..90 (a full range of surface orientations)."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [cb.feat("sphere", "S", {"radius": cb.dim(20)})]})
    d, _ = _call("draft_analysis", {"pull_dir": [0, 0, 1], "min_draft_deg": 1.0, "doc_id": doc})
    assert d["ok"], d
    assert d["min_draft_deg"] is not None and d["max_draft_deg"] is not None, d
    assert d["min_draft_deg"] < -45.0 and d["max_draft_deg"] > 45.0, d  # full orientation span


def test_new_part_seeds_volume_baseline_for_write_diff():
    """new_part seeds _LAST_VOLUME so the FIRST add_features reports a real volume delta
    (volume 0.0 -> X) instead of degrading to the no-baseline 'volume now X' branch."""
    from services.mcp import cad_tools
    r, _ = _call("new_part", {"name": "vbase"})
    doc = r["doc_id"]
    # the empty part's volume baseline is seeded (zero) so the write-diff has a before-value
    assert doc in cad_tools._LAST_VOLUME, "new_part must seed _LAST_VOLUME for the write-diff"
    assert cad_tools._LAST_VOLUME[doc] == 0.0, cad_tools._LAST_VOLUME[doc]
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(30), "width": cb.dim(20), "height": cb.dim(10)})]})
    assert r2["ok"], r2
    ch = r2.get("changes") or {}
    # a baseline was available -> the delta is reported as a number (not the no-baseline branch)
    assert "volume_delta_mm3" in ch, ch
    assert ch["volume_delta_mm3"] > 0.0, ch  # material added from an empty part
    assert cad_tools._LAST_VOLUME[doc] == r2["volume_mm3"], "baseline updated to the new volume"


# --- the write-diff must answer "did I build what I meant?" ------------------------------
# Live 2026-07-30 audit of `changes` across real op types found four ways it lied or went
# quiet. Each test below pins one of them with a geometry number.

def test_a_resumed_part_carrying_geometry_keeps_a_real_volume_baseline():
    """A RESUME must seed _LAST_VOLUME from the engine, not from a fabricated 0.0.

    Measured live 2026-07-30: neither doc_new(resume_if_open=True) nor doc_list reports a
    `volume`, so the old `setdefault(did, entry.get("volume", 0.0) or 0.0)` seeded 0.0 for
    every resume made from a process that had not itself built the part. A resumed
    48000 mm^3 box, then cut by 282.3 mm^3, reported "volume 0.0->47717.7 (+47717.7)" — the
    sign inverted and the magnitude 169x off, on the exact op the model was verifying."""
    from services.mcp import cad_tools
    r, _ = _call("new_part", {"name": "resumebase", "fresh": True})
    doc = r["doc_id"]
    built, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    assert built["ok"], built
    # Drop the in-process baseline: this is what a fresh MCP process / post-restart resume sees.
    cad_tools._LAST_VOLUME.pop(doc, None)
    again, _ = _call("new_part", {"name": "resumebase"})
    assert again["doc_id"] == doc and again.get("resumed"), again
    assert cad_tools._LAST_VOLUME[doc] == pytest.approx(48000.0, rel=1e-6), \
        cad_tools._LAST_VOLUME.get(doc)
    cut, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sketch", "S", {"plane": "top", "entities": [
            {"type": "circle", "center": [10, 10], "radius": 3}]}),
        cb.feat("extrude", "Hole", {"depth": cb.dim(30), "mode": "cut",
                                    "flip": True}, {"profile": "$0"})]})
    assert cut["ok"], cut
    # The cut REMOVED material: the delta must be negative and ~ -pi*9*10, not +47717.
    assert cut["changes"]["volume_delta_mm3"] == pytest.approx(-282.27, abs=0.5), cut["changes"]


def test_a_negligible_volume_change_is_not_reported_as_a_change():
    """A fillet of radius 1e-5 mm moves 0.0005 mm^3 of a 48000 mm^3 box (1e-8 of the body).

    That used to print "volume 48000.0->48000.0 mm^3 (-0.0)" — a line that claims a change,
    shows identical before/after numbers and a zero delta, and reads to the model as a
    successful fillet. It must name itself as effectively no material change instead."""
    r, _ = _call("new_part", {"name": "negligible", "fresh": True})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    tiny, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("fillet", "R", {"radius": cb.dim(1e-5)},
                {"edges": [{"of": "edge", "parallel": "z"}]})]})
    assert tiny["ok"], tiny
    ch = tiny["changes"]
    assert ch["faces_delta"] == 4, ch          # the topology really did change
    assert abs(ch["volume_delta_mm3"]) < 0.01, ch
    assert "effectively UNCHANGED" in ch["summary"], ch["summary"]
    assert "->" not in ch["summary"].split("part bbox")[0], ch["summary"]


def test_a_real_fillet_still_reads_as_a_real_volume_change():
    """The negligible-change floor is relative (1e-7 of the body), so it must not swallow a
    fillet a user would actually see: r=0.5 on the same box removes ~4.3 mm^3 (8e-5 of the
    body), four orders of magnitude clear of the floor."""
    r, _ = _call("new_part", {"name": "realfillet", "fresh": True})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    got, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("fillet", "R", {"radius": cb.dim(0.5)},
                {"edges": [{"of": "edge", "parallel": "z"}]})]})
    assert got["ok"], got
    ch = got["changes"]
    assert ch["volume_delta_mm3"] == pytest.approx(-4.317, abs=0.05), ch
    assert "48000.0->" in ch["summary"] and "effectively UNCHANGED" not in ch["summary"], \
        ch["summary"]


def test_an_engine_feature_warning_names_the_cause_in_the_changes_summary():
    """A sub-tolerance blend (radius < 1e-7, document.cpp) passes the body through and sets
    feature_status[].warning "fillet 1e-08 below tolerance; no material removed".

    That warning used to travel ONLY in features[], one entry in a column of nulls, while
    `changes.summary` — the line the ops cheatsheet tells the model to read — printed the
    generic guess "a cut/boolean may have missed". The engine already knew the answer."""
    r, _ = _call("new_part", {"name": "subtol", "fresh": True})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    got, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("fillet", "R", {"radius": cb.dim(1e-8)},
                {"edges": [{"of": "edge", "parallel": "z"}]})]})
    assert got["ok"], got
    warned = [f["warning"] for f in got["features"] if f.get("warning")]
    assert warned and "below tolerance" in warned[0], got["features"]
    summary = got["changes"]["summary"]
    assert "no geometry change" in summary, summary
    assert "below tolerance" in summary, summary
    # The generic guess must not be what the model reads when the engine gave a real reason.
    assert "may have missed" not in summary, summary


def test_a_warning_on_a_SUCCESSFUL_op_is_prefixed_onto_the_changes_summary():
    """The dangerous case is a warning on an op that otherwise reads as a clean success.

    A blind hole deeper than the stock is clamped to a THROUGH hole (document.cpp ~5460).
    Every number in the write-diff looks right — "added 1 face(s) (+1 cylinder); 1 new
    bore/hole(s); volume 48000.0->47435.5 (-564.5)" — and the model has no reason to look
    at features[] for the one line saying its blind pocket is now a through hole."""
    r, _ = _call("new_part", {"name": "clamped", "fresh": True})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    got, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "H", {"style": "simple", "diameter": cb.dim(6), "depth": cb.dim(999),
                              "end": "blind", "at": [10, 10]},
                {"face": {"of": "face", "side": "top"}})]})
    assert got["ok"], got
    ch = got["changes"]
    assert ch["bores_delta"] == 1 and ch["volume_delta_mm3"] < 0, ch   # it looks like a clean win
    assert ch["summary"].startswith("ENGINE WARNING: "), ch["summary"]
    assert "clamped to a through hole" in ch["summary"], ch["summary"]
    # …and the real deltas still follow the warning, not replaced by it.
    assert "1 new bore/hole(s)" in ch["summary"], ch["summary"]


def test_the_write_diff_carries_the_whole_part_bbox():
    """"Is the part the right size" must not cost a separate measure_part round trip.

    A boss sketched at [200,200] fuses cleanly and every delta looks healthy ("added 6
    face(s); volume 48000->49000 (+1000)") while the 60 mm part silently becomes 240 mm
    wide. Only the bbox says so."""
    r, _ = _call("new_part", {"name": "runaway", "fresh": True})
    doc = r["doc_id"]
    base, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    assert base["changes"]["bbox_mm"] == [60.0, 40.0, 20.0], base["changes"]
    assert "part bbox 60x40x20 mm" in base["changes"]["summary"], base["changes"]["summary"]
    far, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sketch", "S", {"plane": "top", "entities": [
            {"type": "rectangle", "corner1": [200, 200], "corner2": [210, 210]}]}),
        cb.feat("extrude", "Far", {"depth": cb.dim(10), "mode": "boss"}, {"profile": "$0"})]})
    assert far["ok"], far
    ch = far["changes"]
    assert ch["volume_delta_mm3"] == pytest.approx(1000.0, abs=1.0), ch   # looks healthy
    assert ch["bbox_mm"][0] == pytest.approx(240.0, abs=0.01), ch         # but it is not
    assert ch["bbox_mm"][1] == pytest.approx(230.0, abs=0.01), ch


def test_a_dress_op_does_not_report_the_body_it_dressed_as_a_dead_feature():
    """add_features' auto overlap warning must not cry wolf on every finishing op.

    eval_feature_overlap calls a feature dead when it "owns no surviving face", and a dress
    op takes ownership of the whole body: measured live 2026-07-30, an r2 fillet on a plain
    box moves all 6 of the box's faces onto the fillet, so add_features attached
    "1 dead feature(s)" naming the box — for a textbook-correct fillet. A real kill still
    travels as a pair, which names both ends and the shared volume."""
    r, _ = _call("new_part", {"name": "dressnoise", "fresh": True})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    dressed, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("fillet", "R", {"radius": cb.dim(2)},
                {"edges": [{"of": "edge", "parallel": "z"}]})]})
    assert dressed["ok"], dressed
    assert dressed["changes"]["volume_delta_mm3"] < 0, dressed["changes"]  # a real fillet
    assert "overlap_warnings" not in dressed, dressed["overlap_warnings"]
    # …while a cut that eats into an earlier feature still warns, naming both ends.
    r2, _ = _call("new_part", {"name": "dressnoise_cut", "fresh": True})
    doc2 = r2["doc_id"]
    _call("add_features", {"doc_id": doc2, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    cut, _ = _call("add_features", {"doc_id": doc2, "ops": [
        cb.feat("sketch", "S", {"plane": "top", "entities": [
            {"type": "rectangle", "corner1": [-2, -2], "corner2": [20, 20]}]}),
        cb.feat("extrude", "Kill", {"depth": cb.dim(20), "mode": "cut"}, {"profile": "$0"})]})
    assert cut["ok"], cut
    pairs = cut["overlap_warnings"]["pairs"]
    assert pairs and pairs[0]["a_name"] == "B" and pairs[0]["b_name"] == "Kill", pairs
    assert pairs[0]["volume_mm3"] == pytest.approx(4840.0, abs=1.0), pairs


def test_suppression_aliases_change_tree_state_and_echo_canonical_rewrites():
    """Natural suppress/unsuppress verbs must mutate, never fall through to value:false."""
    r, _ = _call("new_part", {"name": "suppression_alias", "fresh": True})
    doc = r["doc_id"]
    built, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(20), "width": cb.dim(20),
                                 "height": cb.dim(20)})]})
    assert built["ok"], built
    fid = built["id_map"]["$0"]
    feature = next(f for f in built["features"] if f["id"] == fid)
    assert feature == {
        "id": fid, "ok": True, "error": None, "warning": None,
        "name": "Block", "type": "box", "suppressed": False,
    }, built

    suppressed, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "suppress_feature", "id": fid}]})
    assert suppressed["ok"] and suppressed["interpreted"], suppressed
    tree = cb.call("doc_tree", {"doc_id": doc})["tree"]
    assert next(f for f in tree["features"] if f["id"] == fid)["suppressed"] is True

    unsuppressed, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "unsuppress_feature", "id": fid}]})
    assert unsuppressed["ok"] and unsuppressed["interpreted"], unsuppressed
    tree = cb.call("doc_tree", {"doc_id": doc})["tree"]
    assert next(f for f in tree["features"] if f["id"] == fid)["suppressed"] is False

    canonical_alias, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "set_suppressed", "id": fid, "suppressed": True}]})
    assert canonical_alias["ok"] and canonical_alias["interpreted"], canonical_alias
    tree = cb.call("doc_tree", {"doc_id": doc})["tree"]
    assert next(f for f in tree["features"] if f["id"] == fid)["suppressed"] is True


def test_one_batch_box_chamfer_keeps_upstream_ownership_noise_out_of_feedback():
    """A later dress feature owns faces; it does not make its same-batch box dead."""
    r, _ = _call("new_part", {"name": "same_batch_chamfer", "fresh": True})
    doc = r["doc_id"]
    result, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(20)}),
        cb.feat("chamfer", "Edge break", {"length": cb.dim(1)},
                {"edges": {"of": "edge", "side": "+z"}}),
    ]})
    assert result["ok"], result
    assert set(result["id_map"]) >= {"$0", "$1"}, result
    by_id = {f["id"]: f for f in result["features"]}
    assert by_id[result["id_map"]["$0"]]["name"] == "Block"
    assert by_id[result["id_map"]["$0"]]["type"] == "box"
    assert by_id[result["id_map"]["$0"]]["suppressed"] is False
    assert by_id[result["id_map"]["$1"]]["name"] == "Edge break"
    assert by_id[result["id_map"]["$1"]]["type"] == "chamfer"
    assert by_id[result["id_map"]["$1"]]["suppressed"] is False
    assert "overlap_warnings" not in result, result.get("overlap_warnings")


# --- new_part resume-or-mint is ATOMIC ---------------------------------------------------
# Live 2026-07-29, run-42508173312214: the trace holds ONE new_part("lexus_f_keychain")
# and the GUI grew THREE tabs (lexus_f_keychain, ...2, ...3). new_part used to decide
# "is it already open?" with a doc_list call and then mint with a separate doc_new, with
# nothing locked across the two — so when the user retried a request whose first worker
# was still inside its 5-minute timeout, both attempts reached the ONE machine-wide
# daemon, both saw the name free, and both minted. doc_new(resume_if_open=True) now makes
# the check and the mint one step under the engine's mint lock.

def _unique_part_name(tag: str) -> str:
    """A name no other document in the SHARED daemon can already answer to."""
    return f"{tag}_{uuid.uuid4().hex[:10]}"


def _in_series(doc_name: str, requested: str) -> bool:
    """`requested` and the sibling names doc_new mints for it.

    Mirrors core/doc/session.cpp unique_doc_name: it strips a TRAILING INTEGER to get
    the series base, so the siblings of "x1" are "x2"/"x3" and do NOT start with "x1".
    """
    n, req = (doc_name or "").strip().casefold(), (requested or "").strip().casefold()
    if not n or not req:
        return False
    if n == req:
        return True
    base = req.rstrip("0123456789") or req
    tail = n[len(base):] if n.startswith(base) else ""
    return bool(tail) and tail.isdigit()


def test_new_part_concurrent_same_name_yields_one_document():
    """Two THREADS calling new_part with the same name at the same instant get ONE doc.

    Sequential calls cannot cover this: the old code was correct whenever the first mint
    had already committed. The defect only exists in the window between the check and the
    mint, so the test has to put two callers inside it."""
    name = _unique_part_name("toctou")
    results: list = []
    barrier = threading.Barrier(2)

    def mint():
        barrier.wait()  # release both callers into the check-then-mint window together
        r, _ = _call("new_part", {"name": name})
        results.append(r)

    threads = [threading.Thread(target=mint) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(isinstance(r, dict) and r.get("ok") for r in results), results
    assert len({r["doc_id"] for r in results}) == 1, (
        f"two concurrent new_part({name!r}) calls minted two documents: {results}")
    # And the daemon really holds one — a second mint renamed to "<name>2" would satisfy a
    # doc_id check on the reply alone only if both replies pointed at it, so assert the pool.
    named = [d for d in cb.call("doc_list", {})["docs"]
             if (d.get("name") or "").startswith(name)]
    assert len(named) == 1, named


def test_new_part_fresh_still_mints_a_distinct_document():
    """fresh=True is the documented escape hatch: it must never resume, and never sweep."""
    name = _unique_part_name("freshdoc")
    first, _ = _call("new_part", {"name": name})
    second, _ = _call("new_part", {"name": name, "fresh": True})
    assert first["ok"] and second["ok"], (first, second)
    assert second["doc_id"] != first["doc_id"], second
    assert not second.get("reused"), second
    assert second["name"] != first["name"], second  # uniquified, and reported honestly
    # fresh=True asks for a document ALONGSIDE what is open, so the empty original stays.
    open_ids = {d["doc_id"] for d in cb.call("doc_list", {})["docs"]}
    assert first["doc_id"] in open_ids, "fresh=True must not discard the existing document"


def test_new_part_resume_reports_the_feature_count_it_resumed_onto():
    """The BUILT/UNBUILT split: a same-named part is
    resumed either way, but a BUILT one must announce what it carries. Resuming a
    half-built body SILENTLY is what produced twelve "boss added no material (it is buried
    inside the body)" failures, so feature_count and the build-on-them note are the
    contract, not decoration."""
    name = _unique_part_name("resume_built")
    first, _ = _call("new_part", {"name": name})
    doc = first["doc_id"]

    # UNBUILT: resumed, zero features, and NOT told to build on anything.
    empty, _ = _call("new_part", {"name": name})
    assert empty["doc_id"] == doc and empty.get("reused"), empty
    assert empty["feature_count"] == 0, empty
    assert "ALREADY CARRIES" not in empty["note"], empty

    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(30), "width": cb.dim(20), "height": cb.dim(10)})]})
    assert r["ok"], r

    # BUILT: same document, and the reply carries the count it resumed onto.
    built, _ = _call("new_part", {"name": name})
    assert built["doc_id"] == doc and built.get("reused"), built
    assert built["feature_count"] == 1, built
    assert "ALREADY CARRIES 1 feature(s)" in built["note"], built
    # A resume must never reset the volume baseline of a part that already has geometry.
    from services.mcp import cad_tools
    assert cad_tools._LAST_VOLUME[doc] == pytest.approx(6000.0, rel=1e-3), \
        cad_tools._LAST_VOLUME[doc]


def test_new_part_and_assembly_never_resume_another_projects_open_doc(tmp_path, monkeypatch):
    """Live 2026-09-27: an agent building "sixaxis_arm" in /tmp/sixaxis-run4 called
    new_assembly("sixaxis_arm"), got the user's Desktop assembly of that name (left open in
    the shared hostd) and added 13 components to it. A document saved OUTSIDE the turn's
    project is not a resume target; one saved inside it still is."""
    home, other = tmp_path / "home", tmp_path / "other"
    home.mkdir()
    other.mkdir()
    for kind in ("part", "assembly"):
        tool = "new_part" if kind == "part" else "new_assembly"
        name = _unique_part_name(f"scoped_{kind}")
        monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(home))
        first, _ = _call(tool, {"name": name})
        doc = first["doc_id"]
        if kind == "part":
            built, _ = _call("add_features", {"doc_id": doc, "ops": [
                cb.feat("box", "B", {"length": cb.dim(10), "width": cb.dim(10),
                                     "height": cb.dim(10)})]})
            assert built["ok"], built
        cb.call("doc_save", {"doc_id": doc, "path": str(home / f"{name}.step+")})

        same, _ = _call(tool, {"name": name})
        assert same["doc_id"] == doc and same.get("reused"), same

        monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(other))
        fresh, _ = _call(tool, {"name": name})
        assert fresh["doc_id"] != doc and not fresh.get("reused"), fresh
        docs = {e["doc_id"]: e for e in cb.call("doc_list", {})["docs"]}
        assert docs[doc]["revision"] == (1 if kind == "part" else 0), docs[doc]
        for d in (doc, fresh["doc_id"]):
            cb.call("doc_close", {"doc_id": d})


def test_new_assembly_fresh_escapes_a_reopened_assembly_of_that_name(tmp_path, monkeypatch):
    """Hard-benchmark build: new_assembly("hinge0") after the deliverable "hinge0" (three
    components) was opened returned it reused and the next asm_add_component calls piled
    onto its three. The reused reply must name the component count and the way out;
    fresh=True must mint a separate EMPTY assembly, as new_part(fresh=True) does."""
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    part, _ = _call("new_part", {"name": _unique_part_name("hinge_leaf")})
    r, _ = _call("add_features", {"doc_id": part["doc_id"], "ops": [
        cb.feat("box", "B", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(2)})]})
    assert r["ok"], r
    leaf = tmp_path / "leaf.step+"
    cb.call("doc_save", {"doc_id": part["doc_id"], "path": str(leaf)})
    name = _unique_part_name("hinge") + "0"
    asm, _ = _call("new_assembly", {"name": name})
    for i in range(3):
        r, _ = _call("asm_add_component", {"doc_id": asm["doc_id"], "path": str(leaf),
                                           "name": f"leaf{i}", "at": [0, 0, 4 * i]})
        assert r.get("ok", True) and "error" not in r, r
    asm_path = tmp_path / f"{name}.step+"
    cb.call("doc_save", {"doc_id": asm["doc_id"], "path": str(asm_path)})
    for d in (asm["doc_id"], part["doc_id"]):
        cb.call("doc_close", {"doc_id": d})
    opened = cb.call("doc_open", {"path": str(asm_path)})
    opened_id = opened["doc_id"]
    try:
        same, _ = _call("new_assembly", {"name": name})
        assert same["doc_id"] == opened_id and same.get("reused"), same
        assert same["component_count"] == 3, same
        assert "3 components" in same["note"] and "fresh=True" in same["note"], same

        fresh, _ = _call("new_assembly", {"name": name, "fresh": True})
        assert fresh["doc_id"] != opened_id and not fresh.get("reused"), fresh
        docs = {e["doc_id"]: e for e in cb.call("doc_list", {})["docs"]}
        assert docs[fresh["doc_id"]]["component_count"] == 0, docs[fresh["doc_id"]]
        assert docs[opened_id]["component_count"] == 3, docs[opened_id]
        cb.call("doc_close", {"doc_id": fresh["doc_id"]})
    finally:
        cb.call("doc_close", {"doc_id": opened_id})


@pytest.mark.parametrize("suffix", ["", "4"])
def test_new_part_resume_discards_unbuilt_orphans(suffix):
    """A worker SIGKILLed on the run timeout leaves its zero-geometry document open in the
    shared daemon forever. The next resume of that name sweeps the empty leftovers — and
    only the empty ones: a sibling in the same series that carries geometry survives.

    Parametrized on the base name's LAST CHARACTER because the engine's unique_doc_name
    strips a trailing integer to get the series base: the siblings of "x1" are "x2"/"x3",
    which do NOT start with "x1". The sweep's series test assumed `name` + digits, so it
    found nothing whenever the name ended in a digit — and this test's random hex suffix
    made that a coin flip. Both shapes are now pinned."""
    name = _unique_part_name("orphan") + suffix
    keeper, _ = _call("new_part", {"name": name})
    orphan, _ = _call("new_part", {"name": name, "fresh": True})   # the killed worker's doc
    built, _ = _call("new_part", {"name": name, "fresh": True})
    r, _ = _call("add_features", {"doc_id": built["doc_id"], "ops": [
        cb.feat("box", "B", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)})]})
    assert r["ok"], r

    resumed, _ = _call("new_part", {"name": name})
    assert resumed["doc_id"] == keeper["doc_id"], resumed
    assert orphan["doc_id"] in (resumed.get("discarded_orphans") or []), resumed
    open_ids = {d["doc_id"] for d in cb.call("doc_list", {})["docs"]}
    assert orphan["doc_id"] not in open_ids, "the empty orphan must be closed"
    assert built["doc_id"] in open_ids, "a document with geometry is NEVER discarded"


def test_new_part_batch_head_resumes_instead_of_breeding_a_twin():
    """`{"op":"new_part"}` as a BATCH HEAD is the second minting path, and the atomic
    resume-or-mint fix originally landed only on the new_part TOOL.

    Measured live 2026-07-30: an agent created "fkey_live" with the tool, built 7
    features, then opened a later batch with {"op":"new_part","name":"fkey_live"} and no
    doc_id — this path called doc_new with NO resume_if_open, so it uniquified and the
    part's remaining work landed in a second document, "fkey_live2". One requested part,
    two tabs, which is exactly the duplication the user reported."""
    name = _unique_part_name("twin")
    first, _ = _call("new_part", {"name": name})
    r, _ = _call("add_features", {"doc_id": first["doc_id"], "ops": [
        cb.feat("box", "Tag", {"length": cb.dim(50), "width": cb.dim(30),
                               "height": cb.dim(4)})]})
    assert r["ok"], r

    # The batch-head form, deliberately WITHOUT doc_id — what the live agent sent.
    got, _ = _call("add_features", {"ops": [
        {"op": "new_part", "name": name},
        cb.feat("cylinder", "Post", {"radius": cb.dim(2), "height": cb.dim(10)})]})
    assert got["ok"], got
    assert got["doc_id"] == first["doc_id"], got
    assert any("resumed the open part" in n for n in (got.get("interpreted") or [])), got

    series = [d for d in cb.call("doc_list", {})["docs"]
              if _in_series(str(d.get("name", "")), name)]
    assert len(series) == 1, f"one requested part must not become two documents: {series}"
    assert series[0]["feature_count"] == 2, series   # both features in ONE document

# ================================================================ AI-experience lane
# Appended tests (HANDOFF_AI_EXPERIENCE §TODO-2 TESTS-MCP — doc pruned 2026-07-23): feature-overlap detection,
# add_features overlap warnings, coincident-contact interference, and the render caption.

# --- pytest import-shadow repair --------------------------------------------------------
# Under `python3 -m pytest services/mcp/test_cad_tools.py` pytest's default (prepend)
# import mode names this module `mcp.test_cad_tools` — this directory has an __init__.py,
# so the PACKAGE `mcp` becomes services/mcp, shadowing the `mcp` SDK that services.mcp's
# tool modules import (`from mcp.server.fastmcp import FastMCP`). Repair sys.modules:
# load the real SDK package as `mcp`, keeping this directory on its __path__ so pytest
# can still import sibling test modules as `mcp.test_*`. (Runs at import time, before
# any test body executes; a no-op when `mcp` already resolves to the real SDK — e.g.
# when run as `python -m services.mcp.test_cad_tools`.)
import importlib.util


def _unshadow_mcp_sdk() -> None:
    _here = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _here:
        return  # `mcp` is absent or already the real SDK — nothing to repair
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _here:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _here])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module  # must be visible to the SDK's own imports
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()


def test_check_feature_overlap_clean_part():
    """A part whose features don't collide reports no pairs and no dead features."""
    r, _ = _call("new_part", {"name": "ov_clean"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    assert r1["ok"], r1
    ov, _ = _call("check_feature_overlap", {"doc_id": doc})
    assert ov["ok"] is True, ov
    assert ov["pairs"] == [] and ov["dead_features"] == [], ov
    assert "No feature overlap" in ov["summary"], ov


def test_check_feature_overlap_boss_swallowed():
    """A cut that erases an EARLIER boss entirely is flagged kind=boss_swallowed — the only
    overlap kind that sets ok:false — and the boss lands in dead_features (it owns no
    surviving faces)."""
    r, _ = _call("new_part", {"name": "ov_swallow"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)})]})
    assert r1["ok"], r1
    # A boss pin through the plate (r4, taller than the plate)...
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Boss", {"radius": cb.dim(4), "height": cb.dim(30)})]})
    assert r2["ok"], r2
    boss_id = r2["features"][-1]["id"]  # `features` lists the WHOLE tree's rebuild status
    # ...then a WIDER, taller coaxial cut removes the whole boss (plus a hole in the base).
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Kill",
                {"radius": cb.dim(6), "height": cb.dim(40), "mode": "cut"})]})
    assert r3["ok"], r3
    kill_id = r3["features"][-1]["id"]

    ov, _ = _call("check_feature_overlap", {"doc_id": doc})
    assert ov["ok"] is False, ov  # boss_swallowed is the one kind that fails the check
    swallowed = [p for p in ov["pairs"] if p["kind"] == "boss_swallowed"]
    assert len(swallowed) == 1, ov["pairs"]
    assert swallowed[0]["a"] == boss_id and swallowed[0]["b"] == kill_id, swallowed
    assert swallowed[0]["volume_mm3"] > 0, swallowed
    assert boss_id in ov["dead_features"], ov
    assert "boss_swallowed" in ov["summary"], ov["summary"]


# --- ref-slot bag dialect: refs written one level too deep, inside params ---------------
#
# Live 2026-07-28, ONE "stepped shaft + dimensioned drawing" task burned 7 add_features
# calls on this. The model wrote the whole refs bag inside params —
#   {"type":"extrude","params":{"refs":{"profile":[{"kind":"region","sketch":"$0",
#                                                   "hint_index":0}]},"depth":…}}
# — and got "refs.profile: required for extrude". The profile WAS supplied, and even the
# same-batch "$0" spelling was exactly right; only the nesting was wrong. The sketch in
# the same batch nested refs.plane="plane.front", which is worse than a rejection: no
# pass could see it, so the sketch silently defaulted to plane.top.

_NESTED_CIRCLE = [{"type": "circle", "center": [0, 0], "radius": 20}]


def _exact_volume(doc: str) -> float:
    """The document's EXACT solid volume (mm^3).

    add_features' own `volume_mm3` is measured off the tessellation, so a curved solid
    reads ~0.2% light — fine as a delta, useless as a closed-form check. measure_part goes
    through the kernel's mass properties, which is the number geometry claims must be
    proved against.
    """
    r, _ = _call("measure_part", {"doc_id": doc})
    assert r["ok"], r
    return r["mass"]["volume_mm3"]


def _nested_refs_extrude_ops(nested: bool) -> list:
    """The failing extrude payload, with refs nested inside params or at the top level."""
    extrude = {"type": "extrude", "name": "Base_Section",
               "params": {"depth": cb.dim(60), "mode": "boss"}}
    profile = [{"kind": "region", "sketch": "$0", "hint_index": 0}]
    if nested:
        extrude["params"]["refs"] = {"profile": profile}
    else:
        extrude["refs"] = {"profile": profile}
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S",
                                          "params": {"plane": "plane.top"},
                                          "entities": _NESTED_CIRCLE}},
        {"op": "add_feature", "feature": extrude},
    ]


def test_nested_params_refs_hoisted_builds_the_same_solid():
    """params.refs is the refs bag one level too deep — hoisting it must build the SAME
    geometry as the top-level spelling, not merely stop erroring."""
    r, _ = _call("new_part", {"name": "nested_refs_hoist"})
    doc = r["doc_id"]
    rn, _ = _call("add_features", {"doc_id": doc, "ops": _nested_refs_extrude_ops(True)})
    assert rn["ok"], rn
    r2, _ = _call("new_part", {"name": "nested_refs_control"})
    doc_control = r2["doc_id"]
    rc, _ = _call("add_features", {"doc_id": doc_control,
                                   "ops": _nested_refs_extrude_ops(False)})
    assert rc["ok"], rc
    # pi*20^2*60 = 75398.22 mm^3 — a real solid, and the same one both ways.
    vol_nested, vol_control = _exact_volume(doc), _exact_volume(doc_control)
    assert vol_nested > 0, vol_nested
    assert abs(vol_nested - math.pi * 400 * 60) < 1e-6, vol_nested
    assert vol_nested == vol_control, (vol_nested, vol_control)
    # The hoist is ECHOED — the model must learn that refs is a sibling of params.
    assert any("params.refs" in n for n in rn.get("interpreted") or []), rn


@pytest.mark.parametrize("plane", ["plane.top", "plane.front", "plane.right"])
def test_nested_reserved_datum_plane_lands_on_that_datum(plane):
    """A reserved datum plane nested in params.refs must reach the slot and orient the
    solid exactly as the top-level spelling does — NOT silently fall back to plane.top.

    Bare "plane.top"/"plane.front"/"plane.right" are already valid ref values
    (core/doc/validate.cpp RESERVED_DATUMS + validate_sel_or_datum), so the world bbox is
    the proof the value arrived: each datum extrudes along a different world axis.
    """
    def build(nested: bool) -> list:
        r, _ = _call("new_part", {"name": f"datum_{plane}_{nested}"})
        doc = r["doc_id"]
        sketch = {"type": "sketch", "name": "S",
                  "entities": [{"type": "circle", "center": [0, 0], "radius": 10}]}
        if nested:
            sketch["params"] = {"refs": {"plane": plane}}
        else:
            sketch["refs"] = {"plane": plane}
        rb, _ = _call("add_features", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": sketch},
            {"op": "add_feature", "feature": {"type": "extrude", "name": "E",
                                              "refs": {"profile": "$0"},
                                              "params": {"depth": cb.dim(30),
                                                         "mode": "boss"}}}]})
        assert rb["ok"], rb
        return cb.call("doc_bbox", {"doc_id": doc})["size"]

    nested_size, top_size = build(True), build(False)
    # The 30 mm extrusion runs along the datum's normal, so its axis identifies the plane.
    long_axis = max(range(3), key=lambda i: nested_size[i])
    assert long_axis == {"plane.top": 2, "plane.front": 1, "plane.right": 0}[plane], \
        (plane, nested_size)
    assert max(abs(a - b) for a, b in zip(nested_size, top_size)) < 1e-9, (nested_size,
                                                                          top_size)


def test_nested_revolve_axis_datum_builds():
    """refs.axis is REQUIRED for a revolve (validate.cpp FEATURE_TYPES), and a nested
    params.refs.axis used to reach no slot at all. Prove the solid, not ok:true."""
    r, _ = _call("new_part", {"name": "nested_revolve_axis"})
    doc = r["doc_id"]
    # A 10x5 rectangle 10 mm off the axis, revolved 360 deg about Z -> an annular ring.
    prof = [{"type": "line", "p1": [10, 0], "p2": [20, 0]},
            {"type": "line", "p1": [20, 0], "p2": [20, 5]},
            {"type": "line", "p1": [20, 5], "p2": [10, 5]},
            {"type": "line", "p1": [10, 5], "p2": [10, 0]}]
    rb, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "RS",
                                          "params": {"refs": {"plane": "plane.front"}},
                                          "entities": prof}},
        {"op": "add_feature", "feature": {"type": "revolve", "name": "Rev",
                                          "params": {"refs": {"profile": "$0",
                                                              "axis": "axis.z"},
                                                     "angle": cb.dim(360),
                                                     "mode": "boss"}}}]})
    assert rb["ok"], rb
    # pi*(20^2 - 10^2)*5 = 4712.389 mm^3.
    assert abs(_exact_volume(doc) - math.pi * 300 * 5) < 1e-6, _exact_volume(doc)


def test_conflicting_params_refs_is_taught_and_mutates_nothing():
    """The same slot filled DIFFERENTLY at both levels names two pieces of geometry for
    one selection — a guess there could build the wrong solid, so it must be rejected with
    a teaching error and leave the document untouched."""
    r, _ = _call("new_part", {"name": "refs_conflict"})
    doc = r["doc_id"]
    ops = [{"op": "add_feature", "feature": {
        "type": "extrude", "name": "E", "refs": {"profile": "f1"},
        "params": {"refs": {"profile": "f9"}, "depth": cb.dim(5), "mode": "boss"}}}]
    snapshot = json.dumps(ops, sort_keys=True)
    rb, _ = _call("add_features", {"doc_id": doc, "ops": ops})
    assert rb["ok"] is False, rb
    err = rb["error"]
    assert "params.refs" in err and "DIFFERENT" in err, err
    assert "'f1'" in err and "'f9'" in err, err  # both candidates named, so it is fixable
    assert json.dumps(ops, sort_keys=True) == snapshot, "the batch was mutated"
    tree, _ = _call("get_part", {"doc_id": doc, "summary": True})
    assert tree["feature_count"] == 0, tree


def test_identical_refs_at_both_levels_is_not_a_conflict():
    """Saying the same thing twice is not a contradiction — it builds."""
    r, _ = _call("new_part", {"name": "refs_same_both"})
    doc = r["doc_id"]
    rb, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "S",
                                          "refs": {"plane": "plane.top"},
                                          "params": {"refs": {"plane": "plane.top"}},
                                          "entities": _NESTED_CIRCLE}},
        {"op": "add_feature", "feature": {"type": "extrude", "name": "E",
                                          "refs": {"profile": "$0"},
                                          "params": {"depth": cb.dim(10),
                                                     "mode": "boss"}}}]})
    assert rb["ok"], rb
    assert abs(_exact_volume(doc) - math.pi * 400 * 10) < 1e-6, _exact_volume(doc)


# --- primitive placement dialect: `center` is the placement point ------------------------

def test_primitive_center_spelling_places_the_cylinder():
    """`center:[x,y,z]` on a primitive IS its `at` point (a primitive is built centred and
    `at` moves its CENTRE — core/doc/document.cpp:3049-3058). It was not an alias, so the
    engine dropped it, built both coaxial cylinders on the origin, and reported only
    "cylinder boss added no material (it is buried inside the existing solid)"."""
    r, _ = _call("new_part", {"name": "prim_center"})
    doc = r["doc_id"]
    rb, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C1", {"radius": cb.dim(20), "height": cb.dim(60),
                                   "center": [0, 0, 0]}),
        cb.feat("cylinder", "C2", {"radius": cb.dim(12), "height": cb.dim(50),
                                   "center": [0, 0, 55]})]})
    assert rb["ok"], rb
    # C1 spans z -30..30, C2 spans z 30..80 — a real stepped shaft, so the volumes ADD.
    expected = math.pi * (400 * 60 + 144 * 50)
    assert abs(_exact_volume(doc) - expected) < 1e-6, (_exact_volume(doc), expected)


def test_primitive_center_with_centered_false_is_taught():
    """`center` names the CENTRE but `centered:false` makes `at` the BASE — the two
    together are contradictory, and quietly relocating the solid is worse than a
    rejection. Nothing is built."""
    r, _ = _call("new_part", {"name": "prim_center_conflict"})
    doc = r["doc_id"]
    rb, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C", {"radius": cb.dim(5), "height": cb.dim(10),
                                  "center": [0, 0, 60], "centered": False})]})
    assert rb["ok"] is False, rb
    assert "centered" in rb["error"] and "contradictory" in rb["error"], rb["error"]
    tree, _ = _call("get_part", {"doc_id": doc, "summary": True})
    assert tree["feature_count"] == 0, tree


def test_add_features_attaches_overlap_warnings():
    """add_features runs the batch-scoped overlap check automatically and attaches
    `overlap_warnings` when the new batch collides with the existing tree (B3)."""
    r, _ = _call("new_part", {"name": "ov_warn"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)})]})
    assert r1["ok"], r1
    assert not r1.get("overlap_warnings"), r1  # first feature alone: nothing to collide with
    # A boss cylinder overlapping the base volume -> the batch return warns boss_overlap.
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Boss", {"radius": cb.dim(4), "height": cb.dim(30)})]})
    assert r2["ok"], r2
    warn = r2.get("overlap_warnings")
    assert warn, r2
    assert warn["pairs"] and warn["pairs"][0]["kind"] == "boss_overlap", warn
    assert warn["summary"], warn
    assert warn.get("dead_features") == [], warn


def test_check_interference_include_coincident_contacts():
    """include_coincident classifies exactly-touching bodies as `contacts` (the SolidWorks
    'coincident faces' case) instead of burying them in clearances; without the flag the
    same pair reports as a 0-distance clearance."""
    r, _ = _call("new_part", {"name": "touch"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    # A copy shifted by EXACTLY the box length -> faces touch at x=20, zero overlap.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [40, 0, 0], "copy": True})]})

    ci, _ = _call("check_interference", {"doc_id": doc, "include_coincident": True})
    assert ci["ok"] is True, ci  # touching is not interfering
    assert ci["interferences"] == [], ci
    assert len(ci["contacts"]) == 1, ci
    assert ci["contacts"][0]["distance"] <= 1e-6, ci
    assert ci["clearances"] == [], ci  # the pair moved out of clearances

    ci2, _ = _call("check_interference", {"doc_id": doc})
    assert ci2["ok"] is True, ci2
    assert ci2["contacts"] == [], ci2
    assert len(ci2["clearances"]) == 1 and ci2["clearances"][0]["distance"] <= 1e-6, ci2


def test_render_part_caption_pairs_image_with_text():
    """render_part returns [ImageContent, TextContent]: the PNG plus the self-sufficient
    caption of the planner's shot grid (mcp's _convert_to_content flattens the [Image, str]
    return), so a non-vision model reading the transcript still knows what each tile shows."""
    doc = _new_plate_with_hole()
    from services.mcp import server

    # call_tool returns the bare flattened content list for image tools (no structured
    # result) — same shape test_render_part_returns_image hunts through.
    result = asyncio.run(server.mcp.call_tool("render_part", {"doc_id": doc}))
    content = result[0] if isinstance(result, tuple) else result
    image = _find_image(content)
    assert image is not None, f"no ImageContent in {content!r}"
    assert getattr(image, "mimeType", "").startswith("image/") and image.data, image
    texts = [c for c in content if getattr(c, "type", "") == "text"]
    assert texts, f"no TextContent caption in {content!r}"
    caption = texts[0].text
    # Every tile is described, the part's size is stated, what no tile sees is accounted
    # for, the SID tags drawn on the tiles are keyed, and the overlay did not silently fail.
    for letter in "ABCD":
        assert f"{letter}: dir [" in caption, caption
    assert "60 x 40 x 10 mm" in caption, caption
    assert "Faces NO tile sees" in caption or "Every face is visible" in caption, caption
    assert "Tags (" in caption, caption
    assert "OVERLAY FAILED" not in caption, caption


# ================================================================ motion/kinematics
# simulate_motion: drive one dimensional mate over a range, re-solve the mate graph each step,
# return per-body trajectory + swept interference (reuses solve_mates + check_interference).

def _bar_part(name: str, L: float, W: float, H: float, sidecar: str) -> tuple:
    """Build a box part, save it as a .step+ component file, return (path, faces)."""
    r, _ = _call("new_part", {"name": name})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", name, {"length": cb.dim(L), "width": cb.dim(W), "height": cb.dim(H)})]})
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}}).get("matches", [])
    path = os.path.join(sidecar, f"{name}.step+")
    cb.call("doc_save", {"doc_id": doc, "path": path})
    return path, faces


def _mechanism(doc_id: str) -> dict:
    """doc_tree without the revision and the persisted studies. A sweep records its Motion
    study (a revision) but must never touch geometry, mates, poses or expressions."""
    snap = cb.call("doc_tree", {"doc_id": doc_id})
    snap.pop("revision", None)
    snap["tree"].pop("studies", None)
    return snap


def _build_swinging_arm_assembly(tmp_path) -> tuple:
    """A driven single-DOF planar CRANK mechanism (honestly named: not a full 4-bar —
    the tool itself drives any dimensional
    mate, so 4-bar/cam-follower need no tool change; this is the minimal *stable* mechanism that
    regression-tests every simulate_motion behavior):

      * a fixed Ground bar, parked far out in +Y and coplanar (Z=0), only there to give the drive
        + planar-lock mates a fixed reference — it is NOT in the Arm's swing region, so it never
        pollutes the interference sweep;
      * a free Arm bar pivoting about the origin, driven by an `angle` mate carrying
        its explicit hinge line; a `parallel` mate retains the planar mechanism's
        physical constraint for inverse dynamics;
      * a fixed Obstacle block sitting in the Arm's swing arc at (14,14).

    Driving the angle mate 0->~150 deg swings the Arm from CLEAR into the Obstacle (~120 deg).
    Returns (asm_doc_id, drive_mate_id, arm_component_id)."""
    sidecar = str(tmp_path)
    bar, bar_faces = _bar_part("Bar", 40, 4, 4, sidecar)
    obs, _ = _bar_part("Obstacle", 10, 10, 10, sidecar)

    asm = cb.call("doc_new", {"name": "SwingArm", "kind": "assembly"})["doc_id"]

    def rev():
        return cb.call("doc_tree", {"doc_id": asm}).get("revision", 0)

    grd_xf = [1, 0, 0, 0, 0, 1, 0, 200, 0, 0, 1, 0, 0, 0, 0, 1]   # Ground far +Y, out of the arc
    obs_xf = [1, 0, 0, 14, 0, 1, 0, 14, 0, 0, 1, 0, 0, 0, 0, 1]   # Obstacle in the swing arc
    cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(), "with_tess": False, "ops": [
        {"op": "asm_insert_component", "source": {"path": bar}, "name": "Ground", "fixed": True,
         "transform": grd_xf},
        {"op": "asm_insert_component", "source": {"path": bar}, "name": "Arm", "fixed": False},
        {"op": "asm_insert_component", "source": {"path": obs}, "name": "Obstacle", "fixed": True,
         "transform": obs_xf},
    ]})
    # drive: angle between the +X end-faces of both bars; lock: parallel between the +Z wide faces.
    xsel = max(bar_faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[0]))
    zsel = max(bar_faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[2]))
    r = cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(), "with_tess": False, "ops": [
        {"op": "asm_add_mate", "mate": {
            "type": "angle", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": xsel},
                     {"component": "k2", "selector": xsel}],
            "value": {"value": 0.0, "axis": [0, 0, 1], "axis_point": [0, -200, 0]}}},
        {"op": "asm_add_mate", "mate": {
            "type": "parallel", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": zsel},
                     {"component": "k2", "selector": zsel}]}},
    ]})
    drive_mate_id = r.get("id_map", {}).get("$0")
    return asm, drive_mate_id, "k2"

def _build_sliding_block_assembly(tmp_path) -> tuple:
    """Fixed cube + translating cube on one driven plane-distance mate.

    The mate normal passes through both COMs, so it is a determinate force path
    for gravity/translation and a clean motion→FEA load fixture.
    """
    cube, faces = _bar_part("MotionSliderCube", 10, 10, 10, str(tmp_path))
    asm = cb.call("doc_new", {"name": "MotionSlider", "kind": "assembly"})["doc_id"]

    def rev():
        return cb.call("doc_tree", {"doc_id": asm}).get("revision", 0)

    cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": rev(), "with_tess": False,
        "ops": [
            {"op": "asm_insert_component", "source": {"path": cube},
             "name": "Ground", "fixed": True},
            {"op": "asm_insert_component", "source": {"path": cube},
             "name": "Slider", "fixed": False,
             "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 20, 0, 0, 0, 1]},
        ]})
    top = max(faces, key=lambda f: f.get("centroid", [0, 0, 0])[2])
    bottom = min(faces, key=lambda f: f.get("centroid", [0, 0, 0])[2])
    added = cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": rev(), "with_tess": False,
        "ops": [{"op": "asm_add_mate", "mate": {
            "type": "distance", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": top},
                     {"component": "k2", "selector": bottom}],
            "value": {"value": 10.0}}}]})
    return asm, added.get("id_map", {}).get("$0"), "k2"


def _build_rotated_slider_assembly(tmp_path) -> tuple:
    """90°-about-Z slider, listed as mate component_a, with a local-X force path."""
    cube, faces = _bar_part("MotionRotatedSliderCube", 10, 10, 10, str(tmp_path))
    asm = cb.call("doc_new", {"name": "MotionRotatedSlider", "kind": "assembly"})["doc_id"]

    def rev():
        return cb.call("doc_tree", {"doc_id": asm}).get("revision", 0)

    cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": rev(), "with_tess": False,
        "ops": [
            {"op": "asm_insert_component", "source": {"path": cube},
             "name": "Ground", "fixed": True},
            {"op": "asm_insert_component", "source": {"path": cube},
             "name": "RotatedSlider", "fixed": False,
             "transform": [0, -1, 0, 0, 1, 0, 0, -15,
                           0, 0, 1, 0, 0, 0, 0, 1]},
        ]})
    side = max(faces, key=lambda f: f.get("centroid", [0, 0, 0])[0])
    ground_side = max(faces, key=lambda f: f.get("centroid", [0, 0, 0])[1])
    moving_rail = max(faces, key=lambda f: f.get("centroid", [0, 0, 0])[1])
    ground_rail = min(faces, key=lambda f: f.get("centroid", [0, 0, 0])[0])
    top = max(faces, key=lambda f: f.get("centroid", [0, 0, 0])[2])
    added = cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": rev(), "with_tess": False,
        "ops": [
            {"op": "asm_add_mate", "mate": {
                "type": "distance", "align": "aligned", "suppressed": False,
                # Selected moving component is deliberately ref0/component_a.
                "refs": [{"component": "k2", "selector": side},
                         {"component": "k1", "selector": ground_side}],
                "value": {"value": 10.0}}},
            {"op": "asm_add_mate", "mate": {
                # Two orthogonal plane constraints eliminate the free in-plane
                # translations and rotations left by the driven distance mate.
                "type": "coincident", "align": "aligned", "suppressed": False,
                "refs": [{"component": "k2", "selector": moving_rail},
                         {"component": "k1", "selector": ground_rail}]}},
            {"op": "asm_add_mate", "mate": {
                "type": "coincident", "align": "aligned", "suppressed": False,
                "refs": [{"component": "k2", "selector": top},
                         {"component": "k1", "selector": top}]}},
        ]})
    return asm, added.get("id_map", {}).get("$0"), "k2"

@pytest.mark.parametrize("line_source", [
    "authored", "authored_parallel", "authored_placed", "authored_tilted", "authored_near_axis", "pin_reference"])
def test_simulate_motion_honors_offset_hinge_line(tmp_path, line_source):
    """An angle-only rear plate folds about the FRONT pin, not a guessed origin axis."""
    point = [31.0, 17.0, 23.0]
    axis = [0, 1, 0]
    invariant_axis = "y"
    authored = line_source != "pin_reference"
    parallel_normal = line_source in ("authored_parallel", "authored_placed", "authored_tilted", "authored_near_axis")
    query = {"of": "face", "side": "back" if parallel_normal else "right", "count": 1}
    placement = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    if line_source == "authored_placed":
        placement = [0, 0, 1, 107, 0, 1, 0, -23, -1, 0, 0, 41, 0, 0, 0, 1]
        point = [130.0, -6.0, 10.0]
    elif line_source == "authored_tilted":
        # Local Y becomes world Z: line direction and invariant must rotate too.
        placement = [1, 0, 0, 107, 0, 0, -1, -23, 0, 1, 0, 41, 0, 0, 0, 1]
        point = [138.0, -46.0, 58.0]
        axis = [0, 0, 1]
        invariant_axis = "z"
    elif line_source == "authored_near_axis":
        # A tiny transverse component is not an exact world-coordinate invariant.
        c, s = math.cos(1e-6), math.sin(1e-6)
        placement = [c, -s, 0, 1400, s, c, 0, -600, 0, 0, 1, 263, 0, 0, 0, 1]
        point = [1400 + c * 31 - s * 17, -600 + s * 31 + c * 17, 286]
        axis = [-s, c, 0]
        invariant_axis = None
    paths, selectors, pin = [], [], None
    for tag, radius, mode in (("Front", 4, "boss"), ("Rear", 5, "cut")):
        doc = cb.call("doc_new", {"kind": "part", "name": f"MotionHinge{tag}"})["doc_id"]
        try:
            features = [
                cb.feat("box", "Plate", {"length": cb.dim(100), "width": cb.dim(60),
                                         "height": cb.dim(8)})]
            if tag == "Front" or not authored:
                features.append(cb.feat("cylinder", "Pin" if tag == "Front" else "Bore",
                        {"radius": cb.dim(radius), "height": cb.dim(18), "mode": mode}))
            features.append(cb.feat("transform", "Place",
                                   {"rotate": [1, 0, 0], "angle": 90, "translate": [31, 17, 23]}))
            rebuilt = cb.call("doc_apply_ops", {"doc_id": doc, "ops": features})
            assert all(s["ok"] for s in rebuilt["rebuild"]["feature_status"]), rebuilt
            selectors.append(cb.call("doc_query", {
                "doc_id": doc, "query": query})["matches"][0])
            if tag == "Front":
                pin = cb.call("doc_query", {
                    "doc_id": doc, "query": {"of": "face", "surf": "round"}})["matches"][0]
            path = str(tmp_path / f"{tag}.step+")
            cb.call("doc_save", {"doc_id": doc, "path": path})
            paths.append(path)
        finally:
            cb.call("doc_close", {"doc_id": doc})
    asm = cb.call("doc_new", {"kind": "assembly", "name": "OffsetHinge"})["doc_id"]
    try:
        cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
            {"op": "asm_insert_component", "source": {"path": paths[0]},
             "name": "Front", "fixed": True, "transform": placement},
            {"op": "asm_insert_component", "source": {"path": paths[1]}, "name": "Rear",
             "transform": placement},
        ]})
        refs = [{"component": f"k{i + 1}", "selector": s}
                for i, s in enumerate(selectors)]
        value = {"value": 0.0, "axis": axis}
        if authored:
            value["axis_point"] = point
            added, _ = _call("asm_mate", {
                "doc_id": asm, "mate_type": "angle", "value": value,
                "a": {"component": "k1", "query": query},
                "b": {"component": "k2", "query": query},
            })
            assert added["ok"] and added["converged"], added
            mate_id = added["mate"]
            # The real failure was an MCP-authored value object on a saved/reopened
            # assembly, with the reference face normal parallel to the hinge line.
            saved, _ = _call("save_part", {
                "doc_id": asm, "path": str(tmp_path / "hinge.step+")})
            assert saved["ok"], saved
            cb.call("doc_close", {"doc_id": asm})
            reopened, _ = _call("import_part", {"path": str(tmp_path / "hinge.step+")})
            asm = reopened["doc_id"]
            solved, _ = _call("solve_mates", {"doc_id": asm})
            assert solved["ok"] and solved["converged"], solved
            assert solved["dof"] == 1 and solved["residual_dof"] == 0, solved
        else:
            refs.append({"component": "k1", "selector": pin})
            added = cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
                {"op": "asm_add_mate", "mate": {
                    "type": "angle", "align": "aligned", "refs": refs, "value": value}},
            ]})
            mate_id = added["id_map"]["$0"]
        before = _mechanism(asm)
        out, _ = _call("simulate_motion", {
            "doc_id": asm, "mate_id": mate_id, "start": 0, "stop": -180,
            "steps": 37, "check_interference": False})
        assert out["ok"], out
        assert out["pose_check"]["ok"], out
        assert len(out["trajectory"]) == 37
        for frame in out["trajectory"]:
            theta = math.radians(frame["value"])
            c, s = math.cos(theta), math.sin(theta)
            expected = [c, 0, s, point[0] - c * point[0] - s * point[2],
                        0, 1, 0, 0,
                        -s, 0, c, point[2] + s * point[0] - c * point[2],
                        0, 0, 0, 1]
            if line_source == "authored_tilted":
                expected = [c, -s, 0, point[0] - c * point[0] + s * point[1],
                            s, c, 0, point[1] - s * point[0] - c * point[1],
                            0, 0, 1, 0,
                            0, 0, 0, 1]
            if line_source == "authored_near_axis":
                import numpy as np
                a = np.asarray(axis)
                cross = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
                rotation = c * np.eye(3) + (1 - c) * np.outer(a, a) + s * cross
                expected = np.eye(4)
                expected[:3, :3] = rotation
                expected[:3, 3] = np.asarray(point) - rotation @ np.asarray(point)
            if line_source in ("authored_placed", "authored_tilted", "authored_near_axis"):
                import numpy as np
                expected = (np.asarray(expected).reshape(4, 4)
                            @ np.asarray(placement).reshape(4, 4)).ravel().tolist()
            actual = next(b["transform"] for b in frame["bodies"] if b["component"] == "k2")
            assert actual == pytest.approx(expected, abs=1e-9), frame
            assert frame["converged"], frame
            if authored:
                assert frame["dof"] == 1 and frame["residual_dof"] == 0, frame
            checked = frame["pose_check"]["bodies"][0]
            assert checked["coordinate_frame"] == "world"
            assert checked["axis_dir"] == pytest.approx(axis)
            if authored:
                reference = next(b["transform"] for b in frame["bodies"]
                                 if b["component"] == checked["reference_component"])
                world_point = [sum(reference[4 * i + j] * local
                                   for j, local in enumerate([31, 17, 23]))
                               + reference[4 * i + 3] for i in range(3)]
                assert checked["axis_point"] == pytest.approx(world_point)
                assert checked["axis_point"] == pytest.approx(point)
                assert checked["axis_dir"] == pytest.approx([reference[i] for i in (1, 5, 9)])
            else:
                # Any point on the pin LINE is valid; its station along Y is immaterial.
                assert [checked["axis_point"][i] for i in (0, 2)] == pytest.approx([point[0], point[2]])
            assert checked["translation_ok"] and checked["rotation_ok"]
            expected_invariant = {"axis": invariant_axis, "ok": True} if invariant_axis else None
            assert checked["invariant_coordinate"] == expected_invariant
        assert _mechanism(asm) == before
        if line_source == "authored":
            # A second mate forbids the requested rotation. Neither convergence
            # of some other pose nor its interference numbers can certify motion.
            cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
                {"op": "asm_add_mate", "mate": {
                    "type": "parallel", "align": "aligned", "refs": refs[:2]}},
            ]})
            failed, _ = _call("simulate_motion", {
                "doc_id": asm, "stop": -10, "steps": 2, "check_interference": True})
            assert not failed["ok"] and not failed["pose_check"]["ok"], failed
            invalid = [f for f in failed["trajectory"] if not f["pose_check"]["ok"]]
            assert invalid
            assert failed["interference"]["checked"] is True
            assert failed["interference"]["complete"] is False
            assert failed["interference"]["frames_checked"] <= len(failed["trajectory"]) - len(invalid)
            for frame in invalid:
                assert not frame["converged"], frame
                assert frame["interference_complete"] is False, frame
                assert frame["interference"] is None, frame
        # Removing the point/third ref leaves only an angular scalar: it cannot
        # advertise a valid rigid-body motion about a made-up line.
        cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
            {"op": "asm_delete_mate", "id": mate_id},
            {"op": "asm_add_mate", "mate": {
                "type": "angle", "align": "aligned", "refs": refs[:2],
                "value": {"value": 0.0, "axis": [0, 1, 0]}}},
        ]})
        if parallel_normal:
            refused, _ = _call("solve_mates", {"doc_id": asm})
            assert not refused["ok"] and not refused["converged"], refused
            assert refused["reason"] == "mate_unresolved", refused
            return
        refused, _ = _call("simulate_motion", {
            "doc_id": asm, "stop": -10, "steps": 2, "check_interference": False})
        assert not refused["ok"], refused
        assert refused["reason"] == "driver_axis_line_unresolved"
        assert not refused["pose_check"]["ok"]
    finally:
        cb.call("doc_close", {"doc_id": asm})


def test_simulate_motion_trajectory_and_swept_interference(tmp_path):
    """simulate_motion drives the angle mate 0->150 deg, returns a per-body trajectory whose
    Arm pose actually changes step-to-step (real PLANAR motion, continuous — no branch flip, no
    out-of-plane wander), and flags the first step where the swinging Arm hits the Obstacle."""
    asm, mate_id, arm_id = _build_swinging_arm_assembly(tmp_path)
    r, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                     "start": 0.0, "stop": 150.0, "steps": 16})
    assert r["ok"], r
    assert r["mate_id"] == mate_id, r
    assert r["driver"]["type"] == "angle" and r["driver"]["unit"] == "deg", r["driver"]
    traj = r["trajectory"]
    assert len(traj) == 16, r

    def arm_xf(frame):
        return next(b["transform"] for b in frame["bodies"] if b["component"] == arm_id)

    def arm_angle(frame):
        xf = arm_xf(frame)
        return math.degrees(math.atan2(xf[4], xf[0]))

    # (1) REAL MOTION: the Arm's rotation changes substantially across the sweep (not stuck).
    angles = [arm_angle(f) for f in traj]
    assert abs(angles[-1] - angles[0]) > 90.0, f"Arm barely moved: {angles}"
    # (2) CONTINUITY (no branch flip): adjacent steps never jump by a large angle.
    for a, b in zip(angles, angles[1:]):
        assert abs(b - a) < 25.0, f"discontinuous pose (branch flip) between {a} and {b}: {angles}"
    # (3) PLANARITY: the parallel mate keeps rotation in the Z-plane — the out-of-plane transform
    #     terms (R02,R12,R20,R21) stay ~0 at every step (guards the underconstraint failure mode
    #     where a lone angle mate lets the solver tilt the Arm out of plane).
    for f in traj:
        xf = arm_xf(f)
        oop = max(abs(xf[2]), abs(xf[6]), abs(xf[8]), abs(xf[9]))
        assert oop < 1e-6, f"Arm left the Z-plane (out-of-plane term {oop}) at value {f['value']}"

    # (4) SWEPT INTERFERENCE REGRESSION: the Arm is CLEAR early and HITS the Obstacle later.
    inter = r["interference"]
    assert inter["checked"] is True, inter
    step = inter["first_collision_step"]
    assert step is not None, f"expected a collision as the Arm swings into the Obstacle: {r}"
    assert step > 0, f"start pose should be clear, collision only mid-sweep: {inter}"
    assert not traj[0].get("interference"), "first frame must be collision-free"
    assert traj[step].get("interference"), "flagged collision step must actually interfere"
    pair = {inter["detail"][0]["a"], inter["detail"][0]["b"]}
    assert pair == {"Arm", "Obstacle"}, f"unexpected clash pair {pair}: {inter}"


def test_simulate_motion_records_one_study_per_joint_and_loop_closes_exactly(tmp_path):
    """The sweep leaves the mechanism untouched, closes its loop on the solved start pose, and
    records the inputs as the doc's native Motion study (what the GUI opens and re-plays).
    Re-sweeping the same joint replaces that study rather than adding another."""
    asm, mate_id, _ = _build_swinging_arm_assembly(tmp_path)
    before = _mechanism(asm)
    r, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                     "start": 0.0, "stop": 90.0, "steps": 7,
                                     "loop": True, "check_interference": False})
    assert r["ok"], r
    assert len(r["trajectory"]) == 8
    assert r["trajectory"][-1]["value"] == 0.0
    assert r["trajectory"][-1]["bodies"] == r["trajectory"][0]["bodies"]
    assert _mechanism(asm) == before
    study = r["motion_study"]
    assert study["doc_id"] == asm
    assert cb.call("sim_study_list", {"doc_id": asm})["studies"] == [{
        "id": study["study_id"], "type": "motion", "name": study["name"],
        "motion": {"drivers": [{"mate_id": mate_id, "start": 0.0, "stop": 90.0}],
                   "steps": 7, "loop": True, "fps": 24.0,
                   "check_interference": False, "min_clearance": 0.0}}]

    r, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                     "start": 0.0, "stop": 45.0, "steps": 4,
                                     "check_interference": True, "min_clearance_mm": 0.5})
    assert r["ok"], r
    studies = cb.call("sim_study_list", {"doc_id": asm})["studies"]
    assert [s["id"] for s in studies] == [study["study_id"]]
    assert studies[0]["motion"]["drivers"] == [{"mate_id": mate_id, "start": 0.0, "stop": 45.0}]
    assert studies[0]["motion"]["steps"] == 4
    assert studies[0]["motion"]["min_clearance"] == 0.5


def test_simulate_motion_autopicks_single_drivable_mate(tmp_path):
    """With exactly one drivable (dimensional) mate, mate_id may be omitted."""
    asm, mate_id, _ = _build_swinging_arm_assembly(tmp_path)
    r, _ = _call("simulate_motion", {"doc_id": asm, "start": 0.0, "stop": 30.0, "steps": 4,
                                     "check_interference": False})
    assert r["ok"], r
    assert r["mate_id"] == mate_id, r


def test_simulate_motion_rejects_non_dimensional_mate(tmp_path):
    """Driving a mate that has no swept value is a clear, fixable error (not a crash)."""
    asm, mate_id, _ = _build_swinging_arm_assembly(tmp_path)
    # add a coincident mate (no driven value) and try to drive IT.
    endsel = {"kind": "face", "centroid": [0.0, 0.0, 3.0], "normal": [0.0, 0.0, 1.0]}
    r = cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": cb.call("doc_tree", {"doc_id": asm}).get("revision", 0),
        "with_tess": False,
        "ops": [{"op": "asm_add_mate", "mate": {
            "type": "coincident", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": endsel},
                     {"component": "k2", "selector": endsel}]}}]})
    coincident_id = r.get("id_map", {}).get("$0")
    out, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": coincident_id,
                                       "start": 0.0, "stop": 10.0, "steps": 3})
    assert out["ok"] is False, out
    assert "coincident" in out["error"] or "dimensional" in out["error"], out


def test_simulate_motion_kinematics_only_has_no_dynamics_key(tmp_path):
    """Regression contract: the default (kinematics) result gains NO new key."""
    asm, mate_id, _ = _build_swinging_arm_assembly(tmp_path)
    r, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                     "start": 0.0, "stop": 30.0, "steps": 4,
                                     "check_interference": False})
    assert r["ok"], r
    assert "dynamics" not in r, sorted(r.keys())


def test_simulate_motion_dynamics_block(tmp_path):
    """A determinate slider produces balanced, non-zero N reactions and COM inertia."""
    asm, mate_id, slider = _build_sliding_block_assembly(tmp_path)
    r, _ = _call("simulate_motion", {
        "doc_id": asm, "mate_id": mate_id, "start": 10.0, "stop": 20.0, "steps": 8,
        "check_interference": False,
        "dynamics": {"duration_s": 0.8, "default_density_g_cm3": 2.7,
                     "gravity": [0, 0, -9.81]}})
    assert r["ok"], r
    dyn = r.get("dynamics")
    assert dyn and dyn["ok"] and dyn["force_path_resolved"], r
    masses = {b["component"]: b["mass_kg"] for b in dyn["bodies"]}
    assert set(masses) == {"k1", slider}, masses
    assert all(abs(m - 0.0027) < 1e-6 for m in masses.values()), masses
    assert all(len(b["inertia_com_kg_m2"]) == 9 for b in dyn["bodies"])
    frames = dyn["frames"]
    assert len(frames) == len(r["trajectory"])
    assert abs(frames[-1]["t_s"] - 0.8) < 1e-9
    assert all(fr["balanced"] and fr["force_residual_n"] < 1e-8 for fr in frames)
    reactions = [reaction for frame in frames for reaction in frame["reactions"]
                 if reaction["mate_id"] == mate_id]
    assert reactions and max(x["force_mag_n"] for x in reactions) > 0.0
    assert dyn.get("worst_case") and dyn.get("per_joint_worst"), dyn


def test_motion_fea_handoff_dry_run(tmp_path):
    """The grounded mate reaction is a boundary reaction, never a duplicate load."""
    from services.mcp import simhandoff as sh

    asm, mate_id, slider = _build_sliding_block_assembly(tmp_path)
    r, _ = _call("motion_fea_handoff", {
        "doc_id": asm, "component": slider, "fixtures": ["bottom"],
        "body_load_face": "top",
        "mate_id": mate_id, "start": 10.0, "stop": 20.0, "steps": 8,
        "dynamics": {"duration_s": 0.8, "default_density_g_cm3": 7.85},
        "dry_run": True})
    assert r["ok"], r
    assert r["component"] == slider and r["part_doc_id"]
    setup = r["setup_diagnostics"]
    assert setup["fixture_mate_id"] == mate_id
    assert setup["fixture_reaction_omitted_from_loads"] is True
    assert not any(load.get("mate_id") == mate_id for load in r["loads_applied"]), r
    body_loads = [load for load in r["loads_applied"]
                  if load["kind"] == "equivalent_body_inertial"]
    assert len(body_loads) == 1, r
    for load in r["loads_applied"]:
        assert load["units"] == "N"
        assert math.sqrt(sum(x * x for x in load["force"])) > 0.0
        assert all(math.isfinite(x) for x in load["force"])
    balance = r["balance_diagnostics"]
    assert balance["body_load_representation_complete"] is True
    assert balance["force_closure_residual_n"] < 1e-8, balance
    assert balance["moment_closure_residual_nm"] < 1e-8, balance
    omitted = balance["omitted_fixture_reaction"]["force_local_n"]
    assert all(math.isclose(body_loads[0]["force"][i], -omitted[i],
                            rel_tol=1e-9, abs_tol=1e-12)
               for i in range(3)), (body_loads[0], omitted)
    env = r.get("simhandoff")
    assert env and env.get("coupling") == "motion_to_fea", env
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    conds = env["conditions"]
    assert any(c["kind"] == "fixed_support" for c in conds), conds
    assert any(c["kind"] == "equivalent_body_inertial_force" and c["units"] == "N"
               for c in conds), conds
    assert all(c.get("case") for c in conds)
    assert env["source"]["domain"] == "motion" and env["target"]["domain"] == "fea", env
    assert r["fea"] is None, r

def test_motion_fea_handoff_uses_selected_body_sign_and_local_basis(tmp_path):
    """The equivalent load closes the rotated body's omitted local fixture reaction."""
    asm, mate_id, slider = _build_rotated_slider_assembly(tmp_path)
    r, _ = _call("motion_fea_handoff", {
        "doc_id": asm, "component": slider, "fixtures": ["right"],
        "body_load_face": "left",
        "mate_id": mate_id, "start": 10.0, "stop": 20.0, "steps": 7,
        "dynamics": {"duration_s": 1.0, "gravity": [0, -9.81, 0]},
        "dry_run": True})
    assert r["ok"], r
    load = next(ld for ld in r["loads_applied"]
                if ld["kind"] == "equivalent_body_inertial")
    local_force = load["force"]
    fixture_force = r["balance_diagnostics"]["omitted_fixture_reaction"]["force_local_n"]
    assert abs(local_force[0]) > 0.0 and abs(local_force[1]) < 1e-8, load
    assert all(math.isclose(local_force[i], -fixture_force[i],
                            rel_tol=1e-9, abs_tol=1e-10)
               for i in range(3)), (load, fixture_force)
    assert r["balance_diagnostics"]["force_closure_residual_n"] < 1e-8
    assert r["balance_diagnostics"]["moment_closure_residual_nm"] < 1e-8


def test_motion_fea_handoff_requires_body_resultant_face(tmp_path):
    """A balanced motion frame is incomplete for static FEA without its body load."""
    asm, mate_id, slider = _build_sliding_block_assembly(tmp_path)
    r, _ = _call("motion_fea_handoff", {
        "doc_id": asm, "component": slider, "fixtures": ["bottom"],
        "mate_id": mate_id, "start": 10.0, "stop": 20.0, "steps": 7,
        "dynamics": {"duration_s": 1.0},
        "dry_run": True})
    assert r["ok"] is False, r
    assert "body_load_face is required" in r["error"], r
    assert r["required_body_load"]["force_local_n"], r
    assert r["balance_diagnostics"]["body_load_representation_complete"] is False


def test_motion_fea_handoff_refuses_fixture_unrelated_to_grounded_mate(tmp_path):
    """A fixed face cannot stand in for a different joint's reaction by guesswork."""
    asm, mate_id, slider = _build_sliding_block_assembly(tmp_path)
    r, _ = _call("motion_fea_handoff", {
        "doc_id": asm, "component": slider, "fixtures": ["top"],
        "body_load_face": "bottom",
        "fixture_mate_id": mate_id,
        "mate_id": mate_id, "start": 10.0, "stop": 20.0, "steps": 7,
        "dynamics": {"duration_s": 1.0},
        "dry_run": True})
    assert r["ok"] is False, r
    assert "fixture face must identify exactly one solved mate" in r["error"], r
    assert r["setup_diagnostics"]["fixture_mate_candidates"] == []


# ================================================================ W2 hole-frame determinism
# hole/bolt_circle `positions` frame: origin = OUTER-boundary centroid (stable when the face
# already carries holes), axes = SketchPlane convention (u = world +X projected, v = n×u).
# Regression for the L-bracket incident: a Ø22.5 pilot bore shifted the whole-face centroid
# by +0.57 mm, so the NEXT hole feature's pattern landed off by that much — parts didn't fit.

def _bore_axes(doc: str) -> list:
    """(x,y,z) axis-point (centroid) of every cylindrical face, sorted for stable compare."""
    m = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "cylindrical"}})
    pts = [tuple(round(v, 6) for v in f["centroid"]) for f in m.get("matches", [])]
    return sorted(pts)


def test_hole_positions_stable_after_prior_cut():
    """A second hole feature lands where its [u,v] says even after a big prior bore moved
    the face's area centroid; top-face axes are u=+X, v=+Y exactly."""
    r, _ = _call("new_part", {"name": "w2_frame"})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Slab", {"length": cb.dim(60), "width": cb.dim(40),
                                "height": cb.dim(10)}),
        # big off-centre bore FIRST — the area centroid of the top face now shifts…
        cb.feat("hole", "Big", {"style": "simple", "end": "through_all",
                                "diameter": cb.dim(20), "positions": [[-15, 0]]},
                {"face": {"of": "face", "side": "top"}}),
        # …but the SECOND hole feature must still land at exactly (+20, +10).
        cb.feat("hole", "Small", {"style": "simple", "end": "through_all",
                                  "diameter": cb.dim(5), "positions": [[20, 10]]},
                {"face": {"of": "face", "side": "top"}}),
    ]})
    assert r["ok"], r
    axes = _bore_axes(doc)
    # box is origin-centred: top-face outer boundary centroid = (0,0); u=+X, v=+Y.
    assert (-15.0, 0.0, 0.0) in axes, axes
    assert (20.0, 10.0, 0.0) in axes, axes
    _call("close_part", {"doc_id": doc})


def test_hole_positions_front_face_axes():
    """On a front (−Y) face the frame is u=+X, v=+Z (world-aligned, not the kernel's
    arbitrary surface parameterization)."""
    r, _ = _call("new_part", {"name": "w2_front"})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Slab", {"length": cb.dim(60), "width": cb.dim(20),
                                "height": cb.dim(40)}),
        cb.feat("hole", "H", {"style": "simple", "end": "through_all",
                              "diameter": cb.dim(6), "positions": [[15, 10]]},
                {"face": {"of": "face", "side": "front"}}),
    ]})
    assert r["ok"], r
    axes = _bore_axes(doc)
    # front face centre (0, -10, 0); u=+X → x=15, v=+Z → z=10; bore axis runs along Y (y=0 mid).
    assert (15.0, 0.0, 10.0) in axes, axes
    _call("close_part", {"doc_id": doc})


# hole world_points: a WORLD point names the bore AXIS. It used to be copied into face-local
# positions=[[x,y]] — measured from the face centre, along u/v — so on an off-centre face
# (L-bracket base, centre x=34) the hole landed in the wrong place, and on a side face
# (v = -Z) the world z was dropped outright.

def _l_bracket(name: str) -> str:
    """Base 60x40x10 spanning x 4..64, y 0..40, z 0..10 (bottom-face centre (34,20,0)) with a
    5 mm wall rising from x 6..11 — so the -X extreme face is the base end at x=4 alone."""
    r, _ = _call("new_part", {"name": name, "fresh": True})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40),
                                "height": cb.dim(10), "at": [34, 20, 5]}),
        cb.feat("box", "Wall", {"length": cb.dim(5), "width": cb.dim(40),
                                "height": cb.dim(40), "at": [8.5, 20, 30]}),
    ]})
    assert r["ok"] and r["body_count"] == 1, r
    return doc


def _has_axis(axes: list, want: tuple) -> bool:
    return any(all(abs(a - b) < 1e-4 for a, b in zip(ax, want)) for ax in axes)


def test_hole_world_points_land_at_the_world_location_on_off_centre_and_side_faces():
    doc = _l_bracket("world_pts_lbracket")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        # bottom face (outward -Z, centre (34,20,0)): the bore must sit at world x=30, y=20.
        cb.feat("hole", "Bottom", {"style": "simple", "end": "through_all",
                                   "diameter": cb.dim(4), "world_points": [[30, 20, 0]]},
                {"face": {"of": "face", "side": "bottom"}}),
        # -X face (centre (4,20,5), v = -Z): z=3 must survive, not be dropped.
        cb.feat("hole", "Side", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(3), "world_points": [[4, 12, 3]]},
                {"face": {"of": "face", "side": "left"}}),
    ]})
    assert r["ok"], r
    assert not any(f.get("warning") for f in r["features"]), r["features"]
    axes = _bore_axes(doc)
    assert _has_axis(axes, (30.0, 20.0, 5.0)), axes    # vertical bore through the 10 mm base
    assert _has_axis(axes, (34.0, 12.0, 3.0)), axes    # horizontal bore along x 4..64
    _call("close_part", {"doc_id": doc})


def test_hole_world_point_off_the_face_plane_is_projected_along_the_axis_and_reported():
    doc = _l_bracket("world_pts_axis")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Mid", {"style": "simple", "end": "through_all",
                                "diameter": cb.dim(4), "world_points": [[50, 30, 7]]},
                {"face": {"of": "face", "side": "bottom"}})]})
    assert r["ok"], r
    warned = [f["warning"] for f in r["features"] if f.get("warning")]
    assert warned and "world_points[0]" in warned[0] and "7 mm off" in warned[0], warned
    assert _has_axis(_bore_axes(doc), (50.0, 30.0, 5.0))
    _call("close_part", {"doc_id": doc})


@pytest.mark.parametrize("point", [[80, 20, 0], [30, 20, 6]])
def test_hole_world_point_whose_axis_misses_the_face_is_refused_by_name(point):
    """[80,20,0] is past the bottom face's x=64 edge; [30,20,6] projects into the bore the
    first feature already drilled there — an opening, not face."""
    doc = _l_bracket("world_pts_miss")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "First", {"style": "simple", "end": "through_all",
                                  "diameter": cb.dim(6), "world_points": [[30, 20, 0]]},
                {"face": {"of": "face", "side": "bottom"}})]})
    assert r["ok"], r
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Miss", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(2), "world_points": [point]},
                {"face": {"of": "face", "side": "bottom"}})]})
    text = json.dumps(r)
    assert r["ok"] is False, r
    assert "world_points[0]" in text and "OUTSIDE the face" in text, text
    _call("close_part", {"doc_id": doc})


def test_hole_positions_and_world_points_together_are_refused():
    doc = _l_bracket("world_pts_both")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Both", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(4), "positions": [[0, 0]],
                                 "world_points": [[30, 20, 0]]},
                {"face": {"of": "face", "side": "bottom"}})]})
    text = json.dumps(r)
    assert r["ok"] is False, r
    assert "world_points" in text and "not both" in text, text
    _call("close_part", {"doc_id": doc})


def test_bolt_circle_world_center_centres_the_ring_at_the_world_point():
    """bottom face frame: u=+X, v=-Y; ring of 4 on pcd 20 about world (40,20)."""
    doc = _l_bracket("world_pts_ring")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("bolt_circle", "Ring", {"diameter": cb.dim(3), "count": 4, "pcd": cb.dim(20),
                                        "world_center": [40, 20, 0]},
                {"face": {"of": "face", "side": "bottom"}})]})
    assert r["ok"], r
    axes = _bore_axes(doc)
    for want in ((50.0, 20.0, 5.0), (30.0, 20.0, 5.0), (40.0, 10.0, 5.0), (40.0, 30.0, 5.0)):
        assert _has_axis(axes, want), (want, axes)
    _call("close_part", {"doc_id": doc})


def _plate_with_posts(name: str) -> str:
    """80x60x20 plate (z -10..10) with two Ø8 posts at (±30,20) rising to z=30 — so the
    word query side:"top" resolves to a POST top, not the plate's own top face."""
    r, _ = _call("new_part", {"name": name, "fresh": True})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(80), "width": cb.dim(60),
                                 "height": cb.dim(20)}),
        cb.feat("cylinder", "PostR", {"radius": cb.dim(4), "height": cb.dim(20),
                                      "at": [30, 20, 20]}),
        cb.feat("cylinder", "PostL", {"radius": cb.dim(4), "height": cb.dim(20),
                                      "at": [-30, 20, 20]}),
    ]})
    assert r["ok"] and r["body_count"] == 1, r
    return doc


def test_word_query_world_point_drills_the_face_its_axis_enters_not_the_extreme_one():
    """side:"top" picks a post top (the extreme +Z face), but the axis through (0,0) enters
    the PLATE top: the bore goes through the plate at (0,0); the posts are untouched."""
    doc = _plate_with_posts("world_pts_posts")
    before = cb.call("eval_mass", {"doc_id": doc})
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Centre", {"style": "simple", "end": "through_all",
                                   "diameter": cb.dim(6), "world_points": [[0, 0, 0]]},
                {"face": {"of": "face", "side": "top"}})]})
    assert r["ok"], r
    warned = " ".join(f["warning"] for f in r["features"] if f.get("warning"))
    assert "misses the face refs.face resolved to" in warned, r["features"]
    axes = _bore_axes(doc)
    assert _has_axis(axes, (0.0, 0.0, 0.0)), axes          # through the 20 mm plate
    assert _has_axis(axes, (30.0, 20.0, 20.0)), axes       # both posts intact
    assert _has_axis(axes, (-30.0, 20.0, 20.0)), axes
    after = cb.call("eval_mass", {"doc_id": doc})
    removed = before["volume_mm3"] - after["volume_mm3"]
    assert removed == pytest.approx(math.pi * 9 * 20, rel=1e-4), (before, after)
    _call("close_part", {"doc_id": doc})


def test_a_concrete_sid_face_stays_strict_when_the_axis_misses_it():
    doc = _plate_with_posts("world_pts_sid")
    m = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})
    sid = m["matches"][0]["sid"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Strict", {"style": "simple", "end": "through_all",
                                   "diameter": cb.dim(6), "world_points": [[0, 0, 0]]},
                {"face": {"of": "face", "sid": sid}})]})
    text = json.dumps(r)
    assert r["ok"] is False, r
    assert "OUTSIDE the face" in text, text
    _call("close_part", {"doc_id": doc})


def test_one_hole_feature_drills_four_boss_tops_at_two_heights():
    """Four Ø12 bosses on a 100x100x10 plate (top z=5): two 10 tall (top z=15), two 20 tall
    (top z=25). One word-query hole with four world points (z=0, on each boss axis) drills
    each boss from its OWN top: blind 5 mm, so each bore's centroid sits 2.5 below it."""
    r, _ = _call("new_part", {"name": "world_pts_bosses", "fresh": True})
    doc = r["doc_id"]
    ops = [cb.feat("box", "Plate", {"length": cb.dim(100), "width": cb.dim(100),
                                    "height": cb.dim(10)})]
    bosses = [((30, 30), 10), ((-30, 30), 10), ((30, -30), 20), ((-30, -30), 20)]
    for i, ((x, y), h) in enumerate(bosses):
        ops.append(cb.feat("cylinder", f"Boss{i}", {"radius": cb.dim(6), "height": cb.dim(h),
                                                     "at": [x, y, 5 + h / 2]}))
    r, _ = _call("add_features", {"doc_id": doc, "ops": ops})
    assert r["ok"] and r["body_count"] == 1, r
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("hole", "Taps", {"style": "simple", "end": "blind", "depth": cb.dim(5),
                                 "diameter": cb.dim(4),
                                 "world_points": [[x, y, 0] for (x, y), _ in bosses]},
                {"face": {"of": "face", "side": "top"}})]})
    assert r["ok"], r
    axes = _bore_axes(doc)
    for (x, y), h in bosses:
        assert _has_axis(axes, (x, y, 5 + h - 2.5)), ((x, y, h), axes)
    _call("close_part", {"doc_id": doc})


def test_side_plus_pick_largest_facing_face():
    """W3: {"side":"top","pick":"largest"} = the LARGEST face FACING up — not the highest.
    L-bracket regression: the extremum rule used to pick the 60x5 wall-top sliver."""
    r, _ = _call("new_part", {"name": "w3_lbracket"})
    doc = r["doc_id"]
    ops = [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "L",
            "params": {"entities": [], "constraints": []}, "refs": {"plane": "plane.right"}}},
    ]
    pts = [(0, 0), (45, 0), (45, 5), (5, 5), (5, 65), (0, 65)]
    for i in range(len(pts)):
        p1, p2 = pts[i], pts[(i + 1) % len(pts)]
        ops.append({"op": "sketch_add_entity", "sketch": "$0",
                    "entity": {"type": "line", "p1": list(p1), "p2": list(p2)}})
    ops.append({"op": "add_feature", "feature": {"type": "extrude", "name": "Body",
        "params": {"mode": "boss", "end": "blind", "depth": cb.dim(60)},
        "refs": {"profile": [{"kind": "region", "sketch": "$0", "centroid": [10, 3],
                              "hint_index": 0}]}}})
    r, _ = _call("add_features", {"doc_id": doc, "ops": ops})
    assert r["ok"], r
    m = cb.call("doc_query", {"doc_id": doc,
                              "query": {"of": "face", "side": "top", "pick": "largest"}})
    faces = m.get("matches", [])
    assert len(faces) == 1, faces
    # the base top (60 x 40 = 2400 mm²), NOT the 60x5=300 mm² wall top at z=65.
    assert abs(faces[0]["area_or_len"] - 2400.0) < 1.0, faces[0]
    # side alone still means the EXTREME face — the wall top.
    m2 = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "top"}})
    tops = m2.get("matches", [])
    assert len(tops) == 1 and abs(tops[0]["area_or_len"] - 300.0) < 1.0, tops
    _call("close_part", {"doc_id": doc})


def _revolved_shaft(name: str) -> str:
    """Ø30 x 40 then Ø20 x 60 along +X: a closed 6-line profile on plane.front revolved 360°
    about axis.x. Its planar faces carry LEFT-handed gp_Pln frames (MakeRevol), the case the
    old `Axis().Direction()` + REVERSED-flip normal idiom got backwards."""
    pts = [(0, 0), (0, 15), (40, 15), (40, 10), (100, 10), (100, 0)]
    lines = [{"kind": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % len(pts)])}
             for i in range(len(pts))]
    r, _ = _call("new_part", {"name": name})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Profile",
            "params": {"entities": lines}, "refs": {"plane": "plane.front"}}},
        {"op": "add_feature", "feature": {"type": "revolve", "name": "Shaft",
            "params": {"angle": 360},
            "refs": {"profile": [{"kind": "region", "sketch": "$0"}], "axis": "axis.x"}}},
    ]})
    assert r["ok"], r
    return doc


def test_revolved_shaft_planar_faces_report_outward_normals():
    """Every planar face of a revolved stepped shaft used to read normal [1,0,0] — including
    the x=0 end, whose outward normal is -X. Both read-backs (doc_query and the
    describe_faces pick path) must report the true outward normal, and side queries must
    still pick the same faces."""
    doc = _revolved_shaft("revolve_normals")
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
    planes = {round(f["centroid"][0], 6): f for f in faces if f["surf"] == "plane"}
    assert sorted(planes) == [0.0, 40.0, 100.0], faces
    assert planes[0.0]["normal"] == [-1.0, 0.0, 0.0], planes[0.0]
    assert planes[0.0]["side"] == "left", planes[0.0]
    # Ø30→Ø20 shoulder: material is on x<40, so it faces +X (toward the small end).
    assert planes[40.0]["normal"] == [1.0, 0.0, 0.0], planes[40.0]
    assert planes[100.0]["normal"] == [1.0, 0.0, 0.0], planes[100.0]
    for f in planes.values():
        pick = cb.call("doc_resolve_pick", {"doc_id": doc, "body_id": f["body"],
                                            "kind": "face", "topo_id": f["topo"]})["selector"]
        assert pick["normal"] == f["normal"], (pick, f)
    lo = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "-x"}})["matches"]
    hi = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": "+x"}})["matches"]
    assert [m["centroid"][0] for m in lo] == [0.0], lo
    assert [m["centroid"][0] for m in hi] == [100.0], hi
    _call("close_part", {"doc_id": doc})


def test_hole_on_revolved_end_face_drills_into_material():
    """A hole on the x=0 end of a revolved shaft drilled OUTWARD (left-handed plane frame
    read as an inward normal): a 0.05 mm sliver bore and ~1 mm³ removed. It must cut a
    Ø6 x 10 bore into the shaft."""
    doc = _revolved_shaft("revolve_end_hole")
    v0 = cb.call("eval_mass", {"doc_id": doc})["volume_mm3"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "hole", "name": "EndHole",
            "params": {"style": "simple", "diameter": 6, "end": "blind", "depth": 10,
                       "positions": [[0, 0]]},
            "refs": {"face": {"of": "face", "side": "-x"}}}}]})
    assert r["ok"], r
    removed = v0 - cb.call("eval_mass", {"doc_id": doc})["volume_mm3"]
    assert removed > 0.95 * math.pi * 3.0 ** 2 * 10.0, removed
    bores = [m for m in cb.call("doc_query", {"doc_id": doc, "query": {
        "of": "face", "surf": "cylinder"}})["matches"] if m["concavity"] == "bore"]
    assert len(bores) == 1, bores
    assert bores[0]["radius"] == pytest.approx(3.0), bores
    assert 2.0 < bores[0]["centroid"][0] <= 10.0, bores  # inside the shaft, not a 0.05 mm sliver
    assert bores[0]["area_or_len"] == pytest.approx(2 * math.pi * 3.0 * 10.0, rel=0.05), bores
    _call("close_part", {"doc_id": doc})


def test_cylinder_faces_report_axis_point_on_the_axis():
    """`centroid` of a fillet (a quarter cylinder) is off-axis; `axis_point` is the
    centroid's foot on the true axis — for doc_query and the describe_faces pick path."""
    r, _ = _call("new_part", {"name": "fillet_axis_point"})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "B",
            "params": {"length": 40, "width": 30, "height": 20},
            "at": [0, 0, 0], "centered": False}},
        {"op": "add_feature", "feature": {"type": "fillet", "name": "F",
            "params": {"radius": 5},
            "refs": {"edges": [{"of": "edge", "surf": "line", "parallel": "y",
                                "near": [40, 15, 20], "count": 1}]}}},
    ]})
    assert r["ok"], r
    cyl = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "cylinder"}})
    assert cyl["count"] == 1, cyl
    m = cyl["matches"][0]
    assert m["radius"] == pytest.approx(5.0), m
    assert m["axis_point"] == pytest.approx([35.0, 15.0, 15.0], abs=1e-6), m
    assert m["centroid"] != pytest.approx(m["axis_point"], abs=1.0), m  # truly off-axis
    pick = cb.call("doc_resolve_pick", {"doc_id": doc, "body_id": m["body"],
                                        "kind": "face", "topo_id": m["topo"]})["selector"]
    assert pick["axis_point"] == pytest.approx([35.0, 15.0, 15.0], abs=1e-6), pick
    planes = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "plane"}})
    assert all(p["axis_point"] is None for p in planes["matches"]), planes
    _call("close_part", {"doc_id": doc})


def test_revolved_shaft_cylinders_axis_point():
    doc = _revolved_shaft("revolve_axis_point")
    cyl = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "cylinder"}})
    pts = sorted(tuple(m["axis_point"]) for m in cyl["matches"])
    assert pts == [pytest.approx((20.0, 0.0, 0.0), abs=1e-6),
                   pytest.approx((70.0, 0.0, 0.0), abs=1e-6)], cyl
    _call("close_part", {"doc_id": doc})


def test_torus_faces_report_bend_radius_tube_radius_axis_and_centre():
    """A Ø25/Ø21 tube swept along (0,0,0)->(300,0,0)->(300,0,200)->(500,0,200) with R50
    bends: each bend is an outer (r 12.5) and an inner (r 10.5) torus quarter whose read-back
    carries major/minor radius, axis and centre — `radius`/`axis` stay null (unscored)."""
    r, _ = _call("new_part", {"name": "torus_readback"})
    doc = r["doc_id"]
    path = [
        {"type": "line", "p1": [0, 0], "p2": [250, 0]},
        {"type": "arc", "center": [250, 50], "radius": 50, "start": -90, "end": 0, "ccw": True},
        {"type": "line", "p1": [300, 50], "p2": [300, 150]},
        {"type": "arc", "center": [350, 150], "radius": 50, "start": 180, "end": 90,
         "ccw": False},
        {"type": "line", "p1": [350, 200], "p2": [500, 200]},
    ]
    section = [{"type": "circle", "center": [0, 0], "radius": 12.5},
               {"type": "circle", "center": [0, 0], "radius": 10.5}]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Path",
            "params": {"entities": path}, "refs": {"plane": "plane.front"}}},
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Section",
            "params": {"entities": section}, "refs": {"plane": "plane.right"}}},
        {"op": "add_feature", "feature": {"type": "sweep", "name": "Tube",
            "params": {"mode": "boss"},
            "refs": {"profile": [{"kind": "region", "sketch": "$1"}], "path": "$0"}}},
    ]})
    assert r["ok"], r
    tori = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "torus"}})
    assert tori["count"] == 4, tori
    got = sorted((tuple(round(v, 4) for v in m["torus_center"]), m["minor_radius"])
                 for m in tori["matches"])
    assert got == [((250.0, 0.0, 50.0), pytest.approx(10.5)),
                   ((250.0, 0.0, 50.0), pytest.approx(12.5)),
                   ((350.0, 0.0, 150.0), pytest.approx(10.5)),
                   ((350.0, 0.0, 150.0), pytest.approx(12.5))], tori
    for m in tori["matches"]:
        assert m["major_radius"] == pytest.approx(50.0), m
        assert [abs(v) for v in m["torus_axis"]] == pytest.approx([0.0, 1.0, 0.0]), m
        assert m["radius"] is None and m["axis"] is None, m
    m = tori["matches"][0]
    pick = cb.call("doc_resolve_pick", {"doc_id": doc, "body_id": m["body"],
                                        "kind": "face", "topo_id": m["topo"]})["selector"]
    for key in ("major_radius", "minor_radius", "torus_axis", "torus_center"):
        assert pick[key] == pytest.approx(m[key]), (key, pick)
    # A match handed back as a query keeps resolving (the torus keys are read-back keys).
    echo = {k: v for k, v in m.items() if k != "side"}
    again = cb.call("doc_query", {"doc_id": doc, "query": {**echo, "of": "face", "surf": "torus",
                                                         "near": m["centroid"], "count": 1}})
    assert again["matches"][0]["sid"] == m["sid"], again
    cyl = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "cylinder"}})
    assert all("major_radius" not in c for c in cyl["matches"]), cyl
    _call("close_part", {"doc_id": doc})


# ================================================================ G4/G5 assembly authoring

def test_asm_ops_rejected_on_part_doc():
    """asm_* ops on a PART doc must fail loudly (they used to no-op silently)."""
    r, _ = _call("new_part", {"name": "not_an_asm"})
    doc = r["doc_id"]
    out, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "asm_insert_component", "source": {"path": "/nonexistent.step+"}}]})
    assert out["ok"] is False, out
    assert "assembly" in json.dumps(out).lower(), out
    _call("close_part", {"doc_id": doc})


def test_assembly_tools_end_to_end(tmp_path):
    """new_assembly + asm_add_component + asm_mate: a pin seats concentrically in a plate's
    bore with faces coincident; the solver lands the pin on the hole axis."""
    # part 1: plate with an off-centre Ø8 bore
    r, _ = _call("new_part", {"name": "aa_plate"})
    plate = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": plate, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(8), "positions": [[15, 5]]},
                {"face": {"of": "face", "side": "top"}}),
    ]})
    assert r["ok"], r
    plate_path = str(tmp_path / "aa_plate.step+")
    r, _ = _call("save_part", {"doc_id": plate, "path": plate_path})
    assert r["ok"], r

    # part 2: a Ø8 x 20 pin
    r, _ = _call("new_part", {"name": "aa_pin"})
    pin = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": pin, "ops": [
        cb.feat("cylinder", "Pin", {"radius": cb.dim(4), "height": cb.dim(20)})]})
    assert r["ok"], r
    pin_path = str(tmp_path / "aa_pin.step+")
    r, _ = _call("save_part", {"doc_id": pin, "path": pin_path})
    assert r["ok"], r

    # assembly: plate fixed, pin mated into the bore
    r, _ = _call("new_assembly", {"name": "aa_asm"})
    assert r["ok"] and r["kind"] == "assembly", r
    asm = r["doc_id"]
    r, _ = _call("asm_add_component", {"doc_id": asm, "path": plate_path, "fixed": True})
    assert r["ok"] and r["component"] == "k1", r
    r, _ = _call("asm_add_component", {"doc_id": asm, "path": pin_path,
                                       "at": [15, 5, 30]})
    assert r["ok"] and r["component"] == "k2", r
    assert r["component_count"] == 2, r
    assert r["placement_source"] == "transform", r

    # ambiguous query → helpful error with candidates
    bad, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric",
                                "a": {"component": "k1", "query": {"of": "face"}},
                                "b": {"component": "k2",
                                      "query": {"of": "face", "surf": "round"}}})
    assert bad["ok"] is False and "candidates" in bad, bad

    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric",
                              "a": {"component": "k1",
                                    "query": {"of": "face", "surf": "round"}},
                              "b": {"component": "k2",
                                    "query": {"of": "face", "surf": "round"}}})
    assert r["ok"], r
    assert r["mate"], r
    assert r["converged"] and r["placement_source"]["k2"] == "mate", r
    bodies = {b["component"]: b for b in (r.get("world_bodies") or [])}
    assert "k2" in bodies, r
    # concentric seats the pin's axis on the bore's axis at (x=15, y=5). Assert on the EXACT
    # B-rep axis centroid (query_geometry), NOT the world-bbox centre: the bbox comes from
    # BRepBndLib::Add on the placed shape, whose triangulation-based deflection enlargement is
    # rotation-dependent and drifts the centre by ~2.5e-3 for a free-to-spin round pin. That is
    # a measurement artefact — the solver itself lands the pin axis on the bore axis to ~1e-9.
    # NB: no "component" key — session.cpp::doc_query reads only body_id before handing the
    # dict to resolve_geom_query, so a component inside a QUERY never scoped anything (the
    # documented shape puts it beside the query, as asm_mate's operands do above). The match
    # set is identical without it; the guard in cad_common now refuses the inert spelling.
    q, _ = _call("query_geometry", {"doc_id": asm,
                                    "query": {"of": "face", "surf": "round"}})
    assert q.get("matches"), q
    cx, cy, _cz = q["matches"][0]["centroid"]
    assert abs(cx - 15.0) < 1e-6 and abs(cy - 5.0) < 1e-6, q["matches"][0]

    # A component imported outside the placement tools has unknown provenance.
    rev = cb.call("doc_tree", {"doc_id": asm})["revision"]
    imported = cb.call("doc_apply_ops", {
        "doc_id": asm, "base_revision": rev, "with_tess": False,
        "ops": [{"op": "asm_insert_component", "source": {"path": pin_path},
                 "name": "Imported", "fixed": True,
                 "transform": [1, 0, 0, 100, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}]})
    imported_id = imported["id_map"]["$0"]
    saved = str(tmp_path / "aa_asm.step+")
    cb.call("doc_save", {"doc_id": asm, "path": saved})
    cb.call("doc_close", {"doc_id": asm})
    reopened = cb.call("doc_open", {"path": saved})["doc_id"]
    assert reopened != asm
    try:
        out, _ = _call("describe_assembly", {"doc_id": reopened})
        assert out["ok"], out
        bodies = {b["component"]: b for b in out["bodies"]}
        assert bodies["k2"]["placement_source"] == "mate", out
        assert bodies[imported_id]["placement_source"] is None, out
        # A later starting-pose note cannot override a persisted mate-owned pose.
        from services.mcp.cad_common import note_placement_source
        note_placement_source(reopened, "k2", "transform")
        out, _ = _call("list_world_bodies", {"doc_id": reopened})
        assert {b["component"]: b["placement_source"] for b in out["bodies"]}["k2"] == "mate"
    finally:
        cb.call("doc_close", {"doc_id": reopened})
    _call("close_part", {"doc_id": pin})
    _call("close_part", {"doc_id": plate})

def test_asm_mate_on_a_saved_assembly_resolves_relative_component_sources(
        tmp_path, monkeypatch):
    """Live 2026-09-24: asm_mate on the saved Rover Review failed "no such file" for every
    component whose stored source is relative to the assembly file (`Parts/…`,
    `../Frame/…`) — the tool resolved it against the worker's cwd instead."""
    parts = tmp_path / "Frame"
    asm_dir = tmp_path / "Assembly"
    parts.mkdir()
    asm_dir.mkdir()
    r, _ = _call("new_part", {"name": "rel_plate"})
    plate = r["doc_id"]
    _call("add_features", {"doc_id": plate, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(8), "positions": [[15, 5]]},
                {"face": {"of": "face", "side": "top"}})]})
    plate_path = str(parts / "rel_plate.step+")
    assert _call("save_part", {"doc_id": plate, "path": plate_path})[0]["ok"]
    r, _ = _call("new_part", {"name": "rel_pin"})
    pin = r["doc_id"]
    _call("add_features", {"doc_id": pin, "ops": [
        cb.feat("cylinder", "Pin", {"radius": cb.dim(4), "height": cb.dim(20)})]})
    pin_path = str(parts / "rel_pin.step+")
    assert _call("save_part", {"doc_id": pin, "path": pin_path})[0]["ok"]

    # Legacy saved assemblies may still contain sibling references outside the
    # assembly directory; they must keep resolving against the assembly file.
    asm = _call("new_assembly", {"name": "rel_asm"})[0]["doc_id"]
    saved = str(asm_dir / "rel_asm.step+")
    cb.call("doc_save", {"doc_id": asm, "path": saved})
    rev = cb.call("doc_tree", {"doc_id": asm})["revision"]
    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    pin_pose = [1, 0, 0, 15, 0, 1, 0, 5, 0, 0, 1, 30, 0, 0, 0, 1]
    cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev, "with_tess": False, "ops": [
        {"op": "asm_insert_component", "source": {"path": "../Frame/rel_plate.step+"},
         "name": "Plate", "fixed": True, "transform": ident},
        {"op": "asm_insert_component", "source": {"path": "../Frame/rel_pin.step+"},
         "name": "Pin", "fixed": False, "transform": pin_pose}]})
    elsewhere = tmp_path / "worker" / "cwd"  # ../Frame from here is NOT the parts folder
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    try:
        ids = [c["id"] for c in cb.call("doc_tree", {"doc_id": asm})["tree"]["components"]]
        r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric",
                                  "a": {"component": ids[0],
                                        "query": {"of": "face", "surf": "round"}},
                                  "b": {"component": ids[1],
                                        "query": {"of": "face", "surf": "round"}}})
        assert r["ok"], r
        cb.call("doc_save", {"doc_id": asm, "path": saved})
    finally:
        cb.call("doc_close", {"doc_id": asm})
    reopened = cb.call("doc_open", {"path": saved})["doc_id"]
    try:
        comps = cb.call("doc_tree", {"doc_id": reopened})["tree"]["components"]
        sources = [(c.get("source") or {}).get("path") for c in comps]
        assert sources == [plate_path, pin_path], sources
        r, _ = _call("describe_assembly", {"doc_id": reopened})
        assert len(r["bodies"]) == 2, r
    finally:
        cb.call("doc_close", {"doc_id": reopened})
        _call("close_part", {"doc_id": pin})
        _call("close_part", {"doc_id": plate})


def test_new_assembly_resolves_relative_sources_against_the_project(tmp_path, monkeypatch):
    """Live 2026-10-10 (six-axis arm): the agent copied `source` objects from a SAVED
    assembly (`parts/redesign/claw_base.step+`, relative to its file) into a NEW unsaved
    assembly. The engine resolved them against hostd's cwd and failed
    "doc_open: no such file: /home/user/parts/...". An unsaved assembly resolves a
    relative source against the project folder; describe_assembly reports the absolute file."""
    project = tmp_path / "project"
    (project / "parts").mkdir(parents=True)
    r, _ = _call("new_part", {"name": "proj_rel_box"})
    part = r["doc_id"]
    box = project / "parts" / "proj_rel_box.step+"
    try:
        added, _ = _call("add_features", {"doc_id": part, "ops": [
            cb.feat("box", "Body", {"length": cb.dim(12), "width": cb.dim(10),
                                    "height": cb.dim(8)})]})
        assert added["ok"], added
        assert _call("save_part", {"doc_id": part, "path": str(box)})[0]["ok"]
    finally:
        _call("close_part", {"doc_id": part})

    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))
    elsewhere = tmp_path / "hostd_cwd"  # parts/... from here is NOT the project
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    asm = _call("new_assembly", {"name": "proj_rel_asm", "fresh": True})[0]["doc_id"]
    try:
        inserted, _ = _call("add_features", {"doc_id": asm, "ops": [
            {"op": "asm_insert_component", "source": {"path": "parts/proj_rel_box.step+"},
             "name": "Box", "fixed": True}]})
        assert inserted["ok"], inserted

        described, _ = _call("describe_assembly", {"doc_id": asm})
        assert described["component_source_base"] == str(project), described
        comps = described["components"]
        # The stored source is unchanged; the resolved file sits next to it.
        assert [c["source"] for c in comps] == [{"path": "parts/proj_rel_box.step+"}], comps
        assert comps[0]["source_resolved"] == str(box), comps

        missing, _ = _call("add_features", {"doc_id": asm, "ops": [
            {"op": "asm_insert_component", "source": {"path": "parts/absent.step+"}}]})
        assert missing["ok"] is False, missing
        text = json.dumps(missing)
        assert str(project / "parts" / "absent.step+") in text, missing
        assert f"base folder '{project}'" in text, missing
        assert str(elsewhere) not in text, missing
    finally:
        cb.call("doc_close", {"doc_id": asm})



def test_saved_assembly_rebases_component_sources_when_project_moves(tmp_path, monkeypatch):
    import shutil
    import zipfile

    original = tmp_path / "original"
    relocated = tmp_path / "relocated"
    (original / "parts").mkdir(parents=True)
    external = tmp_path / "external.step+"
    r, _ = _call("new_part", {"name": "portable_box"})
    part = r["doc_id"]
    try:
        added, _ = _call("add_features", {"doc_id": part, "ops": [
            cb.feat("box", "Body", {"length": cb.dim(12), "width": cb.dim(10),
                                    "height": cb.dim(8)})]})
        assert added["ok"], added
        inside = original / "parts" / "portable_box.step+"
        assert _call("save_part", {"doc_id": part, "path": str(inside)})[0]["ok"]
        shutil.copyfile(inside, external)
    finally:
        _call("close_part", {"doc_id": part})

    asm = _call("new_assembly", {"name": "portable_assembly"})[0]["doc_id"]
    source_file = original / "portable_assembly.step+"
    cb.call("doc_save", {"doc_id": asm, "path": str(source_file)})
    try:
        for path, name, position in (("parts/portable_box.step+", "inside", [0, 0, 0]),
                                     (external, "external", [100, 0, 0])):
            inserted, _ = _call("asm_add_component", {
                "doc_id": asm, "path": str(path), "name": name,
                "fixed": True, "at": position})
            assert inserted["ok"], inserted
        saved, _ = _call("save_part", {"doc_id": asm, "path": str(source_file)})
        assert saved["ok"], saved
        with zipfile.ZipFile(source_file) as archive:
            stored = json.loads(archive.read("document.json"))
            paths = {c["name"]: c["source"]["path"] for c in stored["components"]}
            assert paths == {"inside": "parts/portable_box.step+",
                             "external": str(external)}, paths
            assert "cache.json" in archive.namelist()
            cache = json.loads(archive.read("cache.json"))
            assert cache["components"][0]["path"] == paths["inside"]
            assert cache["components"][1]["path"] == paths["external"]
            assert (cache["response"]["tree"]["components"][0]["source"]["path"]
                    == paths["inside"])
        live = cb.call("doc_tree", {"doc_id": asm})["tree"]["components"]
        assert live[0]["source"]["path"] == paths["inside"]
    finally:
        _call("close_part", {"doc_id": asm})

    shutil.move(str(original), str(relocated))
    monkeypatch.chdir(tmp_path)
    opened = cb.call("doc_open", {"path": str(relocated / source_file.name)})
    assert opened.get("cache_hit") is True, opened
    reopened = opened["doc_id"]
    try:
        report, _ = _call("describe_assembly", {"doc_id": reopened})
        assert report["ok"] and len(report["bodies"]) == 2, report
        checked, _ = _call("check_interference", {"doc_id": reopened})
        assert checked["ok"] and not checked["interferences"], checked
    finally:
        _call("close_part", {"doc_id": reopened})



def test_asm_mate_operand_shapes(tmp_path):
    """Class-2/4 selector-shape gaps in asm_mate `a`/`b` — hunted alongside cad_doc_tools'
    refs.base/refs.tool boolean-operand fix. A bare component-id string, a one-element list
    wrapping the operand dict, and the {"kind":"component","id":...} selector shape other ref
    slots use are all read-equivalent to {"component": "kN", "query": ...} instead of being
    hard-rejected; a genuinely empty/placeholder operand still raises a teaching error."""
    r, _ = _call("new_part", {"name": "op_plate"})
    plate = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": plate, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(8), "positions": [[15, 5]]},
                {"face": {"of": "face", "side": "top"}}),
    ]})
    assert r["ok"], r
    plate_path = str(tmp_path / "op_plate.step+")
    _call("save_part", {"doc_id": plate, "path": plate_path})

    r, _ = _call("new_part", {"name": "op_pin"})
    pin = r["doc_id"]
    _call("add_features", {"doc_id": pin, "ops": [
        cb.feat("cylinder", "Pin", {"radius": cb.dim(4), "height": cb.dim(20)})]})
    pin_path = str(tmp_path / "op_pin.step+")
    _call("save_part", {"doc_id": pin, "path": pin_path})

    r, _ = _call("new_assembly", {"name": "op_asm"})
    asm = r["doc_id"]
    _call("asm_add_component", {"doc_id": asm, "path": plate_path, "fixed": True})
    _call("asm_add_component", {"doc_id": asm, "path": pin_path, "at": [15, 5, 30]})

    good_query = {"of": "face", "surf": "round"}

    # bare component-id string in place of {"component": "kN"} (query defaults to "top",
    # exactly like the dict path already does when `query` is omitted).
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": "k1", "b": {"component": "k2", "query": good_query}})
    assert r["ok"] and r["mate"], r

    # one-element list wrapping the operand dict.
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": [{"component": "k1", "query": good_query}],
                              "b": {"component": "k2", "query": good_query}})
    assert r["ok"] and r["mate"], r

    # {"kind":"component","id":...} selector shape (the pattern other ref slots use).
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": {"kind": "component", "id": "k1", "query": good_query},
                              "b": {"component": "k2", "query": good_query}})
    assert r["ok"] and r["mate"], r

    # genuinely empty operand still teaches instead of guessing.
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": "none", "b": {"component": "k2", "query": good_query}})
    assert r["ok"] is False and "empty" in r["error"], r

    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": {}, "b": {"component": "k2", "query": good_query}})
    assert r["ok"] is False and "component" in r["error"], r

    # a multi-item list is ambiguous, not silently the first element.
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                              "a": [{"component": "k1", "query": good_query},
                                    {"component": "k1", "query": good_query}],
                              "b": {"component": "k2", "query": good_query}})
    assert r["ok"] is False and "list" in r["error"], r

    _call("close_part", {"doc_id": asm})
    _call("close_part", {"doc_id": pin})
    _call("close_part", {"doc_id": plate})


def test_asm_add_component_relative_path_is_project_root_relative(tmp_path, monkeypatch):
    """Live c245: with the assembly saved in `<project>/assemblies/`, asm_add_component(
    path="parts/x.step+") resolved only against the assembly folder and failed "no such
    file" beside the part save_part had just written. Relative paths are project-root
    relative like every other project tool; a miss lists every candidate tried."""
    project = tmp_path / "plane"
    (project / "parts").mkdir(parents=True)
    (project / "assemblies").mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    elsewhere = tmp_path / "worker"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    r, _ = _call("new_part", {"name": "rp_frame"})
    part = r["doc_id"]
    _call("add_features", {"doc_id": part, "ops": [
        cb.feat("box", "Frame", {"length": cb.dim(20), "width": cb.dim(10),
                                 "height": cb.dim(5)})]})
    part_path = project / "parts" / "rp_frame.step+"
    assert _call("save_part", {"doc_id": part, "path": str(part_path)})[0]["ok"]
    asm = _call("new_assembly", {"name": "rp_asm"})[0]["doc_id"]
    cb.call("doc_save", {"doc_id": asm, "path": str(project / "assemblies" / "rp_asm.step+")})
    try:
        r, _ = _call("asm_add_component", {"doc_id": asm, "path": "parts/rp_frame.step+",
                                           "name": "frame", "at": [0, 0, -16.3]})
        assert r["ok"], r
        sources = [(c.get("source") or {}).get("path")
                   for c in cb.call("doc_tree", {"doc_id": asm})["tree"]["components"]]
        assert sources == [str(part_path)], sources

        r, _ = _call("asm_add_component", {"doc_id": asm, "path": "parts/missing.step+"})
        assert r["ok"] is False, r
        assert str(project / "parts" / "missing.step+") in r["tried"], r
        assert str(project / "assemblies" / "parts" / "missing.step+") in r["tried"], r
    finally:
        cb.call("doc_close", {"doc_id": asm})
        _call("close_part", {"doc_id": part})


def test_asm_mate_accepts_face_sids(tmp_path):
    """Live c245: asm_mate(a={"component":"k1","query":"F2bb10f"}) failed "geom query: bad
    'side' 'F2bb10f'" — a bare string query was shipped to the engine as a side word. The
    sids describe_part/query_geometry/render tiles hand out name ONE face: from the
    component's source part, or off the assembly's posed instance."""
    r, _ = _call("new_part", {"name": "sid_plate"})
    plate = r["doc_id"]
    _call("add_features", {"doc_id": plate, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(8), "positions": [[15, 5]]},
                {"face": {"of": "face", "side": "top"}})]})
    plate_path = str(tmp_path / "sid_plate.step+")
    assert _call("save_part", {"doc_id": plate, "path": plate_path})[0]["ok"]
    r, _ = _call("new_part", {"name": "sid_pin"})
    pin = r["doc_id"]
    _call("add_features", {"doc_id": pin, "ops": [
        cb.feat("cylinder", "Pin", {"radius": cb.dim(4), "height": cb.dim(20)})]})
    pin_path = str(tmp_path / "sid_pin.step+")
    assert _call("save_part", {"doc_id": pin, "path": pin_path})[0]["ok"]

    round_face = {"of": "face", "surf": "round"}
    bore = cb.call("doc_query", {"doc_id": plate, "query": round_face})["matches"]
    shank = cb.call("doc_query", {"doc_id": pin, "query": round_face})["matches"]
    assert len(bore) == 1 and len(shank) == 1, (bore, shank)

    asm = _call("new_assembly", {"name": "sid_asm"})[0]["doc_id"]
    try:
        _call("asm_add_component", {"doc_id": asm, "path": plate_path, "fixed": True})
        _call("asm_add_component", {"doc_id": asm, "path": pin_path, "at": [15, 5, 30]})
        # Part-frame sids: bare string and the {"sid": …} query form.
        r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                                  "a": {"component": "k1", "query": bore[0]["sid"]},
                                  "b": {"component": "k2", "query": {"sid": shank[0]["sid"]}}})
        assert r["ok"] and r["mate"], r

        # Assembly-frame sid of the posed pin (stable_sid hashes the WORLD centroid).
        posed = [m for m in cb.call("doc_query", {"doc_id": asm, "query": round_face})["matches"]
                 if m["centroid"][2] > 20]
        assert len(posed) == 1 and posed[0]["sid"] != shank[0]["sid"], posed
        r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                                  "a": {"component": "k1", "query": bore[0]["sid"]},
                                  "b": {"component": "k2", "sid": posed[0]["sid"]}})
        assert r["ok"] and r["mate"], r

        # The pin's sid is not a face of the plate: a miss teaches, never the side parser.
        r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "concentric", "solve": False,
                                  "a": {"component": "k1", "query": posed[0]["sid"]},
                                  "b": {"component": "k2", "query": round_face}})
        assert r["ok"] is False and "carries sid" in r["error"], r
        assert "bad 'side'" not in r["error"], r
    finally:
        _call("close_part", {"doc_id": asm})
        _call("close_part", {"doc_id": pin})
        _call("close_part", {"doc_id": plate})



def test_simulate_motion_mate_id_placeholder(tmp_path):
    """Class-2 selector-shape gap: simulate_motion's mate_id="" / "none" / "null" must read
    the same as omitting it (auto-pick), not fail a literal lookup for a mate named that."""
    r, _ = _call("new_part", {"name": "mp_plate"})
    plate = r["doc_id"]
    _call("add_features", {"doc_id": plate, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                 "height": cb.dim(10)})]})
    plate_path = str(tmp_path / "mp_plate.step+")
    _call("save_part", {"doc_id": plate, "path": plate_path})

    r, _ = _call("new_part", {"name": "mp_arm"})
    arm = r["doc_id"]
    _call("add_features", {"doc_id": arm, "ops": [
        cb.feat("cylinder", "Arm", {"radius": cb.dim(4), "height": cb.dim(20)})]})
    arm_path = str(tmp_path / "mp_arm.step+")
    _call("save_part", {"doc_id": arm, "path": arm_path})

    r, _ = _call("new_assembly", {"name": "mp_asm"})
    asm = r["doc_id"]
    _call("asm_add_component", {"doc_id": asm, "path": plate_path, "fixed": True})
    _call("asm_add_component", {"doc_id": asm, "path": arm_path, "at": [10, 10, 30]})
    r, _ = _call("asm_mate", {"doc_id": asm, "mate_type": "distance", "value": 5.0,
                              "solve": True,
                              "a": {"component": "k1", "query": {"of": "face", "side": "top"}},
                              "b": {"component": "k2", "query": {"of": "face", "side": "bottom"}}})
    assert r["ok"], r

    baseline, _ = _call("simulate_motion", {"doc_id": asm, "stop": 10.0, "steps": 3,
                                            "check_interference": False})
    assert baseline["ok"], baseline

    for placeholder in ("", "none", "None", "null", "N/A"):
        r, _ = _call("simulate_motion", {"doc_id": asm, "mate_id": placeholder, "stop": 10.0,
                                         "steps": 3, "check_interference": False})
        assert r["ok"] and r["mate_id"] == baseline["mate_id"], (placeholder, r)

    _call("close_part", {"doc_id": asm})
    _call("close_part", {"doc_id": arm})
    _call("close_part", {"doc_id": plate})


# ------------------------------------------------------------------ LLM placement dialect
# A live run died on the op below: the model spelled a cylinder's placement the way most CAD
# APIs do — placement:{origin,direction} + boolean:"union" — and the engine's now-CLOSED
# primitive vocabulary rejected all three keys outright. cad_doc_tools._normalize_feature_ops
# rewrites that dialect into the canonical at/axis/mode BEFORE the call. The unit tests lock
# the rewrite; the engine tests lock the build it unblocks.


def _dialect_spout_op() -> dict:
    """The VERBATIM failing op, rebuilt fresh on every call.

    A shared module-level literal would be laundered by the first test that normalizes it
    (or handed to the engine twice), so the purity test could never fail.
    """
    return {"op": "add_feature", "feature": {
        "type": "cylinder", "name": "DrainSpout",
        "params": {"radius": {"value": 25, "expr": None},
                   "height": {"value": 60, "expr": None}},
        "placement": {"origin": [200, 0, 40], "direction": [1, 0, 0]},
        "boolean": "union"}}


def _dialect_cyl(**extra) -> dict:
    """A minimal canonical cylinder feature plus the dialect key(s) under test."""
    return {"type": "cylinder", "name": "C",
            "params": {"radius": cb.dim(5), "height": cb.dim(10)}, **extra}


def _norm_feature(feature: dict) -> dict:
    """Normalize one add_feature op; return the rewritten feature FLATTENED as the engine
    sees it (session.cpp::op_add_feature hoists every non-structural key into params, an
    existing params entry winning), so an assertion doesn't care which level a key landed at.
    """
    from services.mcp.cad_doc_tools import _normalize_feature_ops

    feat = _normalize_feature_ops([{"op": "add_feature", "feature": feature}])[0]["feature"]
    flat = {k: v for k, v in feat.items() if k != "params"}
    flat.update(feat.get("params") or {})
    return flat


def test_normalize_feature_ops_rewrites_the_live_placement_dialect():
    """The exact op from the live failure becomes canonical at/axis/mode."""
    flat = _norm_feature(_dialect_spout_op()["feature"])
    assert flat["at"] == [200, 0, 40], flat
    assert flat["axis"] == [1, 0, 0], flat
    assert flat["mode"] == "boss", flat
    # CONSUMED, not left beside the rewrite — a leftover dialect key is itself a rejected batch.
    assert "placement" not in flat and "boolean" not in flat, flat
    # The genuine params survive untouched.
    assert flat["radius"] == {"value": 25, "expr": None}, flat
    assert flat["height"] == {"value": 60, "expr": None}, flat


def test_normalize_feature_ops_does_not_mutate_the_caller_ops():
    """Pure: the caller's list, op and nested feature/params dicts come back untouched."""
    from services.mcp.cad_doc_tools import _normalize_feature_ops

    op = _dialect_spout_op()
    before = json.loads(json.dumps(op))
    ops = [op]
    out = _normalize_feature_ops(ops)
    assert op == before, op
    assert "placement" in op["feature"] and "boolean" in op["feature"], op
    assert "at" not in op["feature"] and "mode" not in op["feature"], op
    assert ops == [before], ops
    assert out is not ops and out[0] is not op
    assert out[0]["feature"]["at"] == [200, 0, 40], out  # the copy really was rewritten


def test_normalize_feature_ops_alias_table():
    """Every dialect alias maps to its canonical key and is consumed."""
    for key in ("position", "location", "origin"):
        flat = _norm_feature(_dialect_cyl(**{key: [1, 2, 3]}))
        assert flat["at"] == [1, 2, 3] and key not in flat, (key, flat)
    for key in ("direction", "dir", "normal", "orientation"):
        flat = _norm_feature(_dialect_cyl(**{key: [0, 1, 0]}))
        assert flat["axis"] == [0, 1, 0] and key not in flat, (key, flat)
    for key in ("boolean", "operation"):
        for word in ("union", "fuse"):
            flat = _norm_feature(_dialect_cyl(**{key: word}))
            assert flat["mode"] == "boss" and key not in flat, (key, word, flat)
        for word in ("cut", "subtract", "difference"):
            flat = _norm_feature(_dialect_cyl(**{key: word}))
            assert flat["mode"] == "cut" and key not in flat, (key, word, flat)
        flat = _norm_feature(_dialect_cyl(**{key: "new_body"}))
        assert flat["merge"] is False and "mode" not in flat and key not in flat, (key, flat)


def test_normalize_feature_ops_rejects_intersect():
    """The engine has no primitive intersect mode: say so instead of silently fusing."""
    with pytest.raises(ValueError) as exc:
        _norm_feature(_dialect_cyl(boolean="intersect"))
    msg = str(exc.value)
    assert "intersect" in msg, msg
    assert 'mode:"boss"' in msg and 'mode:"cut"' in msg, msg  # names the supported values


def test_normalize_feature_ops_leaves_a_canonical_op_alone():
    """An op already in the documented spelling round-trips byte-for-byte."""
    from services.mcp.cad_doc_tools import _normalize_feature_ops

    ops = [{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "Spout",
        "params": {"radius": cb.dim(25), "height": cb.dim(60)},
        "at": [200, 0, 40], "axis": [1, 0, 0], "mode": "boss", "centered": False}}]
    expect = json.loads(json.dumps(ops))
    assert _normalize_feature_ops(ops) == expect


# ------------------------------------------------------------------ LLM dimension dialect
# The same live run also lost three ways a person states a SIZE: a unit suffix ("25mm" dies
# in the engine's expression lexer as «trailing tokens»), a cylinder spelled by `diameter`,
# and a box spelled by `size:[l,w,h]` — the last two hard-rejected as a missing required
# param. _normalize_feature_ops rewrites all three into the engine's grammar and echoes what
# it read back as `interpreted`. The danger case is `diameter`, which is a REAL param on hole
# and bolt_circle: halving it there would silently drill a bore at half size.


def _norm_dims(feature: dict):
    """Normalize one add_feature op; return (flattened feature, interpreted notes)."""
    from services.mcp.cad_doc_tools import _normalize_feature_ops

    notes: list = []
    feat = _normalize_feature_ops([{"op": "add_feature", "feature": feature}], notes)[0]["feature"]
    flat = {k: v for k, v in feat.items() if k != "params"}
    flat.update(feat.get("params") or {})
    return flat, notes


def test_dim_unit_suffix_converts_to_millimetres():
    """<number><unit> becomes the mm number the engine's expression grammar can take."""
    from services.mcp.cad_doc_tools import _dim_mm

    assert _dim_mm("25mm") == 25
    assert _dim_mm("2.5cm") == 25
    assert _dim_mm("0.5 m") == 500
    assert _dim_mm("1in") == 25.4
    assert _dim_mm('2.5"') == 63.5
    assert _dim_mm("1ft") == 304.8
    assert _dim_mm("2'") == 609.6
    assert _dim_mm("5'6\"") == 1676.4      # the compound imperial form people speak
    assert _dim_mm("20thou") == 0.508
    assert _dim_mm("-3mm") == -3


def test_dim_unit_suffix_leaves_everything_else_for_the_engine():
    """Only a WHOLE number+unit string converts — an expression must reach the evaluator."""
    from services.mcp.cad_doc_tools import _dim_mm

    for s in ("width/2", "25", "2*10", "M8", "1e3", "3d", "", "top", "shaft_d"):
        assert _dim_mm(s) is None, s


def test_normalize_dims_converts_units_and_reports_them():
    """A unit-suffixed param lands as a number; the rewrite is echoed, not silent."""
    flat, notes = _norm_dims({"type": "box", "name": "Blk",
                              "params": {"length": "1in", "width": 30, "height": cb.dim(20)}})
    assert flat["length"] == 25.4 and flat["width"] == 30 and flat["height"] == cb.dim(20)
    assert any("1in" in n and "25.4" in n for n in notes), notes
    # the unit may also arrive in its own field, or inside the dim object's value
    flat, _ = _norm_dims({"type": "box", "name": "B",
                          "params": {"length": {"value": 2, "unit": "in"},
                                     "width": {"value": "3cm", "expr": None}, "height": 5}})
    assert flat["length"] == {"value": 50.8, "expr": None}, flat
    assert flat["width"] == {"value": 30, "expr": None}, flat


def test_normalize_dims_never_touches_an_expression():
    """`expr` is the engine's job: "width/2" reaches it byte-for-byte, in either spelling."""
    from services.mcp.cad_doc_tools import _normalize_feature_ops

    ops = [{"op": "add_feature", "feature": {
        "type": "cylinder", "name": "E",
        "params": {"radius": {"value": 0, "expr": "width/2"}, "height": "width/4"}}}]
    expect = json.loads(json.dumps(ops))
    notes: list = []
    assert _normalize_feature_ops(ops, notes) == expect
    assert notes == []


def test_normalize_diameter_to_radius_per_primitive():
    """diameter -> radius (HALVED), with the engine's real param name per primitive."""
    flat, notes = _norm_dims({"type": "cylinder", "name": "Shaft",
                              "params": {"diameter": 50, "height": 10}})
    assert flat["radius"] == 25 and "diameter" not in flat, flat
    assert any("diameter 50" in n and "radius 25" in n for n in notes), notes

    flat, _ = _norm_dims({"type": "sphere", "name": "S", "params": {"dia": "1in"}})
    assert flat["radius"] == 12.7, flat          # unit converted BEFORE the halving

    flat, _ = _norm_dims({"type": "cone", "name": "C",
                          "params": {"d1": 40, "d2": 10, "height": 10}})
    assert flat["r1"] == 20 and flat["r2"] == 5, flat

    flat, _ = _norm_dims({"type": "torus", "name": "T",
                          "params": {"major_diameter": 100, "minor_diameter": 10}})
    assert flat["major_r"] == 50 and flat["minor_r"] == 5, flat

    # A feature-LEVEL diameter is consumed too: the engine hard-rejects an unknown
    # feature key on a primitive, so leaving it beside the rewrite is a rejected batch.
    flat, _ = _norm_dims({"type": "cylinder", "name": "X", "diameter": 8,
                          "params": {"height": 10}})
    assert flat["radius"] == 4 and "diameter" not in flat, flat

    # A non-numeric diameter is halved AS AN EXPRESSION — still the engine's to evaluate.
    flat, _ = _norm_dims({"type": "cylinder", "name": "Y",
                          "params": {"diameter": "shaft_d", "height": 10}})
    assert flat["radius"] == {"value": 0.0, "expr": "(shaft_d)/2"}, flat


def test_normalize_diameter_never_rewrites_a_hole_or_bolt_circle():
    """hole/bolt_circle take a REAL `diameter`: halving it would silently drill half-size."""
    for ftype, extra in (("hole", {"style": "simple", "end": "through_all"}),
                         ("bolt_circle", {"count": 4, "pcd": 40})):
        flat, notes = _norm_dims({"type": ftype, "name": "H",
                                  "params": {"diameter": 10, **extra}})
        assert flat["diameter"] == 10, (ftype, flat)
        assert "radius" not in flat, (ftype, flat)
        assert notes == [], (ftype, notes)
    # the unit pass still applies there — it changes the SPELLING, never the size
    flat, notes = _norm_dims({"type": "hole", "name": "H",
                              "params": {"diameter": "0.25in", "style": "simple"}})
    assert flat["diameter"] == 6.35 and "radius" not in flat, flat
    assert notes and "6.35" in notes[0], notes


def test_normalize_box_size_shorthand():
    """size:[l,w,h] -> the three named dims; a scalar size is a cube."""
    flat, notes = _norm_dims({"type": "box", "name": "Blk", "params": {"size": [40, 30, 20]}})
    assert (flat["length"], flat["width"], flat["height"]) == (40, 30, 20), flat
    assert "size" not in flat, flat
    assert any("length 40" in n and "width 30" in n for n in notes), notes

    flat, _ = _norm_dims({"type": "box", "name": "Cube", "params": {"size": "1in"}})
    assert (flat["length"], flat["width"], flat["height"]) == (25.4, 25.4, 25.4), flat

    # An explicit named dim WINS over the shorthand; `size` is still consumed.
    flat, _ = _norm_dims({"type": "box", "name": "B",
                          "params": {"size": [40, 30, 20], "height": 5}})
    assert flat["height"] == 5 and flat["length"] == 40 and "size" not in flat, flat

    # Only a box: `size` on a thread is the fastener code "M8", not a dimension.
    flat, notes = _norm_dims({"type": "thread", "name": "T", "params": {"size": "M8"}})
    assert flat["size"] == "M8" and notes == [], (flat, notes)


def test_engine_builds_from_the_dimension_dialect():
    """END TO END: diameter, size and unit strings build the geometry they NAME.

    Measured, not assumed — a diameter-50 cylinder must have a radius-25 B-rep face, and
    a hole drilled by diameter 10 must have a radius-5 bore (the alias must not halve it).
    """
    # cylinder by diameter -> radius 25 (query the real B-rep face, not the volume)
    doc = _call("new_part", {"name": "dialect_dia"})[0]["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Shaft",
                                          "params": {"diameter": 50, "height": 10}}}]})
    assert r.get("ok"), r
    assert any("radius 25" in n for n in r.get("interpreted") or []), r
    q, _ = _call("query_geometry", {"doc_id": doc, "query": {"of": "face", "surf": "cylinder"}})
    assert [m["radius"] for m in q["matches"]] == [25.0], q

    # box by size:[l,w,h] -> 40x30x20 = 24000 mm^3
    box = _call("new_part", {"name": "dialect_size"})[0]["doc_id"]
    r, _ = _call("add_features", {"doc_id": box, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Blk",
                                          "params": {"size": [40, 30, 20]}}}]})
    assert r.get("ok"), r
    assert abs(r["volume_mm3"] - 24000.0) < 1e-6, r

    # unit-suffixed cubes: "25mm" == "2.5cm", and 1in is 25.4mm on a side
    for spelling, side in (("25mm", 25.0), ("2.5cm", 25.0), ("1in", 25.4)):
        # Distinct names per spelling: new_part RESUMES a part of the same name (that is
        # its contract), so reusing "dialect_unit" built the second cube INSIDE the first
        # and the engine rightly answered "boss added no material".
        cube = _call("new_part", {"name": f"dialect_unit_{spelling}"})[0]["doc_id"]
        r, _ = _call("add_features", {"doc_id": cube, "ops": [
            {"op": "add_feature", "feature": {
                "type": "box", "name": "C",
                "params": {"length": spelling, "width": spelling, "height": spelling}}}]})
        assert r.get("ok"), (spelling, r)
        assert abs(r["volume_mm3"] - side ** 3) < 1e-6, (spelling, r)
        _call("close_part", {"doc_id": cube})

    # a hole drilled BY DIAMETER keeps its diameter: a 10 bore is radius 5, not 2.5
    plate = _call("new_part", {"name": "dialect_hole"})[0]["doc_id"]
    r, _ = _call("add_features", {"doc_id": plate, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "P",
                                          "params": {"length": 60, "width": 60, "height": 10}}},
        {"op": "add_feature", "feature": {
            "type": "hole", "name": "H",
            "params": {"diameter": 10, "style": "simple", "end": "through_all"},
            "refs": {"face": {"of": "face", "side": "top"}}}}]})
    assert r.get("ok"), r
    assert not r.get("interpreted"), r  # nothing was rewritten on a hole
    q, _ = _call("query_geometry", {"doc_id": plate, "query": {"of": "face", "surf": "cylinder"}})
    assert [m["radius"] for m in q["matches"]] == [5.0], q

    for did in (doc, box, plate):
        _call("close_part", {"doc_id": did})


def test_as_face_query_parses_a_phrase_into_the_grammar():
    """Free text becomes STRUCTURED query keys — the whole sentence never lands in `side`."""
    from services.mcp.cad_common import _as_face_query

    assert _as_face_query("outer cylindrical wall face at bottom near x=200,y=0") == {
        "of": "face", "surf": "cylinder", "side": "bottom", "pick": "outer"}
    assert _as_face_query("top") == {"of": "face", "side": "top"}
    assert _as_face_query("all the edges") == {"of": "edge"}  # a kind alone is a valid query
    with pytest.raises(ValueError) as exc:
        _as_face_query("please make the thing look nicer")
    assert "geometry query" in str(exc.value) and '"of"' in str(exc.value), str(exc.value)
    q = {"of": "face", "surf": "cylinder", "side": "bottom"}
    assert _as_face_query(q) is q  # a dict is identity, never re-parsed


def test_add_features_accepts_the_live_placement_dialect_end_to_end():
    """The op that killed a live run now BUILDS: a drain spout on a tank wall.

    The block is 340 long, so its +X wall sits at x=170 — exactly where the (centred, the
    verbatim op says nothing about `centered`) 60mm spout starts. The boss therefore lands
    flush on the wall and adds its WHOLE volume, with no overlap to guess at.
    """
    r, _ = _call("new_part", {"name": "dialect_tank"})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Tank", {"length": cb.dim(340), "width": cb.dim(200),
                                "height": cb.dim(200)})]})
    assert r["ok"], r
    m0, _ = _call("measure_part", {"doc_id": doc})
    r, _ = _call("add_features", {"doc_id": doc, "ops": [_dialect_spout_op()]})
    assert r["ok"] is True, r
    assert not r.get("failed_features"), r
    assert all(f["ok"] for f in r["features"]), r
    m1, _ = _call("measure_part", {"doc_id": doc})
    delta = m1["mass"]["volume_mm3"] - m0["mass"]["volume_mm3"]
    expect = math.pi * 25.0 * 25.0 * 60.0
    assert abs(delta - expect) / expect < 0.01, (delta, expect)


def test_dialect_spout_is_oriented_and_off_origin():
    """at/axis are HONOURED, not merely accepted — assert what a dropped placement would break.

    Only the spout is built, with centered:false so its BASE sits at [200,0,40] and it runs
    60mm along +X. A 50-dia x 60 cylinder's axial inertia is the smallest of the three, so
    principal_axes[0] IS its spin axis: +-X here, +-Z (and a COM at the origin) if `axis`/`at`
    had been ignored.
    """
    r, _ = _call("new_part", {"name": "dialect_spout"})
    doc = r["doc_id"]
    op = _dialect_spout_op()
    op["feature"]["centered"] = False
    r, _ = _call("add_features", {"doc_id": doc, "ops": [op]})
    assert r["ok"] is True, r
    m, _ = _call("measure_part", {"doc_id": doc})
    com = m["mass"]["com"]
    assert abs(com[0] - 230.0) < 1e-3, com  # base 200 + half the 60mm length along +X
    assert abs(com[1]) < 1e-3 and abs(com[2] - 40.0) < 1e-3, com
    moments = m["mass"]["principal_moments"]
    assert moments[0] < moments[1], moments  # axes[0] is the axial (spin) direction
    ax = m["mass"]["principal_axes"][0]
    assert abs(abs(ax[0]) - 1.0) < 1e-6, (ax, moments)
    assert abs(ax[1]) < 1e-6 and abs(ax[2]) < 1e-6, ax


def test_rolled_back_batch_error_carries_the_engine_reason():
    """A rolled-back batch reports WHY, per feature, in `error` itself.

    The agent card truncates `error` and never reads `failed_features`, so the old constant
    ("no features were kept; fix and resend") left the model nothing actionable and it resent
    the identical ops.
    """
    r, _ = _call("new_part", {"name": "buried_boss"})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(100), "width": cb.dim(100),
                                 "height": cb.dim(100)})]})
    assert r["ok"], r
    # A small centred cylinder boss is buried inside the block: it adds no material, which is
    # a genuine engine rebuild failure (not a schema rejection), so the batch rolls back.
    bad, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "InnerPin", {"radius": cb.dim(5), "height": cb.dim(10)})]})
    assert bad["ok"] is False and bad["rolled_back"] is True, bad
    err = bad["error"]
    assert "added no material" in err, err   # the engine's ACTUAL reason
    assert "InnerPin" in err, err            # named with the feature the model wrote
    assert bad["failed_features"][0]["id"] in err, bad
    assert "no features were kept" not in err and "fix and resend" not in err, err


def test_query_geometry_echoes_the_resolved_query():
    """query_geometry reports ok + the RESOLVED query, and defaults `of` to "face"."""
    doc = _new_plate_with_hole()
    # No `of` at all — resolve_geom_query defaults it to "face" instead of erroring.
    r, _ = _call("query_geometry", {"doc_id": doc, "query": {"side": "+x"}})
    assert r["ok"] is True, r
    assert r["query"] == {"side": "+x"}, r  # a dict query is echoed verbatim
    assert r["count"] >= 1 and all(m.get("kind") == "face" for m in r["matches"]), r
    # A phrase is normalised into the grammar, and the echo shows what was actually asked.
    r, _ = _call("query_geometry", {"doc_id": doc, "query": "the flat top face"})
    assert r["ok"] is True, r
    q = r["query"]
    assert isinstance(q, dict), r
    assert q["of"] == "face" and q["side"] == "top", r  # a side WORD, not the sentence
    assert r["count"] >= 1, r
    # An empty result is called out rather than looking like a silent success.
    r, _ = _call("query_geometry", {"doc_id": doc, "query": {"of": "face", "surf": "sphere"}})
    assert r["ok"] is True and not r.get("matches"), r
    assert "no face/edge matched" in r["note"], r


# ---- inline-sketch auto-split + semantic hole depth -----------------------------------

#: The exact I-beam profile from the live run that was rejected for carrying its sketch
#: inline: 75 wide x 150 tall, 12.5 flange, 12.5 web. Section area is
#: 2 * (75 * 12.5) + 12.5 * 125 = 3437.5 mm^2.
_IBEAM_ENTS = [
    {"type": "line", "p1": [-37.5, -75], "p2": [37.5, -75]},
    {"type": "line", "p1": [37.5, -75], "p2": [37.5, -62.5]},
    {"type": "line", "p1": [37.5, -62.5], "p2": [6.25, -62.5]},
    {"type": "line", "p1": [6.25, -62.5], "p2": [6.25, 62.5]},
    {"type": "line", "p1": [6.25, 62.5], "p2": [37.5, 62.5]},
    {"type": "line", "p1": [37.5, 62.5], "p2": [37.5, 75]},
    {"type": "line", "p1": [37.5, 75], "p2": [-37.5, 75]},
    {"type": "line", "p1": [-37.5, 75], "p2": [-37.5, 62.5]},
    {"type": "line", "p1": [-37.5, 62.5], "p2": [-6.25, 62.5]},
    {"type": "line", "p1": [-6.25, 62.5], "p2": [-6.25, -62.5]},
    {"type": "line", "p1": [-6.25, -62.5], "p2": [-37.5, -62.5]},
    {"type": "line", "p1": [-37.5, -62.5], "p2": [-37.5, -75]},
]


def test_inline_profile_sketch_builds_the_same_solid_as_the_two_op_form():
    """A profile written INLINE on the extrude is auto-split into sketch+extrude.

    The single-op payload is verbatim from the live run that was rejected with
    "params.sketch carries INLINE geometry". Proof is GEOMETRY: the volume must equal
    both the analytic 3437.5 mm^2 * 500 mm and the hand-written two-op form's volume.
    """
    r, _ = _call("new_part", {"name": "ibeam_inline"})
    inline_doc = r["doc_id"]
    inline, _ = _call("add_features", {"doc_id": inline_doc, "ops": [
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "I-Beam Body",
            "params": {"depth": {"value": 500},
                       "sketch": {"plane": "XY", "entities": _IBEAM_ENTS}}}}]})
    assert inline["ok"], inline
    assert abs(inline["volume_mm3"] - 3437.5 * 500) < 1e-3, inline["volume_mm3"]

    r, _ = _call("new_part", {"name": "ibeam_pair"})
    pair_doc = r["doc_id"]
    pair, _ = _call("add_features", {"doc_id": pair_doc, "ops": [
        {"op": "add_feature", "feature": {
            "type": "sketch", "name": "P", "refs": {"plane": "plane.top"},
            "params": {"entities": _IBEAM_ENTS, "constraints": []}}},
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "I-Beam Body",
            "params": {"mode": "boss", "depth": {"value": 500}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0", "hint_index": 0}]}}}]})
    assert pair["ok"], pair
    assert abs(inline["volume_mm3"] - pair["volume_mm3"]) < 1e-6, (inline, pair)
    assert any("INLINE" in n for n in inline.get("interpreted") or []), inline


def test_inline_profile_split_emits_a_sketch_op_referenced_by_batch_index():
    """The lifted sketch is a real op BEFORE its consumer, referenced as "$<index>"."""
    from services.mcp import cad_doc_tools as T

    notes: list = []
    ops = T._split_inline_profile_sketches([
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body",
            "params": {"depth": {"value": 10},
                       "sketch": {"plane": "XZ", "entities": _IBEAM_ENTS}}}}], notes)
    assert len(ops) == 2, ops
    assert ops[0]["feature"]["type"] == "sketch", ops[0]
    assert ops[0]["feature"]["refs"]["plane"] == "plane.front", ops[0]
    assert "sketch" not in ops[1]["feature"]["params"], ops[1]
    # The ref is by NAME here; _resolve_batch_name_refs turns it into the "$N" position.
    resolved = T._resolve_batch_name_refs(ops, notes)
    assert resolved[1]["feature"]["refs"]["profile"] == [
        {"kind": "region", "sketch": "$0", "hint_index": 0}], resolved[1]


def test_inline_profile_split_covers_revolve_and_a_bare_point_loop():
    """Every profile-bearing type is covered, and a bare point loop becomes a polyline."""
    from services.mcp import cad_doc_tools as T

    notes: list = []
    ops = T._split_inline_profile_sketches([
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Cup",
            "params": {"axis": "Z", "angle": 360,
                       "profile": {"plane": "XZ",
                                   "points": [[10, 0], [20, 0], [20, 40], [10, 40]]}}}}],
        notes)
    assert len(ops) == 2 and ops[0]["feature"]["type"] == "sketch", ops
    ent = ops[0]["feature"]["params"]["entities"]
    assert len(ent) == 1 and ent[0]["type"] == "polyline" and ent[0]["closed"], ent
    assert ops[1]["feature"]["refs"]["profile"][0]["sketch"] == "CupProfile", ops[1]


def test_inline_profile_with_no_resolvable_plane_keeps_the_teaching_error():
    """Ambiguity is never guessed away — a plane-less inline profile still teaches."""
    from services.mcp import cad_doc_tools as T

    notes: list = []
    ops = [{"op": "add_feature", "feature": {
        "type": "extrude", "name": "Body",
        "params": {"depth": {"value": 10}, "sketch": {"entities": _IBEAM_ENTS}}}}]
    assert T._split_inline_profile_sketches(ops, notes) is ops, "must not split"
    assert notes == [], notes
    with pytest.raises(ValueError) as ei:
        T._normalize_profile_feature(ops[0]["feature"], "extrude", [])
    assert "INLINE geometry" in str(ei.value), str(ei.value)


def test_hole_depth_through_all_drills_all_the_way_through():
    """depth:{"value":"through_all"} is an END CONDITION, not a non-numeric distance.

    Rejected live as "params.depth.value: missing or non-numeric". `through_all` is the
    engine's own through-hole end (core/doc/document.cpp), so it needs no depth at all.
    Proof is geometry: the bore breaches BOTH faces, so it leaves no bottom cap — 7 faces
    against a blind hole's 8 — and removes exactly twice the material of a half-deep one.
    """
    def _drill(depth, tag):
        r, _ = _call("new_part", {"name": tag})
        doc = r["doc_id"]
        b, _ = _call("add_features", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Blk", "params": {
                "length": {"value": 100}, "width": {"value": 100},
                "height": {"value": 50}}}},
            {"op": "add_feature", "feature": {
                "type": "hole", "name": "Bore",
                "params": {"diameter": {"value": 20}, "depth": depth},
                "refs": {"face": {"of": "face", "side": "top"}}}}]})
        assert b["ok"], b
        q, _ = _call("query_geometry", {"doc_id": doc, "query": "all faces"})
        return b["volume_mm3"], q["count"]

    thru_vol, thru_faces = _drill({"value": "through_all"}, "bore_thru")
    blind_vol, blind_faces = _drill({"value": 25}, "bore_blind")
    solid = 100.0 * 100.0 * 50.0
    # Breached: no bottom cap face, where the blind bore keeps one.
    assert thru_faces == blind_faces - 1, (thru_faces, blind_faces)
    # Twice the depth -> twice the material removed (the blind bore is exactly half-deep).
    assert abs((solid - thru_vol) - 2.0 * (solid - blind_vol)) < 1.0, (thru_vol, blind_vol)
    assert thru_vol < blind_vol < solid, (thru_vol, blind_vol)


def test_hole_depth_nonsense_word_still_fails_with_the_end_condition_vocabulary():
    """An unparseable depth word must never become a silent no-op or a units complaint."""
    from services.mcp import cad_doc_tools as T

    with pytest.raises(ValueError) as ei:
        T._normalize_hole_feature(
            {"type": "hole", "name": "Bore",
             "params": {"diameter": {"value": 20}, "depth": {"value": "halfway"}}}, [])
    msg = str(ei.value)
    assert "halfway" in msg and "through_all" in msg and "blind hole" in msg, msg


# --------------------------------------------------------------------------------------
# LIVE 2D sketch relation/dimension dialect. Measured 2026-07-30 against the real solver:
# a rejected sketch takes the whole batch (and every extrude referencing it), and a
# SILENTLY IGNORED relation is worse — the sketch solves differently than the model
# believes. These go through the engine so the proof is the SOLVE, not `ok: true`.

_DIALECT_SKETCH = [
    {"type": "line", "id": "e1", "p1": [0, 0], "p2": [40, 3], "construction": False},
    {"type": "line", "id": "e2", "p1": [41, 1], "p2": [38, 25], "construction": False},
    {"type": "circle", "id": "e3", "center": [10, 12], "radius": 4, "construction": False},
    {"type": "circle", "id": "e4", "center": [30, 12], "radius": 3, "construction": False},
]


def _solve_sketch(name: str, constraints: list) -> dict:
    """Build a one-sketch part with `constraints` and return the SOLVED entities by id."""
    r, _ = _call("new_part", {"name": name, "fresh": True})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [{
        "op": "add_feature",
        "feature": {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"},
                    "params": {"entities": json.loads(json.dumps(_DIALECT_SKETCH)),
                               "constraints": constraints}}}]})
    assert r.get("ok"), r
    ov = cb.call("doc_sketch_overview", {"doc_id": doc, "sketch": "f1"})
    return {e["id"]: e for e in ov["entities"]}


def test_equal_radius_synonym_actually_equalises_the_radii():
    """«"equal_radius" is not a constraint type» killed the batch. Proof that the mapped
    relation reaches the SOLVER, not just validation: r=4 and r=3 must both become 3.5."""
    ents = _solve_sketch("eqrad", [{"type": "equal_radius", "refs": ["e3", "e4"]}])
    assert math.isclose(ents["e3"]["radius"], 3.5, abs_tol=1e-6), ents["e3"]
    assert math.isclose(ents["e4"]["radius"], 3.5, abs_tol=1e-6), ents["e4"]


def test_horizontal_distance_dialect_projects_onto_the_h_axis():
    """"horizontal_distance" is the engine's `distance` with axis:'h'. Dropping the axis
    would apply a TRUE point-to-point distance instead — different geometry, not a near
    miss: e1's endpoints stay 3 mm apart in y and spread to exactly 50 mm in x."""
    ents = _solve_sketch("hdist", [{"type": "horizontal_distance",
                                    "refs": ["e1.p1", "e1.p2"], "value": 50}])
    p1, p2 = ents["e1"]["p1"], ents["e1"]["p2"]
    assert math.isclose(abs(p2[0] - p1[0]), 50.0, abs_tol=1e-6), (p1, p2)
    assert math.isclose(abs(p2[1] - p1[1]), 3.0, abs_tol=1e-6), (p1, p2)


def test_reference_dimension_measures_instead_of_driving():
    """THE silent drop this session. {"type":"length","value":80,"reference":true} used to
    return ok and STRETCH e1 from 40.112 mm to 80 mm — the model asked for a measurement
    and got geometry it never requested. `reference` must mean the same as `driven`."""
    def _len(c):
        e = _solve_sketch("refdim", [c])["e1"]
        return math.dist(e["p1"], e["p2"])

    assert math.isclose(_len({"type": "length", "refs": ["e1"], "value": 80}), 80.0,
                        abs_tol=1e-3)
    authored = math.hypot(40.0, 3.0)
    for key in ("driven", "reference", "is_reference"):
        got = _len({"type": "length", "refs": ["e1"], "value": 80, key: True})
        assert math.isclose(got, authored, abs_tol=1e-6), (key, got)


def test_dangling_constraint_ref_names_the_sketch_ids():
    """A relation on an entity that does not exist must fail LOUDLY and say what the real
    ids are — never be dropped, which would leave a sketch that solves differently than
    the model believes it constrained."""
    r, _ = _call("new_part", {"name": "dangle", "fresh": True})
    r2, _ = _call("add_features", {"doc_id": r["doc_id"], "ops": [{
        "op": "add_feature",
        "feature": {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"},
                    "params": {"entities": json.loads(json.dumps(_DIALECT_SKETCH)),
                               "constraints": [{"type": "horizontal", "refs": ["e99"]}]}}}]})
    assert not r2.get("ok"), r2
    msg = str(r2.get("error"))
    assert "e99" in msg and "e1, e2, e3, e4" in msg, msg


def test_circular_sketch_pattern_full_sweep_no_longer_stacks_every_copy():
    """`angle` is the PER-STEP rotation, so {"mode":"circular","count":4,"angle":360} put
    all four copies exactly on the source — an op that reported ok and produced invisible
    duplicates. Read as the TOTAL sweep, the four centres must be four DISTINCT points."""
    r, _ = _call("new_part", {"name": "circpat", "fresh": True})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature",
         "feature": {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"},
                     "params": {"entities": [{"type": "circle", "id": "e1",
                                              "center": [10, 0], "radius": 2,
                                              "construction": False}],
                                "constraints": []}}},
        {"op": "sketch_pattern_entities", "sketch": "$0", "entities": ["e1"],
         "mode": "circular", "count": 4, "center": [0, 0], "angle": 360}]})
    assert r.get("ok"), r
    ov = cb.call("doc_sketch_overview", {"doc_id": doc, "sketch": "f1"})
    centres = {(round(e["center"][0], 3), round(e["center"][1], 3)) for e in ov["entities"]}
    assert len(centres) == 4, sorted(centres)


def test_move_duplicate_synonym_keeps_the_original():
    """{"duplicate":true} was ignored, so sketch_move carried the source geometry away
    instead of copying it — the op reported ok and the original was gone."""
    r, _ = _call("new_part", {"name": "mvcopy", "fresh": True})
    doc = r["doc_id"]
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        {"op": "add_feature",
         "feature": {"type": "sketch", "name": "S", "refs": {"plane": "plane.top"},
                     "params": {"entities": [{"type": "circle", "id": "e1",
                                              "center": [0, 0], "radius": 2,
                                              "construction": False}],
                                "constraints": []}}},
        {"op": "sketch_move", "sketch": "$0", "entities": ["e1"], "dx": 10, "dy": 0,
         "duplicate": True}]})
    assert r.get("ok"), r
    ov = cb.call("doc_sketch_overview", {"doc_id": doc, "sketch": "f1"})
    centres = sorted(round(e["center"][0], 3) for e in ov["entities"])
    assert centres == [0.0, 10.0], centres



if __name__ == "__main__":
    import sys

    if not cb.available():
        print("SKIP: libchudware.dll not staged")
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
