"""ETA estimator for Chudware missions — T-DP18.

Live ETA-to-part-complete:

    ETA = remaining_tokens / effective_tokens_per_sec

where
  ``remaining_tokens``  = Σ expected_output_tokens over not-yet-done subtasks/stages
  ``effective_tokens_per_sec`` = measured decode rate (live or fixture)

Token costs come from **real measured data only** — the tokenmaxx bench
(benchmarks/tokenmaxx/results/matrix.json) recorded per-role-shape output-token
counts against real role handoff schemas (``measure_structured`` in bench.py,
T-W29/W31).  The ledger ``tools_slot.tokens`` field is a prompt-injection size,
NOT a generation count — it must not be used as a numerator (unit mismatch with a
decode-rate denominator).

Honesty contract
----------------
* When a subtask's role shape is unknown or has no measured data, its ETA
  contribution is marked ``unknown`` — never a fabricated point estimate.
* When a subtask has data from only one bench run (n=1), the range is explicitly
  wide (×0.5 to ×2 of the mean).  As more data accumulates the range narrows.
* Frontier-model runs use the provider's observed decode rate, injected the same
  way as the local rate.

Data source citation
--------------------
All token-cost constants below are verbatim from
``benchmarks/tokenmaxx/results/matrix.json`` (T-W29 bench, 2026-07-05,
CUDA llama-server b9860/fdb1db8, RTX 3080 Ti sm_86).
Measured by ``measure_structured()`` in ``benchmarks/tokenmaxx/bench.py``:
  actual ``completion_tokens`` / ``elapsed_s`` from the server's
  ``timings.predicted_per_second`` field — pure decode, excludes prompt-eval.

TPS constants (CUDA, T-W29/W31):
  qwen3-4b    single-stream 175 tok/s; N=8 aggregate 476 tok/s
  gemma3-4b-qat single-stream 162 tok/s; N=8 aggregate 332 tok/s
  Integrator smoke 2026-07-05: 482 tok/s aggregate (recorded in T-W31 notes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Measured token-cost table
# ---------------------------------------------------------------------------

# Source: benchmarks/tokenmaxx/results/matrix.json  micro_evals section.
# Field: json_retry.tokens  (output/generation tokens from the server's
#        completion_tokens — NOT prompt tokens, NOT tools_slot.tokens).
# Each entry: (mean_tokens, n_samples)
#   n_samples=1 because the current gym has one bench run (T-W29, 6 trials,
#   one measurement point per role shape after averaging over trials is not
#   recorded separately — we have one json_retry result per role×model pair).
#   Range widens proportionally when n_samples is small (see _eta_for_node).

class RoleShape(str, Enum):
    """Role shapes correspond to token-spend patterns in the harness."""
    FEATURE_MODELER_EXPANSION = "feature_modeler_expansion"
    REVIEWER_CRITIQUE         = "reviewer_critique"
    WORKER_TOOLCALL           = "worker_toolcall"
    # Stages from phases.py that map to shapes above (coarse mapping for phases
    # where we have no shape-specific bench yet — marked n=0 → range/unknown).
    INTAKE                    = "intake"
    SPECIFY                   = "specify"
    ORGANIZE                  = "organize"
    PLAN                      = "plan"
    BUILD                     = "build"
    VERIFY                    = "verify"
    DELIVER                   = "deliver"
    CORRECT                   = "correct"


# Model IDs as used in routing.yaml / tiers.yaml.
MODEL_QWEN3_4B     = "qwen3-4b"
MODEL_GEMMA3_4B    = "gemma3-4b-qat"
MODEL_GEMMA4_E4B   = "gemma4-e4b"    # retained for completeness; dropped from default set per W29 rec.

# ---------- raw measured output-token counts per (model, role_shape) ----------
# Source: benchmarks/tokenmaxx/results/matrix.json, micro_evals[*][json_retry][tokens]
# and micro_evals[*][gbnf] (gbnf: derived as round(elapsed_s * tps)).
# n=1 for all entries (one bench session, 6 trials aggregated into one point).
_MEASURED_OUTPUT_TOKENS: dict[tuple[str, str], tuple[int, int]] = {
    # (model_id, role_shape): (mean_output_tokens, n_bench_samples)
    (MODEL_QWEN3_4B,  RoleShape.FEATURE_MODELER_EXPANSION): (480, 1),
    (MODEL_QWEN3_4B,  RoleShape.REVIEWER_CRITIQUE):         (953, 1),
    (MODEL_QWEN3_4B,  RoleShape.WORKER_TOOLCALL):           (249, 1),
    # gemma3-4b-qat: production GBNF worker; gbnf path derived as round(1.514*144.65)=219,
    # json_retry tokens for the retry path.
    (MODEL_GEMMA3_4B, RoleShape.FEATURE_MODELER_EXPANSION): (658, 1),
    (MODEL_GEMMA3_4B, RoleShape.REVIEWER_CRITIQUE):         (282, 1),
    (MODEL_GEMMA3_4B, RoleShape.WORKER_TOOLCALL):           (272, 1),
    # gemma4-e4b: retained as reference; not in default set.
    (MODEL_GEMMA4_E4B, RoleShape.FEATURE_MODELER_EXPANSION): (2048, 1),
    (MODEL_GEMMA4_E4B, RoleShape.REVIEWER_CRITIQUE):         (2048, 1),
    (MODEL_GEMMA4_E4B, RoleShape.WORKER_TOOLCALL):           (136,  1),
}

# Phase-to-role-shape mapping.  Many pipeline phases (T-DP phases) map to
# the same harness "role shapes" that were benched.  This is our best current
# mapping; it degrades to range/unknown for unmapped phases.
_PHASE_TO_SHAPE: dict[str, RoleShape] = {
    # intake analyst: short tool-call-shaped turn
    RoleShape.INTAKE:   RoleShape.WORKER_TOOLCALL,
    # spec_writer: expansion / deep-think shape
    RoleShape.SPECIFY:  RoleShape.FEATURE_MODELER_EXPANSION,
    # organize: worker tool-call shape (department instantiation)
    RoleShape.ORGANIZE: RoleShape.WORKER_TOOLCALL,
    # plan: expansion shape (DAG planning)
    RoleShape.PLAN:     RoleShape.FEATURE_MODELER_EXPANSION,
    # build: worker tool-call shape (execute CAD ops)
    RoleShape.BUILD:    RoleShape.WORKER_TOOLCALL,
    # verify: reviewer critique shape
    RoleShape.VERIFY:   RoleShape.REVIEWER_CRITIQUE,
    # deliver: worker tool-call (report emit)
    RoleShape.DELIVER:  RoleShape.WORKER_TOOLCALL,
    # correct: expansion shape (diagnosis + replan)
    RoleShape.CORRECT:  RoleShape.FEATURE_MODELER_EXPANSION,
}

# ---------------------------------------------------------------------------
# Measured TPS constants (decode / generation rate only)
# ---------------------------------------------------------------------------

# Source: benchmarks/tokenmaxx/results/matrix.json
# Field: aggregate_tps at n_slots=8 (parallel fan-out shape of the harness).
# and single_stream.server_decode_tps for single-slot.
# Integrator smoke T-W31: 482 tok/s aggregate (hand note; bench.py gives 476).

# Aggregate (N=8 parallel slots) — use for whole-mission ETA.
LOCAL_TPS_QWEN3_4B_AGG8    = 475.86   # bench.py: aggregate_tps @ N=8
LOCAL_TPS_GEMMA3_4B_AGG8   = 331.52   # bench.py: aggregate_tps @ N=8
LOCAL_TPS_DEFAULT_AGG8     = 482.0    # integrator smoke 2026-07-05 T-W31

# Per-slot / single-stream — use for per-node ETA (one node, one slot).
LOCAL_TPS_QWEN3_4B_SINGLE  = 175.12   # bench.py: single_stream.server_decode_tps
LOCAL_TPS_GEMMA3_4B_SINGLE = 161.54   # bench.py: single_stream.server_decode_tps


# ---------------------------------------------------------------------------
# Range / uncertainty helpers
# ---------------------------------------------------------------------------

# Uncertainty multipliers by sample count.
# n=0  → unknown (no data)
# n=1  → wide: [×0.5, ×2.0] — bench has one data point; real variance unknown
# n=2  → medium: [×0.7, ×1.5]
# n≥5  → narrow: [×0.85, ×1.2]
_RANGE_LOW  = {0: None, 1: 0.5, 2: 0.70, 3: 0.80, 4: 0.85}
_RANGE_HIGH = {0: None, 1: 2.0, 2: 1.50, 3: 1.30, 4: 1.20}
_NARROW_LOW  = 0.85
_NARROW_HIGH = 1.20


def _range_factors(n: int) -> tuple[Optional[float], Optional[float]]:
    """Return (low_mult, high_mult) for n bench samples.  Returns (None, None) for n=0."""
    if n == 0:
        return None, None
    lo = _RANGE_LOW.get(n, _NARROW_LOW)
    hi = _RANGE_HIGH.get(n, _NARROW_HIGH)
    return lo, hi


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class NodeETA:
    """ETA for a single mission node/subtask.

    ``eta_seconds``    — point estimate; None when there is no data.
    ``eta_low_s``      — low end of uncertainty range; None when unknown.
    ``eta_high_s``     — high end of uncertainty range; None when unknown.
    ``unknown``        — True when this node has no cost data (show "?").
    ``tokens_mean``    — the mean output-token count used for the estimate.
    ``tps``            — the tok/s used for the estimate.
    ``n_samples``      — number of bench samples behind the token estimate.
    ``role_shape``     — the role shape resolved for this node (or None).
    """
    eta_seconds:  Optional[float]
    eta_low_s:    Optional[float]
    eta_high_s:   Optional[float]
    unknown:      bool
    tokens_mean:  Optional[int]   = None
    tps:          Optional[float] = None
    n_samples:    int             = 0
    role_shape:   Optional[str]   = None


@dataclass
class MissionETA:
    """ETA for the whole mission.

    ``eta_seconds``  — point estimate; None when any remaining node is unknown.
    ``eta_low_s``    — optimistic bound (all nodes at their low end).
    ``eta_high_s``   — pessimistic bound (all nodes at their high end).
    ``unknown``      — True when at least one remaining node has no cost data.
    ``partial``      — True when some (but not all) remaining nodes have data.
    ``node_etas``    — per-node ETAs, keyed by node id.
    ``remaining_nodes_count`` — how many nodes are not yet done.
    """
    eta_seconds:         Optional[float]
    eta_low_s:           Optional[float]
    eta_high_s:          Optional[float]
    unknown:             bool
    partial:             bool
    node_etas:           dict[str, NodeETA] = field(default_factory=dict)
    remaining_nodes_count: int = 0


@dataclass
class SubtaskSpec:
    """Specification of one not-yet-done subtask for ETA computation.

    ``node_id``     — unique identifier (phase name, DAG node id, etc.)
    ``role_shape``  — one of the ``RoleShape`` string values, or None.
    ``model_id``    — model routing id (e.g. "qwen3-4b"), or None for default.
    ``n_turns``     — expected number of model turns for this subtask (default 1).
                      Multiply the per-turn token cost by this.
    """
    node_id:    str
    role_shape: Optional[str] = None
    model_id:   Optional[str] = None
    n_turns:    int           = 1


# ---------------------------------------------------------------------------
# Core estimator
# ---------------------------------------------------------------------------

class ETAEstimator:
    """Compute mission ETA from measured per-role token costs and a live tok/s rate.

    Parameters
    ----------
    tps_aggregate:
        Effective tokens per second across all running parallel slots.
        For whole-mission ETA: use the N-slot aggregate (e.g. 476 @ N=8 qwen3).
        For single-node ETA: use the per-slot rate (e.g. 175 single-stream).
        Pass None to use the default from the integrator smoke (482 tok/s).
    default_model_id:
        Model id to use when a subtask does not specify one.  Defaults to
        qwen3-4b (the T-W31 primary routing recommendation).
    """

    def __init__(
        self,
        tps_aggregate: Optional[float] = None,
        default_model_id: str = MODEL_QWEN3_4B,
    ) -> None:
        self.tps_aggregate = tps_aggregate if tps_aggregate is not None else LOCAL_TPS_DEFAULT_AGG8
        self.default_model_id = default_model_id

    # ------------------------------------------------------------------
    # Single-node ETA
    # ------------------------------------------------------------------

    def node_eta(
        self,
        spec: SubtaskSpec,
        *,
        tps: Optional[float] = None,
    ) -> NodeETA:
        """Compute ETA for a single not-yet-done subtask.

        ``tps`` overrides the estimator's default (useful for per-node
        single-slot rate vs whole-mission aggregate rate).
        """
        effective_tps = tps if tps is not None else self.tps_aggregate
        shape = self._resolve_shape(spec)
        model = spec.model_id or self.default_model_id

        tokens_mean, n_samples = self._lookup_tokens(model, shape)

        if tokens_mean is None or n_samples == 0:
            return NodeETA(
                eta_seconds=None, eta_low_s=None, eta_high_s=None,
                unknown=True, tokens_mean=None, tps=effective_tps,
                n_samples=0, role_shape=shape,
            )

        total_tokens = tokens_mean * spec.n_turns
        point = total_tokens / effective_tps

        lo_mult, hi_mult = _range_factors(n_samples)
        eta_low  = point * lo_mult  if lo_mult  is not None else None
        eta_high = point * hi_mult if hi_mult is not None else None

        return NodeETA(
            eta_seconds=round(point, 1),
            eta_low_s=round(eta_low, 1) if eta_low is not None else None,
            eta_high_s=round(eta_high, 1) if eta_high is not None else None,
            unknown=False,
            tokens_mean=total_tokens,
            tps=effective_tps,
            n_samples=n_samples,
            role_shape=shape,
        )

    # ------------------------------------------------------------------
    # Whole-mission ETA
    # ------------------------------------------------------------------

    def mission_eta(
        self,
        remaining_subtasks: list[SubtaskSpec],
        *,
        tps_aggregate: Optional[float] = None,
    ) -> MissionETA:
        """Compute whole-mission ETA from a list of not-yet-done subtasks.

        Uses the aggregate TPS (parallel fan-out: all running slots contribute).
        """
        agg_tps = tps_aggregate if tps_aggregate is not None else self.tps_aggregate

        node_etas: dict[str, NodeETA] = {}
        point_total = 0.0
        low_total   = 0.0
        high_total  = 0.0
        has_unknown = False
        has_known   = False

        for spec in remaining_subtasks:
            ne = self.node_eta(spec, tps=agg_tps)
            node_etas[spec.node_id] = ne
            if ne.unknown:
                has_unknown = True
            else:
                has_known = True
                point_total += ne.eta_seconds or 0.0
                low_total   += ne.eta_low_s  or 0.0
                high_total  += ne.eta_high_s or 0.0

        # Whole-mission point only when all remaining nodes are known.
        eta_seconds = round(point_total, 1) if not has_unknown else None
        eta_low     = round(low_total,   1) if has_known else None
        eta_high    = round(high_total,  1) if has_known else None

        return MissionETA(
            eta_seconds=eta_seconds,
            eta_low_s=eta_low,
            eta_high_s=eta_high,
            unknown=has_unknown,
            partial=has_unknown and has_known,
            node_etas=node_etas,
            remaining_nodes_count=len(remaining_subtasks),
        )

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_eta(eta: MissionETA) -> str:
        """Format a MissionETA for display (e.g. status-bar chip).

        Returns a concise string like:
          "ETA ~2m 30s"        — point estimate (all nodes known)
          "ETA 1m – 5m"        — range (all known, wide uncertainty)
          "ETA ≥45s (partial)" — some nodes unknown
          "ETA unknown"        — no data at all
        """
        if not eta.node_etas:
            return "ETA —"

        if eta.eta_seconds is not None:
            # Full point estimate available; show range if it's meaningful.
            lo = eta.eta_low_s or 0.0
            hi = eta.eta_high_s or 0.0
            if hi > 0 and (hi - lo) / max(hi, 1.0) > 0.15:
                return f"ETA {_fmt_s(lo)} – {_fmt_s(hi)}"
            return f"ETA ~{_fmt_s(eta.eta_seconds)}"

        if eta.partial and eta.eta_low_s is not None:
            return f"ETA ≥{_fmt_s(eta.eta_low_s)} (partial)"

        return "ETA unknown"

    @staticmethod
    def format_node_eta(ne: NodeETA) -> str:
        """Format a per-node ETA for the DAG widget tooltip."""
        if ne.unknown:
            return "? (no data)"
        if ne.eta_seconds is not None:
            lo = ne.eta_low_s
            hi = ne.eta_high_s
            if lo is not None and hi is not None and (hi - lo) / max(hi, 1.0) > 0.15:
                return f"~{_fmt_s(lo)} – {_fmt_s(hi)}"
            return f"~{_fmt_s(ne.eta_seconds)}"
        return "?"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_shape(self, spec: SubtaskSpec) -> Optional[str]:
        """Resolve a bench role_shape from the spec.

        Priority:
          1. If spec.role_shape is a known bench shape (has data) → use it.
          2. If spec.role_shape is a phase-level shape → map via _PHASE_TO_SHAPE.
          3. If spec.role_shape is a plain string matching _PHASE_TO_SHAPE key → map.
          4. Try node_id prefix against phase names.
          5. None → unknown.
        """
        shape = spec.role_shape

        if shape is not None:
            # Check if this is already a bench shape (has direct data).
            shape_str = shape.value if isinstance(shape, RoleShape) else str(shape)
            # Try direct phase mapping first (e.g. RoleShape.BUILD → WORKER_TOOLCALL).
            for phase_key, mapped in _PHASE_TO_SHAPE.items():
                pk = phase_key.value if isinstance(phase_key, RoleShape) else str(phase_key)
                if shape_str == pk:
                    return mapped
            # Not a phase key — return as-is (it's a bench shape or unknown string).
            return shape_str

        # No role_shape given: try node_id prefix against phase names.
        node_lower = spec.node_id.lower()
        for phase_key, mapped in _PHASE_TO_SHAPE.items():
            pk = phase_key.value if isinstance(phase_key, RoleShape) else str(phase_key)
            if node_lower.startswith(pk):
                return mapped
        return None

    def _lookup_tokens(
        self,
        model_id: str,
        shape: Optional[str],
    ) -> tuple[Optional[int], int]:
        """Return (mean_output_tokens, n_samples) for a model × shape pair.

        Falls back through:
          1. Exact (model_id, shape) key — shape compared by value string.
          2. Default model (qwen3-4b) with same shape.
          3. (None, 0) — unknown, honest range/unknown reported.
        """
        if shape is None:
            return None, 0

        # Normalize shape to the RoleShape enum member if possible,
        # so lookup works regardless of whether the caller passed a string or enum.
        shape_str = shape.value if isinstance(shape, RoleShape) else str(shape)
        try:
            shape_enum = RoleShape(shape_str)
        except ValueError:
            shape_enum = None  # unknown shape string

        if shape_enum is None:
            return None, 0

        key = (model_id, shape_enum)
        if key in _MEASURED_OUTPUT_TOKENS:
            return _MEASURED_OUTPUT_TOKENS[key]

        # Fallback: try default model
        fallback = (MODEL_QWEN3_4B, shape_enum)
        if fallback in _MEASURED_OUTPUT_TOKENS:
            tokens, n = _MEASURED_OUTPUT_TOKENS[fallback]
            # Reduce n by 1 to widen the range slightly for the fallback case.
            return tokens, max(1, n - 1) if n > 1 else n
        return None, 0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_s(seconds: float) -> str:
    """Format seconds as a human-readable string: '45s', '2m 30s', '1h 5m'."""
    seconds = max(0.0, seconds)
    s = int(math.ceil(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, mn = divmod(m, 60)
    return f"{h}h {mn}m" if mn else f"{h}h"
