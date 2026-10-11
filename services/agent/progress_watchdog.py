"""Per-turn research stall detection over Chudcode RPC notifications."""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from services.agent.chudcode_events import cad_result_payload
from services.agent.online_turn import format_duration
from services.mcp.tool_registry import RISK_READ, catalog, classify_risk

_DEFAULT_RESEARCH_CALLS = 12
_DEFAULT_STALL_SECONDS = 6 * 60
_MAX_NUDGES = 2
_TOOL_DONE = frozenset({
    "tool_end", "tool_complete", "tool_completed", "tool_result",
    "tool_call_end", "tool_call_completed", "tool_execution_end", "end",
    "builtin_tool_end", "builtin_tool_result", "host_tool_end", "host_tool_result",
})


def _limit(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default


def _tool_frame(frame: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    kind = frame.get("type")
    if kind == "custom":
        body = frame.get("data")
        if isinstance(body, Mapping):
            return str(frame.get("customType") or ""), body
    return str(kind or ""), frame


def _url_arg(args: Any) -> bool:
    if isinstance(args, Mapping):
        return any(
            isinstance(args.get(key), str)
            and args[key].strip().lower().startswith(("http://", "https://"))
            for key in ("path", "url", "uri")
        )
    return False

def _resolved_name(name: str, args: Any) -> tuple[str, bool]:
    """Only writing xd://<tool> dispatches CAD; reading it shows the schema."""
    if name == "write" and isinstance(args, Mapping):
        path = args.get("path")
        if isinstance(path, str) and path.startswith("xd://"):
            target = urlsplit(path)
            if target.netloc and not target.path.strip("/"):
                return target.netloc, True
    return name, False


def _succeeded(data: Mapping[str, Any]) -> bool:
    if data.get("isError") is True or data.get("is_error") is True:
        return False
    if data.get("ok", data.get("success", True)) is False:
        return False
    response = data.get("result", data.get("output", data))
    # A CAD ok:false comes back as a normal result; under `write xd://` it sits in
    # details.xdev.inner, so a failed add_features used to count as progress.
    payload = cad_result_payload(response)
    if payload is None and isinstance(response, Mapping) and "details" not in response:
        payload = response
    return not (payload is not None and payload.get("ok") is False)


def _research_tool(name: str, args: Any) -> bool:
    if name in {"web_search", "part_search", "part_source", "match_model"}:
        return True
    if name.startswith(("research_", "part_search_")) or name == "browser":
        return True
    return name in {"read", "fetch", "web_fetch"} and _url_arg(args)


class ProgressWatchdog:
    """Counts completed research calls, never mistaking a failed write for progress.

    ``observe`` and ``due`` are called under the host's per-turn lock. The clock is
    injectable so timing and threshold behavior can be tested without sleeping.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, *, enabled: bool = True):
        self.clock = clock
        self.enabled = enabled
        self.research_limit = _limit("CHUDWARE_PROGRESS_RESEARCH_CALLS", _DEFAULT_RESEARCH_CALLS)
        self.stall_seconds = _limit("CHUDWARE_PROGRESS_STALL_S", _DEFAULT_STALL_SECONDS)
        self.research_calls = 0
        self.nudges = 0
        self.last_progress = clock()
        self.first_research: float | None = None
        self._starts: dict[str, tuple[str, Any]] = {}
        self._completed: set[str] = set()
        self._recent_ids: deque[str] = deque()
        self._nudged_since_mutation = False

    def deadline(self) -> float | None:
        if (not self.enabled or not self.stall_seconds or self.first_research is None
                or self._nudged_since_mutation or self.nudges >= _MAX_NUDGES):
            return None
        return self.last_progress + self.stall_seconds

    def due(self) -> str | None:
        if not self.enabled or self._nudged_since_mutation or self.nudges >= _MAX_NUDGES:
            return None
        now = self.clock()
        deadline = self.deadline()
        if not ((self.research_limit and self.research_calls >= self.research_limit)
                or (deadline is not None and now >= deadline)):
            return None
        self._nudged_since_mutation = True
        self.nudges += 1
        minutes = (now - self.last_progress) / 60
        return (
            f"You have made {self.research_calls} research calls over {minutes:.1f} min "
            "without building anything. Commit to the best-supported dimensions now "
            "(state the source and a clearance/tolerance), source reference geometry "
            "with part_search / fetch_cad_model / make_standard_part if available, and "
            "start modeling. Build and save one part at a time — don't design the whole "
            "mechanism in your head first. Resume research only for a specific blocking unknown."
            + (" This is the second nudge after a CAD mutation." if self.nudges == 2 else "")
        )

    def observe(self, frame: Mapping[str, Any], host_tools: set[str]) -> str | None:
        if not self.enabled:
            return None
        kind, data = _tool_frame(frame)
        if kind == "subagent_event":
            return self._observe_subagent(data, host_tools)
        if kind in {"assistant_delta", "text_delta", "output_text_delta", "output_delta"}:
            if data.get("channel", "text") == "text" and (
                data.get("text") or data.get("delta")
            ):
                self.last_progress = self.clock()
            return self.due()
        if kind == "message_update":
            child = data.get("assistantMessageEvent")
            if isinstance(child, Mapping):
                return self.observe(child, host_tools)
        if kind in {"tool_start", "tool_started", "tool_call_start", "tool_call_started",
                    "tool_execution_start", "builtin_tool_start", "host_tool_start"}:
            call_id = data.get("toolCallId") or data.get("tool_call_id") or data.get("id")
            if call_id:
                self._starts[str(call_id)] = (
                    str(data.get("toolName") or data.get("tool_name") or data.get("name") or ""),
                    data.get("args", data.get("arguments")),
                )
                name, args = self._starts[str(call_id)]
                research_name, _ = _resolved_name(name, args)
                if self.first_research is None and _research_tool(research_name, args):
                    self.first_research = self.clock()
            return self.due()
        if kind == "message":
            message = data.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "toolResult":
                return self.due()
            data = message
        elif kind not in _TOOL_DONE and kind != "host_tool_call":
            return self.due()
        elif kind == "host_tool_call" and not any(
            key in data for key in ("result", "output", "isError")
        ):
            return self.due()

        call_id = data.get("toolCallId") or data.get("tool_call_id") or data.get("callId")
        if call_id and str(call_id) in self._completed:
            return self.due()
        started_name, started_args = self._starts.pop(str(call_id), ("", None))
        name = str(data.get("toolName") or data.get("tool_name") or
                   data.get("tool") or data.get("name") or started_name)
        args = data.get("args", data.get("arguments", started_args))
        name, host_uri = _resolved_name(name, args)
        if not name:
            return self.due()
        if call_id:
            self._completed.add(str(call_id))
            self._recent_ids.append(str(call_id))
            if len(self._recent_ids) > 512:
                self._completed.remove(self._recent_ids.popleft())
        if _research_tool(name, args):
            if self.first_research is None:
                self.first_research = self.clock()
            self.research_calls += 1
        elif (host_uri or name in host_tools or name in catalog()
              or kind.startswith("host_tool_")) and (
                classify_risk(name) != RISK_READ and _succeeded(data)):
            self._mark_progress()
        return self.due()

    def user_input(self) -> None:
        """Time spent waiting on the user (an ask_user form) is not a research stall."""
        self._mark_progress()

    def _mark_progress(self) -> None:
        self.research_calls = 0
        self.first_research = None
        self.last_progress = self.clock()
        self._nudged_since_mutation = False

    def _observe_subagent(self, frame: Mapping[str, Any], host_tools: set[str]) -> str | None:
        """A task sub-agent's successful CAD mutation is the turn's progress.

        Chudcode forwards a sub-agent's own events inside ``subagent_event``
        frames. Only its mutations count: its research is its own brief, and
        eight researching sub-agents must not read as the parent stalling.
        """
        payload = frame.get("payload")
        inner = payload.get("event") if isinstance(payload, Mapping) else None
        if not isinstance(inner, Mapping):
            return self.due()
        kind = str(inner.get("type") or "")
        call_id = str(inner.get("toolCallId") or "")
        if not call_id or call_id in self._completed:
            return self.due()
        if kind == "tool_execution_start":
            self._starts[call_id] = (str(inner.get("toolName") or ""), inner.get("args"))
            return self.due()
        if kind != "tool_execution_end":
            return self.due()
        started_name, started_args = self._starts.pop(call_id, ("", None))
        self._completed.add(call_id)
        self._recent_ids.append(call_id)
        if len(self._recent_ids) > 512:
            self._completed.remove(self._recent_ids.popleft())
        args = inner.get("args", started_args)
        name, host_uri = _resolved_name(str(inner.get("toolName") or started_name), args)
        if (host_uri or name in host_tools or name in catalog()) and (
                classify_risk(name) != RISK_READ and _succeeded(inner)):
            self._mark_progress()
        return self.due()


# The one pre-expiry reminder of an explicitly budgeted turn leaves the agent the larger
# of 20% of the budget and three minutes to save and verify. A budget too short for that
# gets the reminder at 80%; none fires within 30 s of the start.
_BUDGET_REMINDER_LEAD_SECONDS = 180.0
_BUDGET_REMINDER_FRACTION = 0.8
_BUDGET_REMINDER_MIN_OFFSET_SECONDS = 30.0


def budget_reminder_offset(budget_s: float | None) -> float | None:
    """Seconds after turn start for the pre-expiry reminder, or ``None`` for no reminder."""
    if budget_s is None or not budget_s > 0:
        return None
    late = _BUDGET_REMINDER_FRACTION * budget_s
    offset = min(late, budget_s - _BUDGET_REMINDER_LEAD_SECONDS)
    if offset <= _BUDGET_REMINDER_MIN_OFFSET_SECONDS:
        offset = late
    return offset if offset > _BUDGET_REMINDER_MIN_OFFSET_SECONDS else None


class TurnBudgetReminder:
    """The single pre-expiry steer of a turn whose caller sent a positive ``timeout``.

    It is about wall-clock, not research stalls, so it is independent of
    ``ProgressWatchdog``: it never consumes the two research nudges per turn and it also
    fires in Ask/Plan turns, phrased without "save" because they cannot mutate. Like the
    watchdog, ``due`` is called under the host's per-turn lock with an injectable clock.
    """

    def __init__(self, budget_s: float, clock: Callable[[], float] = time.monotonic,
                 *, readonly: bool = False):
        self.clock = clock
        self.budget_s = budget_s
        self.readonly = readonly
        self.started = clock()
        offset = budget_reminder_offset(budget_s)
        self._deadline = None if offset is None else self.started + offset
        self.fired = False

    def deadline(self) -> float | None:
        return None if self.fired else self._deadline

    def remaining(self) -> float:
        return max(0.0, self.started + self.budget_s - self.clock())

    def due(self) -> str | None:
        deadline = self.deadline()
        if deadline is None or self.clock() < deadline:
            return None
        self.fired = True
        left = format_duration(self.remaining())
        if self.readonly:
            return (f"⏱ {left} left of this turn's budget: give your final answer now, "
                    "then use the remaining time to check it.")
        return (f"⏱ {left} left of this turn's budget: save every requested deliverable "
                "now (exact requested paths), then use the remaining time to verify.")
