"""Deliverable project-save tests (CHUDWARE_PROJECT_SAVE_DIR).

When the agent worker jails a run to a user's project folder it sets
CHUDWARE_PROJECT_SAVE_DIR; new_part and the successful add_features path then refresh the
doc's chosen native destination (or assign <dir>/<safe_stem>.step+ to a fresh document).
Unset configuration changes nothing. Ordinary save failures are best-effort, but engine
connection loss propagates. These run WITHOUT an engine: core_bridge.call is faked.

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
        self.paths: dict = {}
        self.saves: list = []
        self.exports: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_new":
            self.names[_DOC] = p.get("name") or "part"
            return {"doc_id": _DOC, "revision": 0}
        if method == "doc_list":
            return {"docs": [{"doc_id": d, "name": n, "revision": 1,
                              "path": self.paths.get(d)}
                             for d, n in self.names.items()]}
        if method == "asm_world_bodies":
            return {"bodies": [{"name": "Block"}]}
        if method == "doc_save":
            path = p.get("path")
            self.saves.append(path)
            Path(path).write_text("STEP+ fake payload")
            if not p.get("preserve_path"):
                self.paths[p["doc_id"]] = path
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
    monkeypatch.setenv("CHUDWARE_HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
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


def test_stem_falls_back_to_doc_id(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    engine.names[_DOC] = "!!!..."
    assert C._project_save(_DOC) == str(tmp_path / f"{_DOC}.step+")
    assert (tmp_path / f"{_DOC}.step+").is_file()
    assert C._project_save_stem("a b-c_d", _DOC) == "a_b-c_d"


def test_project_save_preserves_chosen_destination_even_with_supplied_name(
        engine, monkeypatch, tmp_path):
    project = tmp_path / "project"
    destination = tmp_path / "teammate bundle" / "chosen.step+"
    destination.parent.mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(project))
    engine.names[_DOC] = "assembly"
    engine.call("doc_save", {"doc_id": _DOC, "path": str(destination)})

    written = C._project_save(_DOC, "renamed assembly")

    assert written == str(destination)
    assert engine.paths[_DOC] == str(destination)
    assert not project.exists()


@pytest.mark.parametrize("origin", ["detached", "mcp", "hostd", "orphan", "handoff"])
def test_recovery_or_detached_doc_gets_project_deliverable(
        engine, monkeypatch, tmp_path, origin):
    from services import paths

    project = tmp_path / "project"
    data = tmp_path / "data"
    monkeypatch.setattr(paths, "data_roots", lambda: [data])
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(project))
    engine.names[_DOC] = "recovered"
    candidates = {
        "detached": None,
        "mcp": tmp_path / "autosave" / "session-doc.step+",
        "hostd": data / "hostd_autosave" / "instance" / "session" / "doc.step+",
        "orphan": data / "hostd_autosave" / "instance" / "orphaned" / "doc.step+",
        "handoff": tmp_path / "handoff" / "live-session-doc.step+",
    }
    prior = candidates[origin]
    if prior is not None:
        prior.parent.mkdir(parents=True, exist_ok=True)
        prior.write_text("recovery evidence")
        engine.paths[_DOC] = str(prior)

    written = C._project_save(_DOC, "recovered")

    assert written == str(project / "recovered.step+")
    assert engine.paths[_DOC] == written
    if prior is not None:
        assert prior.read_text() == "recovery evidence"


def test_recovery_directory_prefix_is_still_a_real_destination(
        engine, monkeypatch, tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path / "project"))
    destination = tmp_path / "autosave_deliverables" / "chosen.step+"
    destination.parent.mkdir()
    engine.names[_DOC] = "chosen"
    engine.call("doc_save", {"doc_id": _DOC, "path": str(destination)})

    assert C._project_save(_DOC) == str(destination)


@pytest.mark.parametrize("code", ["engine_connection_lost", "io_error"])
def test_metadata_failure_never_guesses_a_new_destination(
        engine, monkeypatch, tmp_path, code):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path / "project"))

    class MetadataFailure(RuntimeError):
        pass

    failure = MetadataFailure("document metadata unavailable")
    failure.code = code

    def unavailable(method, params=None):
        if method == "doc_list":
            raise failure
        return engine.call(method, params)

    monkeypatch.setattr(C.cb, "call", unavailable)
    if code == "engine_connection_lost":
        with pytest.raises(MetadataFailure):
            C._project_save(_DOC, "supplied name")
    else:
        assert C._project_save(_DOC, "supplied name") is None
    assert engine.saves == []
