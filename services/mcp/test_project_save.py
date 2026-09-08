"""Deliverable project-save tests (CHUDWARE_PROJECT_SAVE_DIR).

When the agent worker jails a run to a user's project folder it sets
CHUDWARE_PROJECT_SAVE_DIR; new_part and the successful add_features path then write the
doc as a real, openable native .step+ at <dir>/<safe_stem>.step+ and report it back as
`saved_path`. Unset (the default) must change nothing, and a failed save must never fail
the tool call. These run WITHOUT an engine: core_bridge.call is faked.

Run:  python3 -m pytest services/mcp/test_project_save.py -q -p no:randomly
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as C  # noqa: E402
from services.mcp import cad_doc_tools as M  # noqa: E402

_DOC = "doc-cpp00000001"


class _FakeEngine:
    """Minimal core-RPC stand-in: docs are real .step+ files written to disk."""

    def __init__(self):
        self.names: dict = {}
        self.saves: list = []
        self.exports: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_new":
            self.names[_DOC] = p.get("name") or "part"
            return {"doc_id": _DOC, "revision": 0}
        if method == "doc_list":
            return {"docs": [{"doc_id": d, "name": n, "revision": 1}
                             for d, n in self.names.items()]}
        if method == "asm_world_bodies":
            return {"bodies": [{"name": "Block"}]}
        if method == "doc_save":
            path = p.get("path")
            self.saves.append(path)
            Path(path).write_text("STEP+ fake payload")
            return {"path": path}
        if method == "export_file":
            path = p.get("path")
            self.exports.append(path)
            Path(path).write_text("flat export fake payload")
            return {"path": path, "bytes": Path(path).stat().st_size,
                    "solid_count": 1, "valid_solids": 1, "validated": True}
        if method == "doc_checkpoint":
            return {"checkpoint": "ck1"}
        if method == "doc_apply_ops":
            return {"rebuild": {"feature_status": [{"id": "f1", "ok": True}]},
                    "tess": {"bodies": [{}]}, "revision": 1, "volume": 100.0,
                    "id_map": {}}
        if method == "doc_query":
            return {"matches": []}
        return {}


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    fake = _FakeEngine()
    monkeypatch.setattr(C.cb, "call", fake.call)
    # Keep the crash-recovery autosave hermetic too (add_features still calls _autosave).
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(tmp_path / "autosave"))
    yield fake


def _new_part(name):
    return json.loads(_tool("new_part")(name=name))


_TOOL_CACHE: dict = {}


def _tool(name):
    """Grab the raw tool function registered on the FastMCP server (bypasses transport)."""
    if name not in _TOOL_CACHE:
        from services.agent.agent_turn import _bind_tool
        _TOOL_CACHE[name] = _bind_tool(name)
    return _TOOL_CACHE[name]


def test_unset_env_writes_nothing_and_payload_is_unchanged(engine, monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    r = _new_part("widget")
    assert r["ok"] is True and r["doc_id"] == _DOC and r["name"] == "widget"
    assert "revision" in r
    assert "saved_path" not in r
    assert engine.saves == []
    assert not list(tmp_path.glob("*.step+"))


def test_new_part_lands_in_project_dir_and_reports_saved_path(engine, monkeypatch,
                                                              tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    r = _new_part("widget")
    assert r["ok"] is True
    written = tmp_path / "widget.step+"
    assert written.is_file()
    assert r["saved_path"] == str(written)



def test_bare_save_and_export_land_in_project_dir(engine, monkeypatch, tmp_path):
    """Replay defect: a pathless delivery must not disappear into chudware_mcp_ scratch."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    _new_part("project widget")
    engine.saves.clear()  # discard new_part's eager project-save call from this assertion

    saved = json.loads(_tool("save_part")(doc_id=_DOC))
    exported = json.loads(_tool("export_part")(fmt="step", doc_id=_DOC))

    assert saved["path"] == str(tmp_path / "project_widget.step+")
    assert exported["path"] == str(tmp_path / "project_widget.step")
    assert Path(saved["path"]).is_file() and Path(exported["path"]).is_file()
    assert engine.saves == [saved["path"]]
    assert engine.exports == [exported["path"]]
    assert not Path(saved["path"]).name.startswith("chudware_mcp_")
    assert not Path(exported["path"]).name.startswith("chudware_mcp_")


def test_bare_save_and_export_keep_temp_fallback_without_project(engine, monkeypatch):
    """External MCP clients have no project folder, so their historical temp default stays."""
    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    _new_part("headless_widget")

    saved = json.loads(_tool("save_part")(doc_id=_DOC))
    exported = json.loads(_tool("export_part")(fmt="step", doc_id=_DOC))

    assert Path(saved["path"]).name.startswith("chudware_mcp_")
    assert Path(exported["path"]).name.startswith("chudware_mcp_")
    Path(saved["path"]).unlink()
    Path(exported["path"]).unlink()

def test_stem_sanitization_matches_host_rule(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    r = _new_part("my bracket v2 (final)!")
    assert r["ok"] is True
    assert (tmp_path / "my_bracket_v2_final.step+").is_file()
    assert r["saved_path"].endswith("my_bracket_v2_final.step+")


def test_add_features_refreshes_the_saved_file(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    _new_part("fixture_part")  # establishes the current doc for doc_id=None
    r = json.loads(_tool("add_features")(doc_id=None, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Block",
                                          "params": {"length": 40, "width": 30,
                                                     "height": 20}}}]))
    assert r["ok"] is True, r
    assert r["saved_path"].endswith(".step+")
    assert Path(r["saved_path"]).is_file()
    assert Path(r["saved_path"]).parent == tmp_path
    # Payload contract otherwise intact.
    assert r["doc_id"] and "revision" in r and "features" in r


def test_add_features_unset_env_has_no_saved_path(engine, monkeypatch):
    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    _new_part("fixture_part")
    r = json.loads(_tool("add_features")(doc_id=None, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Block",
                                          "params": {"length": 40}}}]))
    assert r["ok"] is True, r
    assert "saved_path" not in r
    project_saves = [p for p in engine.saves if "autosave" not in p]
    assert project_saves == []


def test_unwritable_dir_never_fails_the_tool(engine, monkeypatch, tmp_path):
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("I am a file")  # mkdir(exist_ok=True) on a file -> failure path
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(blocked))
    r = _new_part("widget")
    assert r["ok"] is True
    assert "saved_path" not in r


def test_project_save_never_raises_on_engine_failure(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))

    def boom(method, params=None):
        if method == "doc_save":
            raise RuntimeError("disk on fire")
        return engine.call(method, params)

    monkeypatch.setattr(C.cb, "call", boom)
    assert C._project_save(_DOC, "widget") is None
    r = _new_part("widget")
    assert r["ok"] is True and "saved_path" not in r


def test_stem_falls_back_to_doc_id(monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    path = C._project_save(_DOC, "!!!...")
    # engine fake has no doc named _DOC registered via new_part here, but doc_list is
    # best-effort; the stem must fall back to the doc_id, not produce an empty name.
    assert path is None or path.endswith(".step+")
    assert C._project_save_stem("!!!...", _DOC) == _DOC
    assert C._project_save_stem("a b-c_d", _DOC) == "a_b-c_d"
