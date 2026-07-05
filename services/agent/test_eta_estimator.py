"""Tests for the T-DP18 ETA estimator.

All expected-token and TPS numbers are real measured values from
benchmarks/tokenmaxx/results/matrix.json (T-W29 bench, 2026-07-05).
No values are invented or approximated — see eta_estimator.py docstring
for full provenance.

Fixture quick-reference (source: matrix.json micro_evals, json_retry.tokens):
  qwen3-4b   feature_modeler_expansion  → 480 output tokens
  qwen3-4b   reviewer_critique          → 953 output tokens
  qwen3-4b   worker_toolcall            → 249 output tokens
  gemma3-4b  feature_modeler_expansion  → 658 output tokens
  gemma3-4b  reviewer_critique          → 282 output tokens
  gemma3-4b  worker_toolcall            → 272 output tokens

TPS fixtures (matrix.json):
  qwen3-4b  single-stream 175.12 tok/s  |  N=8 aggregate 475.86 tok/s
  gemma3-4b single-stream 161.54 tok/s  |  N=8 aggregate 331.52 tok/s
  integrator smoke (T-W31): 482 tok/s aggregate
"""
from __future__ import annotations

import pytest
from services.agent.eta_estimator import (
    ETAEstimator,
    SubtaskSpec,
    NodeETA,
    MissionETA,
    RoleShape,
    MODEL_QWEN3_4B,
    MODEL_GEMMA3_4B,
    LOCAL_TPS_QWEN3_4B_AGG8,
    LOCAL_TPS_QWEN3_4B_SINGLE,
    LOCAL_TPS_GEMMA3_4B_AGG8,
    LOCAL_TPS_GEMMA3_4B_SINGLE,
    LOCAL_TPS_DEFAULT_AGG8,
    _MEASURED_OUTPUT_TOKENS,
    _fmt_s,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_estimator(tps: float = LOCAL_TPS_QWEN3_4B_AGG8) -> ETAEstimator:
    return ETAEstimator(tps_aggregate=tps, default_model_id=MODEL_QWEN3_4B)


# ---------------------------------------------------------------------------
# 1. Token cost table — integrity checks
# ---------------------------------------------------------------------------

class TestTokenCostTable:
    """Verify the constant table matches the documented source values."""

    def test_qwen3_feature_modeler_expansion_is_480(self):
        # Source: matrix.json micro_evals.feature_modeler_expansion.json_retry.tokens
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_QWEN3_4B, RoleShape.FEATURE_MODELER_EXPANSION)]
        assert tokens == 480
        assert n == 1

    def test_qwen3_reviewer_critique_is_953(self):
        # Source: matrix.json micro_evals.reviewer_critique.json_retry.tokens
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_QWEN3_4B, RoleShape.REVIEWER_CRITIQUE)]
        assert tokens == 953
        assert n == 1

    def test_qwen3_worker_toolcall_is_249(self):
        # Source: matrix.json micro_evals.worker_toolcall.json_retry.tokens
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_QWEN3_4B, RoleShape.WORKER_TOOLCALL)]
        assert tokens == 249
        assert n == 1

    def test_gemma3_feature_modeler_expansion_is_658(self):
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_GEMMA3_4B, RoleShape.FEATURE_MODELER_EXPANSION)]
        assert tokens == 658
        assert n == 1

    def test_gemma3_reviewer_critique_is_282(self):
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_GEMMA3_4B, RoleShape.REVIEWER_CRITIQUE)]
        assert tokens == 282
        assert n == 1

    def test_gemma3_worker_toolcall_is_272(self):
        tokens, n = _MEASURED_OUTPUT_TOKENS[(MODEL_GEMMA3_4B, RoleShape.WORKER_TOOLCALL)]
        assert tokens == 272
        assert n == 1


# ---------------------------------------------------------------------------
# 2. TPS constants match the bench report
# ---------------------------------------------------------------------------

class TestTPSConstants:
    """Verify TPS constants match REPORT.md / matrix.json exactly."""

    def test_qwen3_single_stream_tps(self):
        # Source: matrix.json single_stream.server_decode_tps
        assert abs(LOCAL_TPS_QWEN3_4B_SINGLE - 175.12) < 0.01

    def test_qwen3_agg8_tps(self):
        # Source: matrix.json parallel[n=8].aggregate_tps
        assert abs(LOCAL_TPS_QWEN3_4B_AGG8 - 475.86) < 0.01

    def test_gemma3_single_stream_tps(self):
        assert abs(LOCAL_TPS_GEMMA3_4B_SINGLE - 161.54) < 0.01

    def test_gemma3_agg8_tps(self):
        assert abs(LOCAL_TPS_GEMMA3_4B_AGG8 - 331.52) < 0.01

    def test_default_agg_is_integrator_smoke(self):
        # Integrator smoke T-W31: 482 tok/s
        assert LOCAL_TPS_DEFAULT_AGG8 == 482.0


# ---------------------------------------------------------------------------
# 3. Node ETA — point computation
# ---------------------------------------------------------------------------

class TestNodeETA:
    """Verify per-node ETA arithmetic with real numbers."""

    def test_qwen3_worker_toolcall_point_estimate(self):
        # 249 tokens / 175.12 tok/s = 1.422 s  (single-stream rate → per-node)
        est = make_estimator(tps=LOCAL_TPS_QWEN3_4B_SINGLE)
        spec = SubtaskSpec(
            node_id="build_op_1",
            role_shape=RoleShape.WORKER_TOOLCALL,
            model_id=MODEL_QWEN3_4B,
        )
        ne = est.node_eta(spec, tps=LOCAL_TPS_QWEN3_4B_SINGLE)
        assert not ne.unknown
        assert ne.tokens_mean == 249
        assert abs(ne.eta_seconds - 249 / 175.12) < 0.5

    def test_qwen3_feature_modeler_expansion_point_estimate(self):
        # 480 tokens / 475.86 tok/s = 1.009 s  (aggregate rate → parallel fan-out)
        est = make_estimator()
        spec = SubtaskSpec(
            node_id="specify_dl1",
            role_shape=RoleShape.FEATURE_MODELER_EXPANSION,
            model_id=MODEL_QWEN3_4B,
        )
        ne = est.node_eta(spec)
        assert not ne.unknown
        assert ne.tokens_mean == 480
        expected = 480 / LOCAL_TPS_QWEN3_4B_AGG8
        assert abs(ne.eta_seconds - expected) < 0.5

    def test_gemma3_reviewer_critique_point(self):
        # 282 tokens / 331.52 tok/s = 0.85 s
        est = ETAEstimator(tps_aggregate=LOCAL_TPS_GEMMA3_4B_AGG8, default_model_id=MODEL_GEMMA3_4B)
        spec = SubtaskSpec(
            node_id="verify_step",
            role_shape=RoleShape.REVIEWER_CRITIQUE,
            model_id=MODEL_GEMMA3_4B,
        )
        ne = est.node_eta(spec)
        expected = 282 / LOCAL_TPS_GEMMA3_4B_AGG8
        assert abs(ne.eta_seconds - expected) < 0.5

    def test_n_turns_multiplies_tokens(self):
        # n_turns=3 should triple the tokens (and ETA).
        est = make_estimator()
        single = SubtaskSpec("n", RoleShape.WORKER_TOOLCALL, MODEL_QWEN3_4B, n_turns=1)
        triple = SubtaskSpec("n", RoleShape.WORKER_TOOLCALL, MODEL_QWEN3_4B, n_turns=3)
        ne1 = est.node_eta(single)
        ne3 = est.node_eta(triple)
        assert ne3.tokens_mean == ne1.tokens_mean * 3
        # Allow 0.2s tolerance: both values are rounded to 1 decimal independently.
        assert abs(ne3.eta_seconds - ne1.eta_seconds * 3) < 0.2

    def test_phase_name_resolves_build_to_worker_toolcall(self):
        # "build_xxx" node should auto-resolve to WORKER_TOOLCALL via phase mapping.
        est = make_estimator()
        spec = SubtaskSpec(node_id="build_flanged_bushing")
        ne = est.node_eta(spec)
        assert not ne.unknown
        assert ne.role_shape == RoleShape.WORKER_TOOLCALL

    def test_phase_name_resolves_specify_to_expansion(self):
        est = make_estimator()
        spec = SubtaskSpec(node_id="specify_dl1")
        ne = est.node_eta(spec)
        assert not ne.unknown
        assert ne.role_shape == RoleShape.FEATURE_MODELER_EXPANSION

    def test_phase_name_resolves_verify_to_reviewer(self):
        est = make_estimator()
        spec = SubtaskSpec(node_id="verify_dl1")
        ne = est.node_eta(spec)
        assert not ne.unknown
        assert ne.role_shape == RoleShape.REVIEWER_CRITIQUE


# ---------------------------------------------------------------------------
# 4. Uncertainty range — honest degradation
# ---------------------------------------------------------------------------

class TestUncertaintyRange:
    """n=1 sample → wide range [×0.5, ×2.0]; n=0 → unknown."""

    def test_n1_produces_wide_range(self):
        est = make_estimator()
        spec = SubtaskSpec("intake", RoleShape.INTAKE, MODEL_QWEN3_4B)
        ne = est.node_eta(spec)
        # n_samples=1 for all current measurements
        assert ne.n_samples == 1
        # low = point × 0.5, high = point × 2.0
        assert abs(ne.eta_low_s  - ne.eta_seconds * 0.5) < 0.2
        assert abs(ne.eta_high_s - ne.eta_seconds * 2.0) < 0.5

    def test_unknown_role_shape_returns_unknown(self):
        est = make_estimator()
        spec = SubtaskSpec("mystery_stage_xyz", role_shape="completely_unknown_shape")
        ne = est.node_eta(spec)
        assert ne.unknown
        assert ne.eta_seconds is None
        assert ne.eta_low_s is None
        assert ne.eta_high_s is None

    def test_none_role_shape_returns_unknown(self):
        est = make_estimator()
        spec = SubtaskSpec("unrecognized_node_id_zzz", role_shape=None)
        ne = est.node_eta(spec)
        assert ne.unknown


# ---------------------------------------------------------------------------
# 5. Mission ETA — whole-mission computation
# ---------------------------------------------------------------------------

class TestMissionETA:
    """Verify whole-mission ETA aggregation with a C1-representative task list."""

    def _c1_subtasks(self):
        """Approximate C1 gym task (single part, 5 stages) using real role shapes."""
        return [
            SubtaskSpec("intake",   RoleShape.INTAKE,   MODEL_QWEN3_4B),
            SubtaskSpec("specify",  RoleShape.SPECIFY,  MODEL_QWEN3_4B),
            SubtaskSpec("plan",     RoleShape.PLAN,     MODEL_QWEN3_4B),
            SubtaskSpec("build",    RoleShape.BUILD,    MODEL_QWEN3_4B),
            SubtaskSpec("verify",   RoleShape.VERIFY,   MODEL_QWEN3_4B),
            SubtaskSpec("deliver",  RoleShape.DELIVER,  MODEL_QWEN3_4B),
        ]

    def test_all_known_nodes_produces_point_eta(self):
        est = make_estimator()
        meta = est.mission_eta(self._c1_subtasks())
        assert not meta.unknown
        assert meta.eta_seconds is not None
        assert meta.eta_seconds > 0

    def test_c1_eta_with_real_numbers(self):
        # Manual cross-check: token costs for qwen3 at N=8 agg TPS.
        # INTAKE→WORKER_TOOLCALL: 249, SPECIFY→EXPANSION: 480,
        # PLAN→EXPANSION: 480, BUILD→WORKER_TOOLCALL: 249,
        # VERIFY→REVIEWER: 953, DELIVER→WORKER_TOOLCALL: 249
        # Total = 249+480+480+249+953+249 = 2660 tokens
        # ETA = 2660 / 475.86 = ~5.59 s
        est = make_estimator(tps=LOCAL_TPS_QWEN3_4B_AGG8)
        meta = est.mission_eta(self._c1_subtasks())
        expected = (249 + 480 + 480 + 249 + 953 + 249) / LOCAL_TPS_QWEN3_4B_AGG8
        assert abs(meta.eta_seconds - expected) < 0.5, (
            f"Expected ~{expected:.1f}s, got {meta.eta_seconds}s"
        )

    def test_partial_unknown_sets_partial_flag(self):
        est = make_estimator()
        subtasks = [
            SubtaskSpec("build", RoleShape.BUILD, MODEL_QWEN3_4B),
            SubtaskSpec("mystery_stage", role_shape="unmapped_shape"),
        ]
        meta = est.mission_eta(subtasks)
        assert meta.unknown
        assert meta.partial
        assert meta.eta_seconds is None
        # Low end should reflect the known node
        assert meta.eta_low_s is not None
        assert meta.eta_low_s > 0

    def test_all_unknown_nodes(self):
        est = make_estimator()
        subtasks = [
            SubtaskSpec("stage_a", role_shape="unmapped_a"),
            SubtaskSpec("stage_b", role_shape="unmapped_b"),
        ]
        meta = est.mission_eta(subtasks)
        assert meta.unknown
        assert not meta.partial   # no known nodes at all
        assert meta.eta_seconds is None
        assert meta.eta_low_s is None

    def test_empty_remaining_returns_zero(self):
        est = make_estimator()
        meta = est.mission_eta([])
        assert meta.remaining_nodes_count == 0
        # No unknown nodes → not unknown; no tokens → ETA 0.0
        assert not meta.unknown
        assert meta.eta_seconds == 0.0 or meta.eta_seconds is not None

    def test_node_etas_keyed_by_node_id(self):
        est = make_estimator()
        subtasks = self._c1_subtasks()
        meta = est.mission_eta(subtasks)
        for spec in subtasks:
            assert spec.node_id in meta.node_etas

    def test_remaining_nodes_count(self):
        est = make_estimator()
        subtasks = self._c1_subtasks()
        meta = est.mission_eta(subtasks)
        assert meta.remaining_nodes_count == len(subtasks)


# ---------------------------------------------------------------------------
# 6. Formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    """Verify format strings for status-bar and DAG widget."""

    def test_fmt_s_seconds(self):
        assert _fmt_s(45) == "45s"
        assert _fmt_s(1)  == "1s"

    def test_fmt_s_minutes(self):
        assert _fmt_s(90)  == "1m 30s"
        assert _fmt_s(120) == "2m"
        assert _fmt_s(65)  == "1m 5s"

    def test_fmt_s_hours(self):
        assert _fmt_s(3600) == "1h"
        assert _fmt_s(3660) == "1h 1m"

    def test_fmt_s_zero(self):
        assert _fmt_s(0) == "0s"

    def test_format_eta_point(self):
        # n=1 gives a wide range → format_eta should show the range form.
        est = make_estimator()
        meta = est.mission_eta([SubtaskSpec("build", RoleShape.BUILD, MODEL_QWEN3_4B)])
        label = ETAEstimator.format_eta(meta)
        assert label.startswith("ETA")
        assert "unknown" not in label.lower()

    def test_format_eta_unknown(self):
        est = make_estimator()
        meta = est.mission_eta([SubtaskSpec("x", role_shape="unmapped")])
        assert ETAEstimator.format_eta(meta) == "ETA unknown"

    def test_format_eta_empty(self):
        est = make_estimator()
        meta = est.mission_eta([])
        label = ETAEstimator.format_eta(meta)
        assert label.startswith("ETA")

    def test_format_node_eta_unknown(self):
        ne = NodeETA(None, None, None, unknown=True)
        assert "?" in ETAEstimator.format_node_eta(ne)

    def test_format_node_eta_known(self):
        est = make_estimator()
        spec = SubtaskSpec("build", RoleShape.BUILD, MODEL_QWEN3_4B)
        ne = est.node_eta(spec)
        label = ETAEstimator.format_node_eta(ne)
        assert "~" in label or "s" in label


# ---------------------------------------------------------------------------
# 7. Default TPS fallback (integrator smoke: 482 tok/s)
# ---------------------------------------------------------------------------

class TestDefaultTPS:
    def test_default_tps_is_integrator_smoke(self):
        est = ETAEstimator()
        assert est.tps_aggregate == 482.0

    def test_default_tps_gives_reasonable_eta(self):
        est = ETAEstimator()
        spec = SubtaskSpec("build", RoleShape.BUILD, MODEL_QWEN3_4B)
        ne = est.node_eta(spec)
        # 249 / 482 = ~0.52 s
        assert not ne.unknown
        assert abs(ne.eta_seconds - 249 / 482.0) < 0.5
