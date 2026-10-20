"""import_part/export_part must hand the ENGINE absolute paths.

Live-run root cause (run-74799871076856): the worker chdir's to the project root, so a
project-relative path passes the MCP process's ``os.path.isfile`` check — but the raw,
still-relative path was then forwarded to the engine daemon, whose cwd differs, so
``doc_open``/``import_file`` could not find the file (and a relative EXPORT destination
silently lands in the daemon's cwd instead of the user's project). Explicit relative
exports must use the selected workspace even when the worker cwd differs or a turn
continues. Every engine path is absolute; ``imported_from`` keeps the supplied string.

No engine needed: ``cb.call`` is stubbed and its payloads recorded.

Run:  python3 -m pytest services/mcp/test_cad_exchange_paths.py -q -p no:randomly
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_exchange_tools as M  # noqa: E402

# The exact relative path shape from the live run (spaces, &, nested project folders).
_REL_STEPPLUS = "CAD & Design Geometry/Model Step files/CD-1102_MudBucket_OilRig.step+"


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


def test_import_part_relative_stepplus_goes_absolute(exchange, tmp_path, monkeypatch):
    """(a) A cwd-relative .step+ opens via doc_open with an ABSOLUTE path payload."""
    tools_fn, calls = exchange
    part = tmp_path / _REL_STEPPLUS
    part.parent.mkdir(parents=True)
    part.write_bytes(b"PK\x03\x04 fake native container")
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["import_part"](path=_REL_STEPPLUS))

    assert out["ok"] is True, out
    assert out["doc_id"] == "doc-opened"
    doc_open = [p for m, p in calls if m == "doc_open"]
    assert len(doc_open) == 1, calls
    assert doc_open[0]["path"] == str(part)
    assert os.path.isabs(doc_open[0]["path"])
    # The run record keeps the caller's original string, not the resolved abs path.
    assert out["imported_from"] == _REL_STEPPLUS

def test_import_part_relative_drawplus_uses_drawing_open(exchange, tmp_path, monkeypatch):
    tools_fn, calls = exchange
    drawing = tmp_path / "x.draw+"
    drawing.write_bytes(b"PK\x03\x04 fake drawing container")
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["import_part"](path="x.draw+"))

    assert out["ok"] is True, out
    assert out["doc_id"] == out["drawing_id"] == "dwg-opened"
    assert out["source_state"] == "embedded_opened"
    opens = [p for m, p in calls if m == "drawing_open_standalone"]
    assert opens == [{"path": str(drawing)}]
    assert not [p for m, p in calls if m == "import_file"]


def test_import_part_relative_interchange_goes_absolute(exchange, tmp_path, monkeypatch):
    """(a) Same contract on the import_file branch (plain STEP, no `into`)."""
    tools_fn, calls = exchange
    part = tmp_path / "bracket.step"
    part.write_text("ISO-10303-21;")
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["import_part"](path="bracket.step"))

    assert out["ok"] is True, out
    imp = [p for m, p in calls if m == "import_file"]
    assert len(imp) == 1, calls
    assert imp[0]["path"] == str(part)
    assert os.path.isabs(imp[0]["path"])
    assert out["imported_from"] == "bracket.step"


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

    out = json.loads(tools_fn["export_part"](fmt="step+", path="deliverable",
                                             doc_id="doc-live"))

    assert out["format"] == "step+", out
    saves = [p for m, p in calls if m == "doc_save"]
    assert len(saves) == 1, calls
    assert saves[0]["path"] == str(tmp_path / "deliverable.step+")
    assert out["ok"] is True, out
    assert Path(out["path"]).is_file()
    assert list(worker.iterdir()) == []


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

# Explicit resume reuses the resolved source path; ordinary imports remain independent.


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
            return {"doc_id": "doc-opened"}
        if method == "import_file":
            return {"doc_id": "doc-imported"}
        return {}

    monkeypatch.setattr(M.cb, "call", _fake_call)
    monkeypatch.setattr(M, "_publish_current", lambda *a, **k: None)
    mcp = _StubMCP()
    M.register_cad_exchange_tools(mcp)
    return mcp.tools, calls, part


def test_import_part_resumes_an_already_open_file(exchange_with_open_file):
    """The whole duplicate-part class: no second document, and the caller is told why."""
    tools_fn, calls, part = exchange_with_open_file

    out = json.loads(tools_fn["import_part"](path=str(part), resume=True))

    assert out["ok"] is True, out
    assert out["reused"] is True, out
    assert out["doc_id"] == "doc-already"
    assert out["feature_count"] == 4
    assert [m for m, _ in calls if m in ("doc_open", "import_file")] == []


def test_import_part_resume_survives_a_non_canonical_path(exchange_with_open_file,
                                                          monkeypatch):
    """`./mudbucket.step+` and `dir/../mudbucket.step+` are the same file as the open doc."""
    tools_fn, calls, part = exchange_with_open_file
    monkeypatch.chdir(part.parent)

    out = json.loads(tools_fn["import_part"](path="./sub/../mudbucket.step+", resume=True))

    assert out.get("reused") is True, out
    assert out["doc_id"] == "doc-already"
    assert [m for m, _ in calls if m in ("doc_open", "import_file")] == []


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
    assert "reused" not in out
    assert out["doc_id"] == "doc-opened"

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
