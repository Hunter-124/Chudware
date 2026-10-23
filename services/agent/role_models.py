"""role_models.py — per-role model assignment + auto ranking pick.

Contract:
  * config.json ``ai_roles`` maps role → ``{provider, model}`` | ``"auto"``
    for: advisor | task | planning | vision | default | plm | redaction.
  * Each role entry MAY also carry ``thinking``: ``"auto"|"low"|"medium"|"high"``
    (default ``"auto"``). Concrete levels pin that role's LLM reasoning effort;
    ``auto`` on default/main lets the prompt classifier assign effort, and on
    subagent roles lets the dispatcher assign effort from task complexity.
  * Rankings live in ``model_rankings.yaml`` (shipped) + measured leaderboard
    + user overrides. Precedence: user > measured > shipped.
  * ``resolve_role_model(role, cfg, ...)`` honours ``ai_roles``; ``"auto"`` picks
    the cheapest enabled provider meeting a difficulty-derived rank threshold.
  * Redaction role HARD-refuses non-loopback providers.

This module is the resolution seam for node_runner / mission_dispatch /
Settings RPCs. It does NOT construct Provider objects and does NOT own
``config.get_provider`` (DualWren / offline / compliance wrap).
"""
from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path
import re
from functools import lru_cache
import copy
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

RANKINGS_SCHEMA = "aicad.model_rankings/1"
AI_ROLES_KEY = "ai_roles"
# User overrides for rankings, read from config. Distinct from ai_roles.
USER_RANKINGS_KEY = "model_rankings"

# Third assignment form, beside "auto" (ranked pick) and a fixed {provider, model}:
# follow whatever model the chat composer is currently set to. Resolution yields an
# assignment with EMPTY provider/model and mode == SESSION_DEFAULT; the caller keeps
# its ambient provider rather than constructing one. This exists because "auto" ranks
# across every enabled provider and will happily route a subagent to a provider the
# user never picked (and whose key may be dead) while their chat sits on another.
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
}

# Per-role reasoning / thinking effort levels (Settings → AI → Model roles).
THINKING_LEVELS = ("auto", "low", "medium", "high", "xhigh", "max")
DEFAULT_THINKING = "auto"
CONCRETE_THINKING = frozenset(THINKING_LEVELS[1:])


def clamp_subagent_effort(level: Any) -> str | None:
    """Retain supported top rungs; model-specific clamping happens at the RPC boundary."""
    if level is None:
        return None
    value = str(level).strip().lower()
    return ("medium" if value == "med" else value) or None


def clamp_provider_effort(provider: Any) -> Any:
    """Normalize a legacy effort spelling on a child provider, preserving its parent.

    Richer rungs must reach the chosen model: the online RPC clamps against the
    catalog after selection, rather than capping every sub-agent at high.
    Return the original instance when no normalization is needed.
    """
    try:
        raw = getattr(provider, "reasoning_effort", None)
        clamped = clamp_subagent_effort(raw)
        if clamped is None or clamped == raw:
            return provider
        rebound = copy.copy(provider)
        setattr(rebound, "reasoning_effort", clamped)
        return rebound
    except Exception:  # noqa: BLE001 — provider selection must never raise here
        return provider

_RANKINGS_YAML = Path(__file__).resolve().parent / "model_rankings.yaml"
_DEFAULT_LEADERBOARD = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "leaderboard" / "leaderboard.json"
)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

# Cost preference for auto-pick: lower = preferred when ranks tie.
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

# TaskClass → base minimum rank threshold (1–5).
_TASK_CLASS_MIN_RANK: dict[str, int] = {
    "simple_part": 1,
    "simple": 1,          # alias used by auto_pick smoke / leaf prompts
    "leaf": 1,            # mega-7: VERIFY/DELIVER/INTAKE report nodes
    "modification": 2,
    "multi_feature_part": 3,
    "enclosure": 3,
    "assembly": 4,
    "cam_program": 4,
    "analysis": 4,
    "unknown": 3,
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


@dataclass(frozen=True)
class ModelRank:
    """One ranked model entry after merge."""
    model_id: str
    rank: int                       # 1..5
    source: str                     # shipped | user | measured
    notes: str = ""
    provider: str = ""              # optional preferred provider kind
    pass_rate: float | None = None  # measured only
    cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rank": int(self.rank),
            "source": self.source,
            "notes": self.notes,
        }
        if self.provider:
            d["provider"] = self.provider
        if self.pass_rate is not None:
            d["pass_rate"] = self.pass_rate
        if self.cycles:
            d["cycles"] = self.cycles
        return d


@dataclass(frozen=True)
class RoleModelAssignment:
    """Resolved provider+model for one role (or auto pick, or session default)."""
    role: str
    provider: str
    model: str
    mode: str                       # "fixed" | "auto" | "default"
    rank: int | None = None
    source: str = ""                # ranking source when auto
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
            "rank": self.rank,
            "source": self.source,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Rankings load / merge
# ---------------------------------------------------------------------------


def _clamp_rank(value: Any, default: int = 3) -> int:
    try:
        r = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, r))


def _load_shipped_rankings(path: Path | None = None) -> dict[str, ModelRank]:
    p = path or _RANKINGS_YAML
    out: dict[str, ModelRank] = {}
    if not p.exists():
        return out
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("role_models: failed to parse %s: %s", p, exc)
        return out
    for raw in data.get("models") or []:
        if not isinstance(raw, Mapping):
            continue
        mid = str(raw.get("id") or "").strip()
        if not mid:
            continue
        out[mid] = ModelRank(
            model_id=mid,
            rank=_clamp_rank(raw.get("rank"), 3),
            source=str(raw.get("source") or "shipped"),
            notes=str(raw.get("notes") or ""),
            provider=str(raw.get("provider") or ""),
        )
    return out

@lru_cache(maxsize=1)
def _family_rank_rules() -> tuple[tuple[re.Pattern[str], int, str], ...]:
    """Shipped family rules complement exact user/measured/shipped model rankings."""
    try:
        data = yaml.safe_load(_RANKINGS_YAML.read_text(encoding="utf-8")) or {}
        return tuple(
            (re.compile(str(row["pattern"])), _clamp_rank(row["rank"]), str(row.get("provider") or ""))
            for row in data.get("families", [])
            if isinstance(row, Mapping) and row.get("pattern") and row.get("rank")
        )
    except (OSError, ValueError, yaml.YAMLError, re.error) as exc:
        log.warning("role_models: failed to load family rankings: %s", exc)
        return ()


def _rank_for(model: str, ranks: Mapping[str, ModelRank]) -> ModelRank | None:
    """Exact rows win; future canonical aliases inherit a family rank."""
    bare = model.rsplit("/", 1)[-1]
    exact = ranks.get(model) or (ranks.get(bare) if bare != model else None)
    if exact is not None:
        return exact
    for pattern, rank, provider in _family_rank_rules():
        if pattern.fullmatch(bare):
            return ModelRank(model, rank, "shipped", "catalog family rank", provider)
    return None


def _measured_rank_from_pass_rate(pass_rate: float) -> int:
    """Map leaderboard pass_rate → rank band (1–5).

    Conservative: perfect pass → 5, zero → demote toward 1. Measured ranks
    only apply when cycles >= 1 (caller checks).
    """
    pr = max(0.0, min(1.0, float(pass_rate)))
    if pr >= 0.85:
        return 5
    if pr >= 0.65:
        return 4
    if pr >= 0.40:
        return 3
    if pr >= 0.15:
        return 2
    return 1


# Frontier model id stems: measured C1 gym demotions must not collapse live
# mission auto_pick to ollama (mega-13: grok-4.5 pass_rate=0 → rank 1 → all
# BUILD nodes fell back to llama3.2). Keep a floor so subscription/API frontier
# remains eligible for min_rank≥3/4 analysis/assembly work.
# mega-14: floor raised 4→5. Evidence: mega-13 attempt-1 measured rank=1 wiped
# all nodes to ollama; floor=4 still required harness user override rank=5 for
# analysis min_rank paths that pick frontier only; residual "rank floor ≥5"
# noted in mega-13 findings TOP residual #4. Rationale: frontier API ids must
# remain rank 5 so min_rank=4/5 analysis nodes select xai-sub/grok-4.5 without
# per-run user_rankings pins. Leaves (min_rank=1) still prefer ollama.
# mega-16: openrouter paid deepseek/deepseek-chat-v3.1 is the live frontier when
# xAI is spending-limit-blocked — floor it so analysis min_rank does not collapse
# to ollama-all (axis Y).
_FRONTIER_RANK_FLOOR_IDS = frozenset({
    "grok-4.5", "grok-4-5",
    "claude-opus-4-8", "claude-sonnet-5",
    "gpt-6-sol", "gpt-5.6-sol", "gemini-2.5-pro",
    "kimi-k3",
    "deepseek/deepseek-chat-v3.1", "deepseek-chat-v3.1",
    "deepseek/deepseek-chat-v3", "deepseek-chat-v3",
})
_FRONTIER_MEASURED_FLOOR = 5

# Process-local provider unavailability (mega-16): spending-limit / tier-denied
# marks xai-sub so auto_pick falls through instead of retry-looping SuperGrok.
_UNAVAILABLE_PROVIDERS: set[str] = set()
_UNAVAILABLE_PROVIDER_REASONS: dict[str, str] = {}


def mark_provider_unavailable(provider: str, reason: str = "") -> None:
    """Mark *provider* unavailable for the rest of this process (mega-16).

    Used when xAI returns spending-limit / tier-denied so subsequent
    ``auto_pick_model`` calls fall through to the next ranked enabled provider
    instead of retry-looping SuperGrok on every hard node.
    """
    p = (provider or "").strip().lower()
    if not p:
        return
    _UNAVAILABLE_PROVIDERS.add(p)
    if reason:
        _UNAVAILABLE_PROVIDER_REASONS[p] = str(reason)[:300]
    # Spending-limit on either xai path blocks both classic and subscription.
    if p in ("xai", "xai-sub"):
        _UNAVAILABLE_PROVIDERS.add("xai")
        _UNAVAILABLE_PROVIDERS.add("xai-sub")
        if reason:
            _UNAVAILABLE_PROVIDER_REASONS.setdefault("xai", str(reason)[:300])
            _UNAVAILABLE_PROVIDER_REASONS.setdefault("xai-sub", str(reason)[:300])
    log.info("role_models: provider %s marked unavailable (%s)", p, reason or "no reason")


def clear_unavailable_providers() -> None:
    """Test helper — clear process-local unavailable set."""
    _UNAVAILABLE_PROVIDERS.clear()
    _UNAVAILABLE_PROVIDER_REASONS.clear()


def is_provider_unavailable(provider: str | None) -> bool:
    """True when *provider* was marked unavailable this process."""
    p = (provider or "").strip().lower()
    return bool(p) and p in _UNAVAILABLE_PROVIDERS


def unavailable_providers() -> dict[str, str]:
    """Snapshot of unavailable providers → reason (for harness telemetry)."""
    return {p: _UNAVAILABLE_PROVIDER_REASONS.get(p, "") for p in sorted(_UNAVAILABLE_PROVIDERS)}


def is_provider_unavailable_error(exc: BaseException | str | None) -> bool:
    """True when *exc* is a spending-limit / tier-denied / credits-exhausted class.

    mega-16: SuperGrok returns ``personal-team-blocked:spending-limit`` as
    XaiTierDeniedError (HTTP 403). Treat as provider-unavailable for auto_pick
    fall-through — do NOT retry-loop xai.
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


def _openrouter_key_present(cfg: Mapping[str, Any] | None) -> bool:
    """True when an OpenRouter API key is configured (any supported location).

    mega-16: keys are commonly stored under ``config.keys.openrouter`` (GUI
    keys map) rather than ``openrouter.api_key`` — honour both so paid frontier
    auto_pick works without a harness-only env pin.
    """
    cfg = cfg or {}
    or_cfg = cfg.get("openrouter") or {}
    if isinstance(or_cfg, Mapping) and or_cfg.get("api_key"):
        return True
    if cfg.get("openrouter_api_key"):
        return True
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    keys = cfg.get("keys") or {}
    if isinstance(keys, Mapping) and keys.get("openrouter"):
        return True
    return False



def _load_measured_rankings(
    leaderboard_path: Path | None = None,
) -> dict[str, ModelRank]:
    p = leaderboard_path or _DEFAULT_LEADERBOARD
    out: dict[str, ModelRank] = {}
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("role_models: failed to parse leaderboard %s: %s", p, exc)
        return out
    for entry in data.get("models") or []:
        if not isinstance(entry, Mapping):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        try:
            cycles = int(entry.get("cycles") or 0)
        except (TypeError, ValueError):
            cycles = 0
        if cycles < 1:
            continue
        primary = entry.get("primary") or {}
        try:
            pass_rate = float(primary.get("pass_rate") or 0.0)
        except (TypeError, ValueError):
            pass_rate = 0.0
        rank = _measured_rank_from_pass_rate(pass_rate)
        out[mid] = ModelRank(
            model_id=mid,
            rank=rank,
            source="measured",
            notes=f"leaderboard pass_rate={pass_rate:.3f} cycles={cycles}",
            provider=str(entry.get("provider") or ""),
            pass_rate=pass_rate,
            cycles=cycles,
        )
    return out


def _load_user_rankings(cfg: Mapping[str, Any] | None) -> dict[str, ModelRank]:
    if not cfg:
        return {}
    raw = cfg.get(USER_RANKINGS_KEY) or {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, ModelRank] = {}
    for mid, entry in raw.items():
        mid_s = str(mid).strip()
        if not mid_s:
            continue
        if isinstance(entry, Mapping):
            out[mid_s] = ModelRank(
                model_id=mid_s,
                rank=_clamp_rank(entry.get("rank"), 3),
                source="user",
                notes=str(entry.get("notes") or "user override"),
                provider=str(entry.get("provider") or ""),
            )
        else:
            out[mid_s] = ModelRank(
                model_id=mid_s,
                rank=_clamp_rank(entry, 3),
                source="user",
                notes="user override",
            )
    return out


def load_rankings(
    cfg: Mapping[str, Any] | None = None,
    *,
    rankings_path: Path | None = None,
    leaderboard_path: Path | None = None,
    include_measured: bool = True,
) -> dict[str, ModelRank]:
    """Merge rankings with precedence user > measured > shipped.

    Returns ``{model_id: ModelRank}``.
    """
    merged: dict[str, ModelRank] = dict(_load_shipped_rankings(rankings_path))
    if include_measured:
        for mid, mr in _load_measured_rankings(leaderboard_path).items():
            base = merged.get(mid)
            notes = mr.notes
            rank = int(mr.rank)
            # mega-13/14: do not let zero-pass gym rows demote frontier models
            # below the frontier band (floor=5). analysis min_rank 4–5 must still
            # select xai-sub/grok-4.5 without per-run user pins.
            # mega-16: same floor for openrouter paid deepseek frontier.
            mid_l = str(mid).strip().lower()
            family_frontier = any(
                rank_value == _FRONTIER_MEASURED_FLOOR and pattern.fullmatch(mid_l)
                for pattern, rank_value, _provider in _family_rank_rules()
            )
            if (
                family_frontier
                or mid_l in _FRONTIER_RANK_FLOOR_IDS
                or any(mid_l.startswith(stem) for stem in (
                    "grok-4", "claude-sonnet-4", "claude-opus-4", "gpt-5",
                    "gemini-2.5-pro", "deepseek/deepseek-chat", "deepseek-chat",
                ))
            ):
                if rank < _FRONTIER_MEASURED_FLOOR:
                    notes = (
                        f"{notes}; frontier floor rank>={_FRONTIER_MEASURED_FLOOR} "
                        f"(was measured {rank}; mega-14 auto_pick)"
                    )
                    rank = _FRONTIER_MEASURED_FLOOR
            if base is not None and base.source == "shipped":
                notes = f"{notes} (was shipped rank {base.rank})"
            merged[mid] = ModelRank(
                model_id=mid,
                rank=rank,
                source="measured",
                notes=notes,
                provider=mr.provider or (base.provider if base else ""),
                pass_rate=mr.pass_rate,
                cycles=mr.cycles,
            )
    for mid, mr in _load_user_rankings(cfg).items():
        base = merged.get(mid)
        merged[mid] = ModelRank(
            model_id=mid,
            rank=mr.rank,
            source="user",
            notes=mr.notes or "user override",
            provider=mr.provider or (base.provider if base else ""),
            pass_rate=base.pass_rate if base else None,
            cycles=base.cycles if base else 0,
        )
    return merged


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
    return DEFAULT_THINKING


def _normalize_role_value(value: Any) -> str | dict[str, str]:
    """Coerce a config ai_roles entry to ``"auto"``, ``"default"`` or a role dict.

    Dict form carries ``thinking`` ONLY when concrete (low|medium|high) — auto is
    the default and is omitted so legacy ``{provider, model}`` entries round-trip
    unchanged. When model is auto/default but thinking is concrete, return
    ``{mode: <mode>, thinking}`` so both knobs persist.
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


def get_role_thinking(
    role: str,
    cfg: Mapping[str, Any] | None = None,
) -> str:
    """Return the configured thinking level for *role* (auto|low|medium|high).

    Missing role / missing thinking → ``\"auto\"``. Never raises.
    """
    role_s = (role or "default").strip().lower() or "default"
    try:
        roles = get_ai_roles(cfg)
        entry = roles.get(role_s)
        if entry is None:
            entry = roles.get("default", "auto")
        if isinstance(entry, Mapping):
            return _normalize_thinking(entry.get("thinking"))
        return DEFAULT_THINKING
    except Exception:  # noqa: BLE001
        return DEFAULT_THINKING


def resolve_role_thinking(
    role: str,
    cfg: Mapping[str, Any] | None = None,
    *,
    request: str | None = None,
    task_class: str | None = None,
    node_depth: int = 0,
    risk: str | None = None,
    min_rank: int | None = None,
    dispatch_effort: str | None = None,
    rank: int | None = None,
) -> str | None:
    """Resolve concrete reasoning_effort for *role*, or None to leave unset.

    Semantics:
      * concrete low|medium|high on the role → that level.
      * auto on default/main → classifier assigns from *request* / *task_class*
        (prompt complexity). Falls back to medium when no signal.
      * auto on subagent roles (task/planning/advisor/plm/redaction/…) → use
        *dispatch_effort* when the dispatcher set one; else complexity heuristics
        from task_class / rank / depth; default medium.
      * Absent config → auto → same paths (current behavior extended, not broken).

    Returns None only when intentionally leaving provider default (should be rare;
    auto paths return a concrete level, defaulting to medium).
    """
    if role in ("chat", "worker", "private"):
        from services.agent.preferences import TASK_CLASS_KINDS, resolve_turn_preferences

        return resolve_turn_preferences(request or "", cfg=cfg,
            task_kind=TASK_CLASS_KINDS.get(task_class)).thinking
    role_s = (role or "default").strip().lower() or "default"
    level = get_role_thinking(role_s, cfg)
    if level in CONCRETE_THINKING:
        return level

    # Auto path.
    if role_s == "default":
        return classify_prompt_thinking(
            request or "",
            task_class=task_class,
        )

    # Subagent / other roles: dispatcher override first.
    if dispatch_effort is not None:
        de = _normalize_thinking(dispatch_effort)
        if de in CONCRETE_THINKING:
            return de

    return classify_subagent_thinking(
        task_class=task_class,
        node_depth=node_depth,
        risk=risk,
        min_rank=min_rank,
        rank=rank,
        brief=request,
    )


def classify_prompt_thinking(
    request: str,
    *,
    task_class: str | None = None,
) -> str:
    """Assign thinking effort for the main/default chat role (Auto).

    Extends the workflow_router task classifier with an effort field:
    simple → low, multi-feature/mod → medium, assembly/cam/analysis → high.
    """
    cls = (task_class or "").strip().lower()
    if not cls:
        try:
            from services.agent.pipeline.workflow_router import (  # noqa: PLC0415
                classify_task,
            )
            cls = classify_task(request or "").value
        except Exception:  # noqa: BLE001
            cls = "unknown"
    return _task_class_to_thinking(cls, request=request)


def classify_subagent_thinking(
    *,
    task_class: str | None = None,
    node_depth: int = 0,
    risk: str | None = None,
    min_rank: int | None = None,
    rank: int | None = None,
    brief: str | None = None,
) -> str:
    """Dispatcher-side effort for subagent roles when thinking is Auto.

    Prefer explicit complexity signals; fall back to medium.
    """
    # Rank-based (matches existing mission_dispatch / node_runner heuristics).
    try:
        r = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        r = None
    try:
        mr = int(min_rank) if min_rank is not None else None
    except (TypeError, ValueError):
        mr = None
    eff_rank = r if r is not None else mr
    if eff_rank is not None:
        if eff_rank <= 2:
            return "low"
        if eff_rank <= 3:
            return "medium"
        return "high"

    cls = (task_class or "").strip().lower()
    if not cls and brief:
        try:
            from services.agent.pipeline.workflow_router import (  # noqa: PLC0415
                classify_task,
            )
            cls = classify_task(brief).value
        except Exception:  # noqa: BLE001
            cls = ""
    if cls:
        return _task_class_to_thinking(cls, request=brief or "")

    risk_s = (risk or "").strip().lower()
    if risk_s in ("high", "critical"):
        return "high"
    if risk_s in ("low",):
        return "low"

    # Deeper spawns tend to be narrower — slightly lower effort.
    try:
        depth = int(node_depth or 0)
    except (TypeError, ValueError):
        depth = 0
    if depth >= 3:
        return "low"

    return "medium"


def _task_class_to_thinking(task_class: str, *, request: str = "") -> str:
    """Map a TaskClass string to low|medium|high."""
    cls = (task_class or "unknown").strip().lower()
    if cls in ("simple_part", "simple", "leaf"):
        return "low"
    if cls in ("modification", "multi_feature_part", "unknown"):
        # Unknown: peek at request length / complexity keywords.
        if cls == "unknown" and request:
            lo = request.lower()
            if any(k in lo for k in (
                "assembl", "fea", "cfd", "cam", "mission", "full design",
                "optimize", "prove", "validate",
            )):
                return "high"
            if len(request) < 80 and not any(
                k in lo for k in ("and then", "then ", "also ", "with ")
            ):
                return "low"
        return "medium"
    if cls in ("enclosure",):
        return "medium"
    if cls in ("assembly", "cam_program", "analysis"):
        return "high"
    return "medium"


def get_ai_roles(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the full ai_roles map (missing roles default to ``\"auto\"``)."""
    raw = (cfg or {}).get(AI_ROLES_KEY) or {}
    if not isinstance(raw, Mapping):
        raw = {}
    out: dict[str, Any] = {}
    for role in KNOWN_ROLES:
        out[role] = _normalize_role_value(raw.get(role, "auto"))
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
                    merged["mode"] = mode_in
                elif value.get("provider") or value.get("model"):
                    merged.pop("mode", None)
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


# ---------------------------------------------------------------------------
# Difficulty → min rank
# ---------------------------------------------------------------------------


def min_rank_for_difficulty(
    *,
    task_class: str | None = None,
    node_depth: int = 0,
    risk: str | None = None,
) -> int:
    """Compute minimum rank threshold from TaskClass + spawn depth + risk.

    Deeper spawns require *decreasing* difficulty (lower min rank) so cheap
    models handle leaf work. Risk bumps the floor.
    """
    tc = (task_class or "unknown").strip().lower()
    base = _TASK_CLASS_MIN_RANK.get(tc, 3)

    depth = max(0, int(node_depth or 0))
    demoted = max(1, base - depth)

    risk_s = (risk or "").strip().lower()
    if risk_s in ("high", "critical", "destructive"):
        demoted = min(5, demoted + 1)
    elif risk_s in ("low", "none", "read_only", "ponder"):
        demoted = max(1, demoted - 1)

    return demoted


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
    task_class: str | None = None,
    parent_min_rank: int | None = None,
) -> dict[str, Any]:
    """Evaluate spawn-depth policy for a proposed child at *depth*.

    Returns ``{ok, max_spawn_depth, depth, min_rank, reason}``. Deeper spawns
    require decreasing difficulty (min_rank strictly below parent when parent
    rank is known). ``ok`` is False only when depth exceeds max_spawn_depth.
    """
    max_d = max_spawn_depth_for_capability(letter, cfg=cfg)
    child_min = min_rank_for_difficulty(task_class=task_class, node_depth=depth)
    if parent_min_rank is not None and depth > 0:
        child_min = min(child_min, max(1, int(parent_min_rank) - 1))
    if depth > max_d:
        return {
            "ok": False,
            "max_spawn_depth": max_d,
            "depth": depth,
            "min_rank": child_min,
            "reason": (
                f"spawn depth {depth} exceeds max_spawn_depth {max_d} "
                f"(capability {letter or 'default'})"
            ),
        }
    return {
        "ok": True,
        "max_spawn_depth": max_d,
        "depth": depth,
        "min_rank": child_min,
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
    if name in ("llamacpp", "llama.cpp", "llama-cpp", "lmstudio", "ollama", "vllm", "mock"):
        if base_url:
            return host_is_loopback(base_url)
        if cfg:
            for key in ("llamacpp", "vllm", "openai", "lmstudio", "ollama"):
                sub = cfg.get(key) or {}
                if isinstance(sub, Mapping) and sub.get("base_url"):
                    if name in (key, "llamacpp", "llama.cpp") or (
                        name in ("lmstudio", "ollama") and key == "openai"
                    ):
                        return host_is_loopback(str(sub.get("base_url")))
        return True
    if name in (
        "anthropic", "openai", "gemini", "openrouter", "xai",
        "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub", "kimi-sub",
        "claude-cli", "codex-cli",
    ):
        return False
    if base_url:
        return host_is_loopback(base_url)
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
            f"model on 127.0.0.1/localhost, or set it to \"auto\" with only local "
            f"providers enabled."
        )


# ---------------------------------------------------------------------------
# Enabled providers + auto pick
# ---------------------------------------------------------------------------


def _enabled_providers(cfg: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Build the candidate provider+model list from config / defaults.

    Each entry: ``{provider, model, base_url?}``.
    Honours ``cfg["enabled_providers"]`` when present (list of
    ``{provider, model}`` or provider name strings). Falls back to common
    local + free + configured cloud models so auto mode works out of the box.
    """
    cfg = cfg or {}
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(provider: str, model: str, base_url: str = "") -> None:
        key = (provider.lower(), model)
        if not provider or not model or key in seen:
            return
        seen.add(key)
        entry = {"provider": provider.lower(), "model": model}
        if base_url:
            entry["base_url"] = base_url
        out.append(entry)

    explicit = cfg.get("enabled_providers")
    if isinstance(explicit, list) and explicit:
        for item in explicit:
            if isinstance(item, str):
                if "/" in item:
                    p, _, m = item.partition("/")
                    _add(p.strip(), m.strip())
                continue
            if isinstance(item, Mapping):
                _add(
                    str(item.get("provider") or ""),
                    str(item.get("model") or ""),
                    str(item.get("base_url") or ""),
                )
        # Drop keyless openrouter even from explicit lists (mega-6 401 wipe).
        # Also drop process-local unavailable providers (mega-16 spending-limit).
        if not _openrouter_key_present(cfg):
            out[:] = [e for e in out if e.get("provider") != "openrouter"]
        out[:] = [
            e for e in out
            if not is_provider_unavailable(str(e.get("provider") or ""))
        ]
        return out

    if cfg.get("provider") and cfg.get("model"):
        _add(str(cfg["provider"]), str(cfg["model"]))

    lcfg = cfg.get("llamacpp") or {}
    if isinstance(lcfg, Mapping) and (
        lcfg.get("base_url") or lcfg.get("model") or lcfg.get("models")
        or lcfg.get("host") or lcfg.get("port")
    ):
        base = str(lcfg.get("base_url") or "http://127.0.0.1:1234/v1")
        for mid in (lcfg.get("models") or []):
            _add("llamacpp", str(mid), base)
        if lcfg.get("model"):
            _add("llamacpp", str(lcfg["model"]), base)
        for mid in ("qwen3-4b", "gemma3-4b-qat", "gemma4-e4b"):
            _add("llamacpp", mid, base)

    # Live local Ollama (OpenAI-compatible) — prefer when reachable / configured.
    ocfg = cfg.get("ollama") or {}
    if isinstance(ocfg, Mapping) or True:
        obase = ""
        if isinstance(ocfg, Mapping):
            obase = str(ocfg.get("base_url") or "")
            for mid in (ocfg.get("models") or []):
                _add("ollama", str(mid), obase or "http://127.0.0.1:11434/v1")
            if ocfg.get("model"):
                _add(
                    "ollama",
                    str(ocfg["model"]),
                    obase or "http://127.0.0.1:11434/v1",
                )
        # Default small local when Ollama is the common workstation backend.
        _add("ollama", "llama3.2:latest", "http://127.0.0.1:11434/v1")

    or_cfg = cfg.get("openrouter") or {}
    # mega-6: only offer OpenRouter when a real key exists. Keyless free models
    # auto-picked at min_rank≥3 and died as HTTP 401 provider_auth (19/19 nodes).
    # mega-16: also honour config.keys.openrouter (GUI keys map).
    has_or_key = _openrouter_key_present(cfg)
    if has_or_key:
        if isinstance(or_cfg, Mapping):
            for mid in (or_cfg.get("models") or []):
                _add("openrouter", str(mid))
        # mega-16 paid frontier first (no :free — free pool is 429/day-capped).
        for mid in (
            "deepseek/deepseek-chat-v3.1",
            "qwen/qwen3-coder-30b-a3b-instruct",
        ):
            _add("openrouter", mid)
        # Free-tier OpenRouter candidates (auto_pick ranks them; cost class 1).
        # mega-24: mistralai/mistral-small:free returns 404 "No endpoints found"
        # — removed from candidates so leaves don't thrash on dead free ids.
        for mid in (
            "nvidia/nemotron-ultra:free",
            "google/gemma-3-27b-it:free",
        ):
            _add("openrouter", mid)

    # Legacy/native callers without Chudcode's live candidate list use its
    # locally refreshed catalog snapshot, falling back only when offline.
    def latest(provider: str, pattern: str, fallback: str) -> str:
        from services.agent.model_catalog import models_dev_rows_for_provider
        matches = (
            (tuple(int(piece) for piece in re.findall(r"\d+", mid)), mid)
            for mid in models_dev_rows_for_provider(provider)
            if re.fullmatch(pattern, mid)
        )
        return max(matches, default=((), fallback))[1]

    if cfg.get("api_key") or (cfg.get("anthropic") or {}).get("api_key"):
        _add("anthropic", str((cfg.get("anthropic") or {}).get("model") or
                              latest("anthropic", r"claude-sonnet-\d+(?:-\d+)?", "claude-sonnet-5-5")))
    if (cfg.get("openai") or {}).get("api_key") or cfg.get("openai_api_key"):
        _add("openai", str((cfg.get("openai") or {}).get("model") or
                           latest("openai", r"gpt-\d+(?:\.\d+)?-sol", "gpt-4o")))
    if (cfg.get("gemini") or {}).get("api_key") or cfg.get("gemini_api_key"):
        _add("gemini", str((cfg.get("gemini") or {}).get("model") or
                           latest("google", r"gemini-\d+(?:\.\d+)?-pro(?:-preview)?",
                                  "gemini-3.1-pro-preview")))
    if (cfg.get("xai") or {}).get("api_key") or cfg.get("xai_api_key"):
        _add("xai", str((cfg.get("xai") or {}).get("model") or "grok-4.5"))
    keys_map = cfg.get("keys") or {}
    if (cfg.get("moonshot") or {}).get("api_key") or (
        isinstance(keys_map, Mapping) and keys_map.get("moonshot")
    ):
        _add("moonshot", str((cfg.get("moonshot") or {}).get("model") or "kimi-k3"))

    sub = cfg.get("subscription_auth") or {}
    if isinstance(sub, Mapping):
        if "xai-sub" in sub or "xai" in sub:
            # API model id is dotted grok-4.5 (hyphen grok-4-5 → HTTP 400
            # "Model not found" on attempt-2). Measured demotion is floored
            # to rank>=4 in load_rankings so auto_pick still selects it.
            _add("xai-sub", "grok-4.5")
        if "anthropic-sub" in sub or "claude" in sub:
            _add("anthropic-sub", "claude-sonnet-4")
        if "codex-sub" in sub or "codex" in sub:
            _add("codex-sub", "gpt-5")
        if "kimi-sub" in sub or "kimi" in sub:
            _add("kimi-sub", "k3")

    # mega-16: drop process-local unavailable providers (spending-limit etc.)
    out[:] = [
        e for e in out
        if not is_provider_unavailable(str(e.get("provider") or ""))
    ]
    return out


def _cost_key(provider: str, model: str) -> tuple[int, int, str]:
    """Sort key: lower cost class first, then stable id."""
    p = (provider or "").lower()
    cost = _COST_CLASS.get(p, 9)
    if p == "openrouter" and (":free" in (model or "") or model.endswith(":free")):
        cost = 1  # free tier beats subscription for leaf auto_pick
    return (cost, 0, f"{p}/{model}")


def cost_tier_for(provider: str, model: str = "") -> int:
    """Public cost class for assignment telemetry (lower = cheaper)."""
    return int(_cost_key(provider, model)[0])


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
    if role in ("orchestrator", "department_manager", "layout_engineer"):
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
        "orchestrator", "department_manager", "layout_engineer", "planning",
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



def _vision_capability_tier(provider: str, model: str) -> int:
    """Vision-preference sort tier for auto-pick.

    0 = known vision-capable, 1 = unknown (catalog miss / error),
    2 = known non-vision. Unknown is permissive: kept as fallback above
    known-non-vision so we never hard-exclude unresolved models.
    """
    try:
        from services.agent.model_catalog import lookup_model  # noqa: PLC0415

        row = lookup_model(provider, model)
    except Exception:  # noqa: BLE001
        return 1
    if not isinstance(row, Mapping) or "vision" not in row:
        return 1
    return 0 if bool(row.get("vision")) else 2


def auto_pick_model(
    *,
    cfg: Mapping[str, Any] | None = None,
    task_class: str | None = None,
    node_depth: int = 0,
    risk: str | None = None,
    min_rank: int | None = None,
    rankings: Mapping[str, ModelRank] | None = None,
    enabled: Iterable[Mapping[str, str]] | None = None,
    require_loopback: bool = False,
    prefer_vision: bool = False,
) -> RoleModelAssignment:
    """Pick cheapest enabled provider+model meeting the rank threshold.

    Prefer subscription/free/local over metered. Unknown models (no ranking
    entry) are treated as rank 2 so they remain usable but don't outrank
    measured/shipped frontier picks for hard tasks.

    When *prefer_vision* is True (vision harness role auto-pick), candidates
    are ordered vision-capable first, then unknown capability, then known
    non-vision — each tier still uses the normal cost/rank ordering. If no
    vision-capable model is enabled the pick falls through to the remaining
    pool (never empty/crash solely for lack of vision).
    """
    cfg = cfg or {}
    ranks = rankings if rankings is not None else load_rankings(cfg)
    threshold = (
        int(min_rank)
        if min_rank is not None
        else min_rank_for_difficulty(
            task_class=task_class, node_depth=node_depth, risk=risk
        )
    )
    threshold = max(1, min(5, threshold))

    candidates_raw = list(enabled) if enabled is not None else _enabled_providers(cfg)
    candidates: list[tuple[tuple[int, int, str], dict[str, str], ModelRank | None]] = []
    for c in candidates_raw:
        prov = str(c.get("provider") or "").strip().lower()
        model = str(c.get("model") or "").strip()
        if not prov or not model:
            continue
        # mega-16: skip providers marked unavailable (xai spending-limit etc.)
        if is_provider_unavailable(prov):
            continue
        if require_loopback and not provider_is_loopback(
            prov, base_url=c.get("base_url"), cfg=cfg
        ):
            continue
        mr = _rank_for(model, ranks)
        # Paid openrouter frontier ids get floor rank even if unranked.
        model_rank = mr.rank if mr is not None else 2
        mid_l = model.strip().lower()
        if mid_l in _FRONTIER_RANK_FLOOR_IDS or any(
            mid_l.startswith(stem) for stem in (
                "deepseek/deepseek-chat", "deepseek-chat",
            )
        ):
            model_rank = max(model_rank, _FRONTIER_MEASURED_FLOOR)
        if model_rank < threshold:
            continue
        if prefer_vision:
            sort_key = (
                _vision_capability_tier(prov, model),
                _cost_key(prov, model)[0],
                -model_rank,
                f"{prov}/{model}",
            )
        else:
            sort_key = (
                _cost_key(prov, model)[0],
                -model_rank,
                f"{prov}/{model}",
            )
        candidates.append((sort_key, dict(c), mr))

    if not candidates:
        fallback = [
            c for c in candidates_raw
            if c.get("provider") and c.get("model")
            and not is_provider_unavailable(str(c.get("provider") or ""))
            and (
                not require_loopback
                or provider_is_loopback(
                    str(c["provider"]), base_url=c.get("base_url"), cfg=cfg
                )
            )
        ]
        if not fallback:
            raise RoleModelError(
                f"auto_pick_model: no enabled providers meet min_rank={threshold}"
                + (" with loopback constraint" if require_loopback else "")
            )
        if prefer_vision:
            fallback.sort(
                key=lambda c: (
                    _vision_capability_tier(str(c["provider"]), str(c["model"])),
                    _cost_key(str(c["provider"]), str(c["model"])),
                )
            )
        else:
            fallback.sort(key=lambda c: _cost_key(str(c["provider"]), str(c["model"])))
        pick = fallback[0]
        mr = _rank_for(str(pick["model"]), ranks)
        return RoleModelAssignment(
            role="auto",
            provider=str(pick["provider"]).lower(),
            model=str(pick["model"]),
            mode="auto",
            rank=mr.rank if mr else None,
            source=mr.source if mr else "fallback",
            reason=(
                f"no candidate met min_rank={threshold}; "
                f"fell back to cheapest enabled"
            ),
        )

    candidates.sort(key=lambda t: t[0])
    _sk, pick, mr = candidates[0]
    return RoleModelAssignment(
        role="auto",
        provider=str(pick["provider"]).lower(),
        model=str(pick["model"]),
        mode="auto",
        rank=mr.rank if mr else None,
        source=mr.source if mr else "unranked",
        reason=(
            f"min_rank={threshold} task_class={task_class or 'unknown'} "
            f"depth={node_depth} risk={risk or 'none'}"
        ),
    )


# ---------------------------------------------------------------------------
# Public resolve
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Copilot mode → harness role (mode overhaul)
# ---------------------------------------------------------------------------

# agent→default, plan→planning, swarm→default (+ corporate seats),
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
    if role_s in ("overseer", "orchestrator", "department_manager", "planning"):
        return "planning"
    if role_s == "reviewer":
        return "vision"
    if role_s == "advisor":
        return "advisor"
    if role_s in ("default", "intake", "intake_analyst"):
        return "default"
    return "task"



def resolve_role_model(
    role: str,
    cfg: Mapping[str, Any] | None = None,
    *,
    task_class: str | None = None,
    node_depth: int = 0,
    risk: str | None = None,
    min_rank: int | None = None,
    rankings: Mapping[str, ModelRank] | None = None,
    enabled: Iterable[Mapping[str, str]] | None = None,
    request: str = "",
    turn_preferences: Any | None = None,
) -> RoleModelAssignment:
    """Resolve provider+model for *role* honouring config.json ``ai_roles``.

    * Fixed ``{provider, model}`` entries are returned as mode=fixed.
    * ``\"default\"`` returns mode=default with EMPTY provider/model — the caller
      keeps whatever model the chat composer is on (see :data:`SESSION_DEFAULT`).
    * ``\"auto\"`` difficulty-aware picks from rankings.
    * Redaction role: HARD refuse non-loopback (raises :class:`RoleModelError`).
      ``\"default\"`` is downgraded to ``\"auto\"`` for redaction, because the chat
      model is normally a cloud provider and this role may never leave loopback.
    """
    if role in ("chat", "worker", "private"):
        from services.agent.preferences import TASK_CLASS_KINDS, resolve_turn_preferences

        if turn_preferences is None:
            turn_preferences = resolve_turn_preferences(request, cfg=cfg,
                task_kind=TASK_CLASS_KINDS.get(task_class))
        if min_rank is not None:
            from dataclasses import replace

            turn_preferences = replace(turn_preferences,
                min_rank=max(turn_preferences.min_rank, max(1, min(5, int(min_rank)))))
        return _resolve_auto_seat(role, cfg, request=request, policy=turn_preferences,
                                  rankings=rankings, enabled=enabled)
    role_s = (role or "default").strip().lower() or "default"
    role_s = ROLE_MODEL_KEY_ALIASES.get(role_s, role_s)
    cfg = cfg or {}
    roles = get_ai_roles(cfg)
    entry = roles.get(role_s)
    if entry is None:
        entry = roles.get("default", "auto")

    require_loopback = role_s == "redaction"
    rank_table = rankings if rankings is not None else load_rankings(cfg)

    entry_mode = (
        str(entry.get("mode") or "").lower() if isinstance(entry, Mapping)
        else str(entry or "").strip().lower()
    )
    if entry_mode == SESSION_DEFAULT:
        if require_loopback:
            entry = "auto"  # loopback rule outranks the session model
        else:
            return RoleModelAssignment(
                role=role_s,
                provider="",
                model="",
                mode=SESSION_DEFAULT,
                rank=None,
                source="ai_roles",
                reason=f"default ai_roles.{role_s} (follows the chat model)",
            )

    if isinstance(entry, Mapping):
        prov = str(entry.get("provider") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        if prov and model:
            if require_loopback:
                assert_redaction_loopback(prov, cfg=cfg)
            mr = rank_table.get(model)
            return RoleModelAssignment(
                role=role_s,
                provider=prov,
                model=model,
                mode="fixed",
                rank=mr.rank if mr else None,
                source="ai_roles",
                reason=f"fixed ai_roles.{role_s}",
            )
        entry = "auto"

    try:
        pick = auto_pick_model(
            cfg=cfg,
            task_class=task_class,
            node_depth=node_depth,
            risk=risk,
            min_rank=min_rank,
            rankings=rank_table,
            enabled=enabled,
            require_loopback=require_loopback,
            prefer_vision=(role_s == "vision"),
        )
    except RoleModelError:
        if require_loopback:
            raise RoleModelError(
                "redaction role requires a loopback provider but none of the "
                "enabled providers are local (llamacpp/lmstudio/ollama/vllm on "
                "127.0.0.1). Configure a local model or set ai_roles.redaction "
                "explicitly to a loopback provider+model."
            )
        raise

    if require_loopback:
        assert_redaction_loopback(pick.provider, cfg=cfg)

    return RoleModelAssignment(
        role=role_s,
        provider=pick.provider,
        model=pick.model,
        mode="auto",
        rank=pick.rank,
        source=pick.source,
        reason=pick.reason,
    )


def _resolve_auto_seat(role, cfg, *, request, policy, rankings, enabled):
    """Three Auto seats; legacy per-role assignments remain independently readable."""
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.config import load_config

    cfg = load_config() if cfg is None else cfg
    policy = policy or resolve_turn_preferences(request, cfg=cfg)
    ranks = rankings if rankings is not None else load_rankings(cfg)
    local_only = policy.local_only or role == "private"
    entry = get_ai_roles(cfg)[role]
    if isinstance(entry, Mapping) and entry.get("provider") and entry.get("model"):
        provider, model = str(entry["provider"]), str(entry["model"])
        if local_only and not _auto_local_provider(provider, cfg, entry.get("base_url")):
            raise RoleModelError("This computer only requires a loopback model. Configure a local model in Accounts.")
        return RoleModelAssignment(role, provider, model, "fixed",
            rank=ranks[model].rank if model in ranks else None, source="ai_roles",
            reason=f"Using your ai_roles.{role} model.")
    if entry == SESSION_DEFAULT and not local_only:
        return RoleModelAssignment(role, "", "", SESSION_DEFAULT, source="ai_roles",
                                   reason="Using the current chat model.")
    candidates = list(enabled) if enabled is not None else _enabled_providers(cfg)
    disabled = set(cfg.get("disabled_providers") or ())
    candidates = [c for c in candidates if c.get("authenticated", True) and c.get("provider") not in disabled
                  and not is_provider_unavailable(c.get("provider"))
                  and (not local_only or _auto_local_provider(c.get("provider", ""), cfg, c.get("base_url")))]
    if not candidates:
        raise RoleModelError("No available local model. Configure a loopback model in Accounts."
                             if local_only else "No authenticated model is available. Sign in to a provider in Accounts.")
    def rank(c):
        row = _rank_for(str(c.get("model", "")), ranks)
        return row.rank if row else 2
    threshold = 1 if role == "worker" and policy.cheap_leaves else policy.min_rank
    capable = [c for c in candidates if rank(c) >= threshold]
    if policy.task_kind == "analysis" or policy.priority == "best_results":
        strongest = max(map(rank, candidates))
        capable = [c for c in candidates if rank(c) == strongest]
    if not capable:
        strongest = max(map(rank, candidates))
        capable = [c for c in candidates if rank(c) == strongest]
    def cost(c):
        return _COST_CLASS.get(str(c["provider"]), 5)
    def freshness(c):
        model = str(c["model"])
        match = re.search(r"(?:claude-(?:opus|sonnet)-|gpt-)(\d+)(?:[-.](\d+))?", model)
        return (-int(match[1]), -int(match[2] or 0)) if match else (0, 0)
    if policy.priority == "fastest":
        capable.sort(key=lambda c: (rank(c), cost(c), freshness(c), str(c["model"])))
    else:
        capable.sort(key=lambda c: (cost(c), rank(c), freshness(c), str(c["model"])))
    chosen = capable[0]
    model, provider = str(chosen["model"]), str(chosen["provider"])
    reason = f"Auto chose {model} for {policy.task_kind.replace('_', ' ')} ({policy.thinking} thinking)."
    if rank(chosen) < threshold:
        reason += " No available model meets the requested capability; using the strongest available."
    row = _rank_for(model, ranks)
    return RoleModelAssignment(role, provider, model, "auto", rank(chosen),
                               row.source if row else "unranked", reason)


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


def resolve_for_node(
    role: str | None,
    context: Mapping[str, Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> Optional[RoleModelAssignment]:
    """Convenience for node_runner / mission_dispatch.

    Returns None when ai_roles is unset entirely (caller keeps legacy path).
    When ai_roles is present (even all-auto), resolves through this module.
    """
    if cfg is None:
        try:
            from services.agent import config as agent_config  # noqa: PLC0415
            cfg = agent_config.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    has_ai_roles = isinstance((cfg or {}).get(AI_ROLES_KEY), Mapping)
    role_s = (role or "default").strip().lower() or "default"
    if not has_ai_roles and role_s not in ("redaction", "chat", "worker", "private"):
        return None

    ctx = context or {}
    node_inputs = ctx.get("node_inputs") or {}
    task_class = (
        ctx.get("task_class")
        or node_inputs.get("task_class")
        or ctx.get("taskClass")
    )
    try:
        depth = int(ctx.get("depth") or node_inputs.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    risk = ctx.get("risk") or node_inputs.get("risk")
    return resolve_role_model(
        role_s,
        cfg,
        task_class=str(task_class) if task_class else None,
        node_depth=depth,
        risk=str(risk) if risk else None,
        request=str(ctx.get("brief") or node_inputs.get("brief") or ctx.get("request") or ""),
    )
