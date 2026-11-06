"""Mega-16 routing fixes — paid openrouter tier parsing + xai spending-limit classification."""

from __future__ import annotations


PAID_DEEPSEEK = "deepseek/deepseek-chat-v3.1"
TIER = f"openrouter:{PAID_DEEPSEEK}"


# ---------------------------------------------------------------------------
# 1. paid DeepSeek tier parsing
# ---------------------------------------------------------------------------


def test_parse_openrouter_deepseek_tier():
    from services.agent.tiers import parse_openrouter_tier, profile_tier_for

    assert parse_openrouter_tier(TIER) == PAID_DEEPSEEK
    assert profile_tier_for(TIER) == "frontier"


# ---------------------------------------------------------------------------
# 2. spending-limit → provider_unavailable (no xai retry loop)
# ---------------------------------------------------------------------------


def test_is_provider_unavailable_error_spending_limit():
    from services.agent.role_models import is_provider_unavailable_error

    msg = (
        "xAI subscription tier not API-enabled (Heavy-only gating as of 2026-05): "
        '{"code":"personal-team-blocked:spending-limit","error":'
        '"You have run out of credits or need a Grok subscription."}'
    )
    assert is_provider_unavailable_error(RuntimeError(msg)) is True
    assert is_provider_unavailable_error("spending-limit") is True
    assert is_provider_unavailable_error(ValueError("incorrect api key")) is False


def test_classify_provider_error_spending_limit_not_auth():
    from services.agent.agent_turn import classify_provider_error

    class Fake403(RuntimeError):
        status_code = 403

    exc = Fake403(
        "XaiTierDeniedError: personal-team-blocked:spending-limit "
        "You have run out of credits"
    )
    stop, retryable = classify_provider_error(exc)
    assert stop == "provider_unavailable", (stop, retryable)
    assert retryable is False
