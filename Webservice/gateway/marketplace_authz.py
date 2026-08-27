"""Authorization helpers shared by every marketplace route module.

The gateway already carries seven near-identical copies of ``_require_admin``,
one per feature module. That duplication is existing debt and this module exists
so the marketplace does not add six more to it: ``marketplace.py``,
``marketplace_commerce.py``, ``marketplace_sources.py``,
``marketplace_render.py``, ``admin_marketplace.py`` and ``admin_billing.py`` all
import from here.

The important distinction the marketplace introduces, which no existing module
needed, is between *anonymous* and *unauthenticated*. The storefront is public:
browse, listing detail and generated previews must serve a visitor with no
session, because a marketplace nobody can link to is not a marketplace. But
those same routes render differently for a signed-in caller (owned, favourited,
can-review) and must never leak that state to the wrong person. So there are
three entry points, and choosing the wrong one is the bug class:

``optional_user``   public route, personalised when a session happens to exist
``require_user``    any authenticated route
``require_admin``   operator-only

``optional_user`` returns ``None`` rather than raising, and it is the ONLY one
that may be used on a route reachable without a session.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from auth import AuthError, User, get_current_user
from config import settings


def marketplace_enabled_or_404() -> None:
    """Refuse every marketplace route when the feature is switched off.

    404, not 503: a disabled marketplace should be indistinguishable from one
    that was never built, so a probe of a deployment that does not sell anything
    learns nothing about what it could be made to sell.
    """
    if not settings.marketplace_enabled:
        raise HTTPException(status_code=404, detail="not found")


def optional_user(request: Request) -> User | None:
    """The caller's identity if they have one, else ``None``.

    ``AuthMiddleware`` soft-attempts authentication on public paths and leaves
    ``request.state.user`` unset when it fails, so reading the attribute is the
    whole implementation. It is a named function anyway because
    ``getattr(request.state, "user", None)`` open-coded at ten call sites is ten
    chances to write ``get_current_user`` by reflex and 401 an anonymous visitor
    on the storefront.
    """
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    """The caller, or a 401 response.

    This does NOT just forward to ``get_current_user``, and the reason is subtle
    enough to be worth spelling out.

    ``auth.get_current_user`` raises ``AuthError``, which is a plain
    ``Exception``. That is fine everywhere else in the gateway because
    ``AuthMiddleware`` authenticates BEFORE the route runs and converts its own
    ``AuthError`` into a response. But ``/api/marketplace`` is in
    ``auth.PUBLIC_PATHS``, and on a public path the middleware short-circuits --
    it soft-attaches a user if one is present and then calls straight through
    with no exception handling around the downstream app. An ``AuthError``
    raised inside a handler on a public path therefore propagates as an
    unhandled exception and Starlette answers 500.

    That is not theoretical: every authenticated write in the public subtree
    (favourite, review, report, checkout, tip, collections, gated download)
    returned 500 instead of 401 for an anonymous caller until this translation
    existed, and the storefront branches on 401 to open its sign-in prompt.

    Translating to ``HTTPException`` here keeps the fix in one place instead of a
    try/except at ~15 call sites, and preserves ``AuthError``'s status so a
    suspended account still reads as 403 rather than being flattened to 401.
    """
    try:
        return get_current_user(request)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status or 401, detail=exc.message or "not authenticated"
        ) from exc


def require_admin(request: Request) -> User:
    """The caller if they are an operator, else 403.

    Same ``AuthError`` translation as :func:`require_user` and for the same
    reason: ``/api/admin/marketplace`` is not itself public, but this helper is
    also called from routes that are, and a helper whose failure mode depends on
    which prefix invoked it is a trap.

    Authenticates first, so an anonymous caller gets 401 from the identity check
    rather than 403 from the role check -- matching ``accounts.py:423`` and every
    other admin gate in the gateway.
    """
    user = require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


class MarketplaceError(HTTPException):
    """A marketplace failure with a stable machine-readable code.

    The front end has to branch on *why* something failed — a paid listing needs
    a checkout prompt, an unverified Connect account needs an onboarding link, an
    insufficient credit balance needs a top-up link — and matching on prose
    breaks the moment the prose improves. ``detail`` therefore carries both:
    ``{"code": ..., "detail": ...}``, and FastAPI serialises it as the body.

    ``code`` values in use, all of which the UI branches on:
      ``not_a_seller``            caller has no mk_sellers row
      ``publishing_blocked``      operator barred this seller from writing
      ``connect_incomplete``      cash sale attempted without payouts enabled
      ``insufficient_credits``    credit purchase exceeds the balance
      ``already_owned``           buyer already holds an entitlement
      ``not_purchasable``         listing is free, unlisted, or removed
      ``self_purchase``           seller tried to buy their own listing
      ``attestation_required``    human-made claim filed without an attestation
      ``license_not_sellable``    open/NC license attached to a paid listing
      ``asset_too_large``         over settings.marketplace_max_asset_bytes
      ``quota_exceeded``          over settings.marketplace_seller_quota_bytes
      ``unsupported_format``      extension not in marketplace_schema
      ``source_disabled``         import source switched off
      ``source_no_file_fetch``    source's terms do not permit downloading
      ``stripe_unconfigured``     no secret key on this deployment
    """

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "detail": detail})
        self.code = code


def require_seller(request: Request, seller_row: Any) -> User:
    """The caller, having confirmed they are a seller in good standing.

    ``seller_row`` is whatever the caller already fetched from ``mk_sellers``, so
    this does no I/O and cannot be the source of an extra query. Passing ``None``
    is the "not enrolled yet" path and yields a code the studio UI turns into the
    enrolment form rather than an error toast.
    """
    user = require_user(request)
    if seller_row is None:
        raise MarketplaceError(403, "not_a_seller", "open a seller account first")
    if seller_row["publishing_blocked"]:
        reason = (seller_row["blocked_reason"] or "").strip()
        raise MarketplaceError(
            403,
            "publishing_blocked",
            reason or "publishing is disabled for this account",
        )
    return user
