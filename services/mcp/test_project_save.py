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
        self.native_sources: dict = {}
        self.source_edited: dict = {}
        self.origins: dict = {}  # doc_id -> hostd origin_kind ("gui" = the user opened it)
        self.saves: list = []
        self.exports: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_new":
            self.names[_DOC] = p.get("name") or "part"
            return {"doc_id": _DOC, "revision": 0}
        if method == "doc_list":
            return {"docs": [{"doc_id": d, "name": n, "revision": 1,
                              "path": self.paths.get(d),
                              "native_source_path": self.native_sources.get(d),
                              "native_source_edited": self.source_edited.get(d, False),
                              "origin_kind": self.origins.get(d, "mcp")}
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


def test_add_features_never_writes_the_file_of_a_doc_the_user_opened(engine, monkeypatch,
                                                                      tmp_path):
    """c259: per-batch saves rewrote the user's open base.step+ with every intermediate
    (including a kernel-broken cut) and cleared its dirty flag."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    _new_part("base")
    user_file = tmp_path / "parts" / "base.step+"
    user_file.parent.mkdir()
    user_file.write_text("user's saved design")
    engine.paths[_DOC] = str(user_file)
    engine.origins[_DOC] = "gui"
    engine.saves.clear()
    r = json.loads(_tool("add_features")(doc_id=_DOC, ops=[
        {"op": "add_feature", "feature": {"type": "box", "name": "Block",
                                          "params": {"length": 40, "width": 30,
                                                     "height": 20}}}]))
    assert r["ok"] is True, r
    assert r.get("saved_path") is None
    assert [p for p in engine.saves if "autosave" not in p] == []
    assert user_file.read_text() == "user's saved design"


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


def test_bare_save_of_an_opened_file_saves_in_place(engine, monkeypatch, tmp_path):
    """Live 2026-09-24: a bare save_part of `CAD/Wheels/Wheel Hub ….step+` wrote a
    duplicate `Wheel_Hub_….step+` into the project root."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    home = tmp_path / "CAD" / "Wheels"
    home.mkdir(parents=True)
    opened = home / "Wheel Hub 2026-09-24.step+"
    opened.write_text("original")
    engine.names[_DOC] = "Wheel Hub 2026-09-24"
    engine.paths[_DOC] = str(opened)

    saved = json.loads(_tool("save_part")(doc_id=_DOC))

    assert saved["path"] == str(opened)
    assert sorted(p.name for p in tmp_path.glob("*.step+")) == []


@pytest.mark.parametrize("save_mode", ["automatic", "bare", "explicit"])
def test_study_only_native_import_needs_a_separate_or_explicit_destination(
        engine, monkeypatch, tmp_path, save_mode):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    source = tmp_path / "arm.step+"
    source.write_bytes(b"original source assembly")
    engine.names[_DOC] = "arm"
    engine.native_sources[_DOC] = str(source)

    if save_mode == "automatic":
        written = C._project_save(_DOC)
    else:
        result = json.loads(_tool("save_part")(
            doc_id=_DOC, **({"path": str(source)} if save_mode == "explicit" else {})))
        assert result["ok"] is True, result
        written = result["path"]

    if save_mode == "explicit":
        assert written == str(source)
        assert source.read_text() == "STEP+ fake payload"
    else:
        assert written == str(tmp_path / "arm_2.step+")
        assert source.read_bytes() == b"original source assembly"
        assert Path(written).read_text() == "STEP+ fake payload"


@pytest.mark.parametrize("save_mode", ["automatic", "bare"])
def test_authored_native_import_preserves_source_save_behavior(
        engine, monkeypatch, tmp_path, save_mode):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path / "project"))
    source = tmp_path / "arm.step+"
    source.write_text("original source assembly")
    engine.names[_DOC] = "arm"
    engine.native_sources[_DOC] = str(source)
    engine.source_edited[_DOC] = True

    if save_mode == "automatic":
        written = C._project_save(_DOC)
    else:
        saved = json.loads(_tool("save_part")(doc_id=_DOC))
        assert saved["ok"] is True, saved
        written = saved["path"]

    assert written == str(source)
    assert source.read_text() == "STEP+ fake payload"
    assert not (tmp_path / "project").exists()


def test_persist_study_keeps_the_native_reassigned_identity(monkeypatch):
    studies = [
        {"id": "study-1", "type": "fea_static", "name": "Static"},
        {"id": "study-1", "type": "motion", "name": "Motion: m75"},
    ]
    edits = []

    def call(method, params):
        if method == "sim_study_list":
            return {"studies": studies}
        assert method == "sim_study_edit"
        edits.append((params["study_id"], dict(params["study"])))
        studies[1] = {**params["study"], "id": "study-2"}
        return {"studies": studies, "study_id": "study-2"}

    monkeypatch.setattr(C.cb, "call", call)
    for stop in (80, 40):
        record = {"type": "motion", "name": "Motion: m75",
                  "motion": {"drivers": [{"mate_id": "m75", "start": 0, "stop": stop}]}}
        assert C._persist_study("doc-arm", record) == "study-2"
        assert record["id"] == "study-2"
    assert [edit[0] for edit in edits] == ["study-1", "study-2"]
    assert studies[0] == {"id": "study-1", "type": "fea_static", "name": "Static"}
    assert studies[1]["motion"]["drivers"][0]["stop"] == 40


def test_explicit_save_retires_only_the_copy_the_harness_placed(engine, monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    r = _new_part("Brake Rotor Hat")
    harness_copy = Path(r["saved_path"])
    assert harness_copy.parent == tmp_path and harness_copy.is_file()
    destination = tmp_path / "CAD" / "Wheels" / "Brake Rotor Hat.step+"
    destination.parent.mkdir(parents=True)

    saved = json.loads(_tool("save_part")(doc_id=_DOC, path=str(destination)))

    assert saved["path"] == str(destination) and destination.is_file()
    assert not harness_copy.exists()
    assert saved["removed_stale_copy"] == str(harness_copy)

    # A later explicit save somewhere else must not touch a file the USER chose.
    elsewhere = tmp_path / "Brake Rotor Hat backup.step+"
    again = json.loads(_tool("save_part")(doc_id=_DOC, path=str(elsewhere)))
    assert destination.is_file() and elsewhere.is_file()
    assert "removed_stale_copy" not in again


def test_save_over_the_file_open_in_the_users_tab_is_refused(engine, tmp_path):
    """Live 2026-09-27: the agent rebuilt the arm in a NEW assembly and saved it over
    sixaxis_arm.step+, the file the user had open. The user's tab kept rev C (none of its
    revisions changed, so nothing reached the viewport) and saving that tab would have put
    rev C back over the agent's file. The agent must edit the open document instead."""
    opened = tmp_path / "sixaxis_arm.step+"
    opened.write_text("rev C")
    engine.names.update({"doc-user": "sixaxis_arm", "doc-agent": "sixaxis_arm_revD",
                         "doc-agent-copy": "sixaxis_arm"})
    engine.paths["doc-user"] = str(opened)
    engine.origins["doc-user"] = "gui"

    refused = json.loads(_tool("save_part")(doc_id="doc-agent", path=str(opened)))
    assert refused["ok"] is False
    assert refused["reason"] == "path_open_in_user_tab"
    assert refused["open_doc_id"] == "doc-user"
    assert "doc-user" in refused["content"]
    assert engine.saves == [] and opened.read_text() == "rev C"

    # The user's own document saves in place; another name is still the agent's to take.
    in_place = json.loads(_tool("save_part")(doc_id="doc-user", path=str(opened)))
    assert in_place["ok"] is True and in_place["path"] == str(opened)
    other = json.loads(_tool("save_part")(doc_id="doc-agent",
                                          path=str(tmp_path / "sixaxis_arm_revD.step+")))
    assert other["ok"] is True

    # An AGENT-opened doc at the path does not hold it (agent-owned lifecycle unchanged).
    engine.origins["doc-user"] = "mcp"
    engine.paths["doc-agent-copy"] = str(opened)
    replaced = json.loads(_tool("save_part")(doc_id="doc-agent", path=str(opened)))
    assert replaced["ok"] is True and replaced["path"] == str(opened)


@pytest.mark.parametrize("root_key", ["CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"])
def test_explicit_relative_save_uses_selected_nested_workspace_across_cwds(
        engine, monkeypatch, tmp_path, root_key):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / ".plm").mkdir()
    project = repository / "selected workspace"
    destination = project / "parts" / "redesign" / "base.step+"
    destination.parent.mkdir(parents=True)
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(other_project))
    monkeypatch.setenv(root_key, str(project))
    engine.names[_DOC] = "base"

    for turn in ("initial-worker", "continuation-worker"):
        worker = tmp_path / turn
        worker.mkdir()
        monkeypatch.chdir(worker)
        saved = json.loads(_tool("save_part")(
            doc_id=_DOC, path="parts/redesign/base.step+"))
        assert saved["ok"] is True, saved
        assert saved["path"] == str(destination)
        assert destination.is_file()
        assert list(worker.iterdir()) == []
    assert engine.saves == [str(destination), str(destination)]
    assert not (repository / "parts").exists()
    assert list(other_project.iterdir()) == []


@pytest.mark.parametrize("path_kind", ["absolute", "tilde"])
def test_explicit_save_outside_workspace_needs_no_root(
        engine, monkeypatch, tmp_path, path_kind):
    home = tmp_path / "home"
    home.mkdir()
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    monkeypatch.chdir(worker)
    engine.names[_DOC] = "base"
    destination = home / "base.step+"
    path = str(destination) if path_kind == "absolute" else "~/base.step+"

    saved = json.loads(_tool("save_part")(doc_id=_DOC, path=path))

    assert saved["ok"] is True, saved
    assert saved["path"] == str(destination)
    assert destination.is_file()
    assert engine.saves == [str(destination)]
    assert list(worker.iterdir()) == []


@pytest.mark.parametrize("root_state", ["unset", "missing", "file"])
def test_relative_save_refuses_unavailable_authoritative_root(
        engine, monkeypatch, tmp_path, root_state):
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    if root_state != "unset":
        configured = tmp_path / "unavailable-project"
        if root_state == "file":
            configured.write_text("not a directory")
        monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(configured))
        # An invalid authoritative root must not fall back to this valid root.
        monkeypatch.setenv("CHUDWARE_FS_ROOT", str(worker))
    engine.names[_DOC] = "base"

    refused = json.loads(_tool("save_part")(doc_id=_DOC, path="base.step+"))

    assert refused["ok"] is False, refused
    assert "ABSOLUTE" in refused["error"]
    assert engine.saves == []
    assert list(worker.iterdir()) == []
    assert not (tmp_path / "base.step+").exists()


@pytest.mark.parametrize("path_kind", ["relative_alias", "absolute_alias", "credential_file"])
def test_save_refuses_credential_destinations_after_symlink_resolution(
        engine, monkeypatch, tmp_path, path_kind):
    project = tmp_path / "project"
    project.mkdir()
    secrets = tmp_path / ".ssh"
    secrets.mkdir()
    alias = project / "ordinary-output"
    alias.symlink_to(secrets, target_is_directory=True)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))
    engine.names[_DOC] = "base"
    paths = {
        "relative_alias": "ordinary-output/base.step+",
        "absolute_alias": str(alias / "base.step+"),
        "credential_file": str(project / ".netrc"),
    }

    refused = json.loads(_tool("save_part")(doc_id=_DOC, path=paths[path_kind]))

    assert refused["ok"] is False, refused
    assert "credential/key location" in refused["error"]
    assert engine.saves == []
    assert list(secrets.iterdir()) == []
    assert not (project / ".netrc").exists()


def test_sanitizer_canonicalizes_workspace_and_ordinary_output_symlinks(
        monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    root_alias = tmp_path / "workspace-link"
    root_alias.symlink_to(project, target_is_directory=True)
    destination = tmp_path / "deliverables"
    destination.mkdir()
    (project / "output").symlink_to(destination, target_is_directory=True)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(root_alias))

    assert C.resolve_agent_path("base.step+", "path") == str(project / "base.step+")
    assert C._sanitize_output_path("output/base.step+") == str(destination / "base.step+")
