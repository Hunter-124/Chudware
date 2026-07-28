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
times in a row. A model re-reading the same file with different offsets never trips it
(each streak sees different args); a model stuck calling ``describe_part`` with
identical args because it isn't parsing the result does.

``verify_battery`` is NOT in ``DEFAULT_EXEMPT_TOOLS`` and must not be added: unlike the
job/status-poll tools below (whose result changes purely by time passing, server-side,
with no tool call in between), verify_battery's result only changes when something else
mutated the part. The normal "re-run verify_battery after each fix" pattern never trips
this guard anyway, exemption or not — the intervening add_features call already breaks
the identical-args streak. A genuine back-to-back identical verify_battery repeat, with
nothing mutated in between, IS exactly the wasted call this guard exists to stop.

The guard also grants limited grace to repeats that follow a call which failed BEFORE
reaching the tool body (argument validation / tool-binding errors, see ``note_outcome``
below): the model is reacting to a teaching error, not looping, so the first such
repeats are let through rather than silently swallowing the real error message. A
higher bound still applies so a model that keeps failing validation is eventually cut
off (see ``ToolCallLoopGuard.check``).
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

# Extra consecutive identical calls tolerated when the immediately-preceding
# identical call failed before reaching the tool body (see note_outcome). Keeps
# a hard bound so a model that never fixes its arguments is still cut off.
DEFAULT_VALIDATION_RETRY_ALLOWANCE = 2

# ``status`` values agent_turn.py's teaching-error payloads use for a call
# that failed BEFORE the tool did its real work: unresolved tool name
# (``_unresolved_tool_error``), bad/unknown/missing keyword arguments
# (``_reconcile_kwargs`` / ``arg_reconciliation.unknown_arg_payload`` /
# ``_missing_kwargs_error``), and the one business-logic case that behaves
# identically — ``ask_user`` given an unparseable question batch
# (``interaction.ask_user_bad_arguments_error``) short-circuits before ever
# asking anything, same as a binding failure from the guard's point of view.
# Callers that dispatch through their own resolver (not ``_dispatch_tool``)
# can reuse this set to feed ``note_outcome`` the same way.
VALIDATION_FAILURE_STATUSES: frozenset[str] = frozenset({
    "unknown_tool", "known_but_unbound", "unknown_argument",
    "missing_argument", "bad_arguments",
})


@dataclass(frozen=True)
class RepeatedToolCallDetection:
    """Details needed to steer the model away from a repeated tool call."""

    tool: str
    count: int
    arguments_summary: str
    after_validation_failure: bool = False


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

    Call :meth:`note_outcome` AFTER a dispatch that :meth:`check` allowed through,
    reporting whether it failed BEFORE reaching the tool's real code (argument
    validation / tool-binding teaching errors — never a genuine tool-side
    failure). The next ``check`` for that same (tool, args) then tolerates a few
    more identical repeats — the model is reacting to the teaching error, not
    stuck in a no-op loop — up to ``threshold + validation_retry_allowance``
    before it too is refused, so a model that never fixes its arguments is
    still eventually cut off.
    """

    def __init__(self, threshold: int = DEFAULT_THRESHOLD,
                 exempt_tools: frozenset[str] = DEFAULT_EXEMPT_TOOLS,
                 validation_retry_allowance: int = DEFAULT_VALIDATION_RETRY_ALLOWANCE) -> None:
        self._threshold = max(1, int(threshold))
        self._validation_threshold = self._threshold + max(0, int(validation_retry_allowance))
        self._exempt = frozenset(exempt_tools)
        self._last_key: str | None = None
        self._count = 0
        self._last_failed_before_body = False

    def check(self, tool: str, args: dict | None) -> RepeatedToolCallDetection | None:
        if tool in self._exempt:
            self._last_key = None
            self._count = 0
            self._last_failed_before_body = False
            return None
        canonical = json.dumps(_canonicalize(args or {}), sort_keys=True, default=str)
        key = f"{tool}:{canonical}"
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1
            self._last_failed_before_body = False
        threshold = (self._validation_threshold if self._last_failed_before_body
                     else self._threshold)
        if self._count < threshold:
            return None
        return RepeatedToolCallDetection(
            tool=tool, count=self._count,
            arguments_summary=_summarize(canonical, ARGUMENT_SUMMARY_LIMIT),
            after_validation_failure=self._last_failed_before_body,
        )

    def note_outcome(self, tool: str, args: dict | None, *,
                      failed_before_body: bool) -> None:
        """Record the outcome of the call ``check`` most recently allowed
        through for this (tool, args): did it fail before the tool's real code
        ran (argument validation / binding), or did it actually execute
        (success or a genuine tool-side failure)? Only affects the very next
        ``check`` for this same (tool, args) — a call for a different
        (tool, args) (the streak already moved on) is silently ignored rather
        than corrupting an unrelated streak."""
        if tool in self._exempt:
            return
        canonical = json.dumps(_canonicalize(args or {}), sort_keys=True, default=str)
        if f"{tool}:{canonical}" == self._last_key:
            self._last_failed_before_body = failed_before_body

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
        if detection.after_validation_failure:
            # Repeats following an argument-validation / tool-binding failure
            # already got extra slack (threshold + validation_retry_allowance)
            # to let the model react to the teaching error instead of having it
            # swallowed by this guard — this message only fires once that
            # slack itself runs out, i.e. the arguments are STILL wrong.
            return (
                f"LOOP GUARD: '{detection.tool}' was called {detection.count}× in a "
                f"row with the same arguments ({detection.arguments_summary}), and "
                "every attempt failed argument validation before the tool ever ran — "
                "you already got extra retries to react to that error, but the "
                "arguments are still wrong. This call was refused; re-read the tool's "
                f"real parameter list from its schema{escape} instead of repeating the "
                f"same broken call.{hint}"
            )
        return (
            f"LOOP GUARD: '{detection.tool}' was just called {detection.count}× in a "
            f"row with the same arguments ({detection.arguments_summary}) — this call "
            "was refused rather than repeated. The result did not change last time and "
            "will not change now; read the prior result again, try different "
            f"arguments{escape} instead of repeating.{hint}"
        )
