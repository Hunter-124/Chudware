"""run_dp2_gym.py — T-DP2 gym run: Stage 1 feature-architect.

Exercises the C1-DP2 challenge across all 3 seeds in MOCK mode (scripted turns
appended directly to the ledger — no live model required).

Usage:
    python tools/flywheel/bench/gym/run_dp2_gym.py

Writes results to tools/flywheel/bench/gym/reports/runs/dp2/<cell>/
and prints a concise summary to stdout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the repo root is on sys.path.
_REPO = Path(__file__).resolve().parents[4]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.flywheel.bench.gym.challenges.c1_dp2_feature_stage import CHALLENGE
from tools.flywheel.bench.gym.runner import execution_probe, run_cell

RUNS_DIR = Path(__file__).resolve().parent / "reports" / "runs"
CYCLE = "dp2"
TIER = "local-24k"


def main() -> None:
    probe = execution_probe()
    print(f"Execution probe: mode={probe.mode!r}  "
          f"core={probe.core_available}  frontier={probe.frontier_key}  "
          f"local_model={probe.local_model}")
    print(f"Challenge: {CHALLENGE.id} — {CHALLENGE.name}")
    print(f"Notes: {CHALLENGE.notes}")
    print()

    seed_results: list[dict] = []
    for seed in CHALLENGE.seeds:
        cell = run_cell(CHALLENGE, TIER, seed, cycle=CYCLE, probe=probe,
                        runs_dir=RUNS_DIR)
        seed_results.append(cell.to_dict())

        status = "PASS" if cell.passed else "FAIL"
        print(f"  seed {seed} [{cell.mode}]: {status}")
        for ac in cell.graded.get("criteria", []):
            mark = "✓" if ac["result"] == "PASS" else "✗"
            print(f"    {mark} {ac['id']}: {ac['detail']}")
        print()

    # Seed 1 must PASS; seeds 2 and 3 must FAIL (they inject bad data)
    expected = {1: True, 2: False, 3: False}
    outcome_ok = all(
        seed_results[i]["passed"] == expected[i + 1]
        for i in range(3)
    )

    summary = {
        "challenge": CHALLENGE.id,
        "cycle": CYCLE,
        "tier": TIER,
        "mode": seed_results[0]["mode"],
        "expected_outcomes": expected,
        "outcomes_correct": outcome_ok,
        "cells": seed_results,
    }
    out_path = RUNS_DIR / CYCLE / "summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Summary written → {out_path.relative_to(_REPO)}")

    if outcome_ok:
        print("GYM RESULT: PASS — seed1 passed, seeds 2+3 correctly failed.")
    else:
        wrong = [s for s in (1, 2, 3)
                 if seed_results[s - 1]["passed"] != expected[s]]
        print(f"GYM RESULT: FAIL — unexpected outcomes for seeds {wrong}.")
        sys.exit(1)


if __name__ == "__main__":
    # T-DP19: re-exec into the mcp-venv when this interpreter lacks the DAG deps. This
    # script runs C1-DP2 in MOCK mode (no live model), so it works under any interpreter
    # — but the guard keeps the entrypoint consistent with cli.py / tuning.py.
    from tools.flywheel.bench.gym.runner import ensure_capable_interpreter
    ensure_capable_interpreter("tools.flywheel.bench.gym.run_dp2_gym")
    main()
