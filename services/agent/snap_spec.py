"""Provider → OMP snap-render spec.

Snap compaction re-renders archived history as PNG frames through the OMP
sidecar, and both the render and the reinject budget depend on who will read the
frames: each provider family has its own image budget and its own vision
support. This module maps a provider/model onto the api family OMP's
billingFamily semantics expect, with a best-effort vision probe — so a
mid-session provider switch re-renders with the right assumptions instead of
inheriting the previous provider's.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- constants

# Per-family image budgets, mirroring OMP's provider image budgets; the caller
# caps reinjected frames at these counts.
FAMILY_IMAGE_BUDGETS: dict[str, int] = {
    "anthropic": 90,
    "openai": 200,
    "google": 200,
    "openrouter": 90,
}
DEFAULT_IMAGE_BUDGET = 10

# Hard cap on total decoded frame bytes kept for reinjection.
FRAME_BYTES_BUDGET = 3 * 1024 * 1024

# Substring → api family, checked in order (OMP billingFamily semantics). The
# None/unknown default is "anthropic" — the same safe ceiling OMP's billingFamily
# falls back to.
_FAMILY_TOKENS = (
    (("anthropic", "claude", "kimi"), "anthropic"),
    (("gemini", "google"), "google"),
    (("openrouter",), "openrouter"),
    (("openai", "llamacpp", "vllm", "moonshot", "xai", "codex", "gpt"), "openai"),
)

# Provider names that are local servers or CLI shims — no reliable vision even
# though their api family maps onto a cloud one (claude_cli, codex_cli).
_LOCAL_OR_CLI = ("llamacpp", "vllm", "claude_cli", "codex_cli", "mock")

_CLOUD_FAMILIES = frozenset(FAMILY_IMAGE_BUDGETS)


# --------------------------------------------------------------------------- spec

def spec_for(
    provider_name: str | None = None,
    model: str | None = None,
    cfg: dict | None = None,
    vision: bool | None = None,
) -> dict:
    """Snap-render spec for a provider/model: ``{"api", "model", "model_id",
    "vision"}``.

    ``vision`` resolution: an explicit *vision* wins; else the model catalog is
    probed for the model's vision flag; else a family default (True for cloud
    families, False for local servers / CLI shims).
    """
    family = _family_for(provider_name)
    if vision is None:
        vision = _probe_vision(provider_name or "", model or "", cfg)
    if vision is None:
        vision = _default_vision(provider_name, family)
    return {
        "api": family,
        "model": model or "",
        "model_id": model or "",
        "vision": bool(vision),
    }


def _family_for(provider_name: str | None) -> str:
    """Api family for a provider name (billingFamily semantics; anthropic ceiling)."""
    name = (provider_name or "").lower()
    for tokens, family in _FAMILY_TOKENS:
        if any(token in name for token in tokens):
            return family
    return "anthropic"


def _probe_vision(provider_name: str, model: str, cfg: dict | None) -> bool | None:
    """Best-effort vision flag from the model catalog; None when unresolvable."""
    try:
        from services.agent.model_catalog import resolve_capabilities  # noqa: PLC0415

        caps = resolve_capabilities(provider_name, model, cfg)
        vision = caps.get("vision")
        return vision if isinstance(vision, bool) else None
    except Exception:  # noqa: BLE001 — catalog gaps must not break spec resolution
        return None


def _default_vision(provider_name: str | None, family: str) -> bool:
    """Fallback when the catalog probe fails: cloud families default to vision,
    local servers / CLI shims do not."""
    name = (provider_name or "").lower()
    if any(token in name for token in _LOCAL_OR_CLI):
        return False
    return family in _CLOUD_FAMILIES
