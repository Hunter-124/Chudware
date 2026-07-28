"""Subscription tiers and the concrete limits each one grants.

Pure data plus a lookup. This module NEVER imports stripe and performs no I/O,
so it is trivially unit-testable and safe to import anywhere (including the
session/workspace hot paths that will eventually enforce these caps).

The limits map onto settings the gateway already enforces:
  - max_concurrent_sessions  -> config.settings.max_sessions_per_user
  - heavy_allowed            -> auth.User.heavy (GPU seat eligibility)
  - storage_quota_bytes      -> config.settings.max_user_disk_bytes
  - monthly_llm_tokens       -> chat/provider budget (enforcement wiring TBD)

The numeric values below are PROVISIONAL product decisions — the owner should
confirm them before launch. Stripe price ids are NOT stored here; only the NAME
of the env var that carries each price id, resolved by gateway/billing.py.
"""

from __future__ import annotations

from dataclasses import dataclass

# Env var names (values live in the process environment, resolved by billing.py).
STRIPE_PRICE_PRO_ENV = "CHUDWARE_WEB_STRIPE_PRICE_PRO"
STRIPE_PRICE_TEAM_ENV = "CHUDWARE_WEB_STRIPE_PRICE_TEAM"

_GIB = 1024**3


@dataclass(frozen=True)
class PlanLimits:
    """What one account on this tier may consume. All fields are hard caps."""

    plan: str
    display_name: str
    max_concurrent_sessions: int
    heavy_allowed: bool
    storage_quota_bytes: int
    monthly_llm_tokens: int


@dataclass(frozen=True)
class Plan:
    """A subscription tier. ``stripe_price_env`` is None for non-purchasable tiers."""

    name: str
    display_name: str
    stripe_price_env: str | None
    limits: PlanLimits


PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        display_name="Free",
        stripe_price_env=None,  # not purchasable — the default/landing tier
        limits=PlanLimits(
            plan="free",
            display_name="Free",
            max_concurrent_sessions=1,
            heavy_allowed=False,
            storage_quota_bytes=2 * _GIB,
            monthly_llm_tokens=100_000,
        ),
    ),
    "pro": Plan(
        name="pro",
        display_name="Pro",
        stripe_price_env=STRIPE_PRICE_PRO_ENV,
        limits=PlanLimits(
            plan="pro",
            display_name="Pro",
            max_concurrent_sessions=2,
            heavy_allowed=True,
            storage_quota_bytes=20 * _GIB,
            monthly_llm_tokens=2_000_000,
        ),
    ),
    "team": Plan(
        name="team",
        display_name="Team",
        stripe_price_env=STRIPE_PRICE_TEAM_ENV,
        limits=PlanLimits(
            plan="team",
            display_name="Team",
            max_concurrent_sessions=5,
            heavy_allowed=True,
            storage_quota_bytes=100 * _GIB,
            monthly_llm_tokens=10_000_000,
        ),
    ),
}


def limits_for(plan: str) -> PlanLimits:
    """Return the hard caps for ``plan``. Raises ValueError for unknown names."""
    entry = PLANS.get((plan or "").strip().lower())
    if entry is None:
        raise ValueError(f"unknown plan: {plan!r}")
    return entry.limits


def is_purchasable(plan: str) -> bool:
    """True when ``plan`` is a real tier that can be bought through Checkout."""
    entry = PLANS.get((plan or "").strip().lower())
    return entry is not None and entry.stripe_price_env is not None


__all__ = [
    "PLANS",
    "Plan",
    "PlanLimits",
    "STRIPE_PRICE_PRO_ENV",
    "STRIPE_PRICE_TEAM_ENV",
    "is_purchasable",
    "limits_for",
]
