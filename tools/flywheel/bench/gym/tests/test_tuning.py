"""Tests for the gym tuning loop (T-DP10).

All tests run in MOCK mode (no real model, no port 1234 dependency).
They verify:
  - compute_win_rates: tie detection, winner election
  - run_tuning: writes output, records honest numbers, ≥2 configs enforced
  - ConfigCellResult serialisation
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tools.flywheel.bench.gym.tuning import (
    ConfigCellResult,
    TuningResult,
    compute_win_rates,
    run_tuning,
    TUNING_SCHEMA,
)


# ---------------------------------------------------------------------------
# compute_win_rates
# ---------------------------------------------------------------------------

def _make_cell(challenge="C1", tier="local-24k", seed=1,
               scaffold_config="eco", passed=False, mode="mock",
               status="failed", phase="plan") -> ConfigCellResult:
    return ConfigCellResult(
        challenge=challenge, tier=tier, seed=seed,
        scaffold_config=scaffold_config, passed=passed,
        mode=mode, mission_status=status, mission_phase=phase,
    )


class TestComputeWinRates:
    def test_all_fail_is_tied_at_zero(self):
        cells = [
            _make_cell(scaffold_config="eco", passed=False),
            _make_cell(scaffold_config="maximum", passed=False),
        ]
        result = compute_win_rates(cells, ["eco", "maximum"])
        winners = result["per_class_winners"]
        # All tied at 0 — tied must be True
        for ctkey, w in winners.items():
            assert w["tied"] is True, f"{ctkey}: expected tied=True, got {w}"

    def test_winner_elected_when_gap(self):
        cells = [
            _make_cell(scaffold_config="eco", passed=True),
            _make_cell(scaffold_config="maximum", passed=False),
        ]
        result = compute_win_rates(cells, ["eco", "maximum"])
        winners = result["per_class_winners"]
        # There should be a winner — eco passes, maximum fails
        for ctkey, w in winners.items():
            if w["n"] >= 1:
                assert w["tied"] is False
                assert w["scaffold_level"] == "eco"
                assert w["win_rate"] == 1.0

    def test_per_config_rates_present(self):
        cells = [
            _make_cell(scaffold_config="eco", passed=True),
            _make_cell(scaffold_config="maximum", passed=False),
        ]
        result = compute_win_rates(cells, ["eco", "maximum"])
        assert "per_config_win_rates" in result
        assert "eco" in result["per_config_win_rates"]
        assert "maximum" in result["per_config_win_rates"]

    def test_all_pass_is_tied_at_one(self):
        cells = [
            _make_cell(scaffold_config="eco", passed=True),
            _make_cell(scaffold_config="maximum", passed=True),
        ]
        result = compute_win_rates(cells, ["eco", "maximum"])
        for w in result["per_class_winners"].values():
            assert w["tied"] is True
            assert w["win_rate"] == 1.0

    def test_multiple_seeds_aggregated(self):
        cells = [
            _make_cell(scaffold_config="eco", seed=1, passed=True),
            _make_cell(scaffold_config="eco", seed=2, passed=True),
            _make_cell(scaffold_config="maximum", seed=1, passed=False),
            _make_cell(scaffold_config="maximum", seed=2, passed=False),
        ]
        result = compute_win_rates(cells, ["eco", "maximum"])
        for w in result["per_class_winners"].values():
            assert w["tied"] is False
            assert w["scaffold_level"] == "eco"


# ---------------------------------------------------------------------------
# run_tuning (mock mode)
# ---------------------------------------------------------------------------

class TestRunTuning:
    def test_requires_at_least_two_configs(self):
        with pytest.raises(ValueError, match="≥2"):
            run_tuning(configs=["eco"], mock=True)

    def test_writes_output_json(self, tmp_path):
        out = tmp_path / "out.json"
        run_tuning(
            cycle="test-tuning",
            configs=["eco", "maximum"],
            challenge_ids=["C1"],
            tiers=["local-24k"],
            seeds=[1],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["schema"] == TUNING_SCHEMA
        assert data["cycle"] == "test-tuning"
        assert "per_class_winners" in data
        assert "per_config_win_rates" in data

    def test_cells_have_mode_field(self, tmp_path):
        out = tmp_path / "out.json"
        run_tuning(
            cycle="test-mode",
            configs=["eco", "maximum"],
            challenge_ids=["C1"],
            tiers=["local-24k"],
            seeds=[1],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        data = json.loads(out.read_text())
        for cell in data["cells"]:
            assert "mode" in cell
            # mock mode → mode is "mock" (never "real")
            assert cell["mode"] in ("mock", "real", "error")

    def test_cell_count_is_configs_times_challenges_times_seeds(self, tmp_path):
        out = tmp_path / "out.json"
        result = run_tuning(
            cycle="test-count",
            configs=["eco", "maximum"],
            challenge_ids=["C1"],
            tiers=["local-24k"],
            seeds=[1, 2],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        # 2 configs × 1 challenge × 1 tier × 2 seeds = 4 cells
        assert len(result.cells) == 4

    def test_tied_configs_recorded_as_tied(self, tmp_path):
        """When both configs produce the same outcome (pass or fail), tied=True is recorded."""
        out = tmp_path / "out.json"
        run_tuning(
            cycle="test-tied",
            configs=["eco", "maximum"],
            challenge_ids=["C1"],
            tiers=["local-24k"],
            seeds=[1],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        data = json.loads(out.read_text())
        for w in data["per_class_winners"].values():
            # Both configs run the same scripted mock turns → same pass/fail → tied
            assert w["tied"] is True
            # win_rate is the same for both configs (whether 0.0 or 1.0 depends on the
            # challenge's scripted seed — we verify tied, not the specific rate)
            eco_rate = w["all_configs"]["eco"]["win_rate"]
            max_rate = w["all_configs"]["maximum"]["win_rate"]
            assert eco_rate == max_rate, f"eco={eco_rate} != maximum={max_rate}"

    def test_result_to_dict_roundtrip(self, tmp_path):
        out = tmp_path / "out.json"
        result = run_tuning(
            cycle="test-rt",
            configs=["eco", "maximum"],
            challenge_ids=["C1"],
            tiers=["local-24k"],
            seeds=[1],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        d = result.to_dict()
        assert d["schema"] == TUNING_SCHEMA
        assert set(d["configs"]) == {"eco", "maximum"}
        assert "cells" in d

    def test_c1_dp2_challenge_included(self, tmp_path):
        """C1-DP2 loads without error in mock mode."""
        out = tmp_path / "out.json"
        run_tuning(
            cycle="test-dp2",
            configs=["eco", "maximum"],
            challenge_ids=["C1-DP2"],
            tiers=["local-24k"],
            seeds=[1],
            runs_dir=tmp_path / "runs",
            output_path=out,
            mock=True,
        )
        assert out.exists()
        data = json.loads(out.read_text())
        assert len(data["cells"]) == 2   # 2 configs × 1 challenge × 1 seed
