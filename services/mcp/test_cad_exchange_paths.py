"""import_part/export_part must hand the ENGINE absolute paths.

Live-run root cause (run-74799871076856): the worker chdir's to the project root, so a
project-relative path passes the MCP process's ``os.path.isfile`` check — but the raw,
still-relative path was then forwarded to the engine daemon, whose cwd differs, so
``doc_open``/``import_file`` could not find the file (and a relative EXPORT destination
silently lands in the daemon's cwd instead of the user's project). These tests pin the
contract: every path that crosses ``cb.call`` is absolute, while the caller-facing
``imported_from`` keeps the original caller-supplied string so run records stay readable.

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
def exchange(monkeypatch):
    """Registered exchange tools with cb.call recorded + the engine faked."""
    calls: list = []

    def _fake_call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [{"doc_id": "doc-live", "revision": 3}]}
        if method == "doc_open":
            return {"doc_id": "doc-opened"}
        if method == "import_file":
            return {"doc_id": "doc-imported"}
        return {}

    monkeypatch.setattr(M.cb, "call", _fake_call)
    # The handoff publisher is irrelevant here (and would doc_save into the real handoff dir).
    monkeypatch.setattr(M, "_publish_current", lambda *a, **k: None)
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
    """(b) A relative EXPORT destination is resolved against the worker's cwd before
    crossing to the engine — never silently written into the daemon's cwd."""
    tools_fn, calls = exchange
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["export_part"](fmt="step", path="out/bracket.step",
                                             doc_id="doc-live"))

    assert out["ok"] is True, out
    exp = [p for m, p in calls if m == "export_file"]
    assert len(exp) == 1, calls
    assert exp[0]["path"] == str(tmp_path / "out" / "bracket.step")
    assert os.path.isabs(exp[0]["path"])


def test_export_part_stepplus_alias_goes_absolute(exchange, tmp_path, monkeypatch):
    """(b) The native step+ alias routes through doc_save with an absolute path too."""
    tools_fn, calls = exchange
    monkeypatch.chdir(tmp_path)

    out = json.loads(tools_fn["export_part"](fmt="step+", path="deliverable",
                                             doc_id="doc-live"))

    assert out["format"] == "step+", out
    saves = [p for m, p in calls if m == "doc_save"]
    assert len(saves) == 1, calls
    assert saves[0]["path"] == str(tmp_path / "deliverable.step+")


def test_export_part_tilde_destination_expands(exchange, tmp_path, monkeypatch):
    """'~/x.step' must expand to $HOME — abspath alone would yield '<cwd>/~/x.step'."""
    tools_fn, calls = exchange
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # cwd must NOT be where the file lands

    out = json.loads(tools_fn["export_part"](fmt="step", path="~/bracket.step",
                                             doc_id="doc-live"))

    assert out["ok"] is True, out
    exp = [p for m, p in calls if m == "export_file"]
    assert len(exp) == 1, calls
    assert exp[0]["path"] == str(tmp_path / "bracket.step")


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

# ---------------------------------------------------------------------------------------
# Re-importing an already-open file must RESUME it, never mint a second document.
#
# Live-run root cause (run-138660919763973): the agent imported
# Projects/Testing/mudbucket.step+ and then kept working through tools that mint documents,
# and the user was left with mudbucket.step+ AND mudbucket2.step+ on disk plus three tabs.
# doc_open/import_file mint a fresh doc per call and doc_new uniquifies the colliding name,
# so every re-import compounds. Matching is on the RESOLVED path only — a GUI-created doc
# lists its autosave path, so this can never adopt a document that is not this file.
# ---------------------------------------------------------------------------------------


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

    out = json.loads(tools_fn["import_part"](path=str(part)))

    assert out["ok"] is True, out
    assert out["reused"] is True, out
    assert out["doc_id"] == "doc-already"
    assert out["feature_count"] == 4
    assert [m for m, _ in calls if m in ("doc_open", "import_file")] == []
    # The note must teach: name the file, the doc it resumed, and the corrected call.
    assert "mudbucket.step+" in out["note"]
    assert "doc-already" in out["note"]
    assert 'into="doc-already"' in out["note"]


def test_import_part_resume_survives_a_non_canonical_path(exchange_with_open_file,
                                                          monkeypatch):
    """`./mudbucket.step+` and `dir/../mudbucket.step+` are the same file as the open doc."""
    tools_fn, calls, part = exchange_with_open_file
    monkeypatch.chdir(part.parent)

    out = json.loads(tools_fn["import_part"](path="./sub/../mudbucket.step+"))

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
