"""Provider-adapter layer so any API provider can drive the agent loop."""

# Re-export xAI usable gate for registry / status consumers (optional import).
try:
    from .xai_sub_provider import provider_is_usable as xai_provider_is_usable
except Exception:  # noqa: BLE001 — keep package import light if deps missing
    def xai_provider_is_usable() -> bool:  # type: ignore[misc]
        return False

__all__ = ["xai_provider_is_usable"]
