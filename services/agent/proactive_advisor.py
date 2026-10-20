"""Proactive mid-run advisor — opt-in, token-cheap guidance on thrash/failures.

Hard gate: ``config.proactive_advisor_enabled()`` (Settings checkbox /
``agent.proactive_advisor``, default False). Advisor preference still controls
tone/frequency and must not be ``off``.

Design (token budget first):
  * Always start from a **deterministic** note (no LLM).
  * Cooldown: min 3 tool calls OR 45s between fires; max 3 fires per run_turn.
  * Triggers: fail streak ≥2 in last 6, same tool failed twice consecutively,
    or require-mode every N tool calls.
  * Optional one-shot **advisor-role** LLM only when preference is
    encourage/require AND severity is high (fail streak ≥3 or same-tool x3)
    AND under per-turn / per-run caps (max 1 LLM call per turn, max 3/run).
  * Never blocks the turn; every failure path returns "".
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from services.agent.chudcode_roles import run_role_turn

_log = logging.getLogger("chudware.proactive_advisor")

# Cooldown / caps (deliberately conservative — over-firing trains models to ignore us).
_MIN_TOOL_GAP = 3
_MIN_SECONDS = 45.0
_MAX_FIRES_PER_TURN = 3
_MAX_LLM_PER_TURN = 1
_MAX_LLM_PER_RUN = 3
_REQUIRE_EVERY_N = (
    4  # fallback only; _require_every_n() reads config.supervision_interval_calls()
)
_LLM_MAX_TOKENS = 220
_EVIDENCE_CHARS = 900

_STATE_KEY = "_proactive_advisor_state"


def _state(bucket: dict | None) -> dict:
    """Mutable cooldown counters living on the turn's options dict (or a local)."""
    if bucket is None:
        return {
            "fires": 0,
            "llm_fires": 0,
            "last_fire_tools": -10_000,
            "last_fire_t": 0.0,
            "run_llm_fires": 0,
        }
    st = bucket.get(_STATE_KEY)
    if not isinstance(st, dict):
        st = {
            "fires": 0,
            "llm_fires": 0,
            "last_fire_tools": -10_000,
            "last_fire_t": 0.0,
            "run_llm_fires": int(bucket.get("_proactive_run_llm") or 0),
        }
        bucket[_STATE_KEY] = st
    return st


def _require_every_n() -> int:
    """Cadence for require-mode force-injection (``agent.supervision_interval_calls``).

    Single source of truth for every ``_REQUIRE_EVERY_N``-style read in this
    module — routes through :func:`services.agent.config.supervision_interval_calls`
    so the GUI Settings value (and its ENV override / precedence rules) take
    effect without a restart. 0 disables require-mode entirely (never force).
    Falls back to the historical default (4) if config is unreachable.
    """
    try:
        from services.agent import config  # noqa: PLC0415

        return max(0, int(config.supervision_interval_calls()))
    except Exception:  # noqa: BLE001
        return _REQUIRE_EVERY_N


def _truncate(text: str, n: int = _EVIDENCE_CHARS) -> str:
    s = str(text or "").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def _result_error_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        for k in ("error", "message", "detail", "stderr", "reason"):
            v = result.get(k)
            if v:
                return _truncate(v)
        return _truncate(json.dumps(result, default=str)[:_EVIDENCE_CHARS])
    return _truncate(result)


def _is_policy_refusal(record: dict) -> bool:
    return bool(record.get("policy_refusal")) or (
        record.get("outcome_kind") == "policy_refusal"
    )


def _was_executed(record: dict) -> bool:
    return record.get("executed", True) is not False and not _is_policy_refusal(record)


def _is_executed_failure(record: dict) -> bool:
    return _was_executed(record) and not record.get("ok", True)


def evaluate_trigger(
    *,
    ok: bool,
    tool: str,
    calls: list[dict],
    tool_calls_made: int,
    preference: str,
) -> dict | None:
    """Return a trigger dict or None if nothing should fire.

    Trigger dict keys: severity (\"med\"|\"high\"), reason, fail_n, same_tool_n.
    """
    pref = (preference or "encourage").strip().lower()
    if pref not in ("encourage", "require", "option"):
        return None

    records = list(calls or [])
    current_record = (
        records[-1] if records and records[-1].get("tool") == tool else None
    )
    current_refusal = bool(current_record and _is_policy_refusal(current_record))
    current_failed = (
        _is_executed_failure(current_record) if current_record is not None else not ok
    )

    # Non-executed records are not attempts and cannot form failure/thrash
    # streaks. Filter before taking the window so refusals do not evict actual
    # execution evidence either.
    recent = [record for record in records if _was_executed(record)][-6:]
    fail_n = sum(1 for record in recent if _is_executed_failure(record))
    same_tool_n = 0
    for record in reversed(recent):
        if record.get("tool") == tool and _is_executed_failure(record):
            same_tool_n += 1
        else:
            break
    if current_refusal:
        return {
            "severity": "med",
            "reason": "policy_refusal",
            "fail_n": fail_n,
            "same_tool_n": same_tool_n,
            "policy_refusal": True,
        }

    reason = ""
    severity = "med"
    fire = False

    if current_failed and fail_n >= 2:
        fire = True
        reason = f"fail_streak={fail_n}"
        if fail_n >= 3 or same_tool_n >= 3:
            severity = "high"
    if current_failed and same_tool_n >= 2:
        fire = True
        reason = (reason + "+" if reason else "") + f"same_tool_x{same_tool_n}"
        if same_tool_n >= 3:
            severity = "high"
    require_n = _require_every_n()
    if (
        pref == "require"
        and require_n > 0
        and tool_calls_made > 0
        and tool_calls_made % require_n == 0
    ):
        fire = True
        if not reason:
            reason = f"require_every_{require_n}"
            severity = "med"

    # option: only fire on high severity (strict opt-in feel even if checkbox on)
    if pref == "option" and severity != "high":
        return None

    if not fire:
        return None
    return {
        "severity": severity,
        "reason": reason or "trigger",
        "fail_n": fail_n,
        "same_tool_n": same_tool_n,
    }


def _cooldown_ok(st: dict, tool_calls_made: int, now: float) -> bool:
    if int(st.get("fires") or 0) >= _MAX_FIRES_PER_TURN:
        return False
    last_tools = int(st.get("last_fire_tools") or -10_000)
    last_t = float(st.get("last_fire_t") or 0.0)
    if tool_calls_made - last_tools < _MIN_TOOL_GAP and (now - last_t) < _MIN_SECONDS:
        return False
    return True


def build_deterministic_note(
    *,
    tool: str,
    ok: bool,
    trigger: dict,
    preference: str,
    result: Any = None,
) -> str:
    err = _result_error_text(result)
    err_bit = f" Reason: {err}" if err else ""
    sev = trigger.get("severity", "med")
    fail_n = int(trigger.get("fail_n") or 0)
    same_n = int(trigger.get("same_tool_n") or 0)
    if trigger.get("policy_refusal"):
        return (
            f"[ADVISOR — proactive/policy] {tool!r} was not executed because "
            f"the harness refused it.{err_bit} This is policy data, not a tool "
            "failure or a retry/thrash signal."
        )
    lines = [
        f"[ADVISOR — proactive/{sev}] {trigger.get('reason', 'trigger')}. "
        f"Last tool={tool!r} ok={ok}; fails_in_last_6={fail_n}; same_tool_streak={same_n}."
        f"{err_bit}",
        "Pause thrash: re-read the error, change approach if the same call already failed "
        "twice, verify units/ids before retrying, and do not invent dimensions.",
    ]
    if preference == "require":
        lines.append(
            "Require mode: state the next single concrete step (tool + key args) before more edits."
        )
    return "\n".join(lines)


def _try_llm_advice(
    *,
    tool: str,
    ok: bool,
    trigger: dict,
    result: Any,
    recent_calls: list[dict],
    options: dict | None = None,
    on_event: Callable[..., None] | None = None,
) -> str:
    """Best-effort one-shot advisor role turn. Returns "" on any failure."""
    try:
        from services.agent import config as agent_config  # noqa: PLC0415
        from services.agent.role_models import (  # noqa: PLC0415
            clamp_subagent_effort,
            resolve_role_model,
            resolve_role_thinking,
        )

        source_options = options if isinstance(options, dict) else {}
        cfg_value = source_options.get("config")
        cfg = (
            dict(cfg_value)
            if isinstance(cfg_value, dict)
            else dict(agent_config.load_config() or {})
        )
        assignment = resolve_role_model("advisor", cfg)
        ambient_effort = (
            source_options.get("reasoning_effort")
            or cfg.get("reasoning_effort")
            or cfg.get("thinking")
        )
        effort = resolve_role_thinking(
            "advisor",
            cfg,
            task_class="analysis",
            dispatch_effort=clamp_subagent_effort(ambient_effort),
            rank=getattr(assignment, "rank", None),
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("proactive advisor role resolution failed: %s", exc)
        return ""

    # Compact evidence — last few tool outcomes only.
    rows = []
    for c in list(recent_calls or [])[-5:]:
        rows.append(
            f"- tool={c.get('tool')!r} ok={c.get('ok', True)} "
            f"err={_truncate(c.get('error') or c.get('result') or '', 160)}"
        )
    evidence = "\n".join(rows) if rows else "(no prior calls)"
    err = _result_error_text(result)
    request = (
        "ADVISOR COMPLETION CONTRACT\n"
        "You are the ADVISOR steering a stuck CAD agent. Be brief. "
        "Output JSON only. Prefer steer over escalate.\n\n"
        f"SEVERITY={trigger.get('severity')} REASON={trigger.get('reason')}\n"
        f"LAST_TOOL={tool!r} OK={ok}\n"
        f"ERROR={err or '(none)'}\n"
        f"RECENT:\n{evidence}\n\n"
        "Reply with STRICT JSON only:\n"
        '{"verdict":"steer|revise|escalate","notes":["..."],"focus":"..."}\n'
        "Max 4 short notes. No markdown. No tools or CAD mutators. "
        "No invented dimensions."
    )
    parent_session = str(source_options.get("session_id") or "")
    run_id = str(source_options.get("run_id") or f"{parent_session}:proactive")
    parent_id = str(
        source_options.get("node_id")
        or source_options.get("subagent_id")
        or source_options.get("parent_id")
        or parent_session
    )
    settings = {
        "provider": str(getattr(assignment, "provider", "") or ""),
        "model": str(getattr(assignment, "model", "") or ""),
    }
    if effort:
        settings["reasoning_effort"] = effort
    role_options: dict[str, Any] = {
        "config": cfg,
        "settings": settings,
        "allowed_tools": [],
    }
    if source_options.get("offline") is True or agent_config.offline_enabled(cfg):
        role_options["offline"] = True
        if source_options.get("offline_provider") is not None:
            role_options["offline_provider"] = source_options["offline_provider"]
    for key in ("workspace_dir", "project_dir", "chudcode_session_dir"):
        if source_options.get(key) is not None:
            role_options[key] = source_options[key]

    try:
        turn = run_role_turn(
            request,
            "advisor",
            session_id=parent_session,
            run_id=run_id,
            parent_id=parent_id,
            on_event=on_event,
            options=role_options,
            max_tokens=_LLM_MAX_TOKENS,
            max_total_tokens=_LLM_MAX_TOKENS,
            close_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("proactive advisor Chudcode turn failed: %s", exc)
        return ""
    if not isinstance(turn, dict) or not turn.get("success") or turn.get("cancelled"):
        return ""
    reply = turn.get("result")
    if isinstance(reply, dict):
        reply = next(
            (
                reply.get(key)
                for key in ("text", "result", "message", "content")
                if isinstance(reply.get(key), str)
            ),
            "",
        )
    text = str(reply or "").strip()
    if not text:
        return ""
    # Extract JSON object if the model wrapped it.
    m = re.search(r"\{[\s\S]*\}", text)
    blob = m.group(0) if m else text
    notes: list[str] = []
    focus = ""
    verdict = "steer"
    try:
        data = json.loads(blob)
        if isinstance(data, dict):
            verdict = str(data.get("verdict") or "steer")
            focus = str(data.get("focus") or "").strip()
            raw_notes = data.get("notes") or []
            if isinstance(raw_notes, list):
                notes = [str(n).strip() for n in raw_notes if str(n).strip()][:4]
            elif isinstance(raw_notes, str) and raw_notes.strip():
                notes = [raw_notes.strip()]
    except Exception:  # noqa: BLE001 — free-text fallback
        notes = [_truncate(text, 400)]

    if not notes and not focus:
        return ""
    lines = [f"[ADVISOR — proactive/llm] verdict={verdict}"]
    if focus:
        lines.append(f"focus: {focus}")
    for n in notes:
        lines.append(f"- {n}")
    lines.append("Act on these notes; do not invent dimensions.")
    return "\n".join(lines)


def maybe_proactive_note(
    *,
    ok: bool,
    tool: str,
    result: Any,
    calls: list[dict],
    tool_calls_made: int,
    options: dict | None = None,
    allow_llm: bool = True,
    on_event: Callable[..., None] | None = None,
) -> tuple[str, dict]:
    """Return ``(note, meta)``. Empty note means do not inject.

    *meta* is always a dict (may be empty) suitable for ``on_event``.
    """
    meta: dict[str, Any] = {}
    try:
        from services.agent.config import (  # noqa: PLC0415
            get_advisor_preference,
            proactive_advisor_enabled,
        )
    except Exception:  # noqa: BLE001
        return "", meta

    try:
        if not proactive_advisor_enabled():
            return "", meta
        pref = get_advisor_preference()
        if pref == "off":
            return "", meta
    except Exception:  # noqa: BLE001
        return "", meta

    trigger = evaluate_trigger(
        ok=ok,
        tool=tool,
        calls=calls,
        tool_calls_made=tool_calls_made,
        preference=pref,
    )
    if not trigger:
        return "", meta

    bucket = options if isinstance(options, dict) else None
    st = _state(bucket)
    now = time.monotonic()
    if not _cooldown_ok(st, tool_calls_made, now):
        meta = {"skipped": "cooldown", **trigger, "preference": pref}
        return "", meta

    note = build_deterministic_note(
        tool=tool,
        ok=ok,
        trigger=trigger,
        preference=pref,
        result=result,
    )
    source = "deterministic"
    used_llm = False

    want_llm = (
        allow_llm
        and trigger.get("severity") == "high"
        and not trigger.get("policy_refusal")
        and pref in ("encourage", "require")
        and int(st.get("llm_fires") or 0) < _MAX_LLM_PER_TURN
        and int(st.get("run_llm_fires") or 0) < _MAX_LLM_PER_RUN
    )
    if want_llm:
        llm_note = _try_llm_advice(
            tool=tool,
            ok=ok,
            trigger=trigger,
            result=result,
            recent_calls=calls,
            options=options,
            on_event=on_event,
        )
        if llm_note:
            # Keep deterministic context, append LLM steer.
            note = note + "\n" + llm_note
            source = "deterministic+llm"
            used_llm = True
            st["llm_fires"] = int(st.get("llm_fires") or 0) + 1
            st["run_llm_fires"] = int(st.get("run_llm_fires") or 0) + 1
            if bucket is not None:
                bucket["_proactive_run_llm"] = st["run_llm_fires"]

    st["fires"] = int(st.get("fires") or 0) + 1
    st["last_fire_tools"] = int(tool_calls_made)
    st["last_fire_t"] = now
    if bucket is not None:
        bucket[_STATE_KEY] = st

    meta = {
        "source": "proactive",
        "body_source": source,
        "preference": pref,
        "tool": tool,
        "llm": used_llm,
        **trigger,
    }
    return note, meta


def inject_if_needed(
    messages: list,
    *,
    ok: bool,
    tool: str,
    result: Any,
    calls: list[dict],
    tool_calls_made: int,
    options: dict | None = None,
    on_event: Callable[..., None] | None = None,
    parts_fn: Callable[[str], Any] | None = None,
    allow_llm: bool = True,
) -> bool:
    """Append a proactive note to *messages* when gated. Returns True if injected."""
    note, meta = maybe_proactive_note(
        ok=ok,
        tool=tool,
        result=result,
        calls=calls,
        tool_calls_made=tool_calls_made,
        options=options,
        allow_llm=allow_llm,
        on_event=on_event,
    )
    if not note:
        return False
    content = parts_fn(note) if parts_fn is not None else note
    messages.append({"role": "user", "content": content})
    if on_event is not None and meta:
        try:
            on_event("agent.advisor", meta)
        except Exception:  # noqa: BLE001
            pass
    return True
