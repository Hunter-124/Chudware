"""Commercial subscription tiers and the hard limits each tier grants.

This module is pure data: Stripe price ids stay in deployment configuration,
while the env-var names live beside the plans so provisioning and billing share
one vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

STRIPE_PRICE_DESKTOP_ENV = "CHUDWARE_STRIPE_PRICE_DESKTOP"
STRIPE_PRICE_DESKTOP_EDU_ENV = "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU"
STRIPE_PRICE_STUDIO_ENV = "CHUDWARE_STRIPE_PRICE_STUDIO"
STRIPE_PRICE_STUDIO_EDU_ENV = "CHUDWARE_STRIPE_PRICE_STUDIO_EDU"

_GIB = 1024**3
_TIB = 1024**4


@dataclass(frozen=True)
class PlanLimits:
    plan: str
    display_name: str
    desktop: bool
    cloud: bool
    storage_quota_bytes: int
    included_credits_monthly: int


@dataclass(frozen=True)
class Plan:
    name: str
    display_name: str
    stripe_price_env: str | None
    stripe_price_env_edu: str | None
    limits: PlanLimits


PLANS: dict[str, Plan] = {
    "trial": Plan(
        name="trial",
        display_name="Trial",
        stripe_price_env=None,
        stripe_price_env_edu=None,
        limits=PlanLimits("trial", "Trial", True, True, 5 * _GIB, 0),
    ),
    "desktop": Plan(
        name="desktop",
        display_name="Desktop",
        stripe_price_env=STRIPE_PRICE_DESKTOP_ENV,
        stripe_price_env_edu=STRIPE_PRICE_DESKTOP_EDU_ENV,
        limits=PlanLimits("desktop", "Desktop", True, False, 5 * _GIB, 0),
    ),
    "studio": Plan(
        name="studio",
        display_name="Studio",
        stripe_price_env=STRIPE_PRICE_STUDIO_ENV,
        stripe_price_env_edu=STRIPE_PRICE_STUDIO_EDU_ENV,
        limits=PlanLimits("studio", "Studio", True, True, 100 * _GIB, 0),
    ),
    "enterprise": Plan(
        name="enterprise",
        display_name="Enterprise",
        stripe_price_env=None,
        stripe_price_env_edu=None,
        limits=PlanLimits("enterprise", "Enterprise", True, True, _TIB, 0),
    ),
}


def limits_for(plan: str) -> PlanLimits:
    """Return the hard limits for ``plan``; reject unknown tier names."""
    entry = PLANS.get((plan or "").strip().lower())
    if entry is None:
        raise ValueError(f"unknown plan: {plan!r}")
    return entry.limits


def is_purchasable(plan: str) -> bool:
    """Whether the tier can be bought through self-service Checkout."""
    entry = PLANS.get((plan or "").strip().lower())
    return bool(
        entry
        and entry.stripe_price_env is not None
        and entry.stripe_price_env_edu is not None
    )


__all__ = [
    "PLANS",
    "Plan",
    "PlanLimits",
    "STRIPE_PRICE_DESKTOP_EDU_ENV",
    "STRIPE_PRICE_DESKTOP_ENV",
    "STRIPE_PRICE_STUDIO_EDU_ENV",
    "STRIPE_PRICE_STUDIO_ENV",
    "is_purchasable",
    "limits_for",
]
