"""Shared OMP execution for nested and advisory CAD role turns.

Online turns always use the process-global :class:`OmpSessionHost`.  The
legacy Python provider loop remains reachable only through an explicit
``options={"offline": True}`` request and only with an offline-safe provider.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import replace
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from services.agent.omp_events import RpcFrameState, map_rpc_frame
from services.agent.omp_host import get_default_omp_host
from services.agent.omp_tools import build_host_tools
from services.agent.roles import RoleError, load_role
from services.agent import request_context as tenancy

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
    # provider.  Resolve only the provider/model identifiers OMP needs.
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

    # Caller-supplied nested settings are explicit; inherited parent defaults are not.
    explicit_keys = options.get("_explicit_role_settings")
    if not isinstance(explicit_keys, (list, tuple, set)):
        explicit_keys = nested.keys() if isinstance(nested, Mapping) else ()
    explicit_model = any(_text(options.get(key)) or
                         (key in explicit_keys and _text(settings.get(key)))
                         for key in ("provider", "model"))
    from services.agent.role_models import (
        DEFAULT_THINKING, LocalOnlyRoleModelError, RoleModelError, get_role_thinking,
        mission_role_key, resolve_role_model,
    )
    if not tenancy.in_cell() and (not explicit_model or not _text(settings.get("provider")) or not _text(settings.get("model"))):
        try:
            role_key = mission_role_key(role, cfg)
            assignment = resolve_role_model(role_key, cfg=cfg)
            if assignment is not None and not assignment.is_session_default:
                for key in ("provider", "model"):
                    if not _text(options.get(key)) and key not in explicit_keys:
                        settings[key] = _text(getattr(assignment, key))
        except RoleModelError:
            raise
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            if role in ("private", "redaction"):
                raise
    effort = get_role_thinking(mission_role_key(role, cfg), cfg)
    explicit_effort = any(_text(options.get(key)) or
                          (key in explicit_keys and _text(settings.get(key)))
                          for key in ("thinking", "reasoning_effort"))
    if effort != DEFAULT_THINKING and not explicit_effort:
        settings["reasoning_effort"] = effort
    if not _text(settings.get("provider")) and _text(cfg.get("provider")):
        settings["provider"] = _text(cfg.get("provider"))
    if not _text(settings.get("model")) and _text(cfg.get("model")):
        settings["model"] = _text(cfg.get("model"))
    from services.agent.preferences import resolve_preferences
    from services.agent.role_models import _auto_local_provider

    if resolve_preferences(cfg).local_only and _text(settings.get("provider")) and not _auto_local_provider(settings["provider"], cfg):
        raise LocalOnlyRoleModelError("This computer only requires a loopback model. Configure a local model in Accounts.")
    if role in ("private", "redaction"):
        from services.agent.role_models import RoleModelError, assert_redaction_loopback

        if not _text(settings.get("provider")):
            raise RoleModelError(f"{role} requires a loopback provider")
        assert_redaction_loopback(_text(settings["provider"]), cfg=cfg)
    return {key: value for key, value in settings.items() if value is not None}


def _assemble_role_runtime(
    card: Any,
    role: str,
    run_id: str,
    parent_id: str,
    options: Mapping[str, Any],
    tier: Any,
    capability: Any,
    on_event: Callable[..., None] | None,
) -> tuple[str, list[str], list[Any], Any, dict[str, Any]]:
    from services.agent.mission_dispatch import MissionDispatch, role_prompt

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
        )
    system_prompt, role_allowed_tools = role_prompt(
        role,
        card=card,
        capability=capability,
    )
    # Nested callers may narrow a role card without mutating the process-global
    # registry.  Preserve the allowlist tri-state all the way into
    # omp_tools: absent uses the role policy, None is open, and an explicit
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
        "persist_tool_loads": True,
        "tool_load_tenant": _text(options.get("byok_tenant")),
        "restore_allowed_tools": allowed_tools if "allowed_tools" in options else None,
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
    tool_context: dict[str, Any], initial_tools: list[Any]
) -> Callable[[Any], None]:
    """Restore and publish the owning conversation's explicitly loaded CAD tools."""

    def register(client: Any) -> None:
        from services.agent.omp_tools import _bootstrap_names, _entry_load_mode
        from services.agent.tool_categories import _expand_token, resolve_tool_requests

        available = {_text(getattr(binding, "name", None)) for binding in initial_tools}
        bridge = getattr(client, "_omp_bridge", None)
        persistent = bool(tool_context.get("persist_tool_loads"))
        recorded: set[str] = set()
        restored: set[str] = set()
        authorized = set(available)
        scope = ""
        authority = ""
        if persistent:
            if bridge is None:
                raise RuntimeError("OMP CAD extension bridge is unavailable")
            workspace = _text(tool_context.get("workspace_dir"))
            scope_data = [
                _text(tool_context.get("session_id")),
                str(Path(workspace).expanduser().resolve()) if workspace else "",
                tenancy.owner_key(),
                _text(tool_context.get("tool_load_tenant")),
            ]
            if not scope_data[0]:
                raise ValueError("CAD tool loads require a conversation identity")
            scope = hashlib.sha256(json.dumps(scope_data, separators=(",", ":")).encode()).hexdigest()
            authority = hashlib.sha256(
                json.dumps(scope_data[1:], separators=(",", ":")).encode()
            ).hexdigest()
            role_card = tool_context.get("role_card")
            apply_policy = getattr(role_card, "apply_tool_policy", None)
            if callable(apply_policy):
                unset = object()

                class Capture:
                    allow: Any = unset

                    def set_policy(self, allow=None, **_kwargs):
                        self.allow = allow

                policy = Capture()
                apply_policy(policy)
                if policy.allow is unset:
                    raise RuntimeError("Cannot determine the role's CAD tool policy")
                if policy.allow is not None:
                    if isinstance(policy.allow, (str, bytes)) or not isinstance(policy.allow, Iterable):
                        raise RuntimeError("Invalid role CAD tool policy")
                    permitted: set[str] = set()
                    for token in policy.allow:
                        permitted.update(_expand_token(_text(token)))
                    authorized.intersection_update(permitted)
            explicit = tool_context.get("restore_allowed_tools")
            if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
                permitted: set[str] = set()
                for token in explicit:
                    permitted.update(_expand_token(_text(token)))
                authorized.intersection_update(permitted)
            result = bridge.call("cad_tool_loads_get",
                                 {"scope": scope, "authority": authority}, timeout=20.0)
            if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
                raise RuntimeError("Invalid persisted CAD tool loads")
            values = result["tools"]
            if any(not isinstance(name, str) or not name or len(name) > 256 for name in values):
                raise RuntimeError("Invalid persisted CAD tool loads")
            recorded = set(values)
            legacy_owner = tool_context.get("legacy_tool_loads_owner")
            if result.get("found") is False and result.get("claimed") is False and callable(legacy_owner):
                owner = legacy_owner()
                if owner is not None:
                    legacy = bridge.call("cad_tool_loads_legacy",
                                         {"scope": scope, "authority": authority, **owner}, timeout=20.0)
                    if not isinstance(legacy, Mapping) or not isinstance(legacy.get("tools"), list):
                        raise RuntimeError("Invalid legacy CAD tool loads")
                    values = legacy["tools"]
                    if any(not isinstance(name, str) or not name or len(name) > 256 for name in values):
                        raise RuntimeError("Invalid legacy CAD tool loads")
                    recorded = set(values) & authorized
                    if recorded:
                        bridge.call("cad_tool_loads_put",
                                    {"scope": scope, "authority": authority,
                                     "tools": sorted(recorded)}, timeout=20.0)
            restored = recorded & authorized
            if result.get("rebind") is True:
                bridge.call("cad_tool_loads_put",
                            {"scope": scope, "authority": authority,
                             "tools": sorted(recorded)}, timeout=20.0)
            selected = (set(_bootstrap_names(tool_context, list(available))) & available) | restored
            initial_tools[:] = [
                replace(binding, active=binding.name in selected,
                        load_mode=(_entry_load_mode(
                            binding.name, binding.metadata.get("registry_entry"))
                            if binding.name in selected else "discoverable"))
                for binding in initial_tools
            ]

        # Task sub-agents share this turn's category state.
        refresh_lock = threading.Lock()

        def enable_categories(values: Any) -> dict[str, Any]:
            nonlocal recorded
            with refresh_lock:
                incoming = [values] if isinstance(values, str) else list(values or ())
                before = (set(_bootstrap_names(tool_context, list(available))) & available) | restored
                categories = tool_context["categories"]
                previous = list(categories)
                for value in incoming:
                    name = _text(value)
                    if name and name not in categories:
                        categories.append(name)
                resolved, unknown = resolve_tool_requests(incoming)
                selected = (set(_bootstrap_names(tool_context, list(available))) & available) | restored
                updated = recorded | (resolved & selected & authorized)
                stored = persistent and updated != recorded
                try:
                    if stored:
                        bridge.call("cad_tool_loads_put",
                                    {"scope": scope, "authority": authority,
                                     "tools": sorted(updated)}, timeout=20.0)
                    if selected != before:
                        if bridge is None:
                            raise RuntimeError("OMP CAD extension bridge is unavailable")
                        bridge.publish_tools([
                            replace(binding, active=binding.name in selected,
                                    load_mode=(_entry_load_mode(
                                        binding.name, binding.metadata.get("registry_entry"))
                                        if binding.name in selected else "discoverable")).as_dict()
                            for binding in initial_tools
                        ])
                except Exception:
                    categories[:] = previous
                    if stored:
                        bridge.call("cad_tool_loads_put",
                                    {"scope": scope, "authority": authority,
                                     "tools": sorted(recorded)}, timeout=20.0)
                    raise
                recorded = updated
                result: dict[str, Any] = {
                    "ok": True,
                    "enabled": list(dict.fromkeys([*categories, *sorted(restored)])),
                    "added": sorted(selected - before),
                    "unknown": unknown,
                }
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


def _record_provider_usage(payload: Mapping[str, Any]) -> None:
    from services.agent.providers import token_usage

    token_usage.record_usage(
        provider=_text(payload.get("provider")),
        model=_text(payload.get("model")),
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        total_tokens=payload.get("total_tokens"),
        context_tokens=payload.get("context_tokens"),
        source=_text(payload.get("source")) or "api",
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
    role: str, options: Mapping[str, Any], settings: Mapping[str, Any]
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
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    from services.agent.mission_dispatch import run_linear_role_turn
    from services.agent.providers import token_usage

    try:
        provider = _offline_provider(role, options, settings)
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
    close_session: bool = False,
) -> dict[str, Any]:
    """Run one CAD role turn through OMP, or the explicit local fallback.

    The returned object retains ``run_linear_role_turn`` compatibility
    (``success``, ``stop_reason``, ``result``, ``role``, ``allowed_tools``) and
    adds OMP lineage, usage, cancellation, and persisted-session metadata.
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
    supplied_settings = opts.get("settings")
    # Empty model identifiers mean Default: retain the live parent assignment.
    supplied_settings = {
        key: value for key, value in supplied_settings.items()
        if key not in ("provider", "model") or value not in (None, "")
    } if isinstance(supplied_settings, Mapping) else {}
    opts["_explicit_role_settings"] = tuple(k for k, v in supplied_settings.items() if v not in (None, ""))
    if tenancy.in_cell() and opts.get("offline"):
        return _failure(role_name, "credential_isolation_unsupported", "credential_isolation_unsupported")
    if tenancy.in_cell() and any(key in opts for key in ("api_key", "byok_key")):
        return _failure(role_name, "credential_isolation_unsupported", "credential_isolation_unsupported")
    if opts.get("offline") is not True and (tenancy.in_cell() or "byok_key" not in opts):
        host = get_default_omp_host()
        inherit = getattr(host, "session_launch_params", None)
        if callable(inherit):
            try:
                inherited = inherit(parent_session)
                opts = {**inherited, **opts}
                parent_settings = inherited.get("settings")
                parent_settings = dict(parent_settings) if isinstance(parent_settings, Mapping) else {}
                opts["settings"] = {**parent_settings, **supplied_settings}
                if tenancy.in_cell():
                    if "workspace_dir" in inherited:
                        opts["workspace_dir"] = inherited["workspace_dir"]
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

    if history and not _text(opts.get("omp_session_file")):
        return _failure(
            role_name, "unsupported_history",
            "Cannot resume a role from visible text history; provide the original OMP session file.",
            run_id=effective_run_id, session_id=child_session, parent_id=lineage_parent,
        )
    prompt = request_text
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
    from services.mcp.daemon_bridge import current_agent_run_scope

    host_params["_hostd_doc_scope"] = current_agent_run_scope()
    if max_tokens is not None:
        host_params["max_tokens"] = max_tokens
    if max_total_tokens is not None:
        host_params["max_total_tokens"] = max_total_tokens
    for key in (
        "workspace_dir",
        "omp_session_dir",
        "omp_session_file",
        "host_uris",
        "images",
        "mode", "plan_only", "turn_preferences",
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
        # A separate OMP session: its usage/wait/todo views are not this conversation's.
        "delegate_session": True,
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
            if kind == "agent.provider_usage":
                _add_usage(usage, payload)
                _record_provider_usage(payload)
            _emit(on_event, kind, payload)

    host = None
    try:
        host = get_default_omp_host()
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
            "omp_session_id": host_result.get("omp_session_id"),
            "omp_session_file": host_result.get("omp_session_file"),
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
        if close_session and host is not None:
            if "byok_key" in host_params:
                host.close_session(child_session, credential_params=host_params)
            else:
                host.close_session(child_session)


__all__ = ["run_role_turn"]
