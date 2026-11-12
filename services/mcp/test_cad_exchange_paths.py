"""CAD exchange paths stay anchored to the selected workspace.

Real-engine cases distinguish identically named source files in the workspace and
worker cwd. Other cases exercise destination guards and explicit resume behavior.

Run: python3 -m pytest services/mcp/test_cad_exchange_paths.py -q
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_exchange_tools as M  # noqa: E402

class _StubMCP:
    """Minimal @mcp.tool() host that keeps the raw function."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn
        return deco


@pytest.fixture()
def exchange(monkeypatch, tmp_path):
    """Registered exchange tools with cb.call recorded + the engine faked."""
    calls: list = []

    def _fake_call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [{"doc_id": "doc-live", "revision": 3}]}
        if method == "asm_world_bodies":
            return {"bodies": [{"name": "Block"}]}
        if method == "doc_open":
            return {"doc_id": "doc-opened"}
        if method == "drawing_open_standalone":
            return {"drawing_id": "dwg-opened", "name": "Drawing",
                    "source_state": "embedded_opened"}
        if method == "drawing_save_standalone":
            destination = Path(params["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"PK\x03\x04 fake drawing container")
            return {"drawing_id": params["drawing_id"], "path": str(destination),
                    "source_state": "live"}
        if method == "import_file":
            return {"doc_id": "doc-imported"}
        if method in ("doc_save", "export_file"):
            destination = Path(params["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fake native or interchange payload")
            return {"path": str(destination)}
        return {}

    monkeypatch.setattr(M.cb, "call", _fake_call)
    # The handoff publisher is irrelevant here (and would doc_save into the real handoff dir).
    monkeypatch.setattr(M, "_publish_current", lambda *a, **k: None)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)
    return mcp.tools, calls


@pytest.mark.parametrize("suffix", ["step+", "step"])
def test_import_reads_selected_workspace_not_same_named_worker_file(
        tmp_path, monkeypatch, suffix):
    from services.agent.request_context import bind_roots

    project = tmp_path / "project"
    worker = tmp_path / "worker"
    project.mkdir()
    worker.mkdir()
    monkeypatch.chdir(worker)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(worker))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(worker))
    monkeypatch.setattr(M, "_publish_current", lambda *a, **k: None)
    filename = f"block.{suffix}"
    for folder, length in ((project, 10), (worker, 30)):
        doc = M.cb.call("doc_new", {"name": "PathBlock"})["doc_id"]
        try:
            M.cb.call("doc_apply_ops", {
                "doc_id": doc, "ops": [M.cb.feat("box", "Block", {
                    "length": M.cb.dim(length), "width": M.cb.dim(20),
                    "height": M.cb.dim(3)})]})
            method = "doc_save" if suffix == "step+" else "export_file"
            M.cb.call(method, {"doc_id": doc, "path": str(folder / filename),
                               **({"format": "step"} if suffix == "step" else {})})
        finally:
            M.cb.call("doc_close", {"doc_id": doc})
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)

    with bind_roots(str(project)):
        result = json.loads(mcp.tools["import_part"](filename))

    assert result["ok"], result
    imported = result["doc_id"]
    try:
        mass = M.cb.call("eval_mass", {"doc_id": imported})
        assert mass["volume_mm3"] == pytest.approx(600)
        assert result["imported_from"] == filename
    finally:
        M.cb.call("doc_close", {"doc_id": imported})


def test_export_part_relative_destination_goes_absolute(exchange, tmp_path, monkeypatch):
    """A relative export uses the selected workspace, not worker or daemon cwd."""
    tools_fn, calls = exchange
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)

    out = json.loads(tools_fn["export_part"](fmt="step", path="out/bracket.step",
                                             doc_id="doc-live"))

    assert out["ok"] is True, out
    exp = [p for m, p in calls if m == "export_file"]
    assert len(exp) == 1, calls
    assert exp[0]["path"] == str(tmp_path / "out" / "bracket.step")
    assert os.path.isabs(exp[0]["path"])
    assert Path(out["path"]).is_file()
    assert not (worker / "out").exists()


def test_export_part_stepplus_alias_goes_absolute(exchange, tmp_path, monkeypatch):
    """(b) The native step+ alias routes through doc_save with an absolute path too."""
    tools_fn, calls = exchange
    worker = tmp_path / "continuation-worker"
    worker.mkdir()
    monkeypatch.chdir(worker)
    recorded = []
    monkeypatch.setattr(M, "_note_session_saved", lambda doc, path: recorded.append((doc, path)))

    out = json.loads(tools_fn["export_part"](fmt="step+", path="deliverable",
                                             doc_id="doc-live"))

    assert out["format"] == "step+", out
    saves = [p for m, p in calls if m == "doc_save"]
    assert len(saves) == 1, calls
    assert saves[0]["path"] == str(tmp_path / "deliverable.step+")
    assert saves[0]["if_no_human_dirty"] is True
    assert out["ok"] is True, out
    assert Path(out["path"]).is_file()
    assert recorded == [("doc-live", str(tmp_path / "deliverable.step+"))]
    assert list(worker.iterdir()) == []

def test_native_export_reports_path_binding_conflict(exchange, monkeypatch, tmp_path):
    tools, _ = exchange
    original = M.cb.call
    def guarded(method, params):
        if method == "doc_save":
            raise M.CoreError("path already bound", code="path_bound_to_other_doc",
                              detail={"other_doc_id": "bound"})
        return original(method, params)
    monkeypatch.setattr(M.cb, "call", guarded)
    out = json.loads(tools["export_part"](fmt="step+", path=str(tmp_path / "bound.step+"),
                                          doc_id="doc-live"))
    assert out["ok"] is False
    assert out["code"] == "path_bound_to_other_doc"
    assert out["detail"]["other_doc_id"] == "bound"
    assert "bound document" in out["hint"]

def test_native_export_does_not_report_existing_file_as_saved_on_dirty_refusal(
        exchange, monkeypatch, tmp_path):
    tools, _ = exchange
    destination = tmp_path / "existing.step+"
    destination.write_bytes(b"original")
    original = M.cb.call
    def guarded(method, params):
        if method == "doc_save":
            assert params["if_no_human_dirty"] is True
            return {"ok": False, "code": "human_dirty"}
        return original(method, params)
    monkeypatch.setattr(M.cb, "call", guarded)
    out = json.loads(tools["export_part"](fmt="step+", path=str(destination),
                                          doc_id="doc-live"))
    assert out["ok"] is False
    assert out["code"] == "human_dirty"
    assert destination.read_bytes() == b"original"


def test_export_part_drawplus_uses_standalone_saver(exchange, tmp_path, monkeypatch):
    tools_fn, calls = exchange
    worker = tmp_path / "drawing-worker"
    worker.mkdir()
    monkeypatch.chdir(worker)

    out = json.loads(tools_fn["export_part"](fmt="draw+", path="drawing",
                                             doc_id="dwg-cpp00000001"))

    assert out["ok"] is True, out
    assert out["drawing_id"] == "dwg-cpp00000001"
    saves = [p for m, p in calls if m == "drawing_save_standalone"]
    assert saves == [{"drawing_id": "dwg-cpp00000001",
                      "path": str(tmp_path / "drawing.draw+")}]
    assert Path(out["path"]).is_file()
    assert list(worker.iterdir()) == []

def test_export_part_tilde_destination_expands(exchange, tmp_path, monkeypatch):
    """'~/x.step' must expand to $HOME — abspath alone would yield '<cwd>/~/x.step'."""
    tools_fn, calls = exchange
    monkeypatch.setenv("HOME", str(tmp_path))
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)

    out = json.loads(tools_fn["export_part"](fmt="step", path="~/bracket.step",
                                             doc_id="doc-live"))

    assert out["ok"] is True, out
    exp = [p for m, p in calls if m == "export_file"]
    assert len(exp) == 1, calls
    assert exp[0]["path"] == str(tmp_path / "bracket.step")
    assert Path(out["path"]).is_file()
    assert list(worker.iterdir()) == []


def test_import_part_missing_relative_path_keeps_teaching_envelope(exchange, tmp_path,
                                                                   monkeypatch):
    """(c) A genuinely missing relative path still returns the `file not found` envelope —
    now naming where it actually looked — and never reaches the engine."""
    tools_fn, calls = exchange
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["import_part"](path="nope/ghost.step"))

    assert out["ok"] is False
    assert out["error"] == "file not found: nope/ghost.step"
    assert str(tmp_path) in out["hint"]  # teaches the resolved location it checked
    assert calls == []  # isfile gate fired before any cb.call

# The engine atomically reuses a bound document unless a copy was requested.


@pytest.fixture()
def exchange_with_open_file(monkeypatch, tmp_path):
    """Exchange tools where `part.step+` is ALREADY open as doc-already."""
    part = tmp_path / "mudbucket.step+"
    part.write_bytes(b"PK\x03\x04 fake native container")
    calls: list = []

    def _fake_call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [
                # A GUI doc parked on its autosave path — must never be adopted.
                {"doc_id": "doc-gui", "name": "Part1", "revision": 1,
                 "path": str(tmp_path / "autosave" / "doc-gui.step+")},
                {"doc_id": "doc-already", "name": "mudbucket", "revision": 7,
                 "feature_count": 4, "path": str(part)},
            ]}
        if method == "doc_open":
            if params.get("reuse_open") and Path(params["path"]) == part:
                return {"doc_id": "doc-already", "reused": True, "revision": 7}
            return {"doc_id": "doc-opened"}
        if method == "import_file":
            return {"doc_id": "doc-imported"}
        return {}

    monkeypatch.setattr(M.cb, "call", _fake_call)
    monkeypatch.setattr(M, "_publish_current", lambda *a, **k: None)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)
    return mcp.tools, calls, part


def test_import_part_resumes_an_already_open_file(exchange_with_open_file):
    """The whole duplicate-part class: no second document, and the caller is told why."""
    tools_fn, calls, part = exchange_with_open_file

    out = json.loads(tools_fn["import_part"](path=str(part)))

    assert out["ok"] is True, out
    assert out["reused"] is True, out
    assert out["doc_id"] == "doc-already"
    assert [p for m, p in calls if m == "doc_open"] == [
        {"path": str(part), "detach_path": False, "reuse_open": True}]


def test_import_part_resume_survives_a_non_canonical_path(exchange_with_open_file,
                                                          monkeypatch):
    """`./mudbucket.step+` and `dir/../mudbucket.step+` are the same file as the open doc."""
    tools_fn, calls, part = exchange_with_open_file
    monkeypatch.chdir(part.parent)

    out = json.loads(tools_fn["import_part"](path="./sub/../mudbucket.step+", resume=True))

    assert out.get("reused") is True, out
    assert out["doc_id"] == "doc-already"
    assert [p for m, p in calls if m == "doc_open"][0]["reuse_open"] is True


def test_import_part_into_an_existing_part_still_imports(exchange_with_open_file):
    """`into=` means "add this file as a BODY here" — reuse must not hijack that."""
    tools_fn, calls, part = exchange_with_open_file

    out = json.loads(tools_fn["import_part"](path=str(part), into="doc-target"))

    assert out["ok"] is True, out
    assert "reused" not in out
    assert [m for m, _ in calls if m == "import_file"], calls


def test_import_part_does_not_adopt_an_unrelated_open_doc(exchange_with_open_file,
                                                          tmp_path):
    """A DIFFERENT file still opens normally — the guard is path equality, not a name guess."""
    tools_fn, calls, _part = exchange_with_open_file
    other = tmp_path / "bracket.step+"
    other.write_bytes(b"PK\x03\x04 fake native container")

    out = json.loads(tools_fn["import_part"](path=str(other)))

    assert out["ok"] is True, out
    assert out["reused"] is False
    assert out["doc_id"] == "doc-opened"

def test_import_native_copy_is_explicitly_detached(exchange_with_open_file):
    tools, calls, part = exchange_with_open_file
    out = json.loads(tools["import_part"](path=str(part), copy=True))
    assert out["doc_id"] == "doc-opened"
    assert not out["reused"]
    assert [p for m, p in calls if m == "doc_open"] == [
        {"path": str(part), "detach_path": True, "reuse_open": False}]

def test_solidworks_without_com_falls_through_to_portable_native_import(
        exchange, tmp_path, monkeypatch):
    """Default resolve_features must not make modern SLDPRT unusable off Windows."""
    tools, calls = exchange
    source = tmp_path / "wheel.SLDPRT"
    source.write_bytes(b"modern-solidworks-fixture")

    from services.sw_import.sw_feature_import import SolidWorksUnavailable
    from services.sw_import import sw_translate

    def unavailable(_path):
        raise SolidWorksUnavailable("SolidWorks COM automation is unavailable")

    monkeypatch.setattr(sw_translate, "resolve", unavailable)

    def portable_core(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": []}
        if method == "import_file":
            return {
                "doc_id": "doc-portable",
                "import_fidelity": "saved_display_tessellation",
                "feature_resolved": False,
                "editable_as": "baked_brep",
                "component_names": ["Wheel"],
                "warning": "tessellation fidelity",
            }
        return {}

    monkeypatch.setattr(M.cb, "call", portable_core)
    out = json.loads(tools["import_part"](str(source)))
    assert out["ok"] is True
    assert out["doc_id"] == "doc-portable"
    assert out["import_fidelity"] == "saved_display_tessellation"
    assert out["feature_resolved"] is False
    assert out["editable_as"] == "baked_brep"
    assert out["component_names"] == ["Wheel"]
    assert "SolidWorks COM automation is unavailable" in out["feature_fallback_reason"]
    assert [method for method, _ in calls if method == "import_file"] == ["import_file"]

def _native_exact_core(calls):
    def core(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": []}
        if method == "import_file":
            return {"doc_id": "doc-exact", "import_fidelity": "exact_native_brep",
                    "feature_resolved": False,
                    "solidworks_source": {"saved": {"release": "SolidWorks 2025"}}}
        return {}
    return core


@pytest.mark.parametrize("match, resolved", [(100.0, True), (93.5, False)])
def test_solidworks_native_exact_import_adopts_only_a_matching_recognition(
        exchange, tmp_path, monkeypatch, match, resolved):
    """Without SolidWorks the exact native body is recognized; the rebuild replaces it only
    when it reproduces the source solid, otherwise the exact body stays current."""
    tools, calls = exchange
    source = tmp_path / "bracket.SLDPRT"
    source.write_bytes(b"modern-solidworks-fixture")
    from services.sw_import.sw_feature_import import SolidWorksUnavailable
    from services.sw_import import sw_translate
    from services.mcp import reveng_common

    def unavailable(_path):
        raise SolidWorksUnavailable("SolidWorks COM automation is unavailable")

    monkeypatch.setattr(sw_translate, "resolve", unavailable)
    monkeypatch.setattr(M.cb, "call", _native_exact_core(calls))
    seen = []
    monkeypatch.setattr(reveng_common, "_call_reverse_engineer",
                        lambda src, name=None, hints=None: seen.append((src, name)) or {
                            "ok": True, "doc_id": "doc-rebuilt", "match_pct": match,
                            "features": [{"type": "extrude"}]})
    out = json.loads(tools["import_part"](str(source)))
    assert out["ok"] is True, out
    assert seen == [("doc-exact", "bracket-features")]
    assert out["import_fidelity"] == "exact_native_brep"
    assert out["feature_recovery"]["ref_doc_id"] == "doc-exact"
    assert out["feature_recovery"]["match_pct"] == match
    assert out["feature_resolved"] is resolved
    assert out["doc_id"] == ("doc-rebuilt" if resolved else "doc-exact")
    assert M._CUR["doc_id"] == out["doc_id"]


def test_solidworks_com_failure_continues_to_native_import(exchange, tmp_path, monkeypatch):
    """A registered but broken SolidWorks must not block the file."""
    tools, calls = exchange
    source = tmp_path / "bracket.SLDPRT"
    source.write_bytes(b"modern-solidworks-fixture")
    from services.sw_import import sw_translate
    from services.mcp import reveng_common

    def broken(_path):
        raise RuntimeError("Server execution failed")

    monkeypatch.setattr(sw_translate, "resolve", broken)
    monkeypatch.setattr(M.cb, "call", _native_exact_core(calls))
    monkeypatch.setattr(reveng_common, "_call_reverse_engineer",
                        lambda *a, **k: {"ok": False, "error": "no features recognized"})
    out = json.loads(tools["import_part"](str(source), resolve_features=True))
    assert out["ok"] is True, out
    assert out["doc_id"] == "doc-exact"
    assert out["feature_resolved"] is False
    assert "Server execution failed" in out["feature_recovery"]["solidworks_com"]


def test_solidworks_native_import_without_resolution_skips_recognition(
        exchange, tmp_path, monkeypatch):
    tools, calls = exchange
    source = tmp_path / "bracket.SLDPRT"
    source.write_bytes(b"modern-solidworks-fixture")
    from services.mcp import reveng_common
    monkeypatch.setattr(M.cb, "call", _native_exact_core(calls))
    monkeypatch.setattr(reveng_common, "_call_reverse_engineer",
                        lambda *a, **k: pytest.fail("recognition must not run"))
    out = json.loads(tools["import_part"](str(source), resolve_features=False))
    assert out["ok"] is True and out["doc_id"] == "doc-exact"
    assert "feature_recovery" not in out


def _sw_export_core(calls, features):
    def core(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [{"doc_id": "doc-live", "revision": 3}]}
        if method == "doc_tree":
            return {"tree": {"schema": "aicad.part/1", "name": "block", "features": features}}
        if method == "eval_mass":
            return {"volume_mm3": 8000.0, "com": [10, 5, 2.5]}
        if method == "export_file":
            Path(params["path"]).write_text("ISO-10303-21;")
            return {"path": params["path"]}
        return {}
    return core


_BLOCK = [
    {"id": "f1", "type": "sketch", "name": "Sketch1", "refs": {"plane": "plane.top"},
     "params": {"constraints": [], "entities": [
         {"id": "e1", "type": "line", "p1": [0, 0], "p2": [20, 0]},
         {"id": "e2", "type": "line", "p1": [20, 0], "p2": [20, 10]},
         {"id": "e3", "type": "line", "p1": [20, 10], "p2": [0, 10]},
         {"id": "e4", "type": "line", "p1": [0, 10], "p2": [0, 0]}]}},
    {"id": "f2", "type": "extrude", "name": "Boss1",
     "params": {"mode": "boss", "end": "blind", "depth": {"value": 5, "expr": None}},
     "refs": {"profile": [{"kind": "region", "sketch": "f1"}]}},
]


def test_export_solidworks_macro_writes_a_feature_macro(exchange, tmp_path, monkeypatch):
    tools, calls = exchange
    monkeypatch.setattr(M.cb, "call", _sw_export_core(calls, _BLOCK))
    out = json.loads(tools["export_solidworks"](path=str(tmp_path / "block.swb"),
                                                method="macro"))
    assert out["ok"] is True, out
    assert out["fidelity"] == "feature_tree" and out["steps"] == 2
    text = Path(out["path"]).read_text(encoding="cp1252")
    assert "FM.FeatureExtrusion3(" in text and "(Chudware 8000)" in text


def test_export_solidworks_refuses_unsupported_features_unless_body_fallback(
        exchange, tmp_path, monkeypatch):
    tools, calls = exchange
    features = _BLOCK + [{"id": "f3", "type": "hole", "params": {}, "refs": {}}]
    monkeypatch.setattr(M.cb, "call", _sw_export_core(calls, features))
    refused = json.loads(tools["export_solidworks"](path=str(tmp_path / "block.swb"),
                                                    method="macro"))
    assert refused["ok"] is False and [u["id"] for u in refused["unsupported"]] == ["f3"]
    assert not (tmp_path / "block.swb").exists()
    body = json.loads(tools["export_solidworks"](path=str(tmp_path / "block.swb"),
                                                 method="macro", allow_body_fallback=True))
    assert body["ok"] is True and body["fidelity"] == "imported_body"
    assert (tmp_path / "block.step").exists() and (tmp_path / "block.swb").exists()


def test_solidworks_sidecar_save_refusal_retains_component(exchange, tmp_path, monkeypatch):
    from services.sw_import import sw_translate
    tools, calls = exchange
    source = tmp_path / "assembly.sldasm"
    source.write_bytes(b"assembly")
    monkeypatch.setattr(M.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sw_translate, "resolve", lambda _: {
        "name": "assembly", "blocking": [], "degraded": [], "is_assembly": True,
        "components": [{"part_key": "piece.sldprt", "ops": []}]})
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_new":
            return {"doc_id": "component"}
        if method == "doc_save":
            return {"ok": False, "code": "human_dirty"}
        return {}
    monkeypatch.setattr(M.cb, "call", call)
    result = json.loads(tools["import_part"](str(source)))
    assert result["ok"] is False and result["code"] == "human_dirty"
    assert result["doc_id"] == "component"
    assert next(p for method, p in calls if method == "doc_save")["if_no_human_dirty"]
    assert not any(method == "doc_close" for method, _ in calls)


@pytest.mark.parametrize("fmt", ["step", "step+", "draw+"])
def test_export_absolute_destination_works_without_project_root(
        exchange, monkeypatch, tmp_path, fmt):
    tools, calls = exchange
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)
    destination = tmp_path / "external-delivery" / f"base.{fmt}"

    exported = json.loads(tools["export_part"](
        fmt=fmt, path=str(destination), doc_id="doc-live"))

    assert exported["ok"] is True, exported
    assert exported["path"] == str(destination)
    assert destination.is_file()
    assert list(worker.iterdir()) == []
    writes = [p["path"] for method, p in calls if method in (
        "doc_save", "export_file", "drawing_save_standalone")]
    assert writes == [str(destination)]


@pytest.mark.parametrize("fmt", ["step", "step+", "draw+"])
def test_export_relative_destination_refuses_without_project_root(
        exchange, monkeypatch, tmp_path, fmt):
    tools, calls = exchange
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    refused = json.loads(tools["export_part"](
        fmt=fmt, path=f"base.{fmt}", doc_id="doc-live"))

    assert refused["ok"] is False, refused
    assert "ABSOLUTE" in refused["error"]
    assert not [m for m, _p in calls if m in (
        "doc_save", "export_file", "drawing_save_standalone")]
    assert not (tmp_path / f"base.{fmt}").exists()


@pytest.mark.parametrize("fmt", ["step", "step+", "draw+"])
def test_export_refuses_symlinked_credential_destination(
        exchange, monkeypatch, tmp_path, fmt):
    tools, calls = exchange
    project = tmp_path / "project"
    project.mkdir()
    secrets = tmp_path / ".ssh"
    secrets.mkdir()
    (project / "output").symlink_to(secrets, target_is_directory=True)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))

    refused = json.loads(tools["export_part"](
        fmt=fmt, path=f"output/base.{fmt}", doc_id="doc-live"))

    assert refused["ok"] is False, refused
    assert "credential/key location" in refused["error"]
    assert not [m for m, _p in calls if m in (
        "doc_save", "export_file", "drawing_save_standalone")]
    assert list(secrets.iterdir()) == []


def test_import_without_workspace_does_not_read_worker_file(
        exchange, tmp_path, monkeypatch):
    tools, calls = exchange
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "block.step").write_text("ISO-10303-21;")

    result = json.loads(tools["import_part"]("block.step"))

    assert result["ok"] is False
    assert calls == []


def test_scad_inspection_converts_source_lengths_and_keeps_mm_overrides():
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)
    params = {
        "source": "width=2; count=3; flag=true; computed=width*2; cube(width);",
        "files": [],
        "parameters": {},
        "parameter_schema": [
            {"name": "width", "kind": "length"},
            {"name": "count", "kind": "integer"},
        ],
        "mm_per_source_unit": 25.4,
    }
    inspected = json.loads(mcp.tools["scad_inspect"](params))
    assert inspected["ok"], inspected
    controls = {control["name"]: control for control in inspected["controls"]}
    assert controls["width"]["value"] == pytest.approx(50.8)
    assert controls["count"]["value"] == 3
    assert controls["flag"]["value"] is True
    assert "computed" not in controls

    params["parameters"]["width"] = {"value": 12.7, "expr": None}
    overridden = json.loads(mcp.tools["scad_inspect"](params))
    assert overridden["ok"], overridden
    width = next(control for control in overridden["controls"] if control["name"] == "width")
    assert width["value"] == pytest.approx(12.7) and width["overridden"] is True
    params["mm_per_source_unit"] = 0
    assert json.loads(mcp.tools["scad_inspect"](params))["ok"] is False


def test_scad_conversion_reports_real_blocker_and_preserves_native_geometry():
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)
    created = M.cb.call("doc_new", {"kind": "part", "name": "SCAD projection blocker"})
    doc = created["doc_id"]
    try:
        built = M.cb.call("doc_apply_ops", {
            "doc_id": doc, "base_revision": created["revision"],
            "ops": [M.cb.feat("torus", "Ring", {"major_r": 10, "minor_r": 2})]})
        assert "rejected_tree" not in built, built
        assert all(status["ok"] for status in built["rebuild"]["feature_status"]), built
        fid = built["id_map"]["$0"]
        before = M.cb.call("doc_tree", {"doc_id": doc})
        mass = M.cb.call("eval_mass", {"doc_id": doc})
        assert mass["volume_mm3"] == pytest.approx(2 * math.pi**2 * 10 * 2**2)

        refused = json.loads(mcp.tools["scad_convert"](
            "to_code", doc_id=doc, base_revision=built["revision"], mm_per_source_unit=25.4))
        assert refused["ok"] is False, refused
        blockers = refused["detail"]["report"]["blockers"]
        assert any(blocker.get("id") == fid and blocker.get("type") == "torus"
                   for blocker in blockers), refused
        after = M.cb.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == before["revision"]
        assert after["tree"] == before["tree"]
        assert M.cb.call("eval_mass", {"doc_id": doc})["volume_mm3"] == pytest.approx(
            mass["volume_mm3"])
    finally:
        M.cb.call("doc_close", {"doc_id": doc})
