"""tool_loop_guard.py — cross-call repeated-tool-call detection.

Ported (same algorithm, re-authored for our Python dispatch path) from OMP's
``ToolCallLoopGuard`` (vendor/omp/packages/ai/src/utils/tool-call-loop-guard.ts) — a
vendored utility that was never actually wired into anything in the vendored copy
(omp's own agent/CLI harness that would have consumed it isn't part of what we vendor;
we only pull in its provider/auth/memory/compaction *library* layer). The detector
itself is small, dependency-free, and worth having on our own Python agent loop.

Distinct from — and complementary to — the existing ``max_calls_per_turn`` hard cap
in agent_turn.py's ``_dispatch_tool``: that cap counts calls to a tool NAME regardless
of arguments (blunt; a legitimate tool used 5x with 5 different arguments still trips
it once the count is high enough). This guard only fires on the sharper signal of a
genuinely stuck loop: the SAME tool called with the SAME (canonicalized) arguments N
times in a row. A model re-reading the same file with different offsets, or re-running
verify_battery after each fix, never trips it; a model stuck calling ``describe_part``
with identical args because it isn't parsing the result does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD = 3
ARGUMENT_SUMMARY_LIMIT = 400

# Tools that are legitimately polled with identical arguments while waiting on
# something else to change server-side (job/status polling) — never flagged.
DEFAULT_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "get_job", "dag_status", "workspace_status", "workspace_doc_events",
    "ledger_get", "ledger_events", "buildgraph_status",
})


@dataclass(frozen=True)
class RepeatedToolCallDetection:
    """Details needed to steer the model away from a repeated tool call."""

    tool: str
    count: int
    arguments_summary: str


def _canonicalize(value: Any) -> Any:
    """Order-independent, content-only view of a tool-call args dict (drops key
    order and any intent/scratch fields so semantically-identical calls hash equal)."""
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value)
                if k not in ("__intent", "intent")}
    return value


def _summarize(text: str, limit: int) -> str:
    summary = " ".join(text.split())
    if len(summary) > limit:
        summary = summary[:limit] + "…"
    return summary


class ToolCallLoopGuard:
    """Detects N consecutive identical (tool, args) dispatches within one turn.

    Call :meth:`check` BEFORE dispatching a tool call (not after, like the TS
    original — refusing the repeat outright saves the wasted call rather than
    just nudging on the next turn). Returns a detection once the threshold is
    hit; the caller should feed ``ToolCallLoopGuard.message(detection)`` back to
    the model as the (refused) tool's error result instead of actually dispatching.
    """

    def __init__(self, threshold: int = DEFAULT_THRESHOLD,
                 exempt_tools: frozenset[str] = DEFAULT_EXEMPT_TOOLS) -> None:
        self._threshold = max(1, int(threshold))
        self._exempt = frozenset(exempt_tools)
        self._last_key: str | None = None
        self._count = 0

    def check(self, tool: str, args: dict | None) -> RepeatedToolCallDetection | None:
        if tool in self._exempt:
            self._last_key = None
            self._count = 0
            return None
        canonical = json.dumps(_canonicalize(args or {}), sort_keys=True, default=str)
        key = f"{tool}:{canonical}"
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1
        if self._count < self._threshold:
            return None
        return RepeatedToolCallDetection(
            tool=tool, count=self._count,
            arguments_summary=_summarize(canonical, ARGUMENT_SUMMARY_LIMIT),
        )

    @staticmethod
    def message(detection: RepeatedToolCallDetection) -> str:
        # The escape hatches must never include the tool being refused. Live
        # 2026-07-27: haiku called ask_user with {} four times and the guard
        # answered "change approach (replan/ask_user)" — telling it to call the
        # very tool just blocked, so the only advice on offer was the loop.
        alternatives = [t for t in ("replan", "ask_user") if t != detection.tool]
        escape = (f" or change approach ({'/'.join(alternatives)})"
                  if alternatives else "")
        # An empty-argument repeat is almost always a schema failure, not a
        # genuine retry: the model does not know how to fill the call in, so
        # "try different arguments" alone leaves it stuck.
        empty_args = detection.arguments_summary.strip() in ("{}", "")
        hint = (" The arguments were EMPTY — you are missing the tool's required "
                "fields, so re-read its schema and supply them, or drop this tool "
                "and continue with what you already know."
                if empty_args else "")
        return (
            f"LOOP GUARD: '{detection.tool}' was just called {detection.count}× in a "
            f"row with the same arguments ({detection.arguments_summary}) — this call "
            "was refused rather than repeated. The result did not change last time and "
            "will not change now; read the prior result again, try different "
            f"arguments{escape} instead of repeating.{hint}"
        )
