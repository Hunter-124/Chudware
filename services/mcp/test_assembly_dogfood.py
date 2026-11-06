"""Assembly reference recovery and mate diagnostics through the real MCP registry.

Run with the isolated daemon provided by services/mcp/conftest.py.
"""
from __future__ import annotations

import json
import uuid

import pytest

from services.mcp import core_bridge as cb
from services.mcp import cad_asm_tools
from services.mcp.test_cad_tools import _call

pytestmark = pytest.mark.skipif(not cb.available(), reason="CAD engine unavailable")


def _tool(name, **args):
    result, _ = _call(name, args)
    assert isinstance(result, dict), result
    return result


def _close_source_readers(directory):
    with cad_asm_tools._ASM_QUERY_LOCK:
        for filename in ("base.step+", "moving.step+"):
            cached = cad_asm_tools._ASM_QUERY_DOCS.pop(str(directory / filename), None)
            if cached:
                cb.call("doc_close", {"doc_id": cached[0]})


def _box(name, path):
    part = cb.call("doc_new", {"name": name, "kind": "part", "resume_if_open": False})["doc_id"]
    built = cb.call("doc_apply_ops", {"doc_id": part, "ops": [
        cb.feat("box", "Box", {"length": cb.dim(20), "width": cb.dim(20),
                                "height": cb.dim(10)})]})
    assert all(row["ok"] for row in built["rebuild"]["feature_status"]), built
    cb.call("doc_save", {"doc_id": part, "path": str(path)})
    return part


def _assembly(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    base_path, moving_path = tmp_path / "base.step+", tmp_path / "moving.step+"
    base = _box("mate_base_" + suffix, base_path)
    moving = _box("mate_moving_" + suffix, moving_path)
    assembly = cb.call("doc_new", {"name": "mate_" + suffix, "kind": "assembly",
                                   "resume_if_open": False})["doc_id"]
    inserted = cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [
        {"op": "asm_insert_component", "source": {"path": str(base_path)}, "fixed": True},
        {"op": "asm_insert_component", "source": {"path": str(moving_path)}, "fixed": False},
    ]})
    return base, moving, assembly, inserted["id_map"]["$0"], inserted["id_map"]["$1"]


def _seat(assembly, base, moving):
    return _tool("asm_mate", doc_id=assembly, mate_type="coincident", align="aligned",
                 a={"component": base, "query": {"of": "face", "side": "top"}},
                 b={"component": moving, "query": {"of": "face", "side": "bottom"}})

@pytest.mark.parametrize("new_status, expected_ok", [("ok", True), ("unresolved", False)])
def test_new_mate_reports_individual_status_separately(monkeypatch, new_status, expected_ok):
    class Tools:
        def __init__(self):
            self.functions = {}

        def tool(self):
            def register(fn):
                self.functions[fn.__name__] = fn
                return fn
            return register

    tools = Tools()
    cad_asm_tools.register_cad_asm_tools(tools)
    monkeypatch.setattr(cad_asm_tools, "_doc", lambda doc: doc)
    monkeypatch.setattr(cad_asm_tools, "resolve_mate_ref",
                        lambda doc, comps, tag, ref: {"ref": ref})
    monkeypatch.setattr(cad_asm_tools, "note_placement_source", lambda *args: None)
    monkeypatch.setattr(cad_asm_tools, "placement_source_of", lambda *args: "mate")
    monkeypatch.setattr(cad_asm_tools, "_mate_branch", lambda *args: {})
    monkeypatch.setattr(cad_asm_tools, "_publish_current", lambda *args: None)
    monkeypatch.setattr(cad_asm_tools, "_autosave", lambda *args: None)

    def call(method, params):
        if method == "asm_source_status":
            return {"ok": True, "status": "current", "matches": True, "sources": []}
        if method == "doc_tree":
            return {"tree": {"components": [{"id": "k1"}, {"id": "k2"}]}}
        if method == "doc_apply_ops":
            return {"ok": True, "id_map": {"$0": "m21"}}
        if method == "doc_solve_mates":
            return {"rebuild": {"converged": False, "mates": [
                {"id": "m10", "status": "unresolved", "reason": "lost face"},
                {"id": "m21", "status": new_status},
            ]}}
        if method == "asm_world_bodies":
            return {"bodies": []}
        raise AssertionError(method)

    monkeypatch.setattr(cad_asm_tools.cb, "call", call)
    result = json.loads(tools.functions["asm_mate"](
        "coincident", {"component": "k1"}, {"component": "k2"}, doc_id="doc"))
    assert result["ok"] is expected_ok
    assert result["mate_id"] == "m21" and result["mate_status"]["status"] == new_status
    assert result["network_converged"] is False
    assert result["blocking_mates"][0]["id"] == "m10"
    assert "do not retry" in result["note"]



def test_mates_survive_proven_source_edits_and_refuse_ambiguity(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    base = moving = assembly = reopened = None
    try:
        base, moving, assembly, fixed_id, moving_id = _assembly(tmp_path)
        seated = _seat(assembly, fixed_id, moving_id)
        assert seated["ok"] and seated["converged"], seated
        mate_id = seated["mate"]
        before = cb.call("doc_tree", {"doc_id": assembly})["tree"]["mates"][0]["refs"][1]["selector"]
        assert before.get("owner_feature"), before

        # A separate solid is an unrelated source edit; the picked box face is
        # unchanged. Check the LIVE assembly before saving or explicitly solving.
        extra = cb.feat("box", "Separate", {"length": cb.dim(4), "width": cb.dim(4),
                                            "height": cb.dim(4)})
        extra["feature"].update({"at": [100, 0, 0], "mode": "new", "merge": False})
        edited = cb.call("doc_apply_ops", {"doc_id": moving, "ops": [extra]})
        assert all(row["ok"] for row in edited["rebuild"]["feature_status"]), edited
        world = _tool("list_world_bodies", doc_id=assembly)
        assert world["ok"] and world["logical_member_count"] == 2, world
        assert world["physical_solid_count"] == 3, world
        live = _tool("solve_mates", doc_id=assembly)
        assert live["ok"] and next(m for m in live["mate_residuals"] if m["id"] == mate_id)["status"] == "ok", live

        cb.call("doc_save", {"doc_id": moving, "path": str(tmp_path / "moving.step+")})
        assembly_path = tmp_path / "assembly.step+"
        cb.call("doc_save", {"doc_id": assembly, "path": str(assembly_path)})
        cb.call("doc_close", {"doc_id": assembly})
        assembly = None
        reopened = cb.call("doc_open", {"path": str(assembly_path)})["doc_id"]
        after = _tool("solve_mates", doc_id=reopened)
        assert after["ok"] and after["converged"], after
        assert next(m for m in after["mate_residuals"] if m["id"] == mate_id)["status"] == "ok", after

        # Remove the independent body, then split the picked bottom face into two
        # symmetric, equally plausible patches. Neither is the original face.
        cutter = cb.feat("box", "Split", {"length": cb.dim(2), "width": cb.dim(30),
                                          "height": cb.dim(20)})
        cutter["feature"]["mode"] = "cut"
        edit = cb.call("doc_apply_ops", {"doc_id": moving, "ops": [
            {"op": "delete_feature", "id": "f2"}, cutter]})
        assert all(row["ok"] for row in edit["rebuild"]["feature_status"]), edit
        changed_world = _tool("list_world_bodies", doc_id=reopened)
        assert changed_world["ok"] and changed_world["physical_solid_count"] == 3, changed_world
        failed = _tool("solve_mates", doc_id=reopened)
        assert not failed["ok"] and failed["reason"] == "mate_unresolved", failed
        row = next(m for m in failed["mate_residuals"] if m["id"] == mate_id)
        assert row["status"] == "unresolved", row
        assert "local-frame candidates" in row["reason"] and "not automatically remapped" in row["reason"], row
        candidates = json.loads(row["reason"].split("shown): ", 1)[1])
        bottom = [c for c in candidates if c.get("normal") == [0.0, 0.0, -1.0]]
        assert any(c["centroid"][0] < 0 for c in bottom), candidates
        assert any(c["centroid"][0] > 0 for c in bottom), candidates
        persisted = cb.call("doc_tree", {"doc_id": reopened})["tree"]["mates"][0]["refs"][1]["selector"]
        assert persisted == before
    finally:
        for doc in (reopened, assembly, moving, base):
            if doc:
                cb.call("doc_close", {"doc_id": doc})
        _close_source_readers(tmp_path)


def test_new_mate_diagnostics_are_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    base = moving = assembly = None
    try:
        base, moving, assembly, fixed_id, moving_id = _assembly(tmp_path)
        for _ in range(6):
            result = _seat(assembly, fixed_id, moving_id)
            assert result["ok"], result
        result = _tool("asm_mate", doc_id=assembly, mate_type="distance", align="anti", value=30,
                       a={"component": fixed_id, "query": {"of": "face", "side": "left"}},
                       b={"component": moving_id, "query": {"of": "face", "side": "left"}})
        assert result["ok"] and result["converged"], result
        assert result["mate_residual_total"] == 7
        assert result["mate_residual_omitted"] == 7 - len(result["mate_residuals"])
        assert result["new_mate_status"]["id"] == result["mate"]
        assert any(row["id"] == result["mate"] for row in result["mate_residuals"])
        branch = result["mate_branch"]
        assert branch["configuration"] == "solved" and branch["normal_reference"] == "a", branch
        assert branch["reference_normal_world"] == pytest.approx([-1, 0, 0], abs=1e-8), branch
        assert branch["target_signed_gap_mm"] == pytest.approx(-30)
        assert branch["signed_gap_mm"] == pytest.approx(-30, abs=1e-7)
        assert result["logical_member_count"] == result["physical_solid_count"] == 2
    finally:
        for doc in (assembly, moving, base):
            if doc:
                cb.call("doc_close", {"doc_id": doc})
        _close_source_readers(tmp_path)
