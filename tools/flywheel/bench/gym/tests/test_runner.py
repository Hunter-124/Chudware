"""Smoke test for the gym runner end to end.

Runs cells into a TMP runs_dir (never the committed reports/ tree) and asserts the
harness produces replayable run dirs, an independent grading, a classification pass, and
a real mission INTAKE.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.flywheel.bench.gym.challenges import load_challenge
from tools.flywheel.bench.gym import cli as cli_mod
from tools.flywheel.bench.gym import runner as runner_mod
from tools.flywheel.bench.gym.runner import (
    CycleResult,
    ExecProbe,
    InfrastructureUnavailable,
    UnknownChallengeError,
    execution_probe,
    run_cell,
    run_cycle,
)
from tools.flywheel.bench.gym import report as report_mod
from tools.flywheel.bench.gym.taxonomy import TAXONOMY


def test_probe_reports_chudcode_agent_fn_when_wired():
    """The real-execution probe recognizes the Chudcode-backed DAG node runner."""
    probe = execution_probe()
    assert probe.real_agent_fn is True


def test_default_agent_fn_uses_chudcode_without_provider_construction():
    import inspect
    from services.dag import node_runner

    src = inspect.getsource(node_runner._default_agent_fn)
    assert "run_role_turn" in src
    assert "get_provider" not in src
    assert "providers.anthropic" not in src


def test_paid_gym_route_never_constructs_python_provider(monkeypatch):
    from services.agent import config as agent_config

    monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)
    monkeypatch.setattr(runner_mod, "_load_benchmark_config", lambda: {})
    monkeypatch.setattr(
        agent_config,
        "get_provider",
        lambda *_args, **_kwargs: pytest.fail("online route constructed provider"),
    )

    route = runner_mod._tier_execution("openrouter:openai/gpt-5.6")

    assert route == {
        "offline": False,
        "provider": None,
        "settings": {"provider": "openrouter", "model": "openai/gpt-5.6"},
    }


def test_local_gym_route_marks_injected_provider_offline(monkeypatch):
    provider = SimpleNamespace(name="mock", model="fixture")
    monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)
    monkeypatch.setattr(runner_mod, "_load_benchmark_config", lambda: {})
    monkeypatch.setattr(
        runner_mod,
        "_offline_provider_for_tier",
        lambda tier, cfg: provider,
    )

    route = runner_mod._tier_execution("local-24k")

    assert route["offline"] is True
    assert route["provider"] is provider
    assert route["settings"] == {"provider": "mock", "model": "fixture"}


def test_benchmark_agent_fn_scopes_online_and_offline_nodes():
    seen = []

    def base(request, context):
        seen.append((request, context))
        return context

    original = {"node_id": "n1"}
    online = runner_mod._benchmark_agent_fn(
        base, {"provider": "openrouter", "model": "openai/gpt-5.6"}, False
    )
    online("paid", original)
    offline = runner_mod._benchmark_agent_fn(
        base, {"provider": "mock", "model": "fixture"}, True
    )
    offline("local", original)

    assert original == {"node_id": "n1"}
    assert seen[0][1]["role_model"] == {
        "provider": "openrouter",
        "model": "openai/gpt-5.6",
    }
    assert "offline" not in seen[0][1]
    assert seen[1][1]["role_model"] == {
        "provider": "mock",
        "model": "fixture",
    }
    assert seen[1][1]["offline"] is True


def test_single_cell_produces_replayable_run_dir(tmp_path):
    probe = execution_probe()
    ch = load_challenge("C1")
    cell = run_cell(ch, "local-24k", 1, cycle="t", probe=probe, runs_dir=tmp_path)
    rd = Path(cell.run_dir)
    assert rd.exists() and tmp_path in rd.parents, "run dir must be under the tmp root"
    for f in (
        "ledger.json",
        "graded.json",
        "classifications.json",
        "mission.json",
        "cell.json",
    ):
        assert (rd / f).exists(), f"missing replay artifact {f}"
    mission = json.loads((rd / "mission.json").read_text())
    assert mission.get("status") in {"running", "done", "blocked"}, mission
    assert "intake" in (mission.get("phases_completed") or []), mission
    assert cell.passed, f"C1 seed1 should pass all ACs: {cell.graded}"
    assert cell.classifications == [], "clean seed should classify nothing"


def test_cell_rerun_is_deterministic(tmp_path):
    """Re-running a cell must not double-write ledger events (Ledger loads-from-disk if
    the file exists). Regression for the append-on-rerun bug."""
    probe = execution_probe()
    ch = load_challenge("C1")
    c1 = run_cell(ch, "local-24k", 2, cycle="t", probe=probe, runs_dir=tmp_path)
    n1 = len(json.loads((Path(c1.run_dir) / "ledger.json").read_text())["events"])
    c2 = run_cell(ch, "local-24k", 2, cycle="t", probe=probe, runs_dir=tmp_path)
    n2 = len(json.loads((Path(c2.run_dir) / "ledger.json").read_text())["events"])
    assert n1 == n2, f"re-run doubled events: {n1} -> {n2}"
    assert [x.failure_class for x in c1.classifications] == [
        x.failure_class for x in c2.classifications
    ], "re-run changed classification"


def test_probe_reports_mode_honestly():
    p = execution_probe()
    assert p.mode in {"real", "mock"}
    d = p.to_dict()
    assert set(d) >= {
        "mode",
        "real_agent_fn",
        "frontier_key",
        "local_model",
        "core_available",
    }


def test_full_matrix_all_classes_fire(tmp_path):
    """The full suite must exercise every taxonomy class (the seeds are designed to)."""
    cr = run_cycle(cycle="t", tiers=["local-24k"], runs_dir=tmp_path)
    rep = report_mod.build_report(cr)
    counts = rep["taxonomy"]["counts"]
    fired = {k for k, v in counts.items() if v > 0}
    assert fired == set(TAXONOMY), f"classes never fired: {set(TAXONOMY) - fired}"
    # tool_misuse on the ORIGINAL C1-C8 suite must be exactly the 4 designed cells
    # (C1s2, C4s3, C5s3, C7s3) on 1 tier — the C6-seed3 build-tool false-positive must
    # stay fixed. C9-C15 add their own designed tool_misuse seeds, so scope to C1-C8.
    legacy = {f"C{i}" for i in range(1, 9)}
    legacy_misuse = sum(
        1
        for c in cr.cells
        if c.challenge in legacy
        for x in c.classifications
        if x.failure_class == "tool_misuse"
    )
    assert legacy_misuse == 4, (
        f"C1-C8 tool_misuse should be 4 on 1 tier, got {legacy_misuse}"
    )


def test_grind_dial_delta_is_real(tmp_path):
    """The assess-adjust-rerun loop must produce at least one non-zero deterministic
    delta: tightening grind_threshold 3->2 promotes the 2-attempt grind seed (C4 seed3
    variant) that would not fire at 3. Proves the loop actually changes a cell."""
    from tools.flywheel.bench.gym.challenges import load_challenges

    # count grind at threshold 3 vs 2 over the same ledgers.
    cr = run_cycle(cycle="t", tiers=["local-24k"], runs_dir=tmp_path)
    from tools.flywheel.bench.gym.taxonomy import classify_ledger

    g3 = g2 = 0
    for c in cr.cells:
        lg = json.loads((Path(c.run_dir) / "ledger.json").read_text())
        man = load_challenge(c.challenge).manifest
        g3 += sum(
            1
            for x in classify_ledger(
                lg, manifest=man, graded=c.graded, grind_threshold=3
            )
            if x.failure_class == "grind"
        )
        g2 += sum(
            1
            for x in classify_ledger(
                lg, manifest=man, graded=c.graded, grind_threshold=2
            )
            if x.failure_class == "grind"
        )
    assert g2 > g3, f"grind 3->2 must promote at least one cell: g3={g3} g2={g2}"


def test_capable_probe_does_not_promote_explicit_mock_to_real(tmp_path):
    probe = ExecProbe(
        core_available=True,
        frontier_key=True,
        local_model=True,
        real_agent_fn=True,
        dag_deps=True,
    )
    cell = run_cell(
        load_challenge("C1"),
        "local-24k",
        1,
        cycle="intent",
        probe=probe,
        runs_dir=tmp_path,
        real=False,
    )
    report = report_mod.build_report(
        CycleResult(cycle="intent", tiers=["local-24k"], probe=probe, cells=[cell])
    )
    assert probe.mode == "real"
    assert cell.mode == "mock"
    assert report["execution_mode"] == "mock"

    from tools.flywheel.bench.gym.telemetry import write_cycle_telemetry

    events_path = write_cycle_telemetry(report, tmp_path / "telemetry")
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    cell_event = next(event for event in events if event.get("type") == "gym.cell")
    assert cell_event["mode"] == "mock"
    assert cell_event["training_usable"] is False


def test_requested_real_refuses_before_any_cell_is_graded(tmp_path, monkeypatch):
    incapable = ExecProbe(
        core_available=False,
        frontier_key=False,
        local_model=False,
        real_agent_fn=True,
        dag_deps=True,
    )
    monkeypatch.setattr(runner_mod, "execution_probe", lambda: incapable)
    with pytest.raises(InfrastructureUnavailable) as caught:
        run_cycle(
            cycle="refused",
            tiers=["local-24k"],
            challenge_ids=["C1"],
            runs_dir=tmp_path,
            real=True,
        )
    assert caught.value.status == "infrastructure_unavailable"
    assert list(tmp_path.rglob("graded.json")) == []


def test_unknown_challenge_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_mod,
        "execution_probe",
        lambda: ExecProbe(False, False, False, True, True),
    )
    with pytest.raises(UnknownChallengeError) as caught:
        run_cycle(cycle="unknown", challenge_ids=["C999"], runs_dir=tmp_path)
    assert caught.value.challenge_ids == ["C999"]


def test_core_probe_uses_canonical_occt_build_candidate(tmp_path, monkeypatch):
    from services.mcp import core_bridge

    lib = tmp_path / "core" / "build-occt-linux" / "bin" / "libchudware.so"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"fixture")
    monkeypatch.delenv("CHUDWARE_LIBCHUDWARE", raising=False)
    monkeypatch.delenv("CHUDWARE_DIST", raising=False)
    monkeypatch.setattr(core_bridge, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner_mod, "_repo_root", lambda: tmp_path / "not-the-repo")
    assert runner_mod._core_so_present() is True


def test_execution_probe_recognizes_xai_environment_credential(monkeypatch):
    from services.agent import config as agent_config

    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MOONSHOT_API_KEY",
        "CHUDWARE_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    monkeypatch.setattr(agent_config, "load_config", lambda: {})
    monkeypatch.setattr(runner_mod, "_core_so_present", lambda: True)
    monkeypatch.setattr(runner_mod, "_local_model_present", lambda: False)
    monkeypatch.setattr(runner_mod, "_real_agent_fn_present", lambda: True)
    monkeypatch.setattr(runner_mod, "_dag_deps_importable", lambda: True)
    probe = execution_probe()
    assert probe.frontier_key is True
    assert probe.can_run_real is True
    monkeypatch.delenv("XAI_API_KEY")
    monkeypatch.setattr(
        agent_config,
        "load_config",
        lambda: {"keys": {"anthropic": "stored-config-key"}},
    )
    assert runner_mod._frontier_credentials_present() is True


def _run_args(**overrides):
    values = {
        "tiers": "local-24k",
        "challenges": "C1",
        "division": "local",
        "smoke": False,
        "cycle": "cli",
        "real": False,
        "no_adjust": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cli_returns_nonzero_for_refused_real(monkeypatch, capsys):
    exc = InfrastructureUnavailable(["core library unavailable"])
    monkeypatch.setattr(
        cli_mod, "run_cycle", lambda **_kwargs: (_ for _ in ()).throw(exc)
    )
    assert cli_mod.cmd_run(_run_args(real=True)) != 0
    assert "status=infrastructure_unavailable" in capsys.readouterr().err


def test_cli_returns_nonzero_for_unknown_challenge(monkeypatch, capsys):
    exc = UnknownChallengeError(["C999"])
    monkeypatch.setattr(
        cli_mod, "run_cycle", lambda **_kwargs: (_ for _ in ()).throw(exc)
    )
    assert cli_mod.cmd_run(_run_args(challenges="C999")) != 0
    assert "status=unknown_challenge" in capsys.readouterr().err


def test_cli_returns_nonzero_for_zero_cell_run(monkeypatch, capsys):
    probe = ExecProbe(False, False, False, True, True)
    empty = CycleResult(cycle="cli", tiers=["local-24k"], probe=probe)
    monkeypatch.setattr(cli_mod, "run_cycle", lambda **_kwargs: empty)
    assert cli_mod.cmd_run(_run_args()) != 0
    assert "status=zero_cells" in capsys.readouterr().err


def test_cli_returns_nonzero_when_every_diag_provider_errors(
    tmp_path, monkeypatch, capsys
):
    from tools.flywheel.bench.gym import diag

    result = {
        "tiers": ["xai:grok-4-5"],
        "axes": ["tool_validity"],
        "mock": False,
        "results": [{"tier": "xai:grok-4-5", "error": "provider unavailable"}],
    }
    monkeypatch.setattr(diag, "run_diag", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        diag, "write_diag_results", lambda _result: tmp_path / "diag.json"
    )
    args = SimpleNamespace(tiers="xai:grok-4-5", axes=None, seed=1)
    assert cli_mod.cmd_diag(args) != 0
    assert "status=all_provider_error" in capsys.readouterr().err


def main() -> None:
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"  PASS {name}")
    print("\nGYM RUNNER SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
