"""Tests for benchmark tier routing, diagnostic graders and leaderboard output.

Pure Python with fake/local providers; online routes are asserted without network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.agent.providers.mock_provider import MockProvider
from services.agent.tiers import (
    parse_openrouter_tier,
    parse_xai_tier,
    profile_tier_for,
)
from tools.flywheel.bench.gym.runner import (
    _ChudcodeCompletionProvider,
    _tier_execution,
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
    discover_cells,
    model_id_for_tier,
    SCHEMA,
)


# ---------------------------------------------------------------------------
# Tier parsing
# ---------------------------------------------------------------------------


class TestOpenRouterTier:
    def test_parse_openrouter_tier_valid(self):
        assert (
            parse_openrouter_tier("openrouter:anthropic/claude-opus-4")
            == "anthropic/claude-opus-4"
        )
        assert parse_openrouter_tier("openrouter:openai/gpt-4o") == "openai/gpt-4o"
        assert (
            parse_openrouter_tier("openrouter: google/gemma-2-9b ")
            == "google/gemma-2-9b"
        )

    def test_parse_openrouter_tier_rejects_non(self):
        assert parse_openrouter_tier("local-24k") is None
        assert parse_openrouter_tier("frontier") is None
        assert parse_openrouter_tier("openrouter:") is None
        assert parse_openrouter_tier("") is None
        assert parse_openrouter_tier(None) is None  # type: ignore[arg-type]

    def test_profile_tier_for_openrouter_maps_to_frontier(self):
        assert profile_tier_for("openrouter:anthropic/claude-opus-4") == "frontier"
        assert profile_tier_for("local-24k") == "local-24k"
        assert profile_tier_for("mid") == "mid"

    def test_openrouter_tier_routes_identifiers_to_chudcode(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)

        route = _tier_execution("openrouter:anthropic/claude-opus-4")

        assert route["offline"] is False
        assert route["provider"] is None
        assert route["settings"] == {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4",
        }

    def test_deterministic_env_override_stays_offline(self, monkeypatch):
        monkeypatch.setenv("CHUDWARE_PROVIDER", "mock")
        monkeypatch.setenv("CHUDWARE_MOCK_REPLIES", "")

        route = _tier_execution("openrouter:anthropic/claude-opus-4")

        assert route["offline"] is True
        assert route["provider"].name in ("mock", "mock-roles")

    def test_model_id_for_tier(self):
        m = model_id_for_tier("openrouter:anthropic/claude-opus-4")
        assert m["id"] == "anthropic/claude-opus-4"
        assert m["provider"] == "openrouter"
        assert m["kind"] == "paid"
        local = model_id_for_tier("local-24k")
        assert local["kind"] == "local"
        assert local["provider"] == "llamacpp"


class TestXaiTier:
    def test_parse_xai_tier_valid(self):
        assert parse_xai_tier("xai:grok-4.5") == "grok-4.5"
        assert parse_xai_tier("xai:grok-4-5") == "grok-4-5"
        assert parse_xai_tier("xai: grok-4-1 ") == "grok-4-1"

    def test_parse_xai_tier_rejects_non(self):
        assert parse_xai_tier("local-24k") is None
        assert parse_xai_tier("openrouter:x") is None
        assert parse_xai_tier("xai:") is None
        assert parse_xai_tier("") is None
        assert parse_xai_tier(None) is None  # type: ignore[arg-type]

    def test_profile_tier_for_xai_maps_to_frontier(self):
        assert profile_tier_for("xai:grok-4.5") == "frontier"
        assert profile_tier_for("xai:grok-4-5") == "frontier"

    def test_xai_tier_routes_identifiers_to_chudcode(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)

        route = _tier_execution("xai:grok-4-5")

        assert route["offline"] is False
        assert route["provider"] is None
        assert route["settings"] == {"provider": "xai", "model": "grok-4-5"}

    def test_online_completion_adapter_uses_shared_chudcode_host(self, monkeypatch):
        from services.agent import chudcode_host

        class Host:
            def __init__(self):
                self.turns = []
                self.closed = []

            def run_turn(self, params, emit):
                self.turns.append(dict(params))
                emit({"type": "assistant_delta", "text": "ignored"})
                return {"ok": True, "result": "diagnostic answer"}

            def close_session(self, session_id):
                self.closed.append(session_id)

        host = Host()
        monkeypatch.setattr(chudcode_host, "get_default_chudcode_host", lambda: host)
        provider = _ChudcodeCompletionProvider({"provider": "xai", "model": "grok-4-5"})

        reply = provider.complete(
            "Return JSON only.",
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            max_tokens=321,
        )

        assert reply == "diagnostic answer"
        assert len(host.turns) == 1
        assert host.turns[0]["system_prompt"] == "Return JSON only."
        assert host.turns[0]["request"] == "USER: hello"
        assert host.turns[0]["host_tools"] == []
        assert host.turns[0]["settings"] == {
            "provider": "xai",
            "model": "grok-4-5",
        }
        assert host.turns[0]["max_tokens"] == 321
        assert host.closed == [host.turns[0]["session_id"]]

    def test_model_id_for_tier_xai(self):
        m = model_id_for_tier("xai:grok-4-5")
        assert m["id"] == "grok-4.5"
        assert m["provider"] == "xai"
        assert m["kind"] == "paid"


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
        prov = MockProvider(
            ["Bore_H7", "Fillet_Rim", "BoltCircle_4xM6", "Extrude_Boss", "Pocket_Cut"]
        )
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
        text = (
            '[{"tool": "checkpoint", "args": {}}, {"tool": "edit_feature", "args": {}}]'
        )
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
        paid = next(
            r for r in results["results"] if r["tier"].startswith("openrouter:")
        )
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
                "challenge": "C1",
                "tier": "local-24k",
                "model": "qwen3-4b",
                "mode": "mock",
                "seed": 1,
                "cycle": "cycle-1",
                "passed": False,
                "classifications": [{"failure_class": "incoherent_geometry"}],
                "ledger_path": "",
            },
            {
                "challenge": "C1",
                "tier": "local-24k",
                "model": "qwen3-4b",
                "mode": "mock",
                "seed": 2,
                "cycle": "cycle-1",
                "passed": True,
                "classifications": [],
                "ledger_path": "",
            },
            {
                "challenge": "C1",
                "tier": "openrouter:anthropic/claude-opus-4",
                "model": "anthropic/claude-opus-4",
                "mode": "mock",
                "seed": 1,
                "cycle": "cycle-1",
                "passed": True,
                "classifications": [],
                "ledger_path": "",
            },
        ]
        diag = run_diag(
            ["local-24k", "openrouter:anthropic/claude-opus-4"],
            seed=1,
            provider_factory=self._mock_factory,
        )
        doc = aggregate(
            cells=cells,
            cycle_reports=[
                {
                    "cycle": "cycle-1",
                    "tiers": ["local-24k", "openrouter:anthropic/claude-opus-4"],
                    "execution_mode": "mock",
                    "pass_rates": {"overall": 0.5},
                }
            ],
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
                "tool_validity",
                "vision_fix",
                "feature_id",
                "vc_hygiene",
            }
            assert set(m["cost"]) >= {
                "usd_per_task_med",
                "latency_s_med",
                "tokens_per_task_med",
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

    def test_real_scores_exclude_historical_mock_cells(self):
        cells = [
            {
                "challenge": "C1",
                "tier": "local-24k",
                "model": "qwen3-4b",
                "seed": 1,
                "cycle": "old",
                "division": "local",
                "mode": "mock",
                "passed": True,
                "classifications": [],
                "ledger_path": "",
            },
            {
                "challenge": "C1",
                "tier": "local-24k",
                "model": "qwen3-4b",
                "seed": 1,
                "cycle": "current",
                "division": "local",
                "mode": "real",
                "passed": False,
                "classifications": [],
                "ledger_path": "",
            },
        ]
        doc = aggregate(
            cells=cells,
            cycle_reports=[
                {"cycle": "old", "execution_mode": "mock", "tiers": ["local-24k"]},
                {"cycle": "current", "execution_mode": "real", "tiers": ["local-24k"]},
            ],
            diag={},
        )
        model = next(row for row in doc["models"] if row["id"] == "qwen3-4b")
        assert model["primary"] == {
            "pass_rate": 0.0,
            "cells_total": 1,
            "cells_passed": 0,
        }
        assert not doc["task_suite"].endswith("-MOCK")

    def test_real_report_without_real_cells_cannot_clear_mock_badge(self):
        doc = aggregate(
            cells=[
                {
                    "challenge": "C1",
                    "tier": "local-24k",
                    "model": "qwen3-4b",
                    "seed": 1,
                    "cycle": "old",
                    "division": "local",
                    "mode": "mock",
                    "passed": True,
                    "classifications": [],
                    "ledger_path": "",
                }
            ],
            cycle_reports=[
                {"cycle": "old", "execution_mode": "mock", "tiers": ["local-24k"]},
                {"cycle": "current", "execution_mode": "real", "tiers": ["local-24k"]},
            ],
            diag={},
        )
        assert doc["task_suite"].endswith("-MOCK")

    def test_discover_cells_uses_exact_persisted_identity(self, tmp_path):
        run_dir = (
            tmp_path / "runs" / "current" / "C1_open_openrouter-anthropic-model_s1"
        )
        run_dir.mkdir(parents=True)
        exact_tier = "openrouter:anthropic/model"
        (run_dir / "cell.json").write_text(
            json.dumps(
                {
                    "challenge": "C1",
                    "tier": exact_tier,
                    "model": "anthropic/model",
                    "seed": 1,
                    "division": "open",
                    "cycle": "current",
                    "mode": "real",
                    "run_id": "gym_C1_open_openrouter:anthropic/model_s1",
                    "passed": False,
                    "graded": {"passed": False, "criteria": []},
                    "classifications": [],
                }
            )
        )
        cells = discover_cells(tmp_path, cycles={"current"})
        assert len(cells) == 1
        assert cells[0]["tier"] == exact_tier
        assert cells[0]["model"] == "anthropic/model"
        assert cells[0]["run_id"] == "gym_C1_open_openrouter:anthropic/model_s1"

    def test_build_and_write_from_empty_reports(self, tmp_path):
        """Smoke: aggregate an empty reports tree + mock diag → valid leaderboard."""
        reports = tmp_path / "reports"
        reports.mkdir()
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
        # No cycle reports: only diag-derived entries (local tiers) or none at all.
        assert any(m["kind"] == "local" for m in doc["models"]) or doc["models"] == []


# ---------------------------------------------------------------------------
# Transport-error honesty (retry / exclude / null axis)
# ---------------------------------------------------------------------------


class _RateLimitError(Exception):
    """Mimic openai.RateLimitError by name for is_transport_error."""


# Re-bind the class name so type(exc).__name__ == "RateLimitError".
_RateLimitError.__name__ = "RateLimitError"
_RateLimitError.__qualname__ = "RateLimitError"


class _FlakyThenOkProvider:
    """Fails with 429 for the first N complete() calls, then succeeds."""

    name = "mock"
    supports_vision = True

    def __init__(self, fails_before_ok: int, reply: str = "ok"):
        self.fails_before_ok = fails_before_ok
        self.reply = reply
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        if self.calls <= self.fails_before_ok:
            raise _RateLimitError(
                "Error code: 429 - free-models-per-min rate limited; retry shortly"
            )
        return self.reply


class _AlwaysRateLimitedProvider:
    name = "mock"
    supports_vision = True

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        raise _RateLimitError(
            "Error code: 429 - Provider returned error; temporarily rate-limited upstream"
        )


class _PartialTransportProvider:
    """First *n_fail* tasks always 429; remaining tasks return *ok_reply*."""

    name = "mock"
    supports_vision = False  # skip vision so we only exercise text axes if needed

    def __init__(self, n_fail: int, ok_reply: str):
        self.n_fail = n_fail
        self.ok_reply = ok_reply
        self.task_calls = 0
        self.total_complete_calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.total_complete_calls += 1
        # Each task may retry; count a new task when attempt sequence resets after success
        # or after TransportExhausted. Simpler: track by successful+exhausted task slots.
        # We advance task index only on first attempt of each task by using a flag.
        if not getattr(self, "_in_task", False):
            self.task_calls += 1
            self._in_task = True
            self._attempts_this_task = 0
        self._attempts_this_task += 1
        if self.task_calls <= self.n_fail:
            # Always fail this task (all attempts).
            if self._attempts_this_task >= 4:
                self._in_task = False
            raise _RateLimitError("Error code: 429 - rate limit")
        # Succeed on first attempt for remaining tasks.
        self._in_task = False
        return self.ok_reply


class TestTransportRetryHelper:
    def test_is_transport_error_429(self):
        from tools.flywheel.bench.gym.diag.transport import is_transport_error

        assert is_transport_error(_RateLimitError("Error code: 429 - rate limit"))
        assert is_transport_error(TimeoutError("timed out"))
        assert is_transport_error(ConnectionError("connection refused"))

        # Non-transport capability error
        class NotFoundError(Exception):
            pass

        assert not is_transport_error(
            NotFoundError(
                "Error code: 404 - No endpoints found that support image input"
            )
        )

    def test_retry_then_succeed(self):
        from tools.flywheel.bench.gym.diag.transport import complete_with_retry

        sleeps: list[float] = []
        prov = _FlakyThenOkProvider(fails_before_ok=2, reply="hello")
        reply, meta = complete_with_retry(
            prov,
            "sys",
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            max_tokens=64,
            sleep_fn=sleeps.append,
            rng=lambda: 0.5,  # deterministic jitter factor 1.0× base
        )
        assert reply == "hello"
        assert meta["attempts"] == 3
        assert meta["retried"] is True
        assert prov.calls == 3
        # Two sleeps: bases 2s then 8s (with rng=0.5 → factor 1.0)
        assert len(sleeps) == 2
        assert sleeps[0] == pytest.approx(2.0)
        assert sleeps[1] == pytest.approx(8.0)

    def test_exhaust_raises_transport_exhausted(self):
        from tools.flywheel.bench.gym.diag.transport import (
            TransportExhausted,
            complete_with_retry,
            MAX_TRANSPORT_ATTEMPTS,
        )

        prov = _AlwaysRateLimitedProvider()
        with pytest.raises(TransportExhausted) as ei:
            complete_with_retry(
                prov,
                "sys",
                [{"role": "user", "content": []}],
                sleep_fn=lambda _d: None,
                rng=lambda: 0.0,
            )
        assert ei.value.attempts == MAX_TRANSPORT_ATTEMPTS
        assert prov.calls == MAX_TRANSPORT_ATTEMPTS

    def test_non_transport_propagates(self):
        from tools.flywheel.bench.gym.diag.transport import complete_with_retry

        class Boom(Exception):
            pass

        class P:
            name = "mock"

            def complete(self, *a, **k):
                raise Boom("schema bad")

        with pytest.raises(Boom):
            complete_with_retry(P(), "s", [], sleep_fn=lambda _d: None)


class TestTransportAxisScoring:
    _TV_REPLIES = [
        '```json\n{"tool": "describe_part", "args": {}}\n```',
        '```json\n{"tool": "measure_part", "args": {"query": "bore diameter"}}\n```',
        '```json\n{"tool": "add_features", "args": {"ops": []}}\n```',
        '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
        '```json\n{"tool": "edit_feature", "args": {"feature_id": "F2", "params": {"depth": 12.5}}}\n```',
    ]

    def test_exhaust_error_excluded_from_denominator(self):
        """Exhausted transport tasks are error=true and out of the score denom."""
        from tools.flywheel.bench.gym.diag.tool_validity import run_tool_validity

        # Task 0 always rate-limited; tasks 1..4 return matching valid tool calls.
        class Prov:
            name = "mock"
            supports_vision = True

            def __init__(self):
                self.task_idx = 0
                self.attempts = 0

            def complete(self, system, messages, max_tokens=8000):
                if self.task_idx == 0:
                    self.attempts += 1
                    if self.attempts >= 4:
                        self.task_idx = 1
                        self.attempts = 0
                    raise _RateLimitError("Error code: 429 - rate limited")
                reply = TestTransportAxisScoring._TV_REPLIES[self.task_idx]
                self.task_idx += 1
                return reply

        out = run_tool_validity(Prov(), seed=1)
        assert out["n_errors"] == 1
        assert out["n_scored"] == 4
        assert out["n_passed"] == 4
        assert out["score"] == 1.0  # 4/4 scored; errored excluded
        assert out["errors"]["n_errors"] == 1
        err_tasks = [t for t in out["tasks"] if t.get("error")]
        assert len(err_tasks) == 1
        assert err_tasks[0]["error"] is True
        assert "transport exhausted" in err_tasks[0]["detail"]

    def test_majority_errors_null_axis(self):
        """>50% transport errors → axis score null (untested), not 0."""
        from tools.flywheel.bench.gym.diag.tool_validity import run_tool_validity

        # Tasks 0,1,2 fail completely (3/5 = 60% > 50%); 3,4 succeed.
        class Prov:
            name = "mock"
            supports_vision = True

            def __init__(self):
                self.task_idx = 0
                self.attempts = 0

            def complete(self, system, messages, max_tokens=8000):
                if self.task_idx < 3:
                    self.attempts += 1
                    if self.attempts >= 4:
                        self.task_idx += 1
                        self.attempts = 0
                    raise _RateLimitError("Error code: 429 - rate limited")
                reply = TestTransportAxisScoring._TV_REPLIES[self.task_idx]
                self.task_idx += 1
                return reply

        out = run_tool_validity(Prov(), seed=1)
        assert out["n_errors"] == 3
        assert out["n_scored"] == 2
        assert out["score"] is None  # untested — majority transport errors
        assert out["errors"]["n_errors"] == 3
        assert "axis_null_reason" in out["errors"]

    def test_leaderboard_null_with_errors_is_untested(self):
        from tools.flywheel.bench.gym.leaderboard import _axis_scores_for_model

        diag = {
            "results": [
                {
                    "tier": "openrouter:google/gemma-4-31b-it:free",
                    "model": {
                        "id": "google/gemma-4-31b-it:free",
                        "label": "Gemma",
                        "provider": "openrouter",
                        "kind": "paid",
                    },
                    "axes": {
                        "tool_validity": {
                            "axis": "tool_validity",
                            "score": None,
                            "n_errors": 5,
                            "errors": {
                                "n_errors": 5,
                                "first_detail": "provider error (transport exhausted…)",
                                "axis_null_reason": "5/5 tasks transport-errored (>50%)",
                            },
                            "skipped": False,
                        },
                        "vision_fix": {"score": 0.0, "skipped": False},
                        "feature_id": {"score": None, "skipped": True},
                        "vc_hygiene": {"score": 0.8, "skipped": False},
                    },
                }
            ],
        }
        axes = _axis_scores_for_model(
            diag,
            "google/gemma-4-31b-it:free",
            "openrouter:google/gemma-4-31b-it:free",
        )
        # null-with-errors and skipped → untested None; never dishonest 0 from errors
        assert axes["tool_validity"] is None
        assert axes["feature_id"] is None
        assert axes["vision_fix"] == 0.0  # real capability zero stays
        assert axes["vc_hygiene"] == 0.8
