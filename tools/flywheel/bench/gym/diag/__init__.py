"""Diagnostic suites for the multi-provider model leaderboard (diag-v1).

Axes (contracts/leaderboard.md) — NEVER folded into primary pass rate:

  tool_validity  — parseable + schema-valid fenced-JSON tool calls
  vision_fix     — detect wrong render + name the fix tool call
  feature_id     — name the feature to edit from screenshot + describe_part
  vc_hygiene     — checkpoint/undo before a destructive op

Each suite is cheap (single-turn / few-turn), deterministic graders, fixed seeds.
Vision suites score ``null`` (untested) when ``provider.supports_vision`` is False
— never a dishonest 0.

Transport honesty: see :mod:`tools.flywheel.bench.gym.diag.transport`.
"""
from __future__ import annotations

from .runner import run_diag, DIAG_AXES, write_diag_results
from .tool_validity import TOOL_VALIDITY_TASKS, grade_tool_validity, run_tool_validity
from .vision_fix import VISION_FIX_TASKS, grade_vision_fix, run_vision_fix
from .feature_id import FEATURE_ID_TASKS, grade_feature_id, run_feature_id
from .vc_hygiene import VC_HYGIENE_TASKS, grade_vc_hygiene, run_vc_hygiene
from .transport import (
    MAX_TRANSPORT_ATTEMPTS,
    TRANSPORT_BACKOFF_BASES,
    TransportExhausted,
    complete_with_retry,
    finalize_axis_result,
    is_transport_error,
    transport_error_task,
)

__all__ = [
    "DIAG_AXES",
    "run_diag",
    "write_diag_results",
    "TOOL_VALIDITY_TASKS",
    "grade_tool_validity",
    "run_tool_validity",
    "VISION_FIX_TASKS",
    "grade_vision_fix",
    "run_vision_fix",
    "FEATURE_ID_TASKS",
    "grade_feature_id",
    "run_feature_id",
    "VC_HYGIENE_TASKS",
    "grade_vc_hygiene",
    "run_vc_hygiene",
    "is_transport_error",
    "complete_with_retry",
    "TransportExhausted",
    "finalize_axis_result",
    "transport_error_task",
    "MAX_TRANSPORT_ATTEMPTS",
    "TRANSPORT_BACKOFF_BASES",
]
