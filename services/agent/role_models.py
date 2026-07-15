"""role_models.py — per-role model assignment + auto ranking pick (WS2 / WS4).

Contract (AI_HARNESS_ROADMAP WS2 + wave Contract):
  * config.json ``ai_roles`` maps role → ``{provider, model}`` | ``"auto"``
    for: advisor | task | planning | default | plm | redaction.
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

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

RANKINGS_SCHEMA = "aicad.model_rankings/1"
AI_ROLES_KEY = "ai_roles"
# User overrides for rankings (Settings GUI). Distinct from ai_roles.
USER_RANKINGS_KEY = "model_rankings"

# Roles the Settings GUI / Contract expose. routing.yaml roles still resolve via
# routing.py; these are the *harness-level* role knobs (advisor/task/planning/...).
KNOWN_ROLES = (
    "advisor",
    "task",
    "planning",
    "default",
    "plm",
    "redaction",
)

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
    "claude-cli": 2,
    "codex-cli": 2,
    "xai": 5,
    "anthropic": 5,
    "openai": 5,
    "gemini": 5,
    "openrouter": 4,  # free models may override via model id suffix
    "mock": 0,
}

# TaskClass → base minimum rank threshold (1–5).
_TASK_CLASS_MIN_RANK: dict[str, int] = {
    "simple_part": 1,
    "modification": 2,
    "multi_feature_part": 3,
    "enclosure": 3,
    "assembly": 4,
    "cam_program": 4,
    "analysis": 4,
    "unknown": 3,
}

# Max spawn depth defaults by capability letter (WS4 spawn-depth policy).
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
    """Resolved provider+model for one role (or auto pick)."""
    role: str
    provider: str
    model: str
    mode: str                       # "fixed" | "auto"
    rank: int | None = None
    source: str = ""                # ranking source when auto
    reason: str = ""

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
            if base is not None and base.source == "shipped":
                notes = f"{mr.notes} (was shipped rank {base.rank})"
            merged[mid] = ModelRank(
                model_id=mid,
                rank=mr.rank,
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


def rankings_as_dict(
    cfg: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, dict[str, Any]]:
    """Public view for RPCs: ``{model_id: {rank, source, notes, ...}}``."""
    return {mid: mr.to_dict() for mid, mr in load_rankings(cfg, **kwargs).items()}


# ---------------------------------------------------------------------------
# ai_roles config helpers
# ---------------------------------------------------------------------------


def _normalize_role_value(value: Any) -> str | dict[str, str]:
    """Coerce a config ai_roles entry to ``"auto"`` or ``{provider, model}``."""
    if value is None or value == "" or value == "auto":
        return "auto"
    if isinstance(value, str):
        if "/" in value:
            prov, _, model = value.partition("/")
            return {"provider": prov.strip(), "model": model.strip()}
        return "auto"
    if isinstance(value, Mapping):
        if str(value.get("mode") or "").lower() == "auto":
            return "auto"
        prov = str(value.get("provider") or "").strip()
        model = str(value.get("model") or "").strip()
        if not prov and not model:
            return "auto"
        return {"provider": prov, "model": model}
    return "auto"


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


def set_ai_roles(
    roles_partial: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Merge *roles_partial* into config ai_roles. Returns the full map.

    When *persist* is True, writes via ``config.save_config`` (shallow merge).
    """
    current = dict((cfg or {}).get(AI_ROLES_KEY) or {})
    if not isinstance(current, dict):
        current = {}
    for role, value in (roles_partial or {}).items():
        role_s = str(role).strip()
        if not role_s:
            continue
        current[role_s] = _normalize_role_value(value)
    if persist:
        from services.agent import config as agent_config  # noqa: PLC0415
        agent_config.save_config({AI_ROLES_KEY: current})
        disk = agent_config.load_config()
        return get_ai_roles(disk)
    merged_cfg = dict(cfg or {})
    merged_cfg[AI_ROLES_KEY] = current
    return get_ai_roles(merged_cfg)


def set_user_rankings(
    rankings_partial: Mapping[str, Any],
    cfg: Mapping[str, Any] | None = None,
    *,
    persist: bool = True,
) -> dict[str, dict[str, Any]]:
    """Merge user ranking overrides. Returns full merged rankings view."""
    current = dict((cfg or {}).get(USER_RANKINGS_KEY) or {})
    if not isinstance(current, dict):
        current = {}
    for mid, entry in (rankings_partial or {}).items():
        mid_s = str(mid).strip()
        if not mid_s:
            continue
        if entry is None:
            current.pop(mid_s, None)
            continue
        if isinstance(entry, Mapping):
            prev = dict(current.get(mid_s) or {}) if isinstance(current.get(mid_s), Mapping) else {}
            if "rank" in entry:
                prev["rank"] = _clamp_rank(entry["rank"])
            if "notes" in entry:
                prev["notes"] = str(entry["notes"])
            if "provider" in entry:
                prev["provider"] = str(entry["provider"])
            prev.setdefault("source", "user")
            current[mid_s] = prev
        else:
            current[mid_s] = {
                "rank": _clamp_rank(entry),
                "source": "user",
                "notes": "user override",
            }
    if persist:
        from services.agent import config as agent_config  # noqa: PLC0415
        agent_config.save_config({USER_RANKINGS_KEY: current})
        disk = agent_config.load_config()
        return rankings_as_dict(disk)
    merged_cfg = dict(cfg or {})
    merged_cfg[USER_RANKINGS_KEY] = current
    return rankings_as_dict(merged_cfg)


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
        "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub",
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
    if isinstance(or_cfg, Mapping):
        for mid in (or_cfg.get("models") or []):
            _add("openrouter", str(mid))
    # Free-tier OpenRouter candidates (auto_pick ranks them; construction needs key).
    for mid in (
        "nvidia/nemotron-ultra:free",
        "mistralai/mistral-small:free",
        "google/gemma-3-27b-it:free",
    ):
        _add("openrouter", mid)

    if cfg.get("api_key") or (cfg.get("anthropic") or {}).get("api_key"):
        _add("anthropic", str((cfg.get("anthropic") or {}).get("model") or "claude-sonnet-4"))
    if (cfg.get("openai") or {}).get("api_key") or cfg.get("openai_api_key"):
        _add("openai", str((cfg.get("openai") or {}).get("model") or "gpt-5"))
    if (cfg.get("gemini") or {}).get("api_key") or cfg.get("gemini_api_key"):
        _add("gemini", str((cfg.get("gemini") or {}).get("model") or "gemini-2.5-pro"))
    if (cfg.get("xai") or {}).get("api_key") or cfg.get("xai_api_key"):
        _add("xai", str((cfg.get("xai") or {}).get("model") or "grok-4.5"))

    sub = cfg.get("subscription_auth") or {}
    if isinstance(sub, Mapping):
        if "xai-sub" in sub or "xai" in sub:
            _add("xai-sub", "grok-4.5")
        if "anthropic-sub" in sub or "claude" in sub:
            _add("anthropic-sub", "claude-sonnet-4")
        if "codex-sub" in sub or "codex" in sub:
            _add("codex-sub", "gpt-5")

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
) -> RoleModelAssignment:
    """Pick cheapest enabled provider+model meeting the rank threshold.

    Prefer subscription/free/local over metered. Unknown models (no ranking
    entry) are treated as rank 2 so they remain usable but don't outrank
    measured/shipped frontier picks for hard tasks.
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
        if require_loopback and not provider_is_loopback(
            prov, base_url=c.get("base_url"), cfg=cfg
        ):
            continue
        mr = ranks.get(model)
        if mr is None and "/" in model:
            mr = ranks.get(model.split("/", 1)[-1])
        model_rank = mr.rank if mr is not None else 2
        if model_rank < threshold:
            continue
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
        fallback.sort(key=lambda c: _cost_key(str(c["provider"]), str(c["model"])))
        pick = fallback[0]
        mr = ranks.get(str(pick["model"]))
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
) -> RoleModelAssignment:
    """Resolve provider+model for *role* honouring config.json ``ai_roles``.

    * Fixed ``{provider, model}`` entries are returned as mode=fixed.
    * ``\"auto\"`` (default) difficulty-aware picks from rankings.
    * Redaction role: HARD refuse non-loopback (raises :class:`RoleModelError`).
    """
    role_s = (role or "default").strip().lower() or "default"
    cfg = cfg or {}
    roles = get_ai_roles(cfg)
    entry = roles.get(role_s)
    if entry is None:
        entry = roles.get("default", "auto")

    require_loopback = role_s == "redaction"
    rank_table = rankings if rankings is not None else load_rankings(cfg)

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
    if not has_ai_roles and role_s != "redaction":
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
    )
