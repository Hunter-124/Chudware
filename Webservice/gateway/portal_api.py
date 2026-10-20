"""Authenticated, customer-facing account data for the web portal.

This module composes the account, billing, credit, auth, and entitlement stores;
it does not own their lifecycle or arithmetic. Every route is behind the normal
``chudware_sid``/Access authentication middleware.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

try:
    from . import accounts, authn, billing, credits, entitlement, releases, orgs
    from .auth import get_current_user
    from .db import tx
    from .limits import api_writes
    from .plans import limits_for
except ImportError:  # uvicorn app:app from package dir
    import orgs
    import accounts  # type: ignore
    import authn  # type: ignore
    import billing  # type: ignore
    import credits  # type: ignore
    import entitlement  # type: ignore
    import releases  # type: ignore
    from auth import get_current_user  # type: ignore
    from db import tx  # type: ignore
    from limits import api_writes  # type: ignore
    from plans import limits_for  # type: ignore

router = APIRouter(prefix="/api/portal", tags=["portal"])


def _account(request: Request) -> tuple[Any, Any]:
    user = get_current_user(request)
    account = accounts.get_account(user.email)
    if account is None:
        raise HTTPException(404, "account not found")
    return user, account


@router.get("/account")
async def portal_account(request: Request) -> dict[str, Any]:
    user, account = _account(request)
    try:
        limits = limits_for(account.plan)
    except ValueError:
        limits = limits_for("trial")
    credential = authn.get_credential(user.email)
    team_id = orgs.selected_workspace_team(request, user.email)
    workspace = orgs.resolve_team_bucket(user.email, team_id, write=False).context() if team_id else None
    paid_cloud = bool(limits.cloud) and not (account.plan == "trial" and authn.trial_expired(user.email))
    return {
        "email": account.email,
        "display": account.display,
        "plan": account.plan,
        "role": account.role,
        "status": account.status,
        "created_at": account.created_at,
        "trial_ends_at": account.trial_ends_at,
        "paid_through": account.paid_through,
        "subscription_status": account.subscription_status,
        "credit_balance": account.credit_balance,
        "credit_ceiling_monthly": account.credit_ceiling_monthly,
        "ai_grant": bool(account.ai_grant),
        "cloud": paid_cloud or bool(workspace and orgs.university_eligible(user.email)),
        "workspace": workspace,
        "desktop": bool(limits.desktop),
        "linked_identities": authn.oauth_providers_for(user.email),
        "has_password": credential is not None,
        "billing_configured": billing.billing_configured(),
    }


@router.post("/password")
async def change_password(request: Request, body: dict[str, Any]) -> dict[str, bool]:
    user, _ = _account(request)
    current = str(body.get("current_password") or "")
    replacement = str(body.get("new_password") or "")
    credential = authn.get_credential(user.email)
    if credential is None or not await authn._password_work(
        authn.verify_password, credential["password_hash"], current
    ):
        raise HTTPException(401, "current password is incorrect")
    authn._check_password_policy(replacement)
    api_writes.enforce(user.email, "portal password changes")
    digest = await authn._password_work(authn.hash_password, replacement)
    # No thread-local DB transaction spans either KDF await. Reauthorize the
    # credential under the account lock so OAuth-only grants or another reset
    # cannot be undone by an in-flight password change.
    with tx():
        authn._lock_account(user.email)
        latest = authn.get_credential(user.email)
        if latest is None or latest["password_hash"] != credential["password_hash"]:
            raise HTTPException(401, "current password is incorrect")
        authn._store_password(user.email, digest, verified=True)
    return {"ok": True}


@router.post("/signout-everywhere")
async def signout_everywhere(request: Request) -> JSONResponse:
    user, _ = _account(request)
    api_writes.enforce(user.email, "portal session revocations")
    deleted = authn.delete_sessions(user.email)
    response = JSONResponse({"ok": True, "sessions_deleted": deleted})
    authn.clear_session_cookie(response, request)
    return response


@router.get("/usage")
async def portal_usage(
    request: Request, days: int = Query(default=30, ge=1, le=366)
) -> dict[str, Any]:
    _, account = _account(request)
    return {
        "credit_balance": account.credit_balance,
        "credit_ceiling_monthly": account.credit_ceiling_monthly,
        "history": credits.spend_history(account.email, days),
        "unit": "credits",
    }


@router.get("/invoices")
async def portal_invoices(
    request: Request, limit: int = Query(default=12, ge=1, le=100)
) -> dict[str, Any]:
    user, _ = _account(request)
    return {"invoices": billing.invoice_history(user.email, limit)}


@router.get("/devices")
async def portal_devices(request: Request) -> dict[str, Any]:
    user, _ = _account(request)
    return {"devices": entitlement.list_devices(user.email)}


@router.delete("/devices/{subject}")
async def portal_revoke_device(subject: str, request: Request) -> dict[str, bool]:
    user, _ = _account(request)
    api_writes.enforce(user.email, "portal device revocations")
    if not entitlement.revoke_device(user.email, subject):
        raise HTTPException(404, "device not found")
    return {"ok": True}


@router.post("/download-token")
async def portal_download_token(request: Request) -> dict[str, Any]:
    """A short-lived credential the Linux install script can present.

    ``curl … | sh`` has no browser session and no desktop entitlement, so
    without this the one-line install could only be offered to anonymous
    downloaders — which would mean either an open artifact route or an install
    command that does not work. The token is stateless, HMAC-signed, expires in
    fifteen minutes, is bound to this account, and is re-checked against the
    account's status when it is redeemed.

    Rate-limited as a write even though it writes nothing: minting is a
    signing operation and the button that calls it is one click.
    """
    user, account = _account(request)
    if account.status != "active":
        raise HTTPException(403, "an active account is required to download releases")
    api_writes.enforce(user.email, "portal download tokens")
    token, expires_in = releases.mint_download_token(user.email)
    # Built from the gateway's own configured public base URL, exactly like the
    # release manifests' download URLs, and never from the Host header — a
    # proxy or an attacker can set that, and this string is a command we are
    # telling a customer to pipe into a shell.
    base = releases._public_base_url()
    return {
        "token": token,
        "expires_in": expires_in,
        "install_command": (
            f"curl -fsSL {base}/install.sh | sh -s -- --token {token}"
        ),
    }


__all__ = ["router"]
