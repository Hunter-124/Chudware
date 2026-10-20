from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from services.agent import request_context as rc
from services.agent.online_turn import activate_turn_env


def principal(actor="alice", workspace="team:org/team", right="editor"):
    return {"cell": True, "workspace_key": workspace, "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": right, "epoch": 1, "client_id": 7}


def test_context_restores_and_validates_trusted_envelope(monkeypatch):
    monkeypatch.delenv("CHUDWARE_CELL", raising=False)
    assert rc.owner_key() is None
    with rc.bind_context(principal()):
        assert rc.owner_key() == ("team:org/team", "alice")
        with pytest.raises(ValueError, match="cell_refused"):
            with rc.bind_context({**principal(), "actor_hex": "0" * 32}):
                pass
        assert rc.owner_key()[1] == "alice"
    assert rc.owner_key() is None
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    with pytest.raises(ValueError, match="cell_refused"):
        with rc.bind_context(None):
            pass


def test_actor_process_paths_exist_before_envelope_and_reject_other_actor(monkeypatch):
    from pathlib import Path
    from services import paths
    actor = principal()
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    monkeypatch.setenv("CHUDWARE_ACTOR_SANDBOX", "1")
    monkeypatch.setenv("CHUDWARE_WORKSPACE_KEY", actor["workspace_key"])
    monkeypatch.setenv("CHUDWARE_ACTOR_KEY", actor["actor_key"])
    monkeypatch.setenv("CHUDWARE_ACTOR_HEX", actor["actor_hex"])
    monkeypatch.setenv("XDG_DATA_HOME", "/actor/data")
    assert rc.current_context() == {}
    assert paths.RUNTIME == Path("/actor/run")
    before = rc.actor_state_root()
    assert before.parent == Path("/actor/data")
    with pytest.raises(ValueError, match="cell_refused"):
        rc.owner_key()
    with rc.bind_context(actor):
        assert rc.actor_state_root() == before
        assert paths.RUNTIME == Path("/actor/run")
    for other in (principal("bob"), principal(workspace="team:other/team")):
        with pytest.raises(ValueError, match="cell_refused"):
            with rc.bind_context(other):
                pytest.fail("foreign process envelope accepted")
    monkeypatch.setenv("CHUDWARE_ACTOR_HEX", "0" * 32)
    with pytest.raises(ValueError, match="cell_refused"):
        rc.actor_state_root()
    with pytest.raises(ValueError, match="cell_refused"):
        _ = paths.RUNTIME


def test_turn_roots_are_thread_local_without_global_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", "ambient-root")
    original_cwd = os.getcwd()
    def turn(name):
        path = str(tmp_path / name)
        with activate_turn_env(path, path, SimpleNamespace(clear_pooled_credentials=lambda: None)) as errors:
            assert not errors
            assert rc.root_value("CHUDWARE_PROJECT_DIR") == path
            assert os.environ["CHUDWARE_PROJECT_DIR"] == "ambient-root"
            assert os.getcwd() == original_cwd
            return rc.root_value("CHUDWARE_FS_ROOT")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(turn, ["alice", "bob"])) == {str(tmp_path / "alice"), str(tmp_path / "bob")}
    assert rc.root_value("CHUDWARE_PROJECT_DIR") == "ambient-root"


def test_minted_handles_persist_and_are_bound_to_owner_and_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    with rc.bind_context(principal()):
        session = rc.mint_handle("session")
        run = rc.mint_handle("run", session)
        assert session != run
        rc.require_handle(run, "run", session)
        root = rc.actor_state_root()
        assert session not in {p.name for p in root.rglob("*")}
    with rc.bind_context(principal("bob")):
        for handle, kind in [(session, "session"), (run, "run"), ("absent", "run")]:
            with pytest.raises(KeyError, match="not_found"):
                rc.require_handle(handle, kind)
    with rc.bind_context(principal()):
        rc.require_handle(session, "session")
        with pytest.raises(KeyError, match="not_found"):
            rc.require_handle(run, "run", "wrong-session")


def test_worker_mint_and_control_hide_other_actor(tmp_path, monkeypatch):
    from services.agent import worker_rpc
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: SimpleNamespace(
        cancel=lambda run_id, **kw: calls.append(run_id) or {"ok": True}))
    with rc.bind_context(principal()):
        handles = worker_rpc._agent_handles_new({}, lambda event: None)
        assert handles["ok"]
        credentials = {"_chudware_actor": {"key_source": "byok"}}
        assert worker_rpc.chudcode_control(
            "cancel", {"run_id": handles["run_id"], **credentials})["ok"]
    with rc.bind_context(principal("bob")):
        missing = worker_rpc.chudcode_control("cancel", {"run_id": "absent"})
        assert worker_rpc.chudcode_control("cancel", {"run_id": handles["run_id"]}) == missing
        assert worker_rpc._agent_handles_new({"session_id": handles["session_id"]}, lambda event: None) == missing
    assert calls == [handles["run_id"]]


def test_history_seed_scope_does_not_collide(tmp_path, monkeypatch):
    from services.agent import worker_rpc
    worker_rpc._HISTORY_SEEDED_SESSIONS.clear()
    try:
        for actor in ("alice", "bob"):
            with rc.bind_context(principal(actor)):
                assert worker_rpc._claim_history_seed("same", [{"role": "user", "text": actor}], resumed=False)[1]
                assert not worker_rpc._claim_history_seed("same", [], resumed=False)[1]
    finally:
        worker_rpc._HISTORY_SEEDED_SESSIONS.clear()


@pytest.mark.parametrize("source", ["pool", "subscription", "byok"])
def test_cell_config_refuses_python_provider_before_ambient_loading(source, monkeypatch):
    from services.agent import config
    actor = config.Actor.from_mapping({"email": "fixture@example.test", "plan": "pro",
                                      "credits_remaining": 10, "key_source": source})
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("loaded ambient provider config"))
    monkeypatch.setattr(config, "_resolve_provider", lambda *args, **kwargs: pytest.fail("constructed vendor SDK"))
    with rc.bind_context(principal()):
        with pytest.raises(ValueError, match="provider_broker_route_required"):
            config.get_provider("openai", actor=actor)
        if source == "byok":
            with pytest.raises(ValueError, match="provider_broker_route_required"):
                config._byok_provider_config("openai", {"api_key": "forbidden-key"}, actor)


def test_sandbox_argv_has_fixed_launcher_and_mode():
    with rc.bind_context(principal()):
        assert rc.sandbox_argv(["/bin/sh", "-c", "printf fixture"], "ro") == [
            "/opt/chudware/bin/chudware-actor-sandbox", principal()["actor_hex"],
            "ro", "--", "/bin/sh", "-c", "printf fixture"]


def test_deliverable_artifacts_use_actor_private_state(tmp_path, monkeypatch):
    from services.deliverables.project import artifacts_dir, resolve_project_path
    from services.deliverables.errors import DeliverableError
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CHUDWARE_ARTIFACTS_DIR", str(tmp_path / "operator"))
    with rc.bind_roots(str(tmp_path / "project")):
        with rc.bind_context(principal()):
            own = artifacts_dir()
            (own / "fixture.txt").write_text("alice")
            assert resolve_project_path("fixture.txt").read_text() == "alice"
            assert own != tmp_path / "operator"
        with rc.bind_context(principal("bob")):
            assert artifacts_dir() != own
            for path in (str(own / "fixture.txt"), "../fixture.txt"):
                with pytest.raises(DeliverableError, match="not_found"):
                    resolve_project_path(path)


def test_thread_jobs_share_no_actor_metadata_and_capture_context():
    from services.mcp import cad_common
    import threading
    completed = threading.Event()
    seen = []
    def work():
        seen.append(rc.owner_key()[1])
        completed.set()
        return {"actor": seen[-1]}
    with rc.bind_context(principal()):
        jid = cad_common._start_job("fixture", work)
        assert completed.wait(1)
        assert seen == ["alice"]
        own = cad_common._JOBS[jid]
    with rc.bind_context(principal("bob")):
        assert cad_common._JOBS.get(jid) is None
        assert jid not in list(cad_common._JOBS)
        assert cad_common._JOBS.get("absent") is None
    with rc.bind_context(principal()):
        assert cad_common._JOBS[jid] is own


def test_stdio_dispatch_binds_envelope_not_parameter_identity(monkeypatch):
    from services import worker_main
    monkeypatch.setitem(worker_main._METHODS, "fixture_owner", lambda params, emit: rc.owner_key())
    assert worker_main.dispatch_request({
        "method": "fixture_owner", "ctx": principal(),
        "params": {"ctx": principal("bob")}}, lambda event: None) == ("team:org/team", "alice")
    assert rc.owner_key() is None


def test_cell_model_exec_uses_sandbox_child(monkeypatch, tmp_path):
    from services import kernel_exec
    calls = []
    monkeypatch.setattr(kernel_exec, "cell_path", lambda path, **kwargs: tmp_path)
    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{\"ok\":true}', stderr="")
    monkeypatch.setattr(kernel_exec.subprocess, "run", launch)
    with rc.bind_context(principal()):
        result = kernel_exec.run_code_multi("result = fixture", tmp_path)
    assert result.ok
    assert calls[0][0][:4] == ["/opt/chudware/bin/chudware-actor-sandbox",
                                principal()["actor_hex"], "rw", "--"]
    assert "timeout" not in calls[0][1]


@pytest.mark.parametrize(("method", "key"), [
    ("vendor_part_register_local_cad", "cad_path"),
    ("vendor_part_scan_library", "folder"),
])
def test_vendor_source_paths_are_confined_before_dispatch(method, key, monkeypatch, tmp_path):
    from services import worker_main
    calls = []
    monkeypatch.setitem(worker_main._METHODS, method,
                        lambda params, emit: calls.append(params) or {"ok": True})
    with pytest.raises(ValueError, match="cell_refused"):
        worker_main.dispatch_request({"method": method, "ctx": principal(),
                                      "params": {key: "/actor/other/secret.step"}},
                                     lambda event: None)
    assert not calls
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    alias = tmp_path / "linked-source"
    alias.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match="cell_refused"):
        worker_main.dispatch_request({"method": method, "ctx": principal(),
                                      "params": {key: str(alias)}},
                                     lambda event: None)
    assert not calls
    worker_main.dispatch_request({"method": method, "ctx": principal(),
                                  "params": {key: "parts/fixture.step"}},
                                 lambda event: None)
    assert calls == [{key: "/work/parts/fixture.step"}]


def test_gateway_controls_authorize_actor_without_provider_key_or_grant():
    from services.agent import worker_rpc
    with rc.bind_context(principal()):
        assert worker_rpc._control_credentials({}) == {}
        for key in ("api_key", "web_credential", "byok_key"):
            with pytest.raises(ValueError, match="credential_isolation_unsupported"):
                worker_rpc._control_credentials({key: "upstream-key"})



def test_delegated_stop_is_trusted_cancel_only_without_provider_secret(monkeypatch):
    from services import worker_main
    from services.agent import worker_rpc
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}
    ctx = {**principal(), "control_delegate": delegate}
    calls = []
    monkeypatch.setitem(worker_main._METHODS, "agent_cancel",
                        lambda params, emit: calls.append(rc.control_delegate("cancel")) or {"ok": True})
    monkeypatch.setitem(worker_main._METHODS, "dag_abort",
                        lambda params, emit: pytest.fail("DAG gate rejection is not a delegated stop"))
    assert worker_main.dispatch_request({"method": "agent_cancel", "ctx": ctx,
                                         "params": {}}, lambda event: None)["ok"]
    assert calls == [delegate]
    with rc.bind_context(ctx):
        assert worker_rpc._control_credentials({}) == {}
        with pytest.raises(ValueError, match="forbidden_right"):
            rc.control_delegate("status")
    with pytest.raises(ValueError, match="forbidden_right"):
        worker_main.dispatch_request({"method": "dag_abort", "ctx": ctx,
                                      "params": {}}, lambda event: None)
    with pytest.raises(ValueError, match="forbidden_right"):
        worker_main.dispatch_request({"method": "agent_cancel", "ctx": principal(),
                                      "params": {"control_delegate": delegate}},
                                     lambda event: None)
    with pytest.raises(ValueError, match="forbidden_right"):
        with rc.bind_context({**principal(), "control_delegate": {**delegate, "action": "steer"}}):
            pass


@pytest.mark.parametrize(("method", "extra"), [
    ("session_handoff", {}),
    ("session_btw", {"question": "What changed?"}),
    ("session_compact", {}),
    ("chudcode_export_session", {}),
    ("chudcode_branch_session", {"entry_id": "entry"}),
    ("chudcode_rewind_session", {"user_text": "prompt", "occurrence": 0}),
    ("chudcode_repair_session", {}),
    ("chudcode_close_session", {}),
    ("chudcode_session_stats", {}),
    ("chudcode_compact", {}),
    ("chudcode_set_auto_compaction", {"enabled": True}),
    ("chudcode_set_auto_retry", {"enabled": True}),
    ("chudcode_tool_state", {}),
])
def test_session_rpc_foreign_and_absent_have_identical_typed_failure(
    method, extra, tmp_path, monkeypatch,
):
    from services.agent import chudcode_host, config, session_cmds, worker_rpc
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
    monkeypatch.setattr(session_cmds, "_offline_enabled", lambda: False)
    monkeypatch.setattr(config, "online_compaction_settings", lambda: ("summary", {}))
    host = chudcode_host.ChudcodeSessionHost(
        client_factory=lambda **kwargs: pytest.fail("boundary failure launched a client"),
    )
    monkeypatch.setattr(chudcode_host, "get_default_chudcode_host", lambda: host)
    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: host)
    with rc.bind_context(principal()):
        session_id = rc.mint_handle("session")
        key = rc.scoped_key(session_id)
        session = chudcode_host._Session(
            session_id=session_id, client=object(), resume_file=None,
            workspace_dir="/work", registry_key=key,
            launch_params={"provider": "fixture"},
        )
        host._sessions[key] = session
        assert host.session_launch_params(session_id) == {"provider": "fixture"}
    handler = worker_rpc.SESSION_METHODS.get(method) or getattr(worker_rpc, f"_{method}")
    emitted = []
    with rc.bind_context(principal("bob")):
        for handle in (session_id, "absent"):
            assert handler({"session_id": handle, **extra}, emitted.append) == {
                "ok": False, "reason": "not_found", "code": "not_found",
            }
    assert not emitted
    assert host._sessions[key] is session
    assert not session.closed


def test_cell_refusal_adapter_preserves_only_failed_typed_responses():
    from services.agent import session_cmds, worker_rpc
    with rc.bind_context(principal()):
        assert worker_rpc._cell_refusal({"ok": True, "reason": "not_found"}) is None
        assert worker_rpc._cell_refusal(ValueError("ordinary provider failure")) is None
        for code in ("not_found", "cell_refused", "forbidden_right", "revoked",
                     "credential_isolation_unsupported", "byok_credential_mismatch"):
            refusal = {"ok": False, "reason": code, "code": code}
            assert worker_rpc._cell_refusal(refusal) == refusal
            assert worker_rpc._cell_refusal(
                session_cmds.SessionCommandUnavailable("context added", code=code)
            ) == refusal
    assert worker_rpc._cell_refusal({"ok": False, "code": "not_found"}) is None
