"""task_list.py — the general lane's live task-list (``plan_state``) producer.

Why this module exists: the copilot's Task-list panel
(``host/src/ui/copilot/panel.cpp`` ``showTaskList``, fed by
``AppState::ChatMsg::planState``) is driven by ``kind:"plan_state"`` events, and
those were emitted ONLY from C++ (``core/agent/machine.cpp``, the ``run.cpp``
mission loop).  Since the 2026-07 routing overhaul the ``general_agent`` turn is
the universal ONLINE entry — it never enters either C++ loop — so the panel read
"No task list yet" on every normal request.  This module is the Python-side
producer: ``core/agent/run.cpp`` relays any worker payload carrying a ``"kind"``
key verbatim onto the run's event stream, so a ``plan_state`` emitted here lands
in the existing GUI panel with no C++ change.

Payload contract: ``contracts/agent_events.md`` §plan_state.  The host upserts
items BY ID across events, so ids MUST be stable across revisions — a caller that
omits ids gets positional ``task:<n>`` ids, which stay stable as long as the
model re-publishes the full snapshot in the same order (which the tool docstring
mandates).

Store shape: run-scoped, thread-safe, and bounded on every axis.  A worker
process outlives many runs, so an unbounded dict here would be a slow leak.
"""
from __future__ import annotations

import contextvars
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable

from services.agent import agent_events

# Contract vocabulary (contracts/agent_events.md §plan_state).
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "pass", "fail", "done", "abandoned"})
VALID_SHAPES: frozenset[str] = frozenset(
    {"build_plan", "checklist", "phased_todo", "objectives", "dag"})

# Status spellings models actually write, folded onto the contract vocabulary.
# Shared with services/mcp/task_list_tools.py so the tool and the store agree.
STATUS_ALIASES: dict[str, str] = {
    "todo": "pending", "open": "pending", "not_started": "pending",
    "notstarted": "pending", "not started": "pending", "queued": "pending",
    "waiting": "pending", "pending": "pending",
    "in_progress": "running", "inprogress": "running", "in progress": "running",
    "active": "running", "doing": "running", "started": "running",
    "working": "running", "running": "running", "wip": "running",
    "complete": "done", "completed": "done", "finished": "done", "ok": "done",
    "passed": "done", "success": "done", "succeeded": "done", "done": "done",
    "pass": "pass",
    "failed": "fail", "error": "fail", "errored": "fail", "fail": "fail",
    "blocked": "fail",
    "skipped": "abandoned", "skip": "abandoned", "cancelled": "abandoned",
    "canceled": "abandoned", "dropped": "abandoned", "abandoned": "abandoned",
}

# Title keys, in precedence order — models label the same field five ways.
_TITLE_KEYS = ("title", "name", "text", "task", "step", "label", "description",
               "item", "summary")
_DESC_KEYS = ("desc", "detail", "details", "description", "note", "notes", "why")

# Bounds. A task list is a UI affordance, not a datastore: past ~40 rows the
# panel is unreadable and the payload starts costing real stream bandwidth.
MAX_ITEMS = 40
MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 300
MAX_SUMMARY_CHARS = 400
MAX_RUNS = 32

# Fallback store key. There is no ambient run-id source in this process (unlike
# workspace_tools, which can read services.agent.workspace._session_registry);
# the general lane installs the event sink per turn but the run id never reaches
# tool kwargs. `set_event_sink(fn, run_id=...)` records one when the caller has
# it; otherwise every publish lands under this key, which is correct because a
# worker turn only ever has one live task list at a time.
DEFAULT_RUN_KEY = "default"

_LOCK = threading.Lock()
_RUNS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# The live `on_event` sink for the turn currently executing on this context.
# A contextvar (not a global) so concurrent turns in one worker never cross
# streams, and so clear_event_sink is exception-safe.
_SINK: contextvars.ContextVar[Callable[..., None] | None] = contextvars.ContextVar(
    "task_list_event_sink", default=None)
_RUN_KEY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "task_list_run_key", default=DEFAULT_RUN_KEY)


# --------------------------------------------------------------------------- sink

def set_event_sink(fn: Callable[..., None] | None, run_id: str | None = None):
    """Install *fn* as the live event sink for this context; returns a token.

    ``run_id`` is optional: when the caller knows it, tool calls that omit
    ``run_id`` resolve to it instead of DEFAULT_RUN_KEY.
    """
    tok_run = _RUN_KEY.set(str(run_id).strip()) if str(run_id or "").strip() else None
    return (_SINK.set(fn), tok_run)


def clear_event_sink(token) -> None:
    """Undo :func:`set_event_sink`. Never raises — this runs in ``finally``."""
    try:
        tok_sink, tok_run = token
    except (TypeError, ValueError):
        tok_sink, tok_run = token, None
    for var, tok in ((_SINK, tok_sink), (_RUN_KEY, tok_run)):
        if tok is None:
            continue
        try:
            var.reset(tok)
        except (ValueError, RuntimeError):  # token from another context
            pass


def current_run_key(run_id: str | None = None) -> str:
    """Resolve the store key for *run_id* (explicit → ambient → default)."""
    rid = str(run_id or "").strip()
    if rid:
        return rid
    try:
        return _RUN_KEY.get() or DEFAULT_RUN_KEY
    except LookupError:  # pragma: no cover — default= makes this unreachable
        return DEFAULT_RUN_KEY


# --------------------------------------------------------------------------- normalize

def _cap(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def normalize_status(raw: Any) -> str:
    """Fold a model-written status onto the contract vocabulary (default pending)."""
    s = str(raw or "").strip().lower().replace("-", "_")
    if s in VALID_STATUSES:
        return s
    return STATUS_ALIASES.get(s) or STATUS_ALIASES.get(s.replace("_", " ")) or "pending"


def normalize_shape(raw: Any) -> str:
    s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in VALID_SHAPES:
        return s
    # Common near-misses; anything unknown falls back to the safe default rather
    # than shipping a shape the host's switch does not handle.
    return {"todo": "phased_todo", "todos": "phased_todo", "list": "checklist",
            "tasks": "checklist", "plan": "build_plan",
            "steps": "checklist"}.get(s, "checklist")


def _norm_item(raw: Any, pos: int) -> dict[str, Any] | None:
    """One contract-shaped item, or None when *raw* carries no usable title."""
    if isinstance(raw, str):
        raw = {"title": raw}
    elif not isinstance(raw, dict):
        # A bare number/None in the list is noise, not a task.
        title = _cap(raw, MAX_TITLE_CHARS)
        raw = {"title": title} if title else {}
    title = ""
    for key in _TITLE_KEYS:
        v = raw.get(key)
        if isinstance(v, (str, int, float)) and str(v).strip():
            title = _cap(v, MAX_TITLE_CHARS)
            break
    if not title:
        # No title key matched. Before dropping the item, try the DESCRIPTION keys as a title
        # source: an entry carrying only `desc` has exactly one piece of human text and that
        # text IS the task name. _TITLE_KEYS already accepts the long spelling "description"
        # while _DESC_KEYS owns the short "desc", so writing the SHORTER, more common form
        # silently dropped every item and failed the whole plan with "no usable items"
        # (measured live 2026-07-29: items=[{"id":"1","desc":"Create new assembly",...}] x5).
        # Losing the planning surface makes the agent look like it never planned at all.
        for key in _DESC_KEYS:
            v = raw.get(key)
            if isinstance(v, (str, int, float)) and str(v).strip():
                title = _cap(v, MAX_TITLE_CHARS)
                break
    if not title:
        return None
    item: dict[str, Any] = {
        "id": _cap(raw.get("id") or raw.get("item_id") or f"task:{pos}", 64),
        "title": title,
        "status": normalize_status(raw.get("status") or raw.get("state")),
        # Everything published here is a task — the general lane has no parts/
        # nodes/subagents to describe, and the host colours by kind.
        "kind": "task",
    }
    for key in _DESC_KEYS:
        v = raw.get(key)
        if isinstance(v, (str, int, float)) and str(v).strip() and str(v).strip() != title:
            item["desc"] = _cap(v, MAX_DESC_CHARS)
            break
    # phase/parent_id are optional in the contract: set them only when supplied,
    # so the host does not render an empty phase band on a flat checklist.
    phase = raw.get("phase") or raw.get("group") or raw.get("section")
    if isinstance(phase, (str, int, float)) and str(phase).strip():
        item["phase"] = _cap(phase, 64)
    parent = raw.get("parent_id") or raw.get("parent")
    if isinstance(parent, (str, int, float)) and str(parent).strip():
        item["parent_id"] = _cap(parent, 64)
    return item


def normalize_items(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Contract-shaped, de-duplicated, capped item list."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if len(out) >= MAX_ITEMS:
            break
        item = _norm_item(raw, len(out) + 1)
        if item is None:
            continue
        # Duplicate ids would collide in the host's upsert-by-id map, silently
        # dropping rows; re-key the later one instead of losing it.
        if item["id"] in seen:
            item["id"] = f"{item['id']}#{len(out) + 1}"
        seen.add(item["id"])
        out.append(item)
    return out


# --------------------------------------------------------------------------- store

def publish(run_id: str | None, items: Iterable[Any], *, title: str | None = None,
            summary: str | None = None, shape: str = "checklist") -> dict[str, Any]:
    """Replace the task list for *run_id* and emit one ``plan_state`` event.

    Full-snapshot semantics: *items* is the WHOLE list. Returns the stored
    snapshot ``{"shape","title","summary","revision","items"}``.
    """
    key = current_run_key(run_id)
    norm = normalize_items(items or [])
    with _LOCK:
        prev = _RUNS.get(key) or {}
        snap: dict[str, Any] = {
            "shape": normalize_shape(shape),
            "title": _cap(title, MAX_TITLE_CHARS) or str(prev.get("title") or "Task list"),
            "revision": int(prev.get("revision") or 0) + 1,
            "items": norm,
        }
        summ = _cap(summary, MAX_SUMMARY_CHARS)
        if summ:
            snap["summary"] = summ
        _RUNS[key] = snap
        _RUNS.move_to_end(key)
        while len(_RUNS) > MAX_RUNS:
            _RUNS.popitem(last=False)  # oldest run first — long-lived worker leak guard
    _emit(snap)
    return dict(snap)


def snapshot(run_id: str | None = None) -> dict[str, Any] | None:
    """The current stored snapshot for *run_id*, or None when nothing published."""
    with _LOCK:
        snap = _RUNS.get(current_run_key(run_id))
        return dict(snap) if snap else None


def reset(run_id: str | None = None) -> None:
    """Drop the stored list for *run_id* (next publish starts at revision 1)."""
    with _LOCK:
        _RUNS.pop(current_run_key(run_id), None)


def _emit(payload: dict[str, Any]) -> None:
    """Fan the snapshot to the live sink. Silent no-op when none is installed."""
    try:
        sink = _SINK.get()
    except LookupError:  # pragma: no cover
        sink = None
    if sink is None:
        return
    # emit_event already handles both (kind, payload) and single-dict sink
    # arities and swallows sink exceptions — do not reimplement either.
    agent_events.emit_event("plan_state", dict(payload), live=sink)
