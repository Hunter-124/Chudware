from __future__ import annotations

import contextlib
import json

from tools.flywheel import replay_ledger_failures as replay_mod


def _write_ledger(tmp_path, calls: list[dict]) -> None:
    run = tmp_path / "run-1"
    run.mkdir()
    (run / "ledger.json").write_text(json.dumps({"events": [
        {"kind": "agent_turn", "payload": call} for call in calls
    ]}))


def test_policy_refusals_are_loaded_but_never_dispatched(tmp_path, monkeypatch):
    _write_ledger(tmp_path, [
        {"event": "tool_call", "tool": "add_features", "args": {"ops": []},
         "ok": False, "status": "call_cap_reached", "policy_refusal": True,
         "executed": False, "error": "call cap reached"},
        {"event": "tool_call", "tool": "add_features", "args": {"ops": []},
         "ok": False, "status": "loop_guard_refused", "policy_refusal": True,
         "executed": False, "error": "loop guard refused repeat"},
    ])
    rows = replay_mod.load_failures(tmp_path)

    def forbidden_agent_turn():
        raise AssertionError("a refusal must never reach _bind_tool")

    monkeypatch.setattr(replay_mod, "_agent_turn", forbidden_agent_turn)
    results = replay_mod.replay(rows, doc_id="private-doc", geometry_seeded=True)

    assert len(results) == 2
    assert all(row["status"] == "refusal" for row in results)
    assert all(row["fixed"] is None for row in results)
    assert {row["outcome_kind"] for row in results} == {"policy_refusal"}


    from services.mcp import private_hostd
    monkeypatch.setattr(
        private_hostd, "private_daemon",
        lambda: (_ for _ in ()).throw(
            AssertionError("refusal-only corpus must not start a daemon")),
    )
    assert replay_mod.main(["--runs", str(tmp_path)]) == 0

def test_private_daemon_failure_skips_without_mint_or_dispatch(tmp_path, monkeypatch):
    _write_ledger(tmp_path, [{
        "event": "tool_call", "tool": "measure_part", "args": {},
        "ok": False, "error": "no bodies",
    }])

    @contextlib.contextmanager
    def no_private_daemon():
        yield False

    from services.mcp import private_hostd
    monkeypatch.setattr(private_hostd, "private_daemon", no_private_daemon)
    monkeypatch.setattr(
        replay_mod, "_mint_doc",
        lambda: (_ for _ in ()).throw(AssertionError("must not mint on ambient daemon")),
    )
    monkeypatch.setattr(
        replay_mod, "_agent_turn",
        lambda: (_ for _ in ()).throw(AssertionError("must not bind or call tools")),
    )

    assert replay_mod.main(["--runs", str(tmp_path)]) == 0


def test_seed_failure_skips_geometry_dependent_rows(monkeypatch):
    calls = {"measure": 0}

    def new_part(*, name):
        return json.dumps({"ok": True, "doc_id": "private-doc"})

    def add_features(**_kwargs):
        return json.dumps({"ok": False, "error": "seed rejected"})

    def measure_part(*, doc_id=None):
        calls["measure"] += 1
        raise AssertionError("geometry-dependent replay must not run without seed geometry")

    class AgentTurn:
        @staticmethod
        def _bind_tool(name):
            return {
                "new_part": new_part,
                "add_features": add_features,
                "measure_part": measure_part,
            }.get(name)

        @staticmethod
        def _signature_spec(fn):
            import inspect
            return {"params": tuple(inspect.signature(fn).parameters)}

        @staticmethod
        def _reconcile_kwargs(_tool, _fn, args):
            return args, [], None

    monkeypatch.setattr(replay_mod, "_agent_turn", lambda: AgentTurn)
    doc_id, geometry_seeded, seed_error = replay_mod._mint_doc()
    assert doc_id == "private-doc"
    assert geometry_seeded is False
    assert "seed rejected" in seed_error

    results = replay_mod.replay(
        [{"run": "r", "tool": "measure_part", "args": {}, "was": "no bodies"}],
        doc_id=doc_id,
        geometry_seeded=geometry_seeded,
    )
    assert results[0]["status"] == "skipped"
    assert results[0]["fixed"] is None
    assert calls["measure"] == 0


def test_explicit_tool_filter_with_no_rows_is_nonzero_no_data(tmp_path):
    _write_ledger(tmp_path, [{
        "event": "tool_call", "tool": "measure_part", "args": {},
        "ok": False, "error": "no bodies",
    }])
    assert replay_mod.main([
        "--runs", str(tmp_path), "--tool", "typo_measure_parts",
    ]) == replay_mod.EXIT_NO_DATA
