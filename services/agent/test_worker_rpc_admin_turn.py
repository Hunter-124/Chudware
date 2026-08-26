"""test_worker_rpc_admin_turn.py — services.agent.worker_rpc._agent_admin_turn (harness overhaul).

Proves the ``agent_admin_turn`` worker RPC (the seam core/agent/run.cpp's admin
pre-flight branch calls out to instead of refusing a project/file/PLM
administration request):

  * empty ``request`` is rejected without touching a provider at all.
  * offline mode (``config.get_provider`` raising ``OfflineModeError``) surfaces
    as a clean ``ok: False`` envelope — never an exception — so the C++ caller's
    "any failure -> fall back to the blocked response" contract holds.
  * a reachable provider runs ONE linear role turn via
    ``mission_dispatch.run_linear_role_turn`` and streams events through the
    ``_emit`` callback (worker_main.py's per-request event sink). The default
    role is ``general_agent`` (the inverted-default general lane); an explicit
    ``role`` param runs a specialized card (e.g. project_organizer).

Scripted provider only (T-mega24-style), no model/network/port.
Run: pytest services/agent/test_worker_rpc_admin_turn.py -q
"""
from __future__ import annotations

from services.agent import config as agent_config
from services.agent import worker_rpc


def test_agent_admin_turn_rejects_empty_request(monkeypatch):
    called = {"get_provider": False}
    monkeypatch.setattr(
        agent_config, "get_provider",
        lambda *a, **k: called.__setitem__("get_provider", True) or (_ for _ in ()).throw(
            AssertionError("get_provider should not be called for an empty request")),
    )
    out = worker_rpc._agent_admin_turn({"request": "   "}, lambda data: None)
    assert out == {"ok": False, "reason": "agent_admin_turn: empty request"}
    assert called["get_provider"] is False


def test_agent_admin_turn_offline_falls_back_cleanly(monkeypatch):
    def _raise_offline(*_a, **_k):
        raise agent_config.OfflineModeError(
            "offline mode is enabled: the resolved provider is not local-only")

    monkeypatch.setattr(agent_config, "get_provider", _raise_offline)

    events: list[dict] = []
    out = worker_rpc._agent_admin_turn(
        {"request": "clean up my project files and archive deprecated parts"},
        events.append,
    )
    assert out["ok"] is False
    assert "offline" in out["reason"].lower()
    assert events == []  # never got far enough to run a turn


def test_agent_admin_turn_no_provider_falls_back_cleanly(monkeypatch):
    """Worker-unreachable-equivalent: any other provider-construction failure also
    degrades to a clean ok:false, never an exception across the RPC boundary."""
    def _raise(*_a, **_k):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(agent_config, "get_provider", _raise)

    out = worker_rpc._agent_admin_turn({"request": "organize my files"}, lambda data: None)
    assert out["ok"] is False
    assert "no provider" in out["reason"]


class _OneToolProvider:
    """Emits ONE tool-call reply then a plain-text finish — enough to exercise
    the tool_call event-emission path without a full multi-turn scripted flow."""
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "done, nothing else to do."


def test_agent_admin_turn_runs_general_agent_by_default_and_streams_events(monkeypatch):
    """A tool-calling turn (not just a bare no-code finish) exercises the SAME
    ``agent.tool_call`` event emission a real fs_edit/fs_list/run_shell step
    would — proving events flow through ``_emit`` for the C++ caller to relay,
    without needing the full MCP tool registry wired up in a unit test (the
    scripted tool need not actually resolve). No role param -> the default
    general_agent lane."""
    provider = _OneToolProvider([
        '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
        "Listed the project root; nothing needs reorganizing.",
    ])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)

    events: list[dict] = []
    out = worker_rpc._agent_admin_turn(
        {"request": "list my documents and tidy the folder", "run_id": "r_gen_1"},
        events.append,
    )
    assert out["ok"] is True
    turn = out["turn"]
    assert turn["role"] == "general_agent"
    assert "fs_edit" in turn["allowed_tools"] and "fs_list" in turn["allowed_tools"]
    assert provider.calls == 2
    # The scripted fs_list call streamed as an agent.tool_call event through _emit
    # (worker_main.py tags/relays these; run.cpp's pre-flight branch relays onward).
    assert events, "expected at least one streamed event for the tool call"
    assert all("kind" in e for e in events)
    assert any(e["kind"] == "agent.tool_call" and e.get("tool_name") == "fs_list"
               for e in events)


def test_agent_admin_turn_honors_explicit_role(monkeypatch):
    """An explicit ``role`` param runs that specialized linear card instead of the
    general_agent default (back-compat for the project_organizer admin path)."""
    provider = _OneToolProvider([
        '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
        "Organized the project files.",
    ])
    captured = {"role": None}

    def _capture_provider(*_a, **k):
        captured["role"] = k.get("role")
        return provider

    monkeypatch.setattr(agent_config, "get_provider", _capture_provider)
    out = worker_rpc._agent_admin_turn(
        {"request": "clean up the folders and track it in PLM",
         "role": "project_organizer"},
        lambda data: None,
    )
    assert out["ok"] is True
    assert captured["role"] == "project_organizer"
    assert out["turn"]["role"] == "project_organizer"



def test_agent_admin_turn_rejects_unknown_role_before_provider_dispatch(monkeypatch):
    called = []
    monkeypatch.setattr(
        agent_config,
        "get_provider",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    out = worker_rpc._agent_admin_turn(
        {"request": "build it", "role": "general-agent"},
        lambda data: None,
    )
    assert out == {"ok": False, "reason": "invalid_role"}
    assert called == []
def test_agent_admin_turn_forwards_configured_max_output_tokens(monkeypatch):
    from services.agent import mission_dispatch

    captured = {}
    monkeypatch.setattr(
        agent_config, "load_config", lambda: {"max_output_tokens": 3456}
    )
    monkeypatch.setattr(
        agent_config, "get_provider", lambda *args, **kwargs: object()
    )

    def _run(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "stop_reason": "confirmed"}

    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", _run)
    out = worker_rpc._agent_admin_turn({"request": "inspect it"}, lambda data: None)

    assert out["ok"] is True
    assert captured["max_tokens"] == 3456

def test_agent_admin_turn_derives_schema_tier_from_selected_provider(monkeypatch):
    from services.agent import mission_dispatch

    captured = {}

    class _Provider:
        context_window = 1_000_000
        model = "frontier-test"

    monkeypatch.setattr(agent_config, "load_config", lambda: {})
    monkeypatch.setattr(agent_config, "get_provider", lambda *args, **kwargs: _Provider())

    def _run(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "stop_reason": "confirmed"}

    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", _run)
    out = worker_rpc._agent_admin_turn({"request": "inspect it"}, lambda data: None)

    assert out["ok"] is True
    assert captured["tier"] == "frontier"
    assert captured["capability"] is not None

def test_agent_admin_turn_does_not_repeat_normal_save_finalizer(monkeypatch):
    from services.agent import mission_dispatch
    from services.mcp import cad_common

    monkeypatch.setattr(agent_config, "get_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *args, **kwargs: {"success": True, "stop_reason": "confirmed"},
    )
    sweeps = []
    monkeypatch.setattr(
        cad_common,
        "save_touched_docs",
        lambda: sweeps.append("save") or {"saved": [], "errors": []},
    )

    out = worker_rpc._agent_admin_turn({"request": "inspect it"}, lambda data: None)
    assert out["ok"] is True
    assert sweeps == []


def test_agent_admin_turn_runs_crash_save_finalizer_once(monkeypatch):
    from services.agent import mission_dispatch
    from services.mcp import cad_common

    monkeypatch.setattr(agent_config, "get_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("turn crashed")),
    )
    sweeps = []
    monkeypatch.setattr(
        cad_common,
        "save_touched_docs",
        lambda: sweeps.append("save") or {"saved": [], "errors": []},
    )

    out = worker_rpc._agent_admin_turn({"request": "inspect it"}, lambda data: None)
    assert out["ok"] is False
    assert sweeps == ["save"]

# ──── workspace_dir plumbing (project-folder awareness) ────
# run.cpp's general lane resolves the user's project folder via
# agent_workspace_dir(options) and passes it as ``workspace_dir``. The worker is a
# fresh fork per turn, so _agent_admin_turn sets CHUDWARE_FS_ROOT /
# CHUDWARE_PROJECT_DIR / CHUDWARE_PROJECT_SAVE_DIR and chdirs into it — fs_* tools
# jail on CHUDWARE_FS_ROOT and previously resolved against the CAD source repo
# checkout (the worker's CWD). Absent/invalid param == behaviour exactly as before.

_WS_ENV_KEYS = ("CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR")


def _clear_ws_env(monkeypatch):
    for key in _WS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_agent_admin_turn_workspace_dir_sets_env_and_chdir(monkeypatch, tmp_path):
    """A valid workspace_dir sets the three env vars to the resolved absolute path
    and moves the worker's CWD into the user's project folder."""
    import os
    import sys

    assert sys.platform != "win32" or True  # path assertions use realpath both sides
    _clear_ws_env(monkeypatch)
    start_cwd = os.getcwd()
    provider = _OneToolProvider(["All done; the project is tidy."])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    try:
        out = worker_rpc._agent_admin_turn(
            {"request": "tidy my project folder",
             "workspace_dir": str(tmp_path)},
            lambda data: None,
        )
        assert out["ok"] is True
        root = os.path.realpath(str(tmp_path))
        for key in _WS_ENV_KEYS:
            assert os.path.realpath(os.environ[key]) == root
        assert os.path.realpath(os.getcwd()) == root
    finally:
        os.chdir(start_cwd)


def test_agent_admin_turn_workspace_dir_absent_mutates_nothing(monkeypatch):
    """No workspace_dir param: no env writes, no chdir — exactly today's behaviour."""
    import os

    _clear_ws_env(monkeypatch)
    start_cwd = os.getcwd()
    provider = _OneToolProvider(["Done."])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    out = worker_rpc._agent_admin_turn(
        {"request": "summarize the project"}, lambda data: None)
    assert out["ok"] is True
    for key in _WS_ENV_KEYS:
        assert key not in os.environ
    assert os.getcwd() == start_cwd


def test_agent_admin_turn_workspace_dir_nonexistent_is_ignored(monkeypatch, tmp_path):
    """A non-existent workspace_dir never fails the turn: it is ignored (no env
    writes, no chdir) and the turn still completes."""
    import os

    _clear_ws_env(monkeypatch)
    start_cwd = os.getcwd()
    provider = _OneToolProvider(["Done anyway."])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    out = worker_rpc._agent_admin_turn(
        {"request": "list my files",
         "workspace_dir": str(tmp_path / "does-not-exist")},
        lambda data: None,
    )
    assert out["ok"] is True
    for key in _WS_ENV_KEYS:
        assert key not in os.environ
    assert os.getcwd() == start_cwd

def test_workspace_dir_in_a_subfolder_jails_on_the_whole_project(monkeypatch, tmp_path):
    """The host passes the open DOCUMENT's folder as workspace_dir. The jail root must
    be the PROJECT, not that subfolder — otherwise a run_shell in a sibling folder is
    refused with outside_workspace_root (the live CDR failure).

    Deliverable saves still target the folder the host actually named, so a new part
    lands beside the document it came from.
    """
    import os

    _clear_ws_env(monkeypatch)
    start_cwd = os.getcwd()
    project = tmp_path / "489-final"
    (project / ".plm").mkdir(parents=True)
    geometry = project / "CAD & Design Geometry"   # spaces + '&' on purpose
    (geometry / ".plm").mkdir(parents=True)        # nested marker: the trap
    (project / "CDR").mkdir()

    provider = _OneToolProvider(["Listed it."])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    try:
        out = worker_rpc._agent_admin_turn(
            {"request": "list the CDR folder", "workspace_dir": str(geometry)},
            lambda data: None,
        )
        assert out["ok"] is True
        root = os.path.realpath(str(project))
        assert os.path.realpath(os.environ["CHUDWARE_FS_ROOT"]) == root
        assert os.path.realpath(os.environ["CHUDWARE_PROJECT_DIR"]) == root
        # …but saves stay where the host asked.
        assert os.path.realpath(os.environ["CHUDWARE_PROJECT_SAVE_DIR"]) == \
            os.path.realpath(str(geometry))
        assert os.path.realpath(os.getcwd()) == root

        # The behaviour that actually failed live: a sibling folder is now writable.
        from services.mcp import fs_tools
        assert fs_tools._root_block(str(project / "CDR")) is None
    finally:
        os.chdir(start_cwd)


def test_unmarked_workspace_dir_is_used_as_is(monkeypatch, tmp_path):
    """No project marker anywhere: the passed folder is the root, unchanged. Climbing
    must never widen the jail to $HOME just because no marker was found."""
    import os

    _clear_ws_env(monkeypatch)
    start_cwd = os.getcwd()
    plain = tmp_path / "loose" / "parts"
    plain.mkdir(parents=True)
    provider = _OneToolProvider(["Done."])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    try:
        out = worker_rpc._agent_admin_turn(
            {"request": "list my files", "workspace_dir": str(plain)},
            lambda data: None,
        )
        assert out["ok"] is True
        target = os.path.realpath(str(plain))
        for key in _WS_ENV_KEYS:
            assert os.path.realpath(os.environ[key]) == target
    finally:
        os.chdir(start_cwd)
