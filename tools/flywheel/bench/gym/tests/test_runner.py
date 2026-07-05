"""Smoke test for the gym runner end to end (T-W27).

Runs a single cell (C1, local-24k, seed 1) and asserts the harness produces a replayable
run dir, an independent grading, a classification pass, and a real mission INTAKE. Uses a
tmp cycle name so it never clobbers the committed cycle-1 report.
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.flywheel.bench.gym.challenges import load_challenge
from tools.flywheel.bench.gym.runner import run_cell, execution_probe, run_cycle
from tools.flywheel.bench.gym import report as report_mod


def test_single_cell_produces_replayable_run_dir():
    probe = execution_probe()
    ch = load_challenge("C1")
    cell = run_cell(ch, "local-24k", 1, cycle="smoke-test", probe=probe)
    rd = Path(cell.run_dir)
    assert rd.exists(), "run dir must exist"
    for f in ("ledger.json", "graded.json", "classifications.json", "mission.json"):
        assert (rd / f).exists(), f"missing replay artifact {f}"
    # the mission drove real INTAKE (manifest validated, reached PLAN or later).
    mission = json.loads((rd / "mission.json").read_text())
    assert mission.get("status") in {"running", "done", "blocked"}, mission
    assert "intake" in (mission.get("phases_completed") or []), mission
    # seed 1 is the clean variant → all ACs pass, nothing classified.
    assert cell.passed, f"C1 seed1 should pass all ACs: {cell.graded}"
    assert cell.classifications == [], "clean seed should classify nothing"


def test_cell_rerun_is_deterministic():
    """Re-running a cell must not double-write ledger events (the Ledger loads-from-disk
    if the file exists). A second run over the same run dir must produce an identical
    event count. Regression for the append-on-rerun bug."""
    probe = execution_probe()
    ch = load_challenge("C1")
    c1 = run_cell(ch, "local-24k", 2, cycle="smoke-det", probe=probe)
    n1 = len(json.loads((Path(c1.run_dir) / "ledger.json").read_text())["events"])
    c2 = run_cell(ch, "local-24k", 2, cycle="smoke-det", probe=probe)
    n2 = len(json.loads((Path(c2.run_dir) / "ledger.json").read_text())["events"])
    assert n1 == n2, f"re-run doubled events: {n1} -> {n2}"
    assert [x.failure_class for x in c1.classifications] == \
           [x.failure_class for x in c2.classifications], "re-run changed classification"


def test_probe_reports_mode_honestly():
    p = execution_probe()
    # In this environment there is no real agent_fn (providers.anthropic module absent).
    assert p.mode in {"real", "mock"}
    d = p.to_dict()
    assert set(d) >= {"mode", "real_agent_fn", "frontier_key", "local_model",
                      "core_available"}


def test_full_matrix_all_classes_fire():
    """The full suite must exercise every taxonomy class (the seeds are designed to)."""
    cr = run_cycle(cycle="smoke-matrix", tiers=["local-24k"])
    rep = report_mod.build_report(cr)
    counts = rep["taxonomy"]["counts"]
    fired = {k for k, v in counts.items() if v > 0}
    from tools.flywheel.bench.gym.taxonomy import TAXONOMY
    assert fired == set(TAXONOMY), f"classes never fired: {set(TAXONOMY) - fired}"
    # tool_misuse must be exactly the 3 designed cells (C1s2, C4s3, C5s3) — the C6-seed3
    # build-tool false-positive must stay fixed.
    assert counts["tool_misuse"] == 3, f"tool_misuse should be 3 on 1 tier, got {counts}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
    print("\nGYM RUNNER SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
