"""Smoke test for the gym runner end to end (T-W27).

Runs cells into a TMP runs_dir (never the committed reports/ tree) and asserts the
harness produces replayable run dirs, an independent grading, a classification pass, and
a real mission INTAKE.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tools.flywheel.bench.gym.challenges import load_challenge
from tools.flywheel.bench.gym.runner import run_cell, execution_probe, run_cycle
from tools.flywheel.bench.gym import report as report_mod
from tools.flywheel.bench.gym.taxonomy import TAXONOMY


def test_probe_reports_real_agent_fn_when_wired():
    """T-W30 regression: the execution probe must report real_agent_fn=True now that
    node_runner._default_agent_fn is the real tool-executing turn (config provider
    seam + agent_turn), NOT the historical silent stub that imported a non-existent
    services.agent.providers.anthropic module. This test FAILS if the stub returns."""
    probe = execution_probe()
    assert probe.real_agent_fn is True, (
        "node_runner._default_agent_fn is not the real tool-executing turn "
        "(regressed to the silent stub?)")


def test_default_agent_fn_is_not_the_silent_stub():
    """Directly assert the node_runner default no longer imports the wrong provider
    module and no longer returns a hardcoded confirmed stub."""
    import inspect
    from services.dag import node_runner
    src = inspect.getsource(node_runner._default_agent_fn)
    assert "providers.anthropic " not in src and "providers.anthropic\n" not in src, \
        "still importing the non-existent providers.anthropic module"
    assert "agent_turn" in src, "default agent_fn must run the tool-calling turn"
    assert "get_provider" in src, \
        "default agent_fn must resolve the provider via the config seam"


def test_single_cell_produces_replayable_run_dir(tmp_path):
    probe = execution_probe()
    ch = load_challenge("C1")
    cell = run_cell(ch, "local-24k", 1, cycle="t", probe=probe, runs_dir=tmp_path)
    rd = Path(cell.run_dir)
    assert rd.exists() and tmp_path in rd.parents, "run dir must be under the tmp root"
    for f in ("ledger.json", "graded.json", "classifications.json", "mission.json"):
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
    assert [x.failure_class for x in c1.classifications] == \
           [x.failure_class for x in c2.classifications], "re-run changed classification"


def test_probe_reports_mode_honestly():
    p = execution_probe()
    assert p.mode in {"real", "mock"}
    d = p.to_dict()
    assert set(d) >= {"mode", "real_agent_fn", "frontier_key", "local_model",
                      "core_available"}


def test_full_matrix_all_classes_fire(tmp_path):
    """The full suite must exercise every taxonomy class (the seeds are designed to)."""
    cr = run_cycle(cycle="t", tiers=["local-24k"], runs_dir=tmp_path)
    rep = report_mod.build_report(cr)
    counts = rep["taxonomy"]["counts"]
    fired = {k for k, v in counts.items() if v > 0}
    assert fired == set(TAXONOMY), f"classes never fired: {set(TAXONOMY) - fired}"
    # tool_misuse must be exactly the 4 designed cells (C1s2, C4s3, C5s3, C7s3) on 1 tier —
    # the C6-seed3 build-tool false-positive must stay fixed.
    assert counts["tool_misuse"] == 4, f"tool_misuse should be 4 on 1 tier, got {counts}"


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
        g3 += sum(1 for x in classify_ledger(lg, manifest=man, graded=c.graded,
                                             grind_threshold=3) if x.failure_class == "grind")
        g2 += sum(1 for x in classify_ledger(lg, manifest=man, graded=c.graded,
                                             grind_threshold=2) if x.failure_class == "grind")
    assert g2 > g3, f"grind 3->2 must promote at least one cell: g3={g3} g2={g2}"


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
