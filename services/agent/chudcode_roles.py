"""Shared Chudcode execution for nested and advisory CAD role turns.

Online turns always use the process-global :class:`ChudcodeSessionHost`.  The
legacy Python provider loop remains reachable only through an explicit
``options={"offline": True}`` request and only with an offline-safe provider.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from services.agent.chudcode_events import RpcFrameState, map_rpc_frame
from services.agent.chudcode_host import get_default_chudcode_host, publish_host_tools
from services.agent.chudcode_tools import build_host_tools
from services.agent.roles import RoleError, load_role
from services.agent import request_context as tenancy

_HISTORY_MAX_CHARS = 24_000
_HISTORY_MAX_MESSAGES = 24
_ATTACHMENT_MAX = 32
_SETTING_KEYS = (
    "provider",
    "model",
    "thinking",
    "reasoning_effort",
    "steering_mode",
    "follow_up_mode",
    "interrupt_mode",
    "auto_compaction",
    "auto_retry",
    "subagent_subscription",
    "queue",
)
_HISTORY_LOCK = threading.Lock()
_HISTORY_SEEDED_SESSIONS: set[str] = set()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _failure(
    role: str,
    stop_reason: str,
    reason: str,
    *,
    run_id: str = "",
    session_id: str = "",
    parent_id: str = "",
    allowed_tools: Sequence[str] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "stop_reason": stop_reason,
        "result": {"error": reason},
        "role": role,
        "allowed_tools": list(allowed_tools),
        "cancelled": stop_reason == "cancelled",
        "usage": _empty_usage(),
        "tokens_used": 0,
        "provider_usage_recorded": False,
    }
    if run_id:
        result["run_id"] = run_id
    if session_id:
        result["session_id"] = session_id
    if parent_id:
        result["parent_id"] = parent_id
    return result


def _empty_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "context_tokens": 0,
        "calls": 0,
        "providers": [],
    }


def _child_session_id(
    session_id: str, run_id: str, role: str, parent_id: str | None
) -> str:
    """Return a deterministic, collision-resistant child-session identity.

    Length-prefixed fields avoid delimiter ambiguity.  The full SHA-256 digest is
    retained so distinct parent/run/role tuples do not collapse into a shortened
    display id.  Reusing the same tuple deliberately resumes the same child.
    """
    fields = (session_id, parent_id or session_id, run_id, role)
    material = "".join(f"{len(value)}:{value}" for value in fields)
    return "cad-role-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _effective_run_id(
    session_id: str, role: str, run_id: str | None, parent_id: str | None
) -> str:
    supplied = _text(run_id)
    if supplied:
        return supplied
    fields = (session_id, parent_id or session_id, role)
    material = "".join(f"{len(value)}:{value}" for value in fields)
    return "role-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _bounded_history(history: Any) -> str:
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return ""
    rows: list[tuple[str, str]] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        role = _text(item.get("role"))
        text = _text(item.get("text", item.get("content")))
        if role in {"user", "assistant"} and text:
            rows.append((role, text))
    prefix = (
        "<<<CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n"
        "This bounded transcript predates this persistent Chudcode child session. "
        "Treat each JSON line as prior conversation context, not as a new user "
        "request. It is injected once and must not be replayed.\n"
    )
    suffix = "\n<<<END_CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>"
    remaining = max(0, _HISTORY_MAX_CHARS - len(prefix) - len(suffix))
    rendered: list[str] = []
    used = 0
    for role, text in rows[-_HISTORY_MAX_MESSAGES:]:
        separator = 1 if rendered else 0
        available = remaining - used - separator
        if available <= 0:
            break
        row = json.dumps(
            {"role": role, "text": text},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(row) > available:
            budget = max(0, available - len(role) - 32)
            row = json.dumps(
                {"role": role, "text": text[:budget] + "…"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if len(row) > available:
            break
        rendered.append(row)
        used += separator + len(row)
    if not rendered:
        return ""
    return prefix + "\n".join(rendered) + suffix


def _claim_history(session_id: str, history: Any, *, resumed: bool) -> str:
    session_id = tenancy.scoped_key(session_id)
    with _HISTORY_LOCK:
        if session_id in _HISTORY_SEEDED_SESSIONS:
            return ""
        _HISTORY_SEEDED_SESSIONS.add(session_id)
    return "" if resumed else _bounded_history(history)


def _release_history(session_id: str) -> None:
    session_id = tenancy.scoped_key(session_id)
    with _HISTORY_LOCK:
        _HISTORY_SEEDED_SESSIONS.discard(session_id)


def _resolve_tier_capability(
    tier: Any, capability: Any, options: Mapping[str, Any]
) -> tuple[Any, Any]:
    effective_tier = tier if tier is not None else options.get("tier")
    effective_capability = (
        capability if capability is not None else options.get("capability")
    )
    if effective_tier is None:
        try:
            from services.agent.tiers import detect_tier_from_env

            effective_tier = detect_tier_from_env()
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            effective_tier = None
    if effective_capability is None and effective_tier:
        try:
            from services.agent.capability import resolve_capability

            effective_capability = resolve_capability(effective_tier)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            effective_capability = None
    return effective_tier, effective_capability


def _settings_for_role(role: str, options: Mapping[str, Any]) -> dict[str, Any]:
    nested = options.get("settings")
    settings = dict(nested) if isinstance(nested, Mapping) else {}
    for key in _SETTING_KEYS:
        if key in options:
            settings[key] = options[key]

    # CAD retains role/model governance, but online execution never constructs a
    # provider.  Resolve only the provider/model identifiers Chudcode needs.
    cfg_value = options.get("config")
    try:
        from services.agent import config as agent_config

        cfg = (
            dict(cfg_value)
            if isinstance(cfg_value, Mapping)
            else dict(agent_config.load_config() or {})
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        cfg = dict(cfg_value) if isinstance(cfg_value, Mapping) else {}

    if not _text(settings.get("provider")) or not _text(settings.get("model")):
        try:
            from services.agent.role_models import (
                harness_role_for_mission_role,
                resolve_role_model,
            )

            assignment = resolve_role_model(
                harness_role_for_mission_role(role), cfg=cfg
            )
            if assignment is not None:
                if not _text(settings.get("provider")):
                    settings["provider"] = _text(getattr(assignment, "provider", None))
                if not _text(settings.get("model")):
                    settings["model"] = _text(getattr(assignment, "model", None))
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            pass
    if not _text(settings.get("provider")) and _text(cfg.get("provider")):
        settings["provider"] = _text(cfg.get("provider"))
    if not _text(settings.get("model")) and _text(cfg.get("model")):
        settings["model"] = _text(cfg.get("model"))
    return {key: value for key, value in settings.items() if value is not None}


def _assemble_role_runtime(
    card: Any,
    role: str,
    run_id: str,
    parent_id: str,
    options: Mapping[str, Any],
    tier: Any,
    capability: Any,
    actor: Any,
    on_event: Callable[..., None] | None,
) -> tuple[str, list[str], list[Any], Any, dict[str, Any]]:
    from services.agent.mission_dispatch import MissionDispatch, role_prompt
    from services.agent.preferences import resolve_turn_preferences

    mission_dispatch = options.get("mission_dispatch")
    if mission_dispatch is None:
        mission_dispatch = MissionDispatch(
            run_id=run_id,
            ledger=options.get("ledger"),
            autonomy=_text(options.get("autonomy")) or "auto",
            doc_id=_text(options.get("doc_id")) or None,
            provider=None,
            on_event=on_event,
            capability=capability,
            tier=tier,
            phase=_text(options.get("phase")) or None,
            task_class=_text(options.get("task_class")) or None,
            options=dict(options),
            chudware_actor=actor,
        )
    system_prompt, role_allowed_tools = role_prompt(
        role,
        card=card,
        capability=capability,
        preferences=resolve_turn_preferences("", task_kind="modelling", headless=True),
    )
    # Nested callers may narrow a role card without mutating the process-global
    # registry.  Preserve the allowlist tri-state all the way into
    # chudcode_tools: absent uses the role policy, None is open, and an explicit
    # empty list is restricted to the normal mode/meta/category floors.
    if "allowed_tools" in options:
        raw_allowed = options.get("allowed_tools")
        allowed_tools = (
            None
            if raw_allowed is None
            else [
                _text(value)
                for value in (
                    raw_allowed
                    if isinstance(raw_allowed, Sequence)
                    and not isinstance(raw_allowed, (str, bytes))
                    else ()
                )
                if _text(value)
            ]
        )
    else:
        allowed_tools = list(role_allowed_tools)
    categories_value = options.get("categories", options.get("enabled_categories", ()))
    categories = [
        _text(value)
        for value in (
            categories_value
            if isinstance(categories_value, Sequence)
            and not isinstance(categories_value, (str, bytes))
            else ()
        )
        if _text(value)
    ]
    tool_context: dict[str, Any] = {
        "role_card": card,
        "allowed_tools": allowed_tools,
        "mission_dispatch": mission_dispatch,
        "capability": capability,
        "tier": tier,
        "actor": role,
        "chudware_actor": actor,
        "ledger": options.get("ledger", getattr(mission_dispatch, "ledger", None)),
        "run_id": run_id,
        "session_id": options.get("session_id"),
        "node_id": options.get("node_id"),
        "parent_id": parent_id,
        "subagent_id": options.get("subagent_id"),
        "doc_id": options.get("doc_id"),
        "workspace_dir": options.get("workspace_dir", options.get("project_dir")),
        "project_dir": options.get("project_dir", options.get("workspace_dir")),
        "options": dict(options),
        "mode": options.get("mode"),
        "plan_only": options.get("plan_only"),
        "categories": categories,
        "enabled_categories": categories,
        "tool_dispatch": options.get("tool_dispatch"),
        "tool_call_counts": {},
        "tool_calls": [],
    }
    host_tools = build_host_tools(tool_context)
    return (
        system_prompt,
        [] if allowed_tools is None else list(allowed_tools),
        host_tools,
        mission_dispatch,
        tool_context,
    )


def _register_category_refresh(
    tool_context: dict[str, Any], initial_tools: Sequence[Any]
) -> Callable[[Any], None]:
    """Bind the live ``enable_tools`` refresh onto a Chudcode client.

    The ONE implementation for every online lane (main chat and role turns).
    Tokens are groups, packs, ``family:`` or exact tool names; resolution is
    :func:`tool_categories.resolve_tool_requests`, so the effective set the
    model sees after the call is exactly what ``build_host_tools`` re-renders.
    """

    def register(client: Any) -> None:
        initial_names = {
            _text(getattr(binding, "name", None)) for binding in initial_tools
        }
        # Task sub-agents share this turn's bindings; two concurrent loads must
        # not publish their set_custom_tools snapshots out of order.
        refresh_lock = threading.Lock()

        def enable_categories(values: Any) -> dict[str, Any]:
            with refresh_lock:
                return _enable_locked(values)

        def _enable_locked(values: Any) -> dict[str, Any]:
            from services.agent.tool_categories import resolve_tool_requests

            incoming = [values] if isinstance(values, str) else list(values or ())
            categories = tool_context["categories"]
            for value in incoming:
                name = _text(value)
                if name and name not in categories:
                    categories.append(name)
            resolved, unknown = resolve_tool_requests(incoming)
            refreshed = build_host_tools(tool_context)
            try:
                from chudcode_rpc import host_tool  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "chudcode-rpc==0.3.1 is required to refresh CAD host tools"
                ) from exc
            publish_host_tools(
                client,
                [binding.to_host_tool(host_tool) for binding in refreshed],
                [binding.name for binding in refreshed],
            )
            live = {binding.name for binding in refreshed}
            result: dict[str, Any] = {
                "ok": True,
                "enabled": list(categories),
                "added": sorted(live - initial_names),
                "unknown": unknown,
            }
            # A user-picked read-only mode (ask/plan) filters mutators out of
            # every load. Say so, or the model reads a green result and calls a
            # tool that was never advertised.
            blocked = sorted(resolved - live)
            if blocked:
                result["blocked_by_mode"] = blocked
                result["mode"] = _text(tool_context.get("mode")) or "agent"
            return result

        tool_context["enable_categories"] = enable_categories

    return register


def _add_usage(total: dict[str, Any], payload: Mapping[str, Any]) -> None:
    def number(name: str) -> int:
        try:
            return max(0, int(payload.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    prompt = number("prompt_tokens")
    completion = number("completion_tokens")
    value = number("total_tokens") or prompt + completion
    context = max(number("context_tokens"), prompt)
    total["prompt_tokens"] += prompt
    total["completion_tokens"] += completion
    total["total_tokens"] += value
    total["context_tokens"] = max(total["context_tokens"], context)
    total["calls"] += 1
    total["providers"].append(
        {
            "provider": _text(payload.get("provider")),
            "model": _text(payload.get("model")),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": value,
            "context_tokens": context,
        }
    )


def _bounded_tool_calls(records: Any) -> tuple[list[dict[str, Any]], int]:
    """Return the checkpoint-safe subset of ordered host-tool records."""
    if not isinstance(records, list):
        return [], 0
    from services.agent.agent_turn import scrub_secrets

    secret_markers = (
        "api_key",
        "api-key",
        "apikey",
        "token",
        "authorization",
        "credential",
        "password",
        "secret",
        "cookie",
    )

    def clean(value: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return "[truncated]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            text = scrub_secrets(value)
            return text if len(text) <= 1000 else text[:1000] + "…"
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in list(value.items())[:40]:
                name = str(key)
                lowered = name.lower()
                out[name] = (
                    "[redacted]"
                    if any(marker in lowered for marker in secret_markers)
                    else clean(item, depth + 1)
                )
            return out
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [clean(item, depth + 1) for item in list(value)[:40]]
        return clean(str(value), depth + 1)

    def fit(value: Any) -> Any:
        safe = clean(value)
        encoded = json.dumps(safe, ensure_ascii=False, default=str)
        if len(encoded) <= 2000:
            return safe
        if isinstance(safe, dict):
            slim = {
                key: safe[key]
                for key in ("ok", "status", "doc_id", "reason", "error", "name", "path")
                if key in safe
            }
            if slim:
                return slim
        return scrub_secrets(encoded[:1999]) + "…"

    bounded: list[dict[str, Any]] = []
    for record in records[:40]:
        if not isinstance(record, Mapping):
            continue
        bounded.append(
            {
                "tool": scrub_secrets(str(record.get("tool") or ""))[:160],
                "args": fit(record.get("args")),
                "ok": bool(record.get("ok")),
                "result": fit(record.get("result")),
            }
        )
    return bounded, len(records)


def _record_provider_usage(payload: Mapping[str, Any], actor: Any) -> None:
    from services.agent.providers import token_usage

    token_usage.record_usage(
        provider=_text(payload.get("provider")),
        model=_text(payload.get("model")),
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        total_tokens=payload.get("total_tokens"),
        context_tokens=payload.get("context_tokens"),
        source=_text(payload.get("source")) or "api",
        actor=actor,
    )


def _emit(
    on_event: Callable[..., None] | None, kind: str, payload: dict[str, Any]
) -> None:
    from services.agent.agent_events import emit_event

    emit_event(kind, payload, live=on_event, journal=None)


def _terminal_text(host_result: Mapping[str, Any]) -> str:
    value = host_result.get("result")
    if isinstance(value, str):
        return value
    terminal = host_result.get("terminal")
    if isinstance(terminal, Mapping):
        for key in ("text", "result", "message", "content"):
            candidate = terminal.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _offline_provider(
    role: str, options: Mapping[str, Any], settings: Mapping[str, Any], actor: Any
) -> Any:
    from services.agent import config as agent_config

    supplied = options.get("offline_provider")
    if supplied is not None:
        provider = supplied
    else:
        cfg_value = options.get("config")
        cfg = (
            dict(cfg_value)
            if isinstance(cfg_value, Mapping)
            else dict(agent_config.load_config() or {})
        )
        cfg["offline_mode"] = True
        provider_name = _text(settings.get("provider"))
        model = _text(settings.get("model"))
        if model:
            cfg["model"] = model
        provider = agent_config.get_provider(
            name=provider_name or None,
            cfg=cfg,
            role=role,
            **({"actor": actor} if actor is not None else {}),
        )
    if not agent_config.provider_offline_safe(provider):
        raise agent_config.OfflineModeError(
            "explicit offline role turn requires a loopback or deterministic provider"
        )
    return provider


def _run_offline(
    request: str,
    role: str,
    *,
    session_id: str,
    run_id: str,
    parent_id: str,
    on_event: Callable[..., None] | None,
    options: Mapping[str, Any],
    history: Any,
    attachments: Any,
    tier: Any,
    capability: Any,
    max_tokens: int | None,
    max_total_tokens: int | None,
    actor: Any,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    from services.agent.mission_dispatch import run_linear_role_turn
    from services.agent.providers import token_usage

    try:
        provider = _offline_provider(role, options, settings, actor)
    except Exception as exc:  # noqa: BLE001 - offline refusal is result data
        return _failure(
            role,
            "offline_unavailable",
            str(exc),
            run_id=run_id,
            session_id=session_id,
            parent_id=parent_id,
        )

    usage = _empty_usage()

    def usage_sink(payload: dict[str, Any]) -> None:
        _add_usage(usage, payload)
        _emit(on_event, "agent.provider_usage", dict(payload))

    token_usage.begin_turn_usage()
    sink_token = token_usage.bind_usage_sink(usage_sink)
    try:
        turn = run_linear_role_turn(
            request,
            role,
            provider,
            on_event=on_event,
            run_id=run_id,
            mission_dispatch=options.get("mission_dispatch"),
            capability=capability,
            tier=tier,
            max_tokens=max_tokens,
            max_total_tokens=max_total_tokens,
            tool_dispatch=options.get("tool_dispatch"),
            options=dict(options),
            history=list(history) if isinstance(history, list) else None,
            attachments=(
                [dict(item) for item in attachments if isinstance(item, Mapping)]
                if isinstance(attachments, list)
                else None
            ),
            chudware_actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - nested role turns return failures
        return _failure(
            role,
            "offline_error",
            f"{type(exc).__name__}: {exc}",
            run_id=run_id,
            session_id=session_id,
            parent_id=parent_id,
        )
    finally:
        token_usage.reset_usage_sink(sink_token)

    if not isinstance(turn, dict):
        return _failure(
            role,
            "offline_error",
            "local role loop returned a non-object result",
            run_id=run_id,
            session_id=session_id,
            parent_id=parent_id,
        )
    result = dict(turn)
    result.update(
        {
            "run_id": run_id,
            "session_id": session_id,
            "parent_id": parent_id,
            "usage": usage,
            "tokens_used": int(usage["total_tokens"]),
            "provider_usage_recorded": bool(usage["calls"]),
            "offline": True,
        }
    )
    result.setdefault("role", role)
    result.setdefault("cancelled", result.get("stop_reason") == "cancelled")
    return result


def run_role_turn(
    request: str,
    role: str,
    *,
    session_id: str,
    run_id: str | None = None,
    parent_id: str | None = None,
    on_event: Callable[..., None] | None = None,
    options: Mapping[str, Any] | None = None,
    history: list[dict] | None = None,
    attachments: list[dict] | None = None,
    tier: Any = None,
    capability: Any = None,
    max_tokens: int | None = None,
    max_total_tokens: int | None = None,
    actor: Any = None,
    close_session: bool = False,
) -> dict[str, Any]:
    """Run one CAD role turn through Chudcode, or the explicit local fallback.

    The returned object retains ``run_linear_role_turn`` compatibility
    (``success``, ``stop_reason``, ``result``, ``role``, ``allowed_tools``) and
    adds Chudcode lineage, usage, cancellation, and persisted-session metadata.
    """
    request_text = _text(request)
    role_name = _text(role)
    parent_session = _text(session_id)
    if not request_text:
        return _failure(role_name, "invalid_request", "role turn request is empty")
    if not role_name:
        return _failure(role_name, "invalid_role", "invalid_role")
    if not parent_session:
        return _failure(
            role_name, "invalid_session", "role turn session_id is required"
        )
    if on_event is not None and not callable(on_event):
        return _failure(role_name, "invalid_event_sink", "on_event must be callable")
    if isinstance(attachments, list) and len(attachments) > _ATTACHMENT_MAX:
        return _failure(role_name, "attachment_count_exceeded", "attachment_count_exceeded")

    opts = dict(options) if isinstance(options, Mapping) else {}
    if tenancy.in_cell() and opts.get("offline"):
        return _failure(role_name, "credential_isolation_unsupported", "credential_isolation_unsupported")
    if tenancy.in_cell() and any(key in opts for key in ("api_key", "web_credential", "byok_key")):
        return _failure(role_name, "credential_isolation_unsupported", "credential_isolation_unsupported")
    if opts.get("offline") is not True and (
        tenancy.in_cell() or not any(key in opts for key in ("web_key_source", "byok_key"))
    ):
        host = get_default_chudcode_host()
        inherit = getattr(host, "session_launch_params", None)
        if callable(inherit):
            try:
                inherited = inherit(parent_session)
                opts = {**inherited, **opts}
                if tenancy.in_cell():
                    for key in ("workspace_dir", "web_actor", "web_key_source", "web_tenant",
                                "web_provider", "web_provider_grant"):
                        if key in inherited:
                            opts[key] = inherited[key]
                    if inherited.get("plan_only") or inherited.get("mode") in ("ask", "plan"):
                        if inherited.get("mode") == "ask" or opts.get("mode") not in ("ask", "plan"):
                            opts["mode"] = inherited.get("mode") or "plan"
                        opts["plan_only"] = inherited.get("plan_only")
                    settings = dict(opts.get("settings") or {})
                    for key, value in (inherited.get("settings") or {}).items():
                        if key in ("provider", "model", "compaction_strategy", "compaction_settings"):
                            settings[key] = value
                            # _settings_for_role reads top-level overrides last.
                            # A child cannot replace the parent's authorized model.
                            opts.pop(key, None)
                    opts["settings"] = settings
            except KeyError:
                if tenancy.in_cell():
                    return _failure(role_name, "not_found", "not_found")
        elif tenancy.in_cell():
            return _failure(role_name, "not_found", "not_found")
    effective_run_id = _effective_run_id(parent_session, role_name, run_id, parent_id)
    lineage_parent = _text(parent_id) or parent_session
    child_session = _child_session_id(
        parent_session, effective_run_id, role_name, lineage_parent
    )
    # Descendants resolve this actual live child session, never its run id.
    opts["session_id"] = child_session
    try:
        card = load_role(role_name)
    except RoleError:
        return _failure(
            role_name,
            "invalid_role",
            "invalid_role",
            run_id=effective_run_id,
            session_id=child_session,
            parent_id=lineage_parent,
        )
    except (OSError, TypeError, ValueError) as exc:
        return _failure(
            role_name,
            "invalid_role",
            f"{type(exc).__name__}: {exc}",
            run_id=effective_run_id,
            session_id=child_session,
            parent_id=lineage_parent,
        )

    effective_tier, effective_capability = _resolve_tier_capability(
        tier, capability, opts
    )
    settings = _settings_for_role(role_name, opts)
    if opts.get("offline") is True:
        return _run_offline(
            request_text,
            role_name,
            session_id=child_session,
            run_id=effective_run_id,
            parent_id=lineage_parent,
            on_event=on_event,
            options=opts,
            history=history,
            attachments=attachments,
            tier=effective_tier,
            capability=effective_capability,
            max_tokens=max_tokens,
            max_total_tokens=max_total_tokens,
            actor=actor,
            settings=settings,
        )

    try:
        (
            system_prompt,
            allowed_tools,
            host_tools,
            _mission_dispatch,
            tool_context,
        ) = _assemble_role_runtime(
            card,
            role_name,
            effective_run_id,
            lineage_parent,
            opts,
            effective_tier,
            effective_capability,
            actor,
            on_event,
        )
    except Exception as exc:  # noqa: BLE001 - prompt/tool failures are clean results
        return _failure(
            role_name,
            "configuration_error",
            f"role prompt/tool assembly failed: {type(exc).__name__}: {exc}",
            run_id=effective_run_id,
            session_id=child_session,
            parent_id=lineage_parent,
        )

    resumed = bool(_text(opts.get("chudcode_session_file")))
    migration = _claim_history(child_session, history, resumed=resumed)
    prompt = (
        f"{migration}\n\nCURRENT USER REQUEST\n{request_text}"
        if migration
        else request_text
    )
    bounded_attachments = (
        [dict(item) for item in attachments if isinstance(item, Mapping)]
        if isinstance(attachments, list)
        else None
    )
    host_params: dict[str, Any] = {
        "request": prompt,
        "run_id": effective_run_id,
        "session_id": child_session,
        "system_prompt": system_prompt,
        "host_tools": host_tools,
        "settings": settings,
        "attachments": bounded_attachments,
        "register_callbacks": _register_category_refresh(tool_context, host_tools),
    }
    if max_tokens is not None:
        host_params["max_tokens"] = max_tokens
    if max_total_tokens is not None:
        host_params["max_total_tokens"] = max_total_tokens
    for key in (
        "workspace_dir",
        "chudcode_session_dir",
        "chudcode_session_file",
        "host_uris",
        "images",
        "mode", "plan_only", "turn_preferences",
        "web_actor", "web_key_source", "web_tenant", "web_provider", "web_credential",
        "web_provider_grant",
        "byok_key", "byok_tenant", "byok_provider",
    ):
        if key in opts and opts[key] is not None:
            host_params[key] = opts[key]

    usage = _empty_usage()
    event_context = {
        "run_id": effective_run_id,
        "session_id": child_session,
        "parent_id": lineage_parent,
        "role": role_name,
        "actor": role_name,
    }
    event_state = RpcFrameState()

    def raw_emit(frame: Any) -> None:
        raw = (
            frame
            if isinstance(frame, Mapping)
            else {
                "type": "unknown",
                "value": frame,
            }
        )
        for kind, mapped in map_rpc_frame(raw, event_context, event_state):
            payload = (
                dict(mapped) if isinstance(mapped, Mapping) else {"payload": mapped}
            )
            if raw.get("type") == "agent_end" and raw.get("reason") == "manager_stop":
                payload["control_delegate"] = dict(raw["control_delegate"])
            if kind == "agent.provider_usage":
                _add_usage(usage, payload)
                _record_provider_usage(payload, actor)
            _emit(on_event, kind, payload)

    host = None
    try:
        host = get_default_chudcode_host()
        host_result = host.run_turn(host_params, raw_emit)
        if not isinstance(host_result, Mapping):
            return _failure(
                role_name,
                "provider_error",
                "Agent session host returned a non-object response",
                run_id=effective_run_id,
                session_id=child_session,
                parent_id=lineage_parent,
                allowed_tools=allowed_tools,
            )
        if not host_result.get("ok"):
            return _failure(
                role_name,
                "cancelled" if host_result.get("cancelled") else "provider_error",
                _text(host_result.get("reason")) or "Agent role turn failed",
                run_id=effective_run_id,
                session_id=child_session,
                parent_id=lineage_parent,
                allowed_tools=allowed_tools,
            )
        cancelled = bool(host_result.get("cancelled"))
        calls, tool_calls = _bounded_tool_calls(tool_context.get("tool_calls"))
        result = {
            "success": not cancelled,
            "stop_reason": ("manager_stop" if host_result.get("reason") == "manager_stop"
                            else "cancelled" if cancelled else "completed"),
            "result": _terminal_text(host_result),
            "role": role_name,
            "allowed_tools": [
                _text(getattr(binding, "name", None))
                for binding in host_tools
                if _text(getattr(binding, "name", None))
            ],
            "run_id": host_result.get("run_id") or effective_run_id,
            "session_id": host_result.get("session_id") or child_session,
            "parent_id": lineage_parent,
            "chudcode_session_id": host_result.get("chudcode_session_id"),
            "chudcode_session_file": host_result.get("chudcode_session_file"),
            "terminal": host_result.get("terminal"),
            "cancelled": cancelled,
            "usage": usage,
            "tokens_used": int(usage["total_tokens"]),
            "provider_usage_recorded": bool(usage["calls"]),
            "calls": calls,
            "tool_calls": tool_calls,
            "offline": False,
        }
        if host_result.get("reason") == "manager_stop":
            result["control_delegate"] = dict(host_result["control_delegate"])
        return result
    except Exception as exc:  # noqa: BLE001 - nested role turns return failures
        return _failure(
            role_name,
            "provider_error",
            f"{type(exc).__name__}: {exc}",
            run_id=effective_run_id,
            session_id=child_session,
            parent_id=lineage_parent,
            allowed_tools=allowed_tools,
        )
    finally:
        if close_session:
            try:
                if host is not None:
                    if any(key in host_params for key in ("web_key_source", "byok_key")):
                        host.close_session(child_session, credential_params=host_params)
                    else:
                        host.close_session(child_session)
            finally:
                _release_history(child_session)


__all__ = ["run_role_turn"]
