"""Tests for openrouter tier parsing, diag graders, and leaderboard aggregator.

Pure-python with a mock provider — no network, no engine.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.agent.providers.mock_provider import MockProvider
from services.agent.tiers import (
    parse_openrouter_tier,
    effort_tier_for,
    provider_for_tier,
)
from tools.flywheel.bench.gym.diag.tool_validity import (
    grade_tool_validity,
    run_tool_validity,
    TOOL_VALIDITY_TASKS,
    is_schema_valid_tool_call,
)
from tools.flywheel.bench.gym.diag.vision_fix import grade_vision_fix, run_vision_fix
from tools.flywheel.bench.gym.diag.feature_id import grade_feature_id, run_feature_id
from tools.flywheel.bench.gym.diag.vc_hygiene import (
    grade_vc_hygiene,
    extract_tool_sequence,
    run_vc_hygiene,
)
from tools.flywheel.bench.gym.diag.runner import run_diag, write_diag_results, DIAG_AXES
from tools.flywheel.bench.gym.leaderboard import (
    aggregate,
    validate_leaderboard,
    build_and_write,
    model_id_for_tier,
    SCHEMA,
)


# ---------------------------------------------------------------------------
# Tier parsing
# ---------------------------------------------------------------------------

class TestOpenRouterTier:
    def test_parse_openrouter_tier_valid(self):
        assert parse_openrouter_tier("openrouter:anthropic/claude-opus-4") == \
            "anthropic/claude-opus-4"
        assert parse_openrouter_tier("openrouter:openai/gpt-4o") == "openai/gpt-4o"
        assert parse_openrouter_tier("openrouter: google/gemma-2-9b ") == "google/gemma-2-9b"

    def test_parse_openrouter_tier_rejects_non(self):
        assert parse_openrouter_tier("local-24k") is None
        assert parse_openrouter_tier("frontier") is None
        assert parse_openrouter_tier("openrouter:") is None
        assert parse_openrouter_tier("") is None
        assert parse_openrouter_tier(None) is None  # type: ignore[arg-type]

    def test_effort_tier_for_openrouter_maps_to_frontier(self):
        assert effort_tier_for("openrouter:anthropic/claude-opus-4") == "frontier"
        assert effort_tier_for("local-24k") == "local-24k"
        assert effort_tier_for("mid") == "mid"

    def test_provider_for_tier_openrouter(self, monkeypatch):
        monkeypatch.delenv("AI_CAD_PROVIDER", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
        prov = provider_for_tier("openrouter:anthropic/claude-opus-4", cfg={})
        assert prov.name == "openrouter"
        assert getattr(prov, "model", None) == "anthropic/claude-opus-4" or \
            getattr(prov, "_model", None) == "anthropic/claude-opus-4" or \
            "claude-opus-4" in str(getattr(prov, "model", getattr(prov, "_model", "")))

    def test_provider_for_tier_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("AI_CAD_PROVIDER", "mock")
        monkeypatch.setenv("AI_CAD_MOCK_REPLIES", "")
        prov = provider_for_tier("openrouter:anthropic/claude-opus-4")
        assert prov.name in ("mock", "mock-roles")

    def test_model_id_for_tier(self):
        m = model_id_for_tier("openrouter:anthropic/claude-opus-4")
        assert m["id"] == "anthropic/claude-opus-4"
        assert m["provider"] == "openrouter"
        assert m["kind"] == "paid"
        local = model_id_for_tier("local-24k")
        assert local["kind"] == "local"
        assert local["provider"] == "llamacpp"


# ---------------------------------------------------------------------------
# tool_validity grader
# ---------------------------------------------------------------------------

class TestToolValidityGrader:
    def test_good_fenced_tool_call(self):
        reply = '```json\n{"tool": "describe_part", "args": {}}\n```'
        task = TOOL_VALIDITY_TASKS[0]  # tv1_describe
        g = grade_tool_validity(reply, task)
        assert g["parseable"] is True
        assert g["schema_valid"] is True
        assert g["passed"] is True

    def test_bad_unparseable(self):
        reply = "Sure, I will describe the part for you."
        task = TOOL_VALIDITY_TASKS[0]
        g = grade_tool_validity(reply, task)
        assert g["parseable"] is False
        assert g["passed"] is False

    def test_bad_wrong_tool(self):
        reply = '```json\n{"tool": "delete_feature", "args": {}}\n```'
        task = TOOL_VALIDITY_TASKS[0]  # expects describe_part
        g = grade_tool_validity(reply, task)
        assert g["parseable"] is True
        assert g["schema_valid"] is True
        assert g["passed"] is False

    def test_schema_invalid_missing_tool(self):
        assert is_schema_valid_tool_call({"args": {}}) is False
        assert is_schema_valid_tool_call({"tool": 123, "args": {}}) is False
        assert is_schema_valid_tool_call({"tool": "x", "args": "nope"}) is False
        assert is_schema_valid_tool_call({"done": True, "handoff": {}}) is True

    def test_done_handoff_task(self):
        reply = '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'
        task = next(t for t in TOOL_VALIDITY_TASKS if t.get("expect_done"))
        g = grade_tool_validity(reply, task)
        assert g["passed"] is True

    def test_nested_args_ok(self):
        reply = (
            '```json\n{"tool": "edit_feature", "args": '
            '{"feature_id": "F2", "params": {"depth": 12.5}}}\n```'
        )
        task = next(t for t in TOOL_VALIDITY_TASKS if t["id"] == "tv5_nested_args")
        g = grade_tool_validity(reply, task)
        assert g["passed"] is True

    def test_run_with_mock_provider_all_pass(self):
        replies = [
            '```json\n{"tool": "describe_part", "args": {}}\n```',
            '```json\n{"tool": "measure_part", "args": {"query": "bore diameter"}}\n```',
            '```json\n{"tool": "add_features", "args": {"ops": [{"op": "extrude", "depth": 20}]}}\n```',
            '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
            '```json\n{"tool": "edit_feature", "args": {"feature_id": "F2", "params": {"depth": 12.5}}}\n```',
        ]
        prov = MockProvider(replies)
        out = run_tool_validity(prov, seed=1)
        assert out["axis"] == "tool_validity"
        assert out["n_passed"] == out["n_tasks"] == 5
        assert out["score"] == 1.0

    def test_run_with_mock_provider_all_fail(self):
        prov = MockProvider(["I refuse to call tools."])
        out = run_tool_validity(prov, seed=1)
        assert out["score"] == 0.0
        assert out["n_passed"] == 0


# ---------------------------------------------------------------------------
# vision_fix grader
# ---------------------------------------------------------------------------

class TestVisionFixGrader:
    def _task(self, wrong: bool = True):
        return {
            "id": "t",
            "expected_wrong": wrong,
            "expected_tools": {"add_features", "edit_feature"},
            "expected_keywords": {"boss", "missing", "add_features"},
        }

    def test_good_detects_wrong_and_names_tool(self):
        reply = "WRONG — the boss is missing. Call add_features to extrude it."
        g = grade_vision_fix(reply, self._task(True))
        assert g["passed"] is True

    def test_bad_misses_wrong(self):
        reply = "OK, the part looks fine to me."
        g = grade_vision_fix(reply, self._task(True))
        assert g["passed"] is False

    def test_good_ok_when_expected_ok(self):
        reply = "OK — matches the intended design."
        g = grade_vision_fix(reply, self._task(False))
        assert g["passed"] is True

    def test_bad_false_alarm_on_good_part(self):
        reply = "WRONG — everything is broken, use delete_feature."
        g = grade_vision_fix(reply, self._task(False))
        assert g["passed"] is False

    def test_skip_when_no_vision(self):
        class _NoVision:
            name = "mock"
            supports_vision = False

            def complete(self, *a, **k):
                raise AssertionError("must not be called when vision skipped")

        out = run_vision_fix(_NoVision(), seed=1)
        assert out["skipped"] is True
        assert out["score"] is None  # null, not 0


# ---------------------------------------------------------------------------
# feature_id grader
# ---------------------------------------------------------------------------

class TestFeatureIdGrader:
    def test_good_exact_name(self):
        task = {
            "expected": {"bore_h7", "bore"},
        }
        g = grade_feature_id("Bore_H7", task)
        assert g["passed"] is True

    def test_good_substring(self):
        task = {"expected": {"fillet_rim", "fillet"}}
        g = grade_feature_id("Edit Fillet_Rim to R5", task)
        assert g["passed"] is True

    def test_bad_wrong_feature(self):
        task = {"expected": {"bore_h7", "bore"}}
        g = grade_feature_id("BoltCircle_M4", task)
        assert g["passed"] is False

    def test_empty_fails(self):
        g = grade_feature_id("", {"expected": {"bore"}})
        assert g["passed"] is False

    def test_run_with_mock(self):
        # One correct reply reused (MockProvider clamps at last) — score > 0 for bore-ish.
        prov = MockProvider(["Bore_H7", "Fillet_Rim", "BoltCircle_4xM6",
                             "Extrude_Boss", "Pocket_Cut"])
        out = run_feature_id(prov, seed=1)
        assert out["axis"] == "feature_id"
        assert out["n_tasks"] == 5
        assert out["n_passed"] == 5
        assert out["score"] == 1.0


# ---------------------------------------------------------------------------
# vc_hygiene grader
# ---------------------------------------------------------------------------

class TestVcHygieneGrader:
    def test_extract_sequence_fenced(self):
        text = (
            '```json\n{"tool": "checkpoint", "args": {}}\n```\n'
            '```json\n{"tool": "delete_feature", "args": {"id": "X"}}\n```'
        )
        seq = extract_tool_sequence(text)
        assert seq == ["checkpoint", "delete_feature"]

    def test_extract_sequence_array(self):
        text = '[{"tool": "checkpoint", "args": {}}, {"tool": "edit_feature", "args": {}}]'
        seq = extract_tool_sequence(text)
        assert seq[0] == "checkpoint"
        assert seq[1] == "edit_feature"

    def test_good_checkpoint_before_delete(self):
        reply = (
            '```json\n{"tool": "checkpoint", "args": {"label": "pre-delete"}}\n```\n'
            '```json\n{"tool": "delete_feature", "args": {"feature_id": "Extrude_Boss"}}\n```'
        )
        g = grade_vc_hygiene(reply)
        assert g["passed"] is True
        assert g["sequence"][0].lower() == "checkpoint"

    def test_bad_delete_without_checkpoint(self):
        reply = '```json\n{"tool": "delete_feature", "args": {"feature_id": "X"}}\n```'
        g = grade_vc_hygiene(reply)
        assert g["passed"] is False

    def test_bad_empty(self):
        g = grade_vc_hygiene("I will think about it.")
        assert g["passed"] is False

    def test_run_with_mock_hygienic(self):
        good = (
            '```json\n{"tool": "checkpoint", "args": {}}\n```\n'
            '```json\n{"tool": "delete_feature", "args": {}}\n```'
        )
        prov = MockProvider([good])
        out = run_vc_hygiene(prov, seed=1)
        assert out["axis"] == "vc_hygiene"
        assert out["score"] == 1.0

    def test_run_with_mock_unhygienic(self):
        bad = '```json\n{"tool": "delete_feature", "args": {}}\n```'
        prov = MockProvider([bad])
        out = run_vc_hygiene(prov, seed=1)
        assert out["score"] == 0.0


# ---------------------------------------------------------------------------
# diag runner + leaderboard aggregator
# ---------------------------------------------------------------------------

class TestDiagRunnerAndLeaderboard:
    def _mock_factory(self, tier: str):
        # Scripted replies covering all four axes' task counts.
        tv = [
            '```json\n{"tool": "describe_part", "args": {}}\n```',
            '```json\n{"tool": "measure_part", "args": {"query": "bore diameter"}}\n```',
            '```json\n{"tool": "add_features", "args": {"ops": []}}\n```',
            '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
            '```json\n{"tool": "edit_feature", "args": {"feature_id": "F2", "params": {"depth": 12.5}}}\n```',
        ]
        vf = [
            "WRONG — boss missing; call add_features",
            "OK — matches intended design",
            "WRONG — fillet too large; edit_feature Fillet_edges",
            "WRONG — cut instead of boss; undo then add_features",
            "WRONG — missing mirrored hole; use mirror / add_features",
        ]
        fi = ["Bore_H7", "Fillet_Rim", "BoltCircle_4xM6", "Extrude_Boss", "Pocket_Cut"]
        vc = [
            '```json\n{"tool": "checkpoint", "args": {}}\n```\n'
            '```json\n{"tool": "delete_feature", "args": {}}\n```'
        ] * 5
        # MockProvider walks the list once; pad so later axes still get answers.
        replies = tv + vf + fi + vc
        prov = MockProvider(replies)
        # openrouter tiers should report vision support; local mock may not.
        if tier.startswith("openrouter:"):
            prov.supports_vision = True
        else:
            prov.supports_vision = False
        return prov

    def test_run_diag_mock(self, tmp_path):
        results = run_diag(
            ["local-24k", "openrouter:anthropic/claude-opus-4"],
            seed=1,
            provider_factory=self._mock_factory,
        )
        assert results["schema"] == "aicad.diag/1"
        assert results["mock"] is True
        assert set(results["axes"]) == set(DIAG_AXES)
        assert len(results["results"]) == 2
        # local: vision_fix skipped (supports_vision=False)
        local = next(r for r in results["results"] if r["tier"] == "local-24k")
        assert local["axes"]["vision_fix"]["score"] is None
        assert local["axes"]["vision_fix"]["skipped"] is True
        # openrouter: vision scored
        paid = next(r for r in results["results"]
                    if r["tier"].startswith("openrouter:"))
        assert paid["axes"]["vision_fix"]["score"] is not None
        assert paid["model"]["id"] == "anthropic/claude-opus-4"
        assert paid["model"]["kind"] == "paid"

        path = write_diag_results(results, out_dir=tmp_path)
        assert path.is_file()
        loaded = json.loads(path.read_text())
        assert loaded["schema"] == "aicad.diag/1"

    def test_aggregate_contract_shape(self, tmp_path):
        # Minimal synthetic cells + diag.
        cells = [
            {
                "challenge": "C1", "tier": "local-24k", "seed": 1, "cycle": "cycle-1",
                "passed": False,
                "classifications": [{"failure_class": "incoherent_geometry"}],
                "ledger_path": "",
            },
            {
                "challenge": "C1", "tier": "local-24k", "seed": 2, "cycle": "cycle-1",
                "passed": True, "classifications": [], "ledger_path": "",
            },
            {
                "challenge": "C1", "tier": "openrouter:anthropic/claude-opus-4",
                "seed": 1, "cycle": "cycle-1",
                "passed": True, "classifications": [], "ledger_path": "",
            },
        ]
        diag = run_diag(
            ["local-24k", "openrouter:anthropic/claude-opus-4"],
            seed=1,
            provider_factory=self._mock_factory,
        )
        doc = aggregate(
            cells=cells,
            cycle_reports=[{
                "cycle": "cycle-1",
                "tiers": ["local-24k", "openrouter:anthropic/claude-opus-4"],
                "execution_mode": "mock",
                "pass_rates": {"overall": 0.5},
            }],
            diag=diag,
            force_mock=True,
        )
        errs = validate_leaderboard(doc)
        assert errs == [], errs
        assert doc["schema"] == SCHEMA
        assert doc["task_suite"].endswith("-MOCK")
        assert len(doc["models"]) >= 2

        # Required keys/types on every model.
        for m in doc["models"]:
            assert isinstance(m["id"], str) and m["id"]
            assert m["kind"] in ("paid", "local")
            assert isinstance(m["primary"]["pass_rate"], float)
            assert isinstance(m["primary"]["cells_total"], int)
            assert set(m["axes"]) >= {
                "tool_validity", "vision_fix", "feature_id", "vc_hygiene"
            }
            assert set(m["cost"]) >= {
                "usd_per_task_med", "latency_s_med", "tokens_per_task_med"
            }
            assert isinstance(m["per_challenge"], list)
            assert isinstance(m["failure_taxonomy"], dict)
            if m["kind"] == "local":
                assert m["cost"]["usd_per_task_med"] is None

        # Paid model axes: vision_fix should be a float (not null) under mock-with-vision.
        paid = next(m for m in doc["models"] if m["kind"] == "paid")
        assert paid["id"] == "anthropic/claude-opus-4"
        assert paid["axes"]["tool_validity"] is not None
        # Local vision skipped → null
        local = next(m for m in doc["models"] if m["kind"] == "local")
        assert local["axes"]["vision_fix"] is None

    def test_build_and_write_from_existing_reports(self, tmp_path):
        """Smoke: aggregate real cycle-1 reports + mock diag → valid leaderboard."""
        reports = Path("tools/flywheel/bench/gym/reports")
        if not reports.is_dir():
            pytest.skip("no gym reports tree")
        diag = run_diag(
            ["local-24k"],
            seed=1,
            provider_factory=self._mock_factory,
        )
        diag_path = write_diag_results(diag, out_dir=tmp_path / "diag")
        out = tmp_path / "leaderboard.json"
        path = build_and_write(
            out=out,
            reports_dir=reports,
            diag_path=diag_path,
            force_mock=True,
        )
        assert path == out
        doc = json.loads(out.read_text())
        assert validate_leaderboard(doc) == []
        assert doc["schema"] == SCHEMA
        assert "-MOCK" in doc["task_suite"]
        assert isinstance(doc["models"], list)
        # Existing cycle-1 cells should produce at least the local model.
        assert any(m["kind"] == "local" for m in doc["models"]) or doc["models"] == []
