"""tool_loop_guard.py — cross-call repeated-tool-call detection.

The detector is small and dependency-free. It tracks repeated canonical tool
calls across one deterministic Python agent turn and stops only a genuinely
stuck same-tool/same-arguments streak.

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

# Tools whose result can legitimately change without another agent tool call:
# background jobs, event streams, workspace state, external adapter readiness,
# or concurrent GUI/worker activity.  There is no "changes over time" flag in
# tool_registry metadata, so this list comes from a 2026-08-26 sweep of the warm
# catalog for names containing status/queue/progress/job, followed by inspection
# of each definition.  Re-run that sweep when poll tools are added.  req_status
# is deliberately excluded: despite its name, it MUTATES a requirement and
# takes a required new_status argument.
DEFAULT_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        "box_status",
        "buildgraph_status",
        "dag_status",
        "ecad_status",
        "em_status",
        "fw_status",
        "get_job",
        "intake_status",
        "ledger_events",
        "ledger_get",
        "mission_status",
        "plm_status",
        "reveng_gate_status",
        "reveng_status",
        "scrape_queue_status",
        "sim_jobs",
        "sim_status",
        "vendor_status",
        "workspace_doc_events",
        "workspace_status",
    }
)

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
VALIDATION_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "unknown_tool",
        "known_but_unbound",
        "unknown_argument",
        "missing_argument",
        "bad_arguments",
    }
)


@dataclass(frozen=True)
class RepeatedToolCallDetection:
    """Details needed to steer the model away from a repeated tool call."""

    tool: str
    count: int
    arguments_summary: str
    after_validation_failure: bool = False


def _canonicalize(value: Any) -> Any:
    """Order-independent view of tool args, dropping only the private scratch key."""
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value) if k != "__intent"}
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

    def __init__(
        self,
        threshold: int = DEFAULT_THRESHOLD,
        exempt_tools: frozenset[str] = DEFAULT_EXEMPT_TOOLS,
        validation_retry_allowance: int = DEFAULT_VALIDATION_RETRY_ALLOWANCE,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._validation_threshold = self._threshold + max(
            0, int(validation_retry_allowance)
        )
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
        threshold = (
            self._validation_threshold
            if self._last_failed_before_body
            else self._threshold
        )
        if self._count < threshold:
            return None
        return RepeatedToolCallDetection(
            tool=tool,
            count=self._count,
            arguments_summary=_summarize(canonical, ARGUMENT_SUMMARY_LIMIT),
            after_validation_failure=self._last_failed_before_body,
        )

    def note_outcome(
        self, tool: str, args: dict | None, *, failed_before_body: bool
    ) -> None:
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
        # Empty arguments may be valid for some tools, so describe the observed
        # shape without claiming that required fields are definitely missing.
        empty_args = detection.arguments_summary.strip() in ("{}", "")
        hint = (
            " The arguments were EMPTY. If the tool requires fields, re-read its "
            "schema and supply them; otherwise use the prior result or continue "
            "with what you already know."
            if empty_args
            else ""
        )
        remedies = (
            "Use the prior result, change a real argument, call a different tool "
            "from your available list, or finish with status='blocked' and explain "
            "the obstacle."
        )
        if detection.after_validation_failure:
            # Repeats following an argument-validation / tool-binding failure
            # already got threshold + validation_retry_allowance before this
            # message fires. State only the immediately preceding outcome, which
            # is what the guard actually records.
            return (
                f"LOOP GUARD: '{detection.tool}' was attempted "
                f"{detection.count}× in a row with identical arguments "
                f"({detection.arguments_summary}). The immediately preceding "
                "attempt failed argument validation before the tool body ran, so "
                "extra retry slack was allowed; that slack is now exhausted. "
                "This exact call was not run. Re-read the tool's real parameter "
                f"list instead of repeating the same arguments. {remedies}{hint}"
            )
        return (
            f"LOOP GUARD: '{detection.tool}' was attempted {detection.count}× in "
            f"a row with identical arguments ({detection.arguments_summary}). "
            f"This exact call was not run. {remedies}{hint}"
        )
