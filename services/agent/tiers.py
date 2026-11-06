"""Model tier profiles — loader, tier-to-provider resolution, and tool-schema trim.

Contract: contracts/advisor.md §8 (tier budgets).
Spec: docs/design/ADVISOR_ARCHITECTURE.md §2.9.

What this module provides
-------------------------
* ``load_tier(name)`` / ``load_all_tiers()``
  Load and validate tier profiles from ``services/agent/tiers.yaml``.

* ``TierProfile`` dataclass
  Per-tier dials: context/generation budgets, tool-schema-trim flag and the
  capability profile letter.

* ``detect_tier_from_env()``
  Tier name from the ``CHUDWARE_TIER`` env var (``None`` when unset).

* ``trim_tool_schema(schema)``
  Strip tool description to first sentence for local tiers.

* ``provider_for_tier`` / ``MissionAuthError`` / ``parse_*_tier`` /
  ``load_local_models``
  Tier-to-provider resolution used by the gym/bench harness.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

# --------------------------------------------------------------------------- constants

TIER_SCHEMA = "aicad.tier/1"

_TIERS_PATH = Path(__file__).resolve().parent / "tiers.yaml"

# Repo-wide token-counting heuristic (matches ledger.py and advisor/render.py).
_CHARS_PER_TOKEN = 4

# Env override for manual tier selection (e.g. CHUDWARE_TIER=frontier).
TIER_ENV_VAR = "CHUDWARE_TIER"


# --------------------------------------------------------------------------- errors


class TierError(ValueError):
    """A tier profile is missing or structurally invalid."""


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class SlotBudgets:
    """Token budgets for each §9.1 context slot."""

    system: int
    ledger: int
    state: int
    advisory: int
    tools: int
    history: int

    @property
    def total(self) -> int:
        return (
            self.system
            + self.ledger
            + self.state
            + self.advisory
            + self.tools
            + self.history
        )


@dataclass(frozen=True)
class TierProfile:
    """One tier profile loaded from tiers.yaml."""

    name: str
    context_tokens: int
    generation_headroom: int
    budgets: SlotBudgets
    tool_schema_trim: bool
    # Coercion level A/B/C — independent of turn priority. A = scaffold hard,
    # B = soft gates, C = freedom + HARD floors only. See capability.py.
    capability_profile: str = "C"  # A | B | C

    @property
    def available_tokens(self) -> int:
        """Context tokens minus generation headroom = the assembly budget."""
        return self.context_tokens - self.generation_headroom

    def budget_ok(self) -> bool:
        """Verify slot sum ≤ available_tokens (the §8 invariant)."""
        return self.budgets.total <= self.available_tokens


# --------------------------------------------------------------------------- loading


def _parse_budgets(raw: dict, source: str) -> SlotBudgets:
    keys = ("system", "ledger", "state", "advisory", "tools", "history")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise TierError(f"{source}: budgets missing keys: {missing}")
    return SlotBudgets(**{k: int(raw[k]) for k in keys})


def _profile_from_dict(name: str, raw: dict, source: str) -> TierProfile:
    required = (
        "context_tokens",
        "generation_headroom",
        "budgets",
        "tool_schema_trim",
    )
    missing = [k for k in required if k not in raw]
    if missing:
        raise TierError(f"{source} [{name}]: missing fields: {missing}")

    budgets = _parse_budgets(raw["budgets"], f"{source} [{name}]")
    # capability_profile is required for shipped tiers.yaml; synthetic/test YAML
    # may omit it and inherit the dataclass default ("C").
    cap = raw.get("capability_profile", "C")
    if cap not in ("A", "B", "C"):
        raise TierError(
            f"{source} [{name}]: capability_profile must be A|B|C, got {cap!r}"
        )
    profile = TierProfile(
        name=name,
        context_tokens=int(raw["context_tokens"]),
        generation_headroom=int(raw["generation_headroom"]),
        budgets=budgets,
        tool_schema_trim=bool(raw["tool_schema_trim"]),
        capability_profile=str(cap),
    )
    # Enforce the §8 budget invariant.
    if not profile.budget_ok():
        raise TierError(
            f"{source} [{name}]: budget overflow — "
            f"slots sum {profile.budgets.total} + headroom {profile.generation_headroom} "
            f"= {profile.budgets.total + profile.generation_headroom} "
            f"> context_tokens {profile.context_tokens}"
        )
    return profile


def load_tier(name: str, *, path: Path | None = None) -> TierProfile:
    """Load a single tier profile by name from tiers.yaml."""
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    profiles = data.get("profiles", {})
    if name not in profiles:
        raise TierError(
            f"tier {name!r} not found in {p}; available: {sorted(profiles)}"
        )
    return _profile_from_dict(name, profiles[name], str(p))


def load_all_tiers(*, path: Path | None = None) -> dict[str, TierProfile]:
    """Load all tier profiles from tiers.yaml."""
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    out: dict[str, TierProfile] = {}
    for name, raw in (data.get("profiles") or {}).items():
        out[name] = _profile_from_dict(name, raw, str(p))
    return out


def load_local_models(*, path: Path | None = None) -> dict:
    """Return tiers.yaml's ``local_models`` block — the shipped bundled-model
    defaults per role shape, from the local-model benchmark.

    Keys: ``gbnf_worker`` / ``fanout`` (bundled model ids) and
    ``parallel_slots`` (recommended llama-server ``--parallel``). Returns ``{}``
    when the block is absent (older tiers.yaml). Shipped defaults only —
    routing.yaml role->model bindings and user overrides always win.
    """
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    block = data.get("local_models") or {}
    if not isinstance(block, dict):
        raise TierError(f"{p}: 'local_models' must be a mapping")
    return dict(block)


# --------------------------------------------------------------------------- tier -> provider

# Three local models are bundled/served via llama.cpp (services/installer/llamacpp.py,
# model_manager._REGISTRY). A tier listed here pins a SPECIFIC model instead of always
# falling to tiers.yaml's local_models.fanout — this is what lets gym tiers like
# "local-24k-gemma3-qat" actually route to gemma3-4b-qat rather than collapsing to
# qwen3-4b (the fanout default). A tier not listed here keeps the historical
# fanout-based resolution — no behavior change for existing callers.
LOCAL_TIER_MODELS: dict[str, str] = {
    "local-24k-gemma3-qat": "gemma3-4b-qat",  # smallest footprint, full GBNF grammar
    "local-24k-gemma4-e4b": "gemma4-e4b",  # larger/slower GBNF backup, comparison-only
}


def parse_openrouter_tier(tier: str) -> str | None:
    """Return the OpenRouter model id when *tier* is ``openrouter:<model-id>``.

    Examples::

        openrouter:anthropic/claude-opus-4  →  "anthropic/claude-opus-4"
        openrouter:openai/gpt-4o           →  "openai/gpt-4o"

    Returns ``None`` for any other tier string (local-*, mid, frontier, …).
    """
    if not tier or not isinstance(tier, str):
        return None
    prefix = "openrouter:"
    if not tier.startswith(prefix):
        return None
    model_id = tier[len(prefix) :].strip()
    return model_id or None


def parse_xai_tier(tier: str) -> str | None:
    """Return the xAI model id when *tier* is ``xai:<model-id>``.

    Examples::

        xai:grok-4.5   →  "grok-4.5"
        xai:grok-4-5   →  "grok-4-5"   (hyphen form also accepted)
        xai:grok-4-1   →  "grok-4-1"

    Returns ``None`` for any other tier string. Gym CLI / leaderboard use this
    the same way as :func:`parse_openrouter_tier`.
    """
    if not tier or not isinstance(tier, str):
        return None
    prefix = "xai:"
    if not tier.startswith(prefix):
        return None
    model_id = tier[len(prefix) :].strip()
    return model_id or None


def profile_tier_for(tier: str | None) -> str | None:
    """Map a gym/CLI tier string onto a tiers.yaml budget profile name.

    Parameterized ``openrouter:<model-id>`` and ``xai:<model-id>`` paid tiers
    reuse the ``frontier`` budget profile (same context class). Local / mid /
    frontier names pass through unchanged.
    """
    if not tier:
        return tier
    if parse_openrouter_tier(tier) is not None:
        return "frontier"
    if parse_xai_tier(tier) is not None:
        return "frontier"
    return tier


class MissionAuthError(RuntimeError):
    """Provider credentials missing/invalid — fail closed before a run starts.

    Raised by :func:`provider_for_tier` when an ``xai:*`` tier has neither a
    stored SuperGrok subscription token nor an ``XAI_API_KEY`` / config api_key.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "auth_missing",
        tried: list[str] | None = None,
        tier: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.tried = list(tried or [])
        self.tier = tier

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "code": self.code,
            "tried": list(self.tried),
            "tier": self.tier,
            "error": str(self),
        }


def provider_for_tier(tier: str | None, cfg: Optional[dict] = None):
    """Resolve the :class:`Provider` a *tier* should run on.

    The gym/bench harness (``tools/flywheel/bench/gym/runner.py``) is the only
    production caller. DAG nodes run through ``omp_roles.run_role_turn`` and
    never resolve a provider here.

    A ``local-*`` tier (``local-24k`` / ``local-48k``) MUST run on the bundled
    local model (tiers.yaml ``local_models.fanout``, currently ``qwen3-4b``)
    rather than falling through to the ambient default — otherwise a "local"
    tier run is meaningless (it silently answers from a cloud model). A tier
    listed in ``LOCAL_TIER_MODELS`` (e.g. ``local-24k-gemma3-qat``) pins that
    specific bundled model instead of the fanout default — this is the
    local-model benchmark's per-model tier matrix.

    Parameterized paid tiers remain a benchmark-harness surface. Online
    Chudware turns and DAG nodes do not enter this factory; the OMP host owns
    their provider and model selection.

    Parameterized xAI tiers use ``xai:<model-id>`` (e.g. ``xai:grok-4.5`` /
    ``xai:grok-4-5``). They prefer the SuperGrok subscription provider
    (``xai-sub``) when a credential is stored, else fall back to the classic
    API-key provider (``xai`` / ``XAI_API_KEY``).

    An explicit ``CHUDWARE_PROVIDER`` env override always wins (tests pin it to
    ``mock``; a human can force a frontier key even for a nominally local
    tier run). Every non-local, non-openrouter, non-xai tier (``mid`` /
    ``frontier`` / ``None``/unset) falls straight through to the ordinary
    ``config.get_provider()`` resolution — behaviour is UNCHANGED for any
    caller that does not pass a tier at all, which is the point: this is a
    targeted fix, not a change to cloud-default usage.

    Raises whatever the underlying provider construction raises (e.g. no
    bundled model weights on this machine) — callers fail closed, they never
    silently fall back to a cloud provider on a resolution error.
    """
    import os

    from services.agent import config as _config

    # Explicit env override always wins (tests pin CHUDWARE_PROVIDER=mock).
    if os.environ.get("CHUDWARE_PROVIDER"):
        return _config.get_provider(cfg=cfg)

    # Parameterized OpenRouter paid tiers: openrouter:<model-id>
    or_model = parse_openrouter_tier(tier) if tier else None
    if or_model is not None:
        merged_cfg = dict(cfg or {})
        # Prefer openrouter-scoped key, then keys map, then top-level, then env.
        oc = dict(merged_cfg.get("openrouter") or {})
        keys_map = merged_cfg.get("keys") or {}
        key = (
            oc.get("api_key")
            or (keys_map.get("openrouter") if isinstance(keys_map, dict) else None)
            or merged_cfg.get("api_key")
            or merged_cfg.get("openrouter_api_key")
            or os.environ.get("OPENROUTER_API_KEY")
        )
        # Mirror key into openrouter.api_key so get_provider finds it.
        if key and not oc.get("api_key"):
            oc["api_key"] = key
        merged_cfg["api_key"] = key
        merged_cfg["model"] = or_model
        # Always write oc back — setdefault would keep an empty openrouter:{}
        # and drop the mirrored api_key (mega-16 keys.openrouter path).
        merged_cfg["openrouter"] = oc
        return _config.get_provider("openrouter", cfg=merged_cfg)

    # Parameterized xAI paid tiers: xai:<model-id>
    # Prefer subscription OAuth when a credential is present; else API key.
    xai_model = parse_xai_tier(tier) if tier else None
    if xai_model is not None:
        from services.agent.providers import sub_auth as _sub_auth
        from services.agent.providers.xai_sub_provider import PROVIDER_ID as _XAI_SUB_ID

        merged_cfg = dict(cfg or {})
        # Normalise common CLI hyphen form (grok-4-5) to the dotted wire id
        # (grok-4.5) when that is the documented slug; leave other ids alone.
        wire_model = xai_model
        if wire_model == "grok-4-5":
            wire_model = "grok-4.5"
        elif wire_model == "grok-4-1":
            wire_model = "grok-4-1"  # already the catalog form
        merged_cfg["model"] = wire_model

        sub_cred = _sub_auth.load_credential(_XAI_SUB_ID)
        if sub_cred and sub_cred.get("access_token"):
            return _config.get_provider("xai-sub", cfg=merged_cfg)

        xc = dict(merged_cfg.get("xai") or {})
        key = (
            xc.get("api_key")
            or merged_cfg.get("api_key")
            or os.environ.get("XAI_API_KEY")
        )
        # Fail closed: never construct XAIProvider with a missing/empty key.
        # Offline mode also refuses cloud xai — surface the same structured error
        # rather than N parallel 400 "Incorrect API key" storms mid-DAG (mega-1).
        key_s = str(key).strip() if key is not None else ""
        if not key_s:
            tried = ["xai-sub", "xai"]
            offline = False
            try:
                offline = bool(
                    _config.offline_enabled(merged_cfg if merged_cfg else None)
                )
            except Exception:  # noqa: BLE001
                offline = False
            if offline:
                msg = (
                    f"xAI tier {tier!r} unavailable: offline_mode is on and no "
                    f"xai-sub credential is stored (cloud API key path is refused)."
                )
            else:
                msg = (
                    f"xAI tier {tier!r} has no credentials: no xai-sub OAuth token "
                    f"in subscription_auth and no XAI_API_KEY / config api_key. "
                    f"Sign in with xAI (device code) or set XAI_API_KEY before "
                    f"launching the mission."
                )
            raise MissionAuthError(
                msg, code="auth_missing", tried=tried, tier=str(tier)
            )
        merged_cfg["api_key"] = key_s
        merged_cfg.setdefault("xai", xc)
        return _config.get_provider("xai", cfg=merged_cfg)

    if not tier or not tier.startswith("local"):
        return _config.get_provider(cfg=cfg)

    local_models = load_local_models()
    model_id = LOCAL_TIER_MODELS.get(tier) or local_models.get("fanout") or "qwen3-4b"
    from services.agent import model_manager

    model_path = model_manager.get_model_path(model_id)
    if model_path is None:
        # get_model_path() returning None means the GGUF isn't provisioned on this
        # machine. str(None) == "None" is truthy, which used to silently defeat
        # services.installer.llamacpp's find_default_model() fallback — fail closed instead.
        raise model_manager.ModelProvisionError(
            f"local tier {tier!r} needs model {model_id!r}, which is not "
            f"provisioned on this machine (no GGUF found for it). Provision "
            f"the model before launching this tier."
        )
    merged_cfg = dict(cfg or {})
    merged_cfg["llamacpp"] = {
        **(merged_cfg.get("llamacpp") or {}),
        "model_path": str(model_path),
        "model": model_id,
    }
    # llamacpp (not openai) so structured-output nodes get GBNF grammars, which
    # the bundled local tiers rely on.
    return _config.get_provider("llamacpp", cfg=merged_cfg)


# --------------------------------------------------------------------------- tier detection


def detect_tier_from_env() -> Optional[str]:
    """Return the tier name from ``CHUDWARE_TIER`` env var, or ``None`` if unset.

    Manual tier selection; ``None`` means no tier was pinned.
    """
    val = os.environ.get(TIER_ENV_VAR, "").strip()
    return val if val else None


# --------------------------------------------------------------------------- tool schema trim


def trim_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Truncate a tool's description to its first sentence.

    Used by local tiers (``tool_schema_trim: true``) to shrink the tools slot.
    Operates on a copy; never mutates the original registry entry.

    Expected schema shape: ``{"description": "...", ...}`` (MCP tool schema).
    """
    if not isinstance(schema, dict):
        return schema
    desc = schema.get("description")
    if not desc:
        return schema
    # First sentence = text up to (and including) the first period, '!', or '?'
    # that actually ends a sentence, OR the entire string if no boundary found.
    # A '.' does NOT end a sentence when it closes a common abbreviation
    # ("e.g.", "i.e.", "etc.") or sits inside a token (version numbers,
    # decimals, paths) — those used to truncate descriptions mid-word
    # ("Find paths matching a glob (e.").
    _ABBREVS = ("e.g", "i.e", "etc", "vs", "cf", "approx", "fig")
    for i, ch in enumerate(desc):
        if ch in ".!?":
            if ch == ".":
                head = desc[:i].rstrip().lower()
                word = (head.rsplit(None, 1)[-1] if head else "").lstrip("([{'\"`")
                if word in _ABBREVS:
                    continue
                nxt = desc[i + 1] if i + 1 < len(desc) else ""
                if nxt and not nxt.isspace():
                    continue
            short = desc[: i + 1].strip()
            if short and short != desc.strip():
                out = dict(schema)
                out["description"] = short
                return out
            break
    return schema


# --------------------------------------------------------------------------- token counting


def _count_tokens(text: str) -> int:
    """Estimate token count using the repo-wide 4-chars/token heuristic."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN
