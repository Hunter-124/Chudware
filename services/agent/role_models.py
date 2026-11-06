"""role_models.py — per-role model assignment.

Contract:
  * config.json ``ai_roles`` maps role → ``{provider, model}`` | ``"default"``
    for: advisor | task | planning | vision | default | plm | redaction.
  * Each role entry MAY also carry ``thinking``: ``"auto"|"low"|"medium"|"high"``
    (default ``"auto"``). Concrete levels pin that role's LLM reasoning effort;
    ``auto`` defers to the dispatcher's per-spawn effort, or to the runtime default.
  * ``resolve_role_model(role, cfg)`` honours ``ai_roles``. A legacy stored
    ``"auto"`` (ranked pick) is no longer resolved; it must be reassigned.
  * Redaction role HARD-refuses non-loopback providers.
  * ``advanced_overrides`` (config.json, default off) gates per-role overrides: while off
    every non-restricted role dispatches through its size tier (``ROLE_SIZES`` →
    ``model_sizes``) and stored ``ai_roles`` overrides are retained but ignored. Root chat and
    the restricted ``redaction``/``private`` seats are never gated.
  * An override is ``fixed``/``auto`` or ``{mode: "size", size, thinking?}`` (follow that
    tier's current assignment). Default (follow chat) is not an override for a tier role.
  * Each ``model_sizes[size]`` carries ``thinking``; a concrete tier effort applies to every
    role dispatched through that tier unless an enabled override sets its own thinking.

This module is the resolution seam for node_runner / mission_dispatch /
Settings RPCs. It does NOT construct Provider objects and does NOT own
``config.get_provider`` (DualWren / offline / compliance wrap).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
import re
from typing import Any, Mapping
from urllib.parse import urlparse

log = logging.getLogger(__name__)

AI_ROLES_KEY = "ai_roles"

# Assignment form beside a fixed {provider, model}: follow whatever model the chat
# composer is currently set to. Resolution yields an assignment with EMPTY
# provider/model and mode == SESSION_DEFAULT; the caller keeps its ambient
# provider rather than constructing one.
SESSION_DEFAULT = "default"

# Roles the Settings GUI / Contract expose. routing.yaml roles still resolve via
# routing.py; these are the *harness-level* role knobs (advisor/task/planning/...).
KNOWN_ROLES = (
    "chat",
    "worker",
    "private",
    "advisor",
    "task",
    "planning",
    "vision",
    "default",
    "plm",
    "redaction",
    "sketch2d",
    "model3d",
    "memory",
    "cam",
    "sim",
    "ecad",
    "drawing",
    "reveng",
)

# Specialist role ids → Settings model-picker keys (ai_roles.*).
ROLE_MODEL_KEY_ALIASES: dict[str, str] = {
    "cam_programmer": "cam",
    "sim_engineer": "sim",
    "ecad_engineer": "ecad",
    "pcb_engineer": "ecad",
    "layout_engineer": "ecad",
    "firmware_engineer": "ecad",
    "drawing_detailer": "drawing",
    "re_modeler": "reveng",
    "re_datasheet_reader": "reveng",
    "re_geometry_verifier": "reveng",
    "general_agent": "task",
    "geometry_designer": "model3d",
    "sketcher": "sketch2d",
    "spec_writer": "drawing",
    "feature_architect": "planning",
    "sourcing_specialist": "plm",
    "assembly_fit_engineer": "model3d",
    "fixer": "model3d",
    "feature_modeler": "model3d",
    "manufacturing_planner": "planning",
    "project_organizer": "planning",
    "reviewer": "vision",
    "overseer": "planning",
    "orchestrator": "planning",
    "linkage_planner": "planning",
    "intake_analyst": "default",
    "design_thinker": "planning",
    "clarifier": "task",
}

# Private seats are deliberately not mapped to cloud-capable size assignments.
ROLE_SIZES: dict[str, str] = {
    "chat": "large", "worker": "medium", "advisor": "large", "task": "medium",
    "planning": "large", "vision": "large", "default": "large", "plm": "small",
    "sketch2d": "medium", "model3d": "large", "memory": "small", "cam": "large",
    "sim": "large", "ecad": "large", "drawing": "medium", "reveng": "large",
}
ROLE_SIZES.update({alias: ROLE_SIZES[target] for alias, target in ROLE_MODEL_KEY_ALIASES.items()})
MODEL_SIZES = ("small", "medium", "large")
ASSIGNMENT_MODE_KEY = "assignment_mode"
MODEL_SIZES_KEY = "model_sizes"
# Master switch for per-role overrides (config.json key, missing = off).
ADVANCED_OVERRIDES_KEY = "advanced_overrides"
# Seats that never dispatch through a size tier and are never gated by the switch above.
RESTRICTED_ROLES = ("private", "redaction")

# Per-role reasoning / thinking effort levels (Settings → AI → Model roles).
THINKING_LEVELS = ("auto", "off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_THINKING = "auto"
CONCRETE_THINKING = frozenset(THINKING_LEVELS[1:])


def clamp_subagent_effort(level: Any) -> str | None:
    """Retain supported top rungs; model-specific clamping happens at the RPC boundary."""
    if level is None:
        return None
    value = str(level).strip().lower()
    return ("medium" if value == "med" else value) or None


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Provider cost class for assignment telemetry (lower = cheaper):
# subscription / free / local beat metered API keys.
_COST_CLASS: dict[str, int] = {
    "llamacpp": 0,
    "lmstudio": 0,
    "ollama": 0,
    "vllm": 1,
    "xai-sub": 2,
    "anthropic-sub": 2,
    "codex-sub": 2,
    "kimi-sub": 2,
    "claude-cli": 2,
    "codex-cli": 2,
    "xai": 5,
    "anthropic": 5,
    "openai": 5,
    "gemini": 5,
    "moonshot": 5,   # Kimi K3 metered frontier ($3/$15 per 1M)
    "openrouter": 4,  # free models may override via model id suffix
    "mock": 0,
}

# Max spawn depth defaults by capability letter (spawn-depth policy).
_MAX_SPAWN_DEPTH_BY_LETTER: dict[str, int] = {
    "A": 2,
    "B": 4,
    "C": 8,
}
DEFAULT_MAX_SPAWN_DEPTH = 8


class RoleModelError(RuntimeError):
    """Raised when a role cannot be resolved (e.g. redaction non-loopback)."""


class LocalOnlyRoleModelError(RoleModelError):
    """A configured model violates the user's local-only policy."""


@dataclass(frozen=True)
class RoleModelAssignment:
    """Resolved provider+model for one role (or session default)."""
    role: str
    provider: str
    model: str
    mode: str                       # "fixed" | "default"
    source: str = ""                # config section the assignment came from
    reason: str = ""

    @property
    def is_session_default(self) -> bool:
        """True when this role follows whatever model the chat composer is set to.

        Callers MUST keep their ambient provider instead of building one from
        ``provider``/``model`` — both are empty for this mode by construction.
        """
        return self.mode == SESSION_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "source": self.source,
            "reason": self.reason,
        }


def is_provider_unavailable_error(exc: BaseException | str | None) -> bool:
    """True when *exc* is a spending-limit / tier-denied / credits-exhausted class.

    SuperGrok returns ``personal-team-blocked:spending-limit`` as
    XaiTierDeniedError (HTTP 403); callers treat that as non-retryable.
    """
    if exc is None:
        return False
    text = f"{type(exc).__name__}: {exc}".lower() if not isinstance(exc, str) else exc.lower()
    markers = (
        "spending-limit",
        "spending limit",
        "personal-team-blocked",
        "run out of credits",
        "out of credits",
        "credits exhausted",
        "tier not api-enabled",
        "tier_denied",
        "xaitierdenied",
        "subscription tier not api-enabled",
        "need a grok subscription",
        "resource_exhausted",
        "quota exceeded",
        "insufficient credits",
        "payment required",
    )
    if any(m in text for m in markers):
        return True
    # Structured type without relying on import at call sites.
    name = type(exc).__name__ if not isinstance(exc, str) else ""
    if name in ("XaiTierDeniedError",):
        return True
    return False


# ---------------------------------------------------------------------------
# ai_roles config helpers
# ---------------------------------------------------------------------------


def _normalize_thinking(value: Any) -> str:
    """Coerce a thinking field to auto|low|medium|high (default auto)."""
    if value is None or value == "":
        return DEFAULT_THINKING
    s = str(value).strip().lower()
    if s in ("med",):
        return "medium"
    if s in THINKING_LEVELS:
        return s
    return s


def _normalize_role_value(value: Any) -> str | dict[str, str]:
    """Coerce a config ai_roles entry to ``"auto"``, ``"default"`` or a role dict.

    Dict form carries ``thinking`` ONLY when concrete (low|medium|high) — auto is
    the default and is omitted so legacy ``{provider, model}`` entries round-trip
    unchanged. When model is auto/default but thinking is concrete, return
    ``{mode: <mode>, thinking}`` so both knobs persist.
    ``{mode: "size", size, thinking?}`` references a size tier and carries neither provider
    nor model; an unknown size is unusable and degrades to Default.
    Legacy string ``"auto"`` / ``"provider/model"`` are accepted, as is the newer
    ``"default"`` (follow the chat composer's model).
    """
    if value is None or value == "" or value == "auto":
        return "auto"
    if isinstance(value, str):
        if value.strip().lower() == SESSION_DEFAULT:
            return SESSION_DEFAULT
        if "/" in value:
            prov, _, model = value.partition("/")
            return {"provider": prov.strip(), "model": model.strip()}
        return "auto"
    if isinstance(value, Mapping):
        thinking = _normalize_thinking(value.get("thinking"))
        mode = str(value.get("mode") or "").lower()
        prov = str(value.get("provider") or "").strip()
        model = str(value.get("model") or "").strip()
        # An explicit mode wins over the presence of stale provider/model keys, so
        # switching a role to Default in Settings does not silently stay pinned.
        if mode == "size":
            size = str(value.get("size") or "").strip().lower()
            if size not in MODEL_SIZES:
                return SESSION_DEFAULT
            ref: dict[str, str] = {"mode": "size", "size": size}
            if thinking != DEFAULT_THINKING:
                ref["thinking"] = thinking
            return ref
        if mode == SESSION_DEFAULT:
            if thinking == DEFAULT_THINKING:
                return SESSION_DEFAULT
            return {"mode": SESSION_DEFAULT, "thinking": thinking}
        is_auto = mode == "auto" or (not prov and not model)
        if is_auto:
            if thinking == DEFAULT_THINKING:
                return "auto"
            return {"mode": "auto", "thinking": thinking}
        out: dict[str, str] = {"provider": prov, "model": model}
        if thinking != DEFAULT_THINKING:
            out["thinking"] = thinking
        return out
    return "auto"

def get_assignment_mode(cfg: Mapping[str, Any] | None = None) -> str:
    cfg = cfg or {}
    mode = cfg.get(ASSIGNMENT_MODE_KEY)
    if mode in ("simple", "advanced"):
        return mode
    roles = cfg.get(AI_ROLES_KEY)
    if isinstance(roles, Mapping) and roles:
        return "advanced"
    return "simple"

def advanced_overrides_enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """Per-role overrides dispatch only while ``advanced_overrides`` is on (missing = off)."""
    return (cfg or {}).get(ADVANCED_OVERRIDES_KEY) is True


def _follows_chat(entry: Any) -> bool:
    """True for a normalized ``ai_roles`` entry in Default mode (follow the chat model)."""
    return entry == SESSION_DEFAULT or (isinstance(entry, Mapping) and entry.get("mode") == SESSION_DEFAULT)


def _stored_role_names(cfg: Mapping[str, Any] | None = None) -> set[str]:
    cfg = cfg or {}
    stored = cfg.get("active_role_overrides")
    if isinstance(stored, list):
        return {str(role) for role in stored}
    roles = cfg.get(AI_ROLES_KEY)
    return set(roles) if get_assignment_mode(cfg) == "advanced" and isinstance(roles, Mapping) else set()


def stored_role_overrides(cfg: Mapping[str, Any] | None = None) -> set[str]:
    """Roles that retain a stored override, whether or not the switch currently applies it.

    Default (follow chat) is not an override for a tier-dispatched role: such an entry, and the
    legacy ``"default"`` string, resolve exactly like no entry. Root chat and the restricted
    seats are read straight from ``ai_roles`` and keep their entries as stored.
    """
    roles = (cfg or {}).get(AI_ROLES_KEY)
    roles = roles if isinstance(roles, Mapping) else {}
    return {role for role in _stored_role_names(cfg)
            if role == "chat" or role in RESTRICTED_ROLES
            or not _follows_chat(_normalize_role_value(roles.get(role, SESSION_DEFAULT)))}


def active_role_overrides(cfg: Mapping[str, Any] | None = None) -> set[str]:
    """Roles whose stored override dispatch honours: the retained set while the switch is on."""
    return stored_role_overrides(cfg) if advanced_overrides_enabled(cfg) else set()


def legacy_mission_worker_fallback(cfg: Mapping[str, Any] | None = None) -> bool:
    """Whether untouched mission seats still borrow the ``worker`` override.

    Only ever true with overrides on: flag off is pure tier dispatch.
    """
    cfg = cfg or {}
    if not advanced_overrides_enabled(cfg):
        return False
    stored = cfg.get("legacy_mission_worker_fallback")
    if stored is None:
        return (get_assignment_mode(cfg) == "advanced"
                and "active_role_overrides" not in cfg
                and "worker" in stored_role_overrides(cfg))
    return bool(stored)


def configured_role_key(role: str, cfg: Mapping[str, Any] | None = None) -> str:
    """Keep an explicitly overridden specialist seat under its own key while overrides are on."""
    role_s = (role or "default").strip().lower() or "default"
    roles = (cfg or {}).get(AI_ROLES_KEY)
    if role_s in active_role_overrides(cfg) and isinstance(roles, Mapping) and role_s in roles:
        return role_s
    return ROLE_MODEL_KEY_ALIASES.get(role_s, role_s)


def _active_override(role: str, cfg: Mapping[str, Any] | None = None) -> str | dict[str, str] | None:
    """Normalized override entry dispatch honours for a configured, non-restricted *role*."""
    roles = (cfg or {}).get(AI_ROLES_KEY)
    if role in active_role_overrides(cfg) and isinstance(roles, Mapping) and role in roles:
        return _normalize_role_value(roles[role])
    return None


def _size_reference(entry: Any) -> str | None:
    """The size tier a normalized override entry follows, or None."""
    if isinstance(entry, Mapping) and entry.get("mode") == "size":
        return str(entry.get("size") or "") or None
    return None


def _size_wire(value: Any, *, with_thinking: bool = False) -> dict[str, str]:
    normalized = _normalize_role_value(value)
    if isinstance(normalized, Mapping) and normalized.get("mode") == "size":
        normalized = SESSION_DEFAULT  # a tier assignment cannot reference another size
    if isinstance(normalized, Mapping):
        mode = "fixed" if normalized.get("provider") and normalized.get("model") else str(normalized.get("mode") or "auto")
        wire = {"mode": mode, "provider": str(normalized.get("provider") or ""),
                "model": str(normalized.get("model") or "")}
    else:
        wire = {"mode": normalized, "provider": "", "model": ""}
    if with_thinking:
        wire["thinking"] = (_normalize_thinking(normalized.get("thinking"))
                            if isinstance(normalized, Mapping) else DEFAULT_THINKING)
    return wire


def get_model_sizes(cfg: Mapping[str, Any] | None = None) -> dict[str, dict[str, str]]:
    raw = (cfg or {}).get(MODEL_SIZES_KEY)
    raw = raw if isinstance(raw, Mapping) else {}
    return {size: _size_wire(raw.get(size, SESSION_DEFAULT), with_thinking=True) for size in MODEL_SIZES}


def _chat_role_key(cfg: Mapping[str, Any] | None = None) -> str:
    roles = (cfg or {}).get(AI_ROLES_KEY)
    if get_assignment_mode(cfg) == "advanced" and (not isinstance(roles, Mapping) or "chat" not in roles):
        return "default"
    return "chat"


def get_chat_assignment(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Effective root-chat assignment for display and online resolution."""
    if "chat_assignment" in (cfg or {}):
        return _size_wire(cfg["chat_assignment"])
    if get_assignment_mode(cfg) == "simple":
        large = get_model_sizes(cfg)["large"]
        return {key: large[key] for key in ("mode", "provider", "model")}
    return _size_wire(get_ai_roles(cfg)[_chat_role_key(cfg)])


def role_settings_view(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"roles": get_ai_roles(cfg), "known_roles": list(KNOWN_ROLES),
            "assignment_mode": get_assignment_mode(cfg), "model_sizes": get_model_sizes(cfg),
            "role_sizes": dict(ROLE_SIZES), "chat_assignment": get_chat_assignment(cfg),
            ADVANCED_OVERRIDES_KEY: advanced_overrides_enabled(cfg),
            # Retained overrides: stored state, whether or not the switch applies them.
            "active_role_overrides": sorted(stored_role_overrides(cfg)),
            "legacy_mission_worker_fallback": legacy_mission_worker_fallback(cfg),
            "legacy_mission_worker_fallback_exclusions": sorted(
                (cfg or {}).get("legacy_mission_worker_fallback_exclusions") or ())}


def _validate_assignment(value: Any, *, allow_thinking: bool, allow_size: bool = False) -> None:
    if isinstance(value, str):
        if value in ("auto", SESSION_DEFAULT) or ("/" in value and all(value.split("/", 1))):
            return
        raise ValueError("assignment must be auto, default, or a provider/model")
    if not isinstance(value, Mapping) or not value:
        raise ValueError("assignment must be a non-empty mapping")
    allowed = ({"mode", "provider", "model"} | ({"thinking"} if allow_thinking else set())
               | ({"size"} if allow_size else set()))
    if set(value) - allowed:
        raise ValueError("unknown assignment field")
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError("assignment fields must be strings")
    modes = ("auto", SESSION_DEFAULT, "fixed", "tier") + (("size",) if allow_size else ())
    if "mode" in value and value["mode"] not in modes:
        raise ValueError("invalid assignment mode")
    if "thinking" in value and value["thinking"] not in THINKING_LEVELS:
        raise ValueError("invalid thinking level")
    if value.get("mode") == "size":
        if value.get("size") not in MODEL_SIZES:
            raise ValueError("size assignment requires size small, medium or large")
    elif "size" in value:
        raise ValueError("size is only valid with mode size")


def _requests_tier(role: str, value: Any) -> bool:
    """True when a role update clears the role's override so it dispatches through its tier.

    ``tier`` always does; Default (follow chat) does too for every role except root chat and
    the restricted seats, which keep their own Default semantics.
    """
    if isinstance(value, Mapping) and value.get("mode") == "tier":
        return True
    if role == "chat" or role in RESTRICTED_ROLES:
        return False
    return value == SESSION_DEFAULT or (isinstance(value, Mapping) and value.get("mode") == SESSION_DEFAULT)


def update_role_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and persist all requested fields under one config lock."""
    from services.agent import config as agent_config

    keys = {"roles", "model_sizes", "assignment_mode", "chat_assignment", ADVANCED_OVERRIDES_KEY}
    if not keys.intersection(params):
        raise ValueError(
            "roles, model_sizes, chat_assignment, assignment_mode or advanced_overrides required")
    roles = params.get("roles")
    sizes = params.get("model_sizes")
    mode = params.get("assignment_mode")
    chat = params.get("chat_assignment")
    if "chat_assignment" in params:
        _validate_assignment(chat, allow_thinking=False)
        if _size_wire(chat)["mode"] == "fixed" and not all((_size_wire(chat)["provider"], _size_wire(chat)["model"])):
            raise ValueError("chat_assignment requires both provider and model")
    if "roles" in params and not isinstance(roles, Mapping):
        raise ValueError("roles must be a mapping")
    if "model_sizes" in params and not isinstance(sizes, Mapping):
        raise ValueError("model_sizes must be a mapping")
    if "assignment_mode" in params and mode not in ("simple", "advanced"):
        raise ValueError("assignment_mode must be simple or advanced")
    if ADVANCED_OVERRIDES_KEY in params and not isinstance(params[ADVANCED_OVERRIDES_KEY], bool):
        raise ValueError("advanced_overrides must be a boolean")
    for role, value in (roles or {}).items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError("invalid role")
        # Root chat is set through chat_assignment; the restricted seats stay on loopback models.
        sizeless = role == "chat" or role in RESTRICTED_ROLES
        if sizeless and isinstance(value, Mapping) and ("size" in value or value.get("mode") == "size"):
            raise ValueError(f"{role} cannot follow a model size")
        _validate_assignment(value, allow_thinking=True, allow_size=not sizeless)
    for size, value in (sizes or {}).items():
        if size not in MODEL_SIZES:
            raise ValueError("unknown model size")
        _validate_assignment(value, allow_thinking=True)

    with agent_config.config_lock():
        cfg = agent_config.read_config_uncached()
        updates: dict[str, Any] = {}
        # Snapshot the previously effective root selection before tier edits.
        previous_chat = get_chat_assignment(cfg)
        if ("chat_assignment" not in cfg and previous_chat["mode"] == SESSION_DEFAULT
                and (cfg.get("provider") or cfg.get("model"))):
            previous_chat = _size_wire({"provider": str(cfg.get("provider") or ""),
                                        "model": str(cfg.get("model") or "")})
        updates["chat_assignment"] = _size_wire(chat) if "chat_assignment" in params else previous_chat
        # Retained overrides are stored state, independent of the switch: a save with
        # overrides disabled must never drop them.
        active = stored_role_overrides(cfg)
        if "legacy_mission_worker_fallback" not in cfg:
            updates["legacy_mission_worker_fallback"] = (
                get_assignment_mode(cfg) == "advanced"
                and "active_role_overrides" not in cfg and "worker" in active
            )
        fallback_exclusions = set(cfg.get("legacy_mission_worker_fallback_exclusions") or ())
        updates["active_role_overrides"] = sorted(active)
        updates[ADVANCED_OVERRIDES_KEY] = (params[ADVANCED_OVERRIDES_KEY]
                                           if ADVANCED_OVERRIDES_KEY in params
                                           else advanced_overrides_enabled(cfg))
        if roles is not None:
            current = dict(cfg.get(AI_ROLES_KEY) or {})
            for role, value in roles.items():
                if isinstance(value, Mapping) and value.get("mode") == "fixed":
                    previous = current.get(role)
                    previous = previous if isinstance(previous, Mapping) else {}
                    if not (value.get("provider") or previous.get("provider")) or not (value.get("model") or previous.get("model")):
                        raise ValueError(f"{role} requires both provider and model")
            tier_roles = {role for role, value in roles.items() if _requests_tier(role, value)}
            effective_roles = {role: value for role, value in roles.items() if role not in tier_roles}
            for role in roles:
                if role in tier_roles:
                    fallback_exclusions.add(role)
                else:
                    fallback_exclusions.discard(role)
            updates["legacy_mission_worker_fallback_exclusions"] = sorted(fallback_exclusions)
            for role in roles:
                if role in tier_roles:
                    active.discard(role)
                else:
                    active.add(role)
            _apply_role_updates(current, effective_roles)
            for role in effective_roles:
                entry = current[role]
                if isinstance(entry, Mapping) and entry.get("mode") not in ("auto", SESSION_DEFAULT, "size"):
                    if not entry.get("provider") or not entry.get("model"):
                        raise ValueError(f"{role} requires both provider and model")
                if role in RESTRICTED_ROLES:
                    if entry == SESSION_DEFAULT or (isinstance(entry, Mapping) and entry.get("mode") == SESSION_DEFAULT):
                        raise ValueError(f"{role} cannot follow the chat model; it requires a loopback provider")
                    if isinstance(entry, Mapping) and entry.get("provider"):
                        assert_redaction_loopback(entry["provider"], cfg=cfg)
            updates[AI_ROLES_KEY] = current
            updates["active_role_overrides"] = sorted(active)
        if sizes is not None:
            current_sizes = get_model_sizes(cfg)
            for size, value in sizes.items():
                if isinstance(value, Mapping):
                    # Start from the stored tier so a model change keeps its thinking and a
                    # thinking-only update keeps its model.
                    merged = dict(current_sizes[size])
                    merged.update(value)
                    if value.get("mode") in ("auto", SESSION_DEFAULT):
                        merged.update(provider="", model="")
                    elif value.get("provider") or value.get("model"):
                        merged["mode"] = "fixed"
                    if merged["mode"] == "fixed" and (not merged["provider"] or not merged["model"]):
                        raise ValueError(f"{size} requires both provider and model")
                    current_sizes[size] = _size_wire(merged, with_thinking=True)
                else:
                    current_sizes[size] = {**_size_wire(value), "thinking": current_sizes[size]["thinking"]}
            updates[MODEL_SIZES_KEY] = current_sizes
        if mode is not None:
            updates[ASSIGNMENT_MODE_KEY] = mode
        agent_config.save_config(updates, replace=tuple(k for k in (AI_ROLES_KEY, MODEL_SIZES_KEY, "chat_assignment", "active_role_overrides") if k in updates))
        cfg.update(updates)
        return role_settings_view(cfg)


def _entry_thinking(entry: Any) -> str:
    return _normalize_thinking(entry.get("thinking")) if isinstance(entry, Mapping) else DEFAULT_THINKING


def _tier_thinking(size: str, cfg: Mapping[str, Any] | None = None) -> str:
    """Concrete effort configured on a size tier, else Auto."""
    level = get_model_sizes(cfg)[size].get("thinking", DEFAULT_THINKING)
    return level if level in CONCRETE_THINKING else DEFAULT_THINKING


def get_role_thinking(
    role: str,
    cfg: Mapping[str, Any] | None = None,
) -> str:
    """Return the configured thinking level for *role* (``auto`` or a concrete level).

    * Root chat and the restricted seats read their stored ``ai_roles`` entry, ungated and
      never from a tier.
    * Every other role uses an enabled override's own thinking. A size-reference override
      without its own thinking, and a role with no enabled override (switch off, Default or
      absent entry), take the thinking of the tier it dispatches through. A fixed or auto
      override without thinking stays ``auto`` (the dispatcher decides).

    Missing role / missing thinking → ``\"auto\"``. Never raises.
    """
    role_s = (role or "default").strip().lower() or "default"
    try:
        roles = get_ai_roles(cfg)
        if role_s == "chat" or role_s in RESTRICTED_ROLES:
            key = _chat_role_key(cfg) if role_s == "chat" else role_s
            if key not in _stored_role_names(cfg):
                return DEFAULT_THINKING
            entry = roles.get(key)
            if entry is None:
                entry = roles.get("default", "auto")
            return _entry_thinking(entry)
        key = configured_role_key(role_s, cfg)
        override = _active_override(key, cfg)
        if override is None:
            size = ROLE_SIZES.get(key, ROLE_SIZES["task"])
        else:
            own = _entry_thinking(override)
            size = _size_reference(override)
            if own != DEFAULT_THINKING or not size:
                return own
        return _tier_thinking(size, cfg)
    except Exception:  # noqa: BLE001
        return DEFAULT_THINKING


def resolve_role_thinking(
    role: str,
    cfg: Mapping[str, Any] | None = None,
    *,
    dispatch_effort: str | None = None,
) -> str | None:
    """Keep explicit effort exactly; otherwise let the runtime choose its default.

    A concrete role/tier effort (``get_role_thinking``) wins; an Auto role defers to the
    dispatcher's ``dispatch_effort``; with neither, ``None`` means pass nothing.
    """
    level = get_role_thinking(role, cfg)
    if level != DEFAULT_THINKING:
        return level
    if dispatch_effort:
        effort = _normalize_thinking(dispatch_effort)
        if effort != DEFAULT_THINKING:
            return effort
    return None


def get_ai_roles(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the full ai_roles map, with absent roles following the chat."""
    raw = (cfg or {}).get(AI_ROLES_KEY) or {}
    if not isinstance(raw, Mapping):
        raw = {}
    out: dict[str, Any] = {}
    for role in KNOWN_ROLES:
        out[role] = _normalize_role_value(raw.get(role, SESSION_DEFAULT))
    for k, v in raw.items():
        if k not in out:
            out[str(k)] = _normalize_role_value(v)
    return out


def _apply_role_updates(current: dict, roles_partial: Mapping[str, Any] | None) -> dict:
    """Apply *roles_partial* onto the *current* ai_roles map in place, and return it.

    Only roles named in *roles_partial* are touched; every other entry is left byte-identical
    so one role's save can never rewrite its siblings.
    """
    for role, value in (roles_partial or {}).items():
        role_s = str(role).strip()
        if not role_s:
            continue
        # Merge thinking onto existing entry when partial update omits model
        # fields but includes thinking (or vice versa).
        if isinstance(value, Mapping) and role_s in current:
            prev = current[role_s]
            if isinstance(prev, Mapping):
                merged = dict(prev)
                for k, v in value.items():
                    if v is not None and v != "":
                        merged[k] = v
                # An explicit mode clears any fixed provider/model, so flipping a
                # role to Auto or Default in Settings cannot leave a stale pin
                # behind on the entry (it would be inert, but it reads as if the
                # role were still bound to that model).
                mode_in = str(value.get("mode") or "").lower()
                if mode_in in ("auto", SESSION_DEFAULT):
                    merged.pop("provider", None)
                    merged.pop("model", None)
                    merged.pop("size", None)
                    merged["mode"] = mode_in
                elif mode_in == "size":
                    merged.pop("provider", None)
                    merged.pop("model", None)
                elif value.get("provider") or value.get("model"):
                    merged.pop("mode", None)
                    merged.pop("size", None)
                elif mode_in == "fixed":
                    merged.pop("size", None)
                current[role_s] = _normalize_role_value(merged)
                continue
        current[role_s] = _normalize_role_value(value)
    return current


def set_ai_roles(
    roles_partial: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Merge *roles_partial* into config ai_roles. Returns the full map.

    When *persist* is True the on-disk map is re-read INSIDE the config lock and only the
    roles named in *roles_partial* are touched. That matters because ``get_ai_roles``
    materializes an entry for every :data:`KNOWN_ROLES` member: a caller that hands us one
    of those expanded maps as *cfg* would otherwise persist roles the user never configured.
    Building from disk makes that structurally impossible — the write can only ever contain
    what was already on disk plus what the caller explicitly changed.

    Partial updates merge *thinking* onto the existing role entry when the caller only
    sends one side of the assignment.
    """
    if persist:
        from services.agent import config as agent_config  # noqa: PLC0415
        # One critical section for read → apply → write, so a concurrent writer cannot
        # land between our read of ai_roles and our write of it. config_lock is
        # re-entrant, so the nested save_config below shares this acquisition.
        with agent_config.config_lock():
            current = dict(agent_config.read_config_uncached().get(AI_ROLES_KEY) or {})
            _apply_role_updates(current, roles_partial)
            # `current` IS the whole ai_roles map, so it must REPLACE what is on disk.
            # A plain save_config deep-merges dict-valued keys, which would union each
            # role entry with the old one and resurrect the provider/model we just
            # cleared — the role would read as "default" while still carrying a pin.
            agent_config.save_config({AI_ROLES_KEY: current}, replace=(AI_ROLES_KEY,))
            return get_ai_roles(agent_config.load_config())

    current = dict((cfg or {}).get(AI_ROLES_KEY) or {})
    if not isinstance(current, dict):
        current = {}
    _apply_role_updates(current, roles_partial)
    merged_cfg = dict(cfg or {})
    merged_cfg[AI_ROLES_KEY] = current
    return get_ai_roles(merged_cfg)


def max_spawn_depth_for_capability(
    letter: str | None = None,
    *,
    cfg: Mapping[str, Any] | None = None,
    default: int = DEFAULT_MAX_SPAWN_DEPTH,
) -> int:
    """Return the spawn-depth policy knob for a capability letter / config.

    Precedence:
      1. config.json ``ai_roles_policy.max_spawn_depth`` (int)
      2. capability letter default (A=2, B=4, C=8)
      3. *default* (8)
    """
    if cfg:
        policy = cfg.get("ai_roles_policy") or {}
        if isinstance(policy, Mapping) and "max_spawn_depth" in policy:
            try:
                return max(0, int(policy["max_spawn_depth"]))
            except (TypeError, ValueError):
                pass
    if letter:
        return _MAX_SPAWN_DEPTH_BY_LETTER.get(str(letter).upper(), default)
    return default


def check_spawn_depth_policy(
    depth: int,
    *,
    letter: str | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate spawn-depth policy for a proposed child at *depth*.

    Returns ``{ok, max_spawn_depth, depth, reason}``. ``ok`` is False only when
    depth exceeds max_spawn_depth.
    """
    max_d = max_spawn_depth_for_capability(letter, cfg=cfg)
    if depth > max_d:
        return {
            "ok": False,
            "max_spawn_depth": max_d,
            "depth": depth,
            "reason": (
                f"spawn depth {depth} exceeds max_spawn_depth {max_d} "
                f"(capability {letter or 'default'})"
            ),
        }
    return {
        "ok": True,
        "max_spawn_depth": max_d,
        "depth": depth,
        "reason": "within policy",
    }


# ---------------------------------------------------------------------------
# Loopback / redaction hard floor
# ---------------------------------------------------------------------------


def host_is_loopback(base_url: str | None) -> bool:
    if not base_url:
        return False
    try:
        host = (urlparse(str(base_url)).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in _LOOPBACK_HOSTS


def provider_is_loopback(
    provider: str,
    *,
    base_url: str | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """True if *provider* is a local loopback-only backend."""
    name = (provider or "").strip().lower()
    if name == "mock":
        return not base_url or host_is_loopback(base_url)
    if name in ("llamacpp", "llama.cpp", "llama-cpp", "lmstudio", "ollama", "vllm"):
        if base_url:
            return host_is_loopback(base_url)
        key = "llamacpp" if name in ("llama.cpp", "llama-cpp") else name
        sub = (cfg or {}).get(key) or {}
        if isinstance(sub, Mapping) and sub.get("base_url"):
            return host_is_loopback(str(sub["base_url"]))
        if name in ("lmstudio", "ollama"):
            sub = (cfg or {}).get("openai") or {}
            if isinstance(sub, Mapping) and sub.get("base_url"):
                return host_is_loopback(str(sub["base_url"]))
        return True
    if name in (
        "anthropic", "openai", "gemini", "openrouter", "xai",
        "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub", "kimi-sub",
        "claude-cli", "codex-cli",
    ):
        return False
    return False


def assert_redaction_loopback(
    provider: str,
    *,
    base_url: str | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> None:
    """HARD floor: redaction role must resolve to a loopback provider."""
    if not provider_is_loopback(provider, base_url=base_url, cfg=cfg):
        raise RoleModelError(
            f"redaction role HARD-refuses non-loopback provider {provider!r}. "
            f"Configure ai_roles.redaction to a local (llamacpp/lmstudio/ollama/vllm) "
            f"model on 127.0.0.1/localhost."
        )


def cost_tier_for(provider: str, model: str = "") -> int:
    """Public cost class for assignment telemetry (lower = cheaper)."""
    p = (provider or "").lower()
    if p == "openrouter" and ":free" in (model or ""):
        return 1  # free tier beats subscription
    return _COST_CLASS.get(p, 9)


# ---------------------------------------------------------------------------
# mega-8: typed flat-refusal + cheap-leaf one-shot envelope (advisor Q2/Q3)
# ---------------------------------------------------------------------------

# Local / free tiers that must NOT run the multi-turn tool loop on leaf nodes.
_CHEAP_LEAF_COST_MAX = 1  # llamacpp/lmstudio/ollama=0, vllm/openrouter:free=1

# Routing telemetry: leaf one-shot schema/engine misses (process-local).
_LEAF_MISS_COUNT = 0


def leaf_miss_count() -> int:
    """How many cheap-leaf one-shots failed validation this process."""
    return int(_LEAF_MISS_COUNT)


def reset_leaf_miss_count() -> None:
    """Test helper — clear the leaf_miss counter."""
    global _LEAF_MISS_COUNT
    _LEAF_MISS_COUNT = 0


def record_leaf_miss(n: int = 1) -> int:
    """Increment leaf_miss routing telemetry; return the new total."""
    global _LEAF_MISS_COUNT
    _LEAF_MISS_COUNT = int(_LEAF_MISS_COUNT) + max(0, int(n or 0))
    return _LEAF_MISS_COUNT


def is_cheap_leaf_provider(provider: str | None, model: str = "") -> bool:
    """True when *provider*/*model* is local/tiny (cost_tier ≤ 1)."""
    if not provider:
        return False
    return cost_tier_for(str(provider), model or "") <= _CHEAP_LEAF_COST_MAX


def is_leaf_task_class(task_class: str | None) -> bool:
    """True for conservation leaf / simple report classes."""
    tc = (task_class or "").strip().lower()
    return tc in ("leaf", "simple", "simple_part")


# Typed refusal when a multi-domain PLAN/BUILD node stays flat (advisor Q2).
# Free-form ledger_note does NOT satisfy — schema is strict.
TYPED_REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["reason", "domains_considered", "why_flat"],
    "properties": {
        "reason": {"type": "string", "maxLength": 400},
        "domains_considered": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "why_flat": {"type": "string", "maxLength": 600},
    },
}

# Cheap-leaf one-shot fill-in envelope (advisor Q3 primary).
LEAF_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string", "maxLength": 800},
        "ops": {
            "type": "array",
            "items": {"type": "object"},
        },
        "no_op": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 600},
        "artifacts": {
            "type": "array",
            "items": {"type": "object"},
        },
    },
}


def _schema_validate(value: Any, schema: dict[str, Any], path: str = "root") -> list[str]:
    """Small dependency-free subset validator (type/required/properties/enum/array)."""
    errs: list[str] = []
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        for req in schema.get("required", []) or []:
            if req not in value:
                errs.append(f"{path}: missing required key {req!r}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                errs.extend(_schema_validate(value[key], sub, f"{path}.{key}"))
        return errs
    if stype == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array, got {type(value).__name__}"]
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < int(min_items):
            errs.append(f"{path}: array length {len(value)} < minItems {min_items}")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errs.extend(_schema_validate(item, item_schema, f"{path}[{i}]"))
        return errs
    if stype == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string, got {type(value).__name__}"]
        mx = schema.get("maxLength")
        if mx is not None and len(value) > int(mx):
            errs.append(f"{path}: string length {len(value)} > maxLength {mx}")
        return errs
    if stype == "boolean":
        if not isinstance(value, bool):
            return [f"{path}: expected boolean, got {type(value).__name__}"]
        return errs
    if stype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{path}: expected number, got {type(value).__name__}"]
        return errs
    if stype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{path}: expected integer, got {type(value).__name__}"]
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    return errs


def validate_typed_refusal(payload: Any) -> list[str]:
    """Strict validate a typed flat-refusal envelope. [] = valid.

    Free-form ledger notes / bare strings NEVER pass — domains_considered must
    be a non-empty array and reason/why_flat must be strings.
    """
    if not isinstance(payload, dict):
        return ["typed_refusal: expected object"]
    # Reject common free-form aliases that are not the typed envelope.
    if "domains_considered" not in payload or "why_flat" not in payload:
        return ["typed_refusal: missing required keys (reason, domains_considered, why_flat)"]
    errs = _schema_validate(payload, TYPED_REFUSAL_SCHEMA, "typed_refusal")
    # Extra: each domain tag must be a non-empty string.
    domains = payload.get("domains_considered")
    if isinstance(domains, list):
        if not domains:
            errs.append("typed_refusal.domains_considered: must be non-empty")
        for i, d in enumerate(domains):
            if not isinstance(d, str) or not d.strip():
                errs.append(f"typed_refusal.domains_considered[{i}]: empty domain tag")
    reason = payload.get("reason")
    if isinstance(reason, str) and not reason.strip():
        errs.append("typed_refusal.reason: empty")
    why = payload.get("why_flat")
    if isinstance(why, str) and not why.strip():
        errs.append("typed_refusal.why_flat: empty")
    return errs


def normalize_leaf_envelope(payload: Any) -> dict[str, Any] | None:
    """Shallow-normalize local-model garbage; return dict or None if not object."""
    if not isinstance(payload, dict):
        return None
    out = dict(payload)
    if out.get("reason") is None:
        out.pop("reason", None)
    if out.get("no_op") is None:
        out.pop("no_op", None)
    arts = out.get("artifacts")
    if isinstance(arts, list):
        norm_arts = []
        for a in arts:
            if isinstance(a, dict):
                norm_arts.append(a)
            elif isinstance(a, str) and a.strip():
                norm_arts.append({"name": a.strip()})
        out["artifacts"] = norm_arts
    ops = out.get("ops")
    if isinstance(ops, list):
        out["ops"] = [o for o in ops if isinstance(o, dict)]
    # mega-24 thrash: ollama leaves emit no_op=false + empty reason + empty/partial
    # ops, or no_op=true WITH non-empty ops (DELIVER DL2 leaf_oneshot_miss).
    # Prefer real ops; if only no_op=false garbage, coerce to no_op with a
    # default reason when goal is present (INTAKE already wrote manifest).
    # Do NOT invent reason for bare {goal, no_op:true} missing reason — that
    # remains a validation error (mega-8 contract).
    ops_list = out.get("ops") if isinstance(out.get("ops"), list) else []
    has_ops = len(ops_list) > 0
    no_op = out.get("no_op")
    reason = out.get("reason")
    if has_ops and no_op is True:
        # Live thrash (DELIVER DL2): no_op=true WITH ops — prefer ops always.
        # mega-8 test updated to match salvage (conflict is unusable either way).
        out.pop("no_op", None)
        out.pop("reason", None)
    elif not has_ops and no_op is False:
        goal = out.get("goal")
        if isinstance(goal, str) and goal.strip():
            out["no_op"] = True
            out["reason"] = (
                reason.strip() if isinstance(reason, str) and reason.strip()
                else "leaf oneshot: no concrete ops; goal recorded"
            )
    return out


def validate_leaf_envelope(payload: Any) -> list[str]:
    """Strict validate a cheap-leaf one-shot envelope. [] = valid.

    Must declare *goal*. Either:
      * ``ops`` is a non-empty array of objects, OR
      * ``no_op`` is true with a non-empty ``reason``.
    ``artifacts`` is optional (array of objects when present).

    mega-8: local models emit ``reason: null`` and string artifacts — normalize
    those before schema check so salvageable envelopes still pass.
    mega-24: also salvage no_op/ops conflicts (see normalize_leaf_envelope).
    """
    payload = normalize_leaf_envelope(payload)
    if payload is None:
        return ["leaf_envelope: expected object"]

    errs = _schema_validate(payload, LEAF_ENVELOPE_SCHEMA, "leaf_envelope")
    goal = payload.get("goal")
    if isinstance(goal, str) and not goal.strip():
        errs.append("leaf_envelope.goal: empty")
    ops = payload.get("ops")
    no_op = payload.get("no_op")
    reason = payload.get("reason")
    has_ops = isinstance(ops, list) and len(ops) > 0
    has_noop = no_op is True and isinstance(reason, str) and bool(reason.strip())
    if not has_ops and not has_noop:
        errs.append(
            "leaf_envelope: require non-empty ops[] OR (no_op=true + reason)"
        )
    if no_op is True and has_ops:
        # normalize should have cleared this; keep as hard fail if residual.
        errs.append("leaf_envelope: no_op=true conflicts with non-empty ops")
    return errs


def domain_tags_from_context(
    context_pack: Mapping[str, Any] | None = None,
    node_inputs: Mapping[str, Any] | None = None,
) -> list[str]:
    """Collect domain tags for multi-domain soft-gate decisions.

    Precedence: explicit ``domain_tags`` on pack/inputs → derived from
    task_class / deliverable kind / role stage hints.
    """
    pack = dict(context_pack or {})
    inputs = dict(node_inputs or pack.get("node_inputs") or {})
    raw = pack.get("domain_tags") or inputs.get("domain_tags") or []
    if isinstance(raw, (list, tuple)) and raw:
        out = [str(x).strip().lower() for x in raw if str(x).strip()]
        # de-dupe preserving order
        seen: set[str] = set()
        uniq: list[str] = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    tags: list[str] = []
    tc = str(pack.get("task_class") or inputs.get("task_class") or "").lower()
    kind = str(inputs.get("kind") or inputs.get("deliverable_kind") or "").lower()
    role = str(
        pack.get("role_id") or inputs.get("role") or pack.get("role") or ""
    ).lower()

    def _add(t: str) -> None:
        if t and t not in tags:
            tags.append(t)

    # Role-level multi-domain PLAN cards.
    if role in ("orchestrator", "layout_engineer"):
        _add("plan")
    if role == "layout_engineer":
        _add("layout")
        _add("assembly")
    if role in ("feature_modeler", "sketcher", "geometry_designer"):
        _add("geometry")
    if role in ("assembly_fit_engineer",):
        _add("assembly")
        _add("fit")
    if role in ("sim_engineer",):
        _add("analysis")
    if role == "ecad_engineer":
        _add("ecad")
        _add("analysis")
    if role == "pcb_engineer":
        _add("ecad")
        _add("pcb")
        _add("analysis")
    if role in ("firmware_engineer",):
        _add("firmware")
        _add("analysis")
    if role in ("cam_programmer",):
        _add("cam")
    if role in ("spec_writer",):
        _add("spec")
    # Task class / deliverable kind.
    if tc in ("assembly",) or kind == "assembly":
        _add("assembly")
        _add("geometry")
    if tc in ("multi_feature_part", "enclosure") or kind in ("part", "enclosure"):
        _add("geometry")
    if tc in ("analysis",) or kind == "analysis":
        _add("analysis")
    if tc in ("cam_program",) or kind == "cam_program":
        _add("cam")
    if kind in ("report", "drawing"):
        _add("report")

    # Explicit multi-domain list on the request (comma / space separated).
    multi = pack.get("domains") or inputs.get("domains")
    if isinstance(multi, str) and multi.strip():
        for part in multi.replace(";", ",").split(","):
            _add(part.strip().lower())
    elif isinstance(multi, (list, tuple)):
        for part in multi:
            _add(str(part).strip().lower())

    return tags


def is_multi_domain_plan_or_build(
    *,
    role_id: str | None = None,
    node_type: str | None = None,
    domain_tags: list[str] | None = None,
    task_class: str | None = None,
) -> bool:
    """True when the node is a PLAN or BUILD coordinator subject to the gate.

    Leaf workers (feature_modeler at pure geometry, reviewer, intake) are not
    gated — only multi-domain PLAN/BUILD ranks that must spawn or refuse.

    mega-8 live: mission_template stamps *every* worker ``type=design``
    (including n_spec_*), so node_type alone must NOT imply BUILD. SPECIFY
    (spec_writer) is free-form product work — not the delegate/typed-refusal
    forcing function (that targets PLAN/layout + multi-domain BUILD).
    """
    role = (role_id or "").strip().lower()
    ntype = (node_type or "").strip().lower()
    tags = list(domain_tags or [])
    tc = (task_class or "").strip().lower()

    # Never gate SPECIFY / report / intake workers, nor single-domain producing
    # ECAD/firmware BUILD legs (mega-11) — they execute tools flat, not spawn.
    if role in (
        "spec_writer", "reviewer", "intake_analyst", "clarifier",
        "overseer",  # join/audit — not a multi-domain coordinator
        "ecad_engineer", "pcb_engineer", "firmware_engineer",
    ):
        return False
    # Leaf report/verify classes never hit this gate.
    if tc in ("leaf", "simple", "simple_part"):
        return False

    plan_roles = {
        "orchestrator", "layout_engineer", "planning",
    }
    build_roles = {
        "feature_modeler", "assembly_fit_engineer", "geometry_designer",
    }
    # PLAN by coordinating role or explicit plan node type.
    is_plan = role in plan_roles or ntype == "plan"
    # BUILD only for real geometry/assembly workers — NOT every type=design
    # node (mega-8: n_spec_* are type=design + task_class multi_feature/assembly).
    is_build = role in build_roles or ntype == "build"
    if not (is_plan or is_build):
        return False

    # Gate only when multi-domain (≥2 tags). Single-domain leaves execute flat.
    if len(tags) >= 2:
        return True
    # PLAN roles with assembly/multi task_class are multi-domain even if tags
    # collapsed to one — layout/orchestrator always coordinate slices.
    if role in plan_roles and tc in ("assembly", "multi_feature_part", "unknown", ""):
        return True
    if role == "layout_engineer":
        return True
    return False


def extract_typed_refusal(payload: Any) -> dict[str, Any] | None:
    """Pull a typed refusal object from a handoff / agent result if valid.

    mega-9 a2 residual: models sometimes bury the envelope under free-form
    ``ledger_note`` text / decision payloads instead of handoff.outputs —
    salvage JSON objects that already match the schema.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [
        payload.get("typed_refusal"),
        payload.get("flat_refusal"),
        payload.get("delegation_refusal"),
    ]
    handoff = payload.get("handoff")
    if isinstance(handoff, dict):
        outs = handoff.get("outputs") if isinstance(handoff.get("outputs"), dict) else {}
        candidates.extend([
            handoff.get("typed_refusal"),
            outs.get("typed_refusal"),
            outs.get("flat_refusal"),
            outs.get("delegation_refusal"),
        ])
        # Whole outputs may itself be the envelope.
        candidates.append(outs if outs else None)
    # Direct envelope shape on payload.
    candidates.append(payload)
    # Scan tool call args for ledger_note bodies that embed the envelope.
    calls = payload.get("calls")
    if isinstance(calls, list):
        for c in calls:
            if not isinstance(c, dict):
                continue
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            for key in ("payload", "body", "text", "note", "content"):
                candidates.append(_maybe_json_obj(args.get(key)))
            candidates.append(_maybe_json_obj(c.get("result")))
    for c in candidates:
        if isinstance(c, dict) and not validate_typed_refusal(c):
            return {
                "reason": str(c["reason"]).strip(),
                "domains_considered": [
                    str(x).strip() for x in c["domains_considered"] if str(x).strip()
                ],
                "why_flat": str(c["why_flat"]).strip(),
            }
    return None


def _maybe_json_obj(value: Any) -> Any:
    """Parse a JSON object from str, or return dict as-is; else None."""
    if isinstance(value, dict):
        # Nested typed_refusal key.
        if "typed_refusal" in value and isinstance(value["typed_refusal"], dict):
            return value["typed_refusal"]
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or ("domains_considered" not in s and "why_flat" not in s):
        return None
    import json as _json  # noqa: PLC0415
    # Full parse.
    try:
        obj = _json.loads(s)
        if isinstance(obj, dict):
            if "typed_refusal" in obj and isinstance(obj["typed_refusal"], dict):
                return obj["typed_refusal"]
            return obj
    except Exception:  # noqa: BLE001
        pass
    # Substring salvage: first {...} containing the keys.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = _json.loads(s[start:end + 1])
            if isinstance(obj, dict):
                if "typed_refusal" in obj and isinstance(obj["typed_refusal"], dict):
                    return obj["typed_refusal"]
                return obj
        except Exception:  # noqa: BLE001
            return None
    return None


def parse_leaf_envelope_text(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse model text into a leaf envelope. Returns (obj|None, errors).

    mega-8 live: ollama often emits truncated / trailing-comma JSON inside a
    giant ops array. Prefer a full parse; on failure, salvage a minimal valid
    envelope from ``goal`` (+ no_op/reason) so axis C can still complete.
    """
    import json
    import re

    if not text or not str(text).strip():
        return None, ["leaf_envelope: empty response"]
    raw = str(text).strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    blob = m.group(1) if m else None
    if blob is None:
        m2 = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL | re.IGNORECASE)
        blob = m2.group(1) if m2 else None
    if blob is None:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            blob = raw[start : end + 1]
        elif start >= 0:
            # Truncated object with no closing brace (live ollama DL5).
            blob = raw[start:]
    if not blob:
        return None, ["leaf_envelope: no JSON object in response"]

    def _strip_trailing_commas(s: str) -> str:
        return re.sub(r",\s*([}\]])", r"\1", s)

    obj = None
    parse_errs: list[str] = []
    for candidate in (blob, _strip_trailing_commas(blob)):
        try:
            obj = json.loads(candidate)
            break
        except Exception as exc:  # noqa: BLE001
            parse_errs.append(str(exc))
            obj = None

    if obj is None:
        goal_m = re.search(r'"goal"\s*:\s*"((?:\\.|[^"\\])*)"', blob)
        if not goal_m:
            return None, [
                f"leaf_envelope: json parse error: "
                f"{parse_errs[-1] if parse_errs else 'unknown'}"
            ]
        goal = goal_m.group(1)
        try:
            goal = bytes(goal, "utf-8").decode("unicode_escape")
        except Exception:  # noqa: BLE001
            pass
        reason_m = re.search(r'"reason"\s*:\s*"((?:\\.|[^"\\])*)"', blob)
        reason = reason_m.group(1) if reason_m else (
            "partial JSON from leaf model; ops unparseable"
        )
        obj = {"goal": goal, "no_op": True, "reason": reason}
    norm = normalize_leaf_envelope(obj) or obj
    errs = validate_leaf_envelope(norm)
    if errs:
        return None, errs
    return norm, []



# ---------------------------------------------------------------------------
# Copilot mode → harness role (mode overhaul)
# ---------------------------------------------------------------------------

# agent→default, plan→planning, swarm→default,
# edit→task, ask→default (read-only tools; no dedicated role card).
_MODE_TO_HARNESS_ROLE: dict[str, str] = {
    "agent": "default",
    "plan": "planning",
    "swarm": "default",
    "edit": "task",
    "ask": "default",
}


def role_for_copilot_mode(mode: str | None) -> str:
    """Map a copilot ``options.mode`` string to a harness ``ai_roles`` key.

    Returns one of :data:`KNOWN_ROLES` (never empty). Unknown / empty mode →
    ``\"default\"`` (Agent).
    """
    m = (mode or "agent").strip().lower() or "agent"
    return _MODE_TO_HARNESS_ROLE.get(m, "default")

def harness_role_for_mission_role(role: str | None) -> str:
    """Map YAML/DAG mission roles to one canonical ``ai_roles`` seat."""
    role_s = (role or "").strip().lower()
    if role_s in ROLE_MODEL_KEY_ALIASES:
        return ROLE_MODEL_KEY_ALIASES[role_s]
    if role_s in KNOWN_ROLES:
        return role_s
    if role_s in ("overseer", "orchestrator", "planning"):
        return "planning"
    if role_s == "reviewer":
        return "vision"
    if role_s == "advisor":
        return "advisor"
    if role_s in ("default", "intake", "intake_analyst"):
        return "default"
    return "task"


def mission_role_key(role: str | None, cfg: Mapping[str, Any] | None = None) -> str:
    role_s = (role or "").strip().lower()
    roles = (cfg or {}).get(AI_ROLES_KEY)
    if role_s in active_role_overrides(cfg) and isinstance(roles, Mapping) and role_s in roles:
        return role_s
    return harness_role_for_mission_role(role)



def resolve_role_model(
    role: str,
    cfg: Mapping[str, Any] | None = None,
    *,
    turn_preferences: Any | None = None,
) -> RoleModelAssignment:
    """Resolve provider+model for *role* honouring config.json ``ai_roles``.

    Fixed assignments preserve their exact identity; Default follows the chat. A size-reference
    override resolves through that tier's current assignment. While ``advanced_overrides`` is
    off every non-restricted role resolves through its size tier and stored overrides are ignored.
    Legacy ranked Auto remains stored but must be reassigned explicitly.
    Private and redaction roles require an explicit verified loopback model.
    """
    role = configured_role_key((role or "default").strip().lower() or "default", cfg)
    cfg = cfg or {}
    from services.agent.preferences import resolve_preferences

    local_only = (turn_preferences.local_only if turn_preferences is not None
                  else resolve_preferences(cfg).local_only)
    restricted = role in RESTRICTED_ROLES
    if role == "chat":
        entry = get_chat_assignment(cfg)
        source = "chat_assignment"
    elif restricted:
        entry = get_ai_roles(cfg).get(role, SESSION_DEFAULT)
        source = "ai_roles"
    else:
        entry = _active_override(role, cfg)
        source = "ai_roles"
        size = _size_reference(entry) if entry is not None else ROLE_SIZES.get(role, ROLE_SIZES["task"])
        if entry is None or size:
            entry = get_model_sizes(cfg)[size]
            source = "model_sizes"
    mode = (str(entry.get("mode") or "").lower() if isinstance(entry, Mapping)
            else str(entry or "").strip().lower())
    if restricted and mode == "size":
        raise RoleModelError(f"{role} requires an explicit loopback provider and model")
    if mode == SESSION_DEFAULT:
        if restricted:
            raise RoleModelError(f"{role} requires an explicit loopback provider and model")
        return RoleModelAssignment(role, "", "", SESSION_DEFAULT, source=source,
                                   reason="Using the current chat model.")
    if isinstance(entry, Mapping):
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if provider and model and mode != "auto":
            if restricted:
                assert_redaction_loopback(provider, cfg=cfg)
            elif local_only and not _auto_local_provider(provider, cfg):
                raise LocalOnlyRoleModelError("This computer only requires a loopback model. Configure a local model in Accounts.")
            return RoleModelAssignment(role, provider, model, "fixed", source=source,
                                       reason=f"Using the configured {role} model.")
    raise RoleModelError(
        f"Previous ranked Auto selection for {role} needs a new assignment; "
        "choose a model or follow the chat model."
    )


def _auto_local_provider(provider, cfg, base_url=None):
    # Frozen offline table: cloud provider ids never become local by URL alone.
    name = str(provider).lower()
    if name in ("mock", "mock-roles", "base"):
        return True
    if name not in ("openai", "llamacpp", "lmstudio", "ollama", "vllm"):
        return False
    section = cfg.get(name) or {}
    url = base_url or section.get("base_url")
    if not url and name == "openai":
        url = cfg.get("base_url")
    return host_is_loopback(url) if url else name != "openai"

