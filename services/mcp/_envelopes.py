"""The JSON envelope conventions shared by the physics / office / graph tool modules.

Every error writer here emits ONE error shape: ``ok: false``, ``error`` (the message
string), ``code`` (a stable snake_case token), and — only when there is something to say —
``hint`` and ``detail``. A writer that has always answered ``reason`` keeps that key as an
alias of ``error``, so a caller that reads either finds the message. ``hint`` is never a
copy of ``error``: it appears only when the writer has advice the message lacks.

The conventions still differ in their *wire form*, and the differences are real: a family
that has always answered a ``str`` must keep answering a ``str``, and the piping/hydraulic
helpers must keep returning a ``dict``. Each is spelled out once, on its own terms, with
the modules that speak it named:

* :func:`analysis_envelope` — ``antenna_tools``, ``em_tools``: ``str`` envelopes with
  keyword-only ``hint``/``next_actions`` and a numpy-aware writer.
* :func:`report_err` — ``heatsink``, ``ecad_reliability``: the same error shape, but
  ``hint``/``next_actions`` are positional and the writer is plain.
* :func:`reason_err` — ``piping_tools``, ``hydraulic_tools``: a ``dict``.
* :func:`compact_err` / :func:`sorted_ok` — ``office_tools``, ``plm_tools``,
  ``graph_tools``: single-line error strings, key-sorted success strings.
* :func:`error_payload` — the shape itself, for modules that build their own dict.

The JSON writer itself is one function; every place the conventions disagree
about ``indent`` / ``sort_keys`` / ``default`` is an argument to it, not a fork.
:func:`dumps` is also the shared reply path that stamps ``engine: "private"`` onto a reply
while the process is served by a private in-process engine (see :func:`stamp_engine`).
:func:`tool_reply` (every registered tool's reply, via ``tool_registry._guard``) also keeps a
JSON reply under :func:`reply_budget` characters (:func:`shrink_reply`).
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

_CODE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")


def stamp_engine(payload: Any) -> Any:
    """*payload* with ``engine: "private"`` and its warning added while hostd was
    unreachable and a standalone client fell back to a private in-process engine.

    Replies from such an engine describe documents the app and other agents cannot see, so
    the caller must be told on every reply, not only in a log line. Any other payload (or
    the healthy path) comes back unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    bridge = sys.modules.get(f"{__package__}.core_bridge")
    notice = bridge.private_engine_notice() if bridge is not None else None
    return payload if notice is None else {**payload, **notice}


def dumps(payload: Any, *, indent: Optional[int] = 2, sort_keys: bool = False,
          default: Optional[Callable[[Any], Any]] = None) -> str:
    """The single JSON writer behind every convention below."""
    return json.dumps(stamp_engine(normalize_error(payload)), indent=indent, sort_keys=sort_keys,
                      default=default)


def code_for(message: Any) -> str:
    """A stable machine token for a refusal: the message itself when it already is a
    snake_case token (the ``reason="no_circuit"`` convention), else the generic ``error``."""
    text = str(message)
    return text if _CODE_TOKEN.match(text) else "error"


def normalize_error(payload: Any) -> Any:
    """Give reason-only or structured-error refusals a top-level message and code."""
    if not isinstance(payload, dict) or payload.get("ok") is True:
        return payload
    error = payload.get("error")
    reason = payload.get("reason")
    if error is None and (payload.get("ok") is not False or reason is None):
        return payload
    nested = error if isinstance(error, dict) else {}
    message = nested.get("message") or nested.get("error") if nested else error
    message = str(message if message is not None else reason)
    code = payload.get("code") or nested.get("code") or code_for(message)
    if error == message and payload.get("code") == code:
        return payload
    result = {**payload, "error": message, "code": str(code)}
    if nested and "detail" not in result and nested.get("detail") is not None:
        result["detail"] = nested["detail"]
    if nested and "hint" not in result and nested.get("hint"):
        result["hint"] = nested["hint"]
    return result


def tool_reply(value: Any, tool: str = "") -> Any:
    """Normalize tool refusals, add private-engine disclosure, and keep the reply readable.

    A JSON reply (a ``dict`` or a JSON-object ``str``) larger than :func:`reply_budget` is
    shrunk by :func:`shrink_reply` — MCP clients refuse a result past ~25k tokens, and an
    unreadable mutation reply hides whether the edit committed. Non-JSON text, lists
    ([image, caption]) and model objects pass through untouched.
    """
    budget = reply_budget()
    if isinstance(value, dict):
        result = stamp_engine(normalize_error(value))
        return shrink_reply(result, budget, tool) or result
    if isinstance(value, str) and value.lstrip().startswith("{"):
        oversized = bool(budget) and len(value) > budget
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(parsed, dict):
            result = stamp_engine(normalize_error(parsed))
            if oversized:
                shrunk = shrink_reply(result, budget, tool)
                # Whitespace alone may be the overflow (indent=2 on a deep reply): the
                # compact form then carries every byte of data and needs no note.
                return json.dumps(shrunk or result, ensure_ascii=False, default=str,
                                  separators=(",", ":"))
            if result is not parsed:
                return json.dumps(result, ensure_ascii=False)
    return value


# --------------------------------------------------------------------------- reply budget
# Claude Code refuses a tool result past ~25k tokens ("exceeds maximum allowed tokens") and
# the agent then sees nothing — fatal for a mutation, whose reply says whether it committed.
# Every tool reply passes through tool_reply (tool_registry._guard), so this is the one
# place that guarantees a readable reply. Tool-specific compaction (measure_part,
# check_interference, describe_assembly, ...) still owns the useful default shape; this is
# the safety net for whatever a tool did not anticipate.
REPLY_BUDGET_ENV = "CHUDWARE_MCP_REPLY_BUDGET"
DEFAULT_REPLY_BUDGET = 60_000  # characters of compact JSON (~15-20k tokens)

# Top-level keys the net never drops: the verdict, identity and revision of the reply.
# Scalars stay verbatim; containers under them are only capped generously.
_PROTECTED_KEYS = frozenset({
    "ok", "error", "code", "reason", "hint", "detail", "doc_id", "revision", "name",
    "body_count", "volume_mm3", "mass_g", "summary", "warnings", "still_running", "job_id",
    "status", "state", "complete", "clear", "converged", "collision_free", "dof",
    "total_count", "next_offset", "offset", "page_size", "count", "interpreted", "engine",
    "engine_warning", "truncated", "_truncated",
})

# (items kept per list, characters kept per string), tried in order until the reply fits.
_SHRINK_STEPS = ((50, 4000), (20, 2000), (10, 1000), (5, 400), (3, 200), (1, 120), (0, 80))
_MAP_KEYS_CAP = 64  # a dict with more keys than this is a map (id -> row), not a record

# How to see the rest, per tool: the narrowing parameter the agent should use.
_NARROW_HINTS = {
    "check_interference": "scope with members=[...], page clearances with clearance_limit= / "
                          "clearance_offset=, include_parts=True for the parts list",
    "measure_part": "measure_part(body_id=N) for one body; body_limit= / body_offset= page "
                    "mass.bodies (heaviest first)",
    "describe_assembly": "describe_assembly(offset=, limit=) pages bodies/components; "
                         "check_interference(members=[...]) for a scoped fit",
    "get_part": "get_part(summary=True), describe_part(), or inspect_feature(<id>) for one "
                "feature",
    "add_features": "ok/revision/body_count above are authoritative for whether the batch "
                    "committed; read get_part(summary=True) to verify and send smaller batches",
    "cad_guide": "ask for a focused topic, e.g. cad_guide('<feature_type>'), not 'ops'",
    "query_geometry": "page with offset= / page_size=, or tighten the query "
                      "(side/surf/near/count)",
    "list_standard_parts": "list_standard_parts(family=<key>) lists one family's sizes",
    "solve_mates": "list_world_bodies() for placements",
    "wait_job": "read the finished job's result with the domain reader (sim_results / "
                "describe_results) instead of the raw job record",
    "get_job": "read the finished job's result with the domain reader (sim_results / "
               "describe_results) instead of the raw job record",
}


_FULL_REPLIES: ContextVar[bool] = ContextVar("chudware_full_replies", default=False)


@contextmanager
def full_replies() -> Iterator[None]:
    """Turn the reply budget off for calls in this context.

    The OMP host-tool projection (services/agent/omp_tools) bounds results itself and
    spills the complete JSON to a file the agent can load, so it must receive the full
    reply; this net is for MCP clients, which have no such recovery."""
    token = _FULL_REPLIES.set(True)
    try:
        yield
    finally:
        _FULL_REPLIES.reset(token)


def reply_budget() -> int:
    """The reply budget in characters; ``CHUDWARE_MCP_REPLY_BUDGET=0`` disables the net,
    and so does :func:`full_replies`."""
    if _FULL_REPLIES.get():
        return 0
    raw = os.environ.get(REPLY_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_REPLY_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_REPLY_BUDGET


def _compact_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))


def _shrink(value: Any, items: int, chars: int, path: str, cut: dict) -> Any:
    """*value* with lists/maps cut to *items* entries and strings to *chars* characters.

    Every cut is recorded in *cut* as ``{path: original_size}``.
    """
    if isinstance(value, str):
        if len(value) <= chars:
            return value
        cut[f"{path}(chars)"] = len(value)
        return value[:chars] + f"... [+{len(value) - chars} chars]"
    if isinstance(value, dict):
        entries = list(value.items())
        if len(entries) > _MAP_KEYS_CAP and len(entries) > items:
            cut[path or "(reply)"] = len(entries)
            entries = entries[:items]
        return {k: _shrink(v, items, chars, f"{path}.{k}" if path else str(k), cut)
                for k, v in entries}
    if isinstance(value, (list, tuple)):
        rows = list(value)
        if len(rows) > items:
            cut[path or "(reply)"] = len(rows)
            rows = rows[:items]
        return [_shrink(v, items, chars, f"{path}[]", cut) for v in rows]
    return value


def shrink_reply(payload: dict, budget: int, tool: str = "") -> Optional[dict]:
    """A copy of *payload* that fits *budget* characters of compact JSON, or None when it
    already fits (or the budget is disabled).

    Protected top-level scalars (ok, error, code, doc_id, revision, body_count, summary,
    still_running, job_id, ...) are never dropped. Lists and maps are cut to their first N
    entries, long strings are clipped, and the reply gains ``_truncated`` ({field path:
    original count}) plus a ``truncated`` note naming how to page or narrow this tool.
    """
    if not budget or not isinstance(payload, dict):
        return None
    original = _compact_size(payload)
    if original <= budget:
        return None
    narrow = _NARROW_HINTS.get(tool) or ("narrow the request (a filter, offset/limit paging "
                                         "or a summary flag) to see the rest")
    note_key = "truncated" if not isinstance(payload.get("truncated"), (list, dict, str)) \
        else "reply_truncated"

    def _noted(out: dict, cut: dict, items: int) -> dict:
        out["_truncated"] = cut
        out[note_key] = (f"This reply was {original} chars, over the {budget}-char MCP result "
                         f"budget, so lists were cut to their first {items} entries and long "
                         f"strings clipped; `_truncated` gives each cut field's full size. "
                         f"To see the rest: {narrow}.")
        return out

    for items, chars in _SHRINK_STEPS:
        cut: dict = {}
        out: dict = {}
        for key, value in payload.items():
            if key in _PROTECTED_KEYS:
                out[key] = _shrink(value, max(items, 10), max(chars, 2000), key, cut)
            else:
                out[key] = _shrink(value, items, chars, key, cut)
        out = _noted(out, cut, items)
        if _compact_size(out) <= budget:
            return out
    # Last resort: only the protected verdict/identity fields survive.
    cut = {key: _compact_size(value) for key, value in payload.items()
           if key not in _PROTECTED_KEYS}
    out = {key: _shrink(value, 3, 500, key, {}) for key, value in payload.items()
           if key in _PROTECTED_KEYS}
    out = _noted(out, {f"{key}(chars)": n for key, n in cut.items()}, 0)
    out[note_key] += " Every other field was dropped."
    return out


def error_payload(message: Any, *, code: str = "", hint: str = "", detail: Any = None,
                  **extra: Any) -> dict:
    """The one error shape: ``{ok: false, error, code, [hint], [detail], **extra}``.

    ``code`` defaults to :func:`code_for`. ``hint`` and ``detail`` are present only when
    given; a hint that merely repeats the message is not advice, so it is never invented.
    """
    text = str(message)
    out: dict[str, Any] = {**extra, "ok": False, "error": text, "code": code or code_for(text)}
    if hint:
        out["hint"] = hint
    if detail is not None:
        out["detail"] = detail
    return out


def json_default(obj: Any, *, mark_callables: bool = False) -> Any:
    """Fallback for values ``json`` cannot write.

    numpy scalars/arrays (which leak out of worker payloads) become plain Python
    values; anything else is stringified. ``mark_callables`` renders functions as
    ``"<callable>"`` instead of their address-bearing repr.
    """
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:  # noqa: BLE001
        pass
    if mark_callables and callable(obj):
        return "<callable>"
    return str(obj)


def plain_dumps(payload: dict) -> str:
    """Indented JSON with no coercion — heatsink / ecad_reliability / piping / hydraulic."""
    return dumps(payload)


def stringly_dumps(obj: Any) -> str:
    """Indented JSON with a ``str`` fallback for exotic values — graph_tools."""
    return dumps(obj, default=str)


def analysis_envelope(*, mark_callables: bool = False) -> tuple[
        Callable[..., str], Callable[..., str], Callable[..., str]]:
    """The antenna_tools / em_tools convention, as ``(dumps, err, ok)``.

    The three helpers share one serializer, so the numpy-default difference
    between the two modules is bound here once instead of forking the envelope
    builders: ``antenna_tools`` marks callables, ``em_tools`` does not.
    """
    def _default(obj: Any) -> Any:
        return json_default(obj, mark_callables=mark_callables)

    def analysis_dumps(payload: dict) -> str:
        return dumps(payload, default=_default)

    def analysis_err(reason: str, *, hint: str = "", next_actions: Optional[list] = None,
                     **extra: Any) -> str:
        extra.setdefault("warnings", [])
        extra.setdefault("next_actions", next_actions or [])
        return analysis_dumps(error_payload(reason, hint=hint, **extra))

    def analysis_ok(payload: dict, *, hint: str = "", next_actions: Optional[list] = None,
                    warnings: Optional[list] = None) -> str:
        out = {
            "ok": True,
            "warnings": list(warnings or []),
            "hint": hint,
            "next_actions": list(next_actions or []),
            **payload,
        }
        return analysis_dumps(out)

    return analysis_dumps, analysis_err, analysis_ok


def report_err(reason: str, hint: str = "", next_actions: Optional[list] = None,
               **extra: Any) -> str:
    """Error string with a positional hint — heatsink / ecad_reliability."""
    extra.setdefault("warnings", [])
    extra.setdefault("next_actions", next_actions or [])
    return plain_dumps(error_payload(reason, hint=hint, **extra))


def reason_err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    """Refusal **dict** (never serialized here) — piping / hydraulic.

    ``reason`` is kept beside ``error`` for the callers that have always read it.
    """
    return error_payload(reason, hint=hint or "", reason=reason, **extra)


def compact_err(reason: str, hint: str = "") -> str:
    """Single-line refusal string — office_tools / plm_tools / graph_tools."""
    return dumps(error_payload(reason, hint=hint, reason=reason), indent=None)


def sorted_ok(**fields: Any) -> str:
    """Key-sorted success string — office_tools / plm_tools."""
    return dumps({"ok": True, **fields}, sort_keys=True)
