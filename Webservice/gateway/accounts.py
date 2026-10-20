"""Account store: registration, approval queue, and entitlement decisions.

The authorization half of identity. ``authn.py`` proves *who* a caller is; this
module decides *whether they may use the service* — pending / active / denied /
suspended, on which plan, in which role. A newly seen identity is registered as
``pending`` with NO session rights and becomes active either by verifying its
email address (self-serve, onto a trial) or by an admin decision.
Each registration receives an immutable UUID (``accounts.uid``); re-registering
a deleted email creates a new principal. Deletion revokes sessions and engine
access, retains personal trees as tombstones, and refuses an organization owner
until ownership is transferred.

Storage is PostgreSQL via ``db.tx()`` — one pooled connection per call inside a
real transaction, so read-modify-write sequences are serialized by the database
instead of by a process-local lock. Synchronous, so it stays callable from both
the ASGI middleware and request handlers without an event loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

try:
    from .db import tx
    from .limits import api_writes
    from urllib.parse import urlparse
except ImportError:  # allow `uvicorn app:app` from the package dir
    from db import tx  # type: ignore
    from limits import api_writes  # type: ignore
    from urllib.parse import urlparse

log = logging.getLogger("chudware.gateway.accounts")

STATUSES = frozenset({"pending", "active", "denied", "suspended"})
# "trial" is set by self-serve signup; only desktop/studio are self-purchased.
PLANS = frozenset({"trial", "desktop", "studio", "enterprise"})
ROLES = frozenset({"user", "admin"})

router = APIRouter(tags=["accounts"])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    email: str           # PK, lowercased
    uid: str             # immutable UUID; email is never a workspace identity
    display: str
    status: str          # pending | active | denied | suspended
    plan: str            # trial | desktop | studio | enterprise
    role: str            # user | admin — ONLY meaningful when status == active
    created_at: float    # epoch seconds
    decided_at: float | None
    decided_by: str
    stripe_customer_id: str
    notes: str
    heavy: bool = False  # admin-granted; self-registered accounts are never heavy
    trial_ends_at: float | None = None
    subscribed_since: float | None = None
    paid_through: float | None = None
    subscription_id: str = ""
    subscription_status: str = ""
    price_id: str = ""
    credit_balance: int = 0
    credit_ceiling_monthly: int = 0
    ai_grant: bool = False
    is_edu: bool = False


def is_authorized(account: Account | None) -> bool:
    """Session rights exist ONLY for active accounts. Pending/denied/suspended
    identities may hold a valid CF Access session and still get nothing here."""
    return account is not None and account.status == "active"


def effective_role(account: Account | None) -> str:
    """Role as granted by the database — never escalated. Anything that is not
    an active account carrying a known role collapses to plain ``user``."""
    if account is None or account.status != "active":
        return "user"
    return account.role if account.role in ROLES else "user"


def effective_plan(account: Account | None) -> str:
    if account is None or account.status != "active":
        return "trial"
    return account.plan if account.plan in PLANS else "trial"


# ---------------------------------------------------------------------------
# Store (PostgreSQL — schema lives in migrations/0001_init.sql)
# ---------------------------------------------------------------------------


def _row_to_account(row: Mapping[str, Any] | None) -> Account | None:
    if row is None:
        return None
    return Account(
        email=row["email"],
        uid=str(row["uid"]),
        display=row["display"],
        status=row["status"],
        plan=row["plan"],
        role=row["role"],
        created_at=float(row["created_at"]),
        decided_at=None if row["decided_at"] is None else float(row["decided_at"]),
        decided_by=row["decided_by"],
        stripe_customer_id=row["stripe_customer_id"],
        notes=row["notes"],
        heavy=bool(row["heavy"]),
        trial_ends_at=(
            None if row["trial_ends_at"] is None else float(row["trial_ends_at"])
        ),
        subscribed_since=(
            None if row["subscribed_since"] is None else float(row["subscribed_since"])
        ),
        paid_through=(
            None if row["paid_through"] is None else float(row["paid_through"])
        ),
        subscription_id=row["subscription_id"],
        subscription_status=row["subscription_status"],
        price_id=row["price_id"],
        credit_balance=int(row["credit_balance"]),
        credit_ceiling_monthly=int(row["credit_ceiling_monthly"]),
        ai_grant=bool(row["ai_grant"]),
        is_edu=bool(row["is_edu"]),
    )


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _cells():
    # cells resolves accounts; keep the reverse import lazy.
    try:
        from . import cells
    except ImportError:
        import cells  # type: ignore
    return cells

def delete_account(email: str) -> Account | None:
    """Revoke access and retain deleted personal files before removing identity."""
    email = _normalize_email(email)
    with tx() as cur:
        # Follow organization writers' lock order; never delete their owner
        # while a transfer or a membership mutation is being authorized.
        cur.execute(
            "SELECT o.id FROM organizations o JOIN org_members m ON m.org_id = o.id"
            " WHERE m.email = %s ORDER BY o.id FOR UPDATE OF o",
            (email,),
        )
        cur.fetchall()
        cur.execute("SELECT * FROM accounts WHERE email = %s FOR UPDATE", (email,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT 1 FROM org_members WHERE email = %s AND role = 'owner'", (email,)
        )
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail="transfer organization ownership before deleting account")
        account = _row_to_account(row)
        assert account is not None
        cur.execute("UPDATE accounts SET status = 'suspended' WHERE email = %s", (email,))
        cur.execute(
            "UPDATE sessions SET revoked_at = now() WHERE email = %s AND revoked_at IS NULL",
            (email,),
        )
        _cells().revoke(email)
        _cells().tombstone_personal(email, uid=account.uid)
        cur.execute("DELETE FROM accounts WHERE email = %s", (email,))
    try:
        from .chat import hub
    except ImportError:
        from chat import hub  # type: ignore
    hub.revoke(email)
    return account



# ---------------------------------------------------------------------------
# Registration + lookups
# ---------------------------------------------------------------------------


def ensure_account(email: str, display: str = "") -> Account:
    """Return the account for ``email``, registering it on first sight.

    Idempotent and cheap: no write when the row already exists. New rows are
    ALWAYS ``pending`` and ALWAYS role ``user`` — admin is only ever granted
    through the admin API, never self-assigned, and activation is earned by
    verifying the address (``authn.start_trial``) or granted by an admin.
    Never resurrects or overwrites an existing (e.g. denied) account.
    """
    email = _normalize_email(email)
    display = (display or "").strip() or email.split("@")[0] or "user"
    with tx() as cur:
        cur.execute("SELECT * FROM accounts WHERE email = %s", (email,))
        row = cur.fetchone()
        if row is None:
            # Two callers racing a first sighting both reach here; the loser's
            # INSERT blocks on the primary key, then no-ops, and the re-SELECT
            # (READ COMMITTED takes a fresh snapshot per statement) returns the
            # winner's row. One row, one created_at, no lock held in Python.
            cur.execute(
                """
                INSERT INTO accounts
                    (email, display, status, plan, role, created_at, decided_at, decided_by)
                VALUES (%s, %s, 'pending', 'trial', 'user', %s, NULL, '')
                ON CONFLICT (email) DO NOTHING
                """,
                (email, display, time.time()),
            )
            cur.execute("SELECT * FROM accounts WHERE email = %s", (email,))
            row = cur.fetchone()
    account = _row_to_account(row)
    assert account is not None  # row exists: we just wrote it or it pre-existed
    return account


def get_account(email: str) -> Account | None:
    with tx() as cur:
        cur.execute(
            "SELECT * FROM accounts WHERE email = %s", (_normalize_email(email),)
        )
        row = cur.fetchone()
    return _row_to_account(row)


def get_account_by_stripe_customer(customer_id: str) -> Account | None:
    customer_id = (customer_id or "").strip()
    if not customer_id:
        return None
    with tx() as cur:
        cur.execute(
            "SELECT * FROM accounts WHERE stripe_customer_id = %s", (customer_id,)
        )
        row = cur.fetchone()
    return _row_to_account(row)


# ---------------------------------------------------------------------------
# Mutations (admin / billing)
# ---------------------------------------------------------------------------


def set_status(
    email: str,
    status: str,
    *,
    decided_by: str = "",
    notes: str | None = None,
) -> Account | None:
    """Transition an account's status. Role/plan are untouched; a suspended or
    denied admin keeps the role column but loses all rights via is_authorized.
    Suspension/denial revoke every session atomically with the decision, so
    reinstatement never revives a dormant bearer. Session issuance must lock
    the same account row before checking status and inserting its session.
    Role can never change here — self-service escalation is impossible."""
    email = _normalize_email(email)
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with tx() as cur:
        cur.execute(
            """
            UPDATE accounts
               SET status = %s, decided_at = %s, decided_by = %s,
                   notes = COALESCE(%s, notes)
             WHERE email = %s
         RETURNING *
            """,
            (status, time.time(), decided_by.strip(), notes, email),
        )
        row = cur.fetchone()
        if row is not None and status in {"suspended", "denied"}:
            cur.execute(
                "UPDATE sessions SET revoked_at = now()"
                " WHERE email = %s AND revoked_at IS NULL",
                (email,),
            )
    if row is not None and status != "active":
        # Status changes also remove live chat access, even for idle sockets.
        # Import locally: chat -> orgs -> accounts is the normal import path.
        try:
            from .chat import hub
        except ImportError:
            from chat import hub  # type: ignore
        hub.revoke(email)
    if row is not None:
        _cells().revoke(email)
    return _row_to_account(row)


def set_plan(email: str, plan: str) -> Account | None:
    email = _normalize_email(email)
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    with tx() as cur:
        cur.execute(
            "UPDATE accounts SET plan = %s WHERE email = %s RETURNING *", (plan, email)
        )
        row = cur.fetchone()
    if row is not None:
        _cells().revoke(email)
    return _row_to_account(row)


def set_role(email: str, role: str) -> Account | None:
    """Grant or revoke the admin role.

    Deliberately NOT reachable from the HTTP API: role is the one column an
    authenticated caller must never be able to move, so the only writer is the
    operator CLI (``python -m authn grant-admin``) running against the store
    with database credentials. That replaces the hand-written
    ``UPDATE accounts SET role='admin'`` the cutover had to use because no
    mechanism existed at all.
    """
    email = _normalize_email(email)
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    with tx() as cur:
        cur.execute(
            "UPDATE accounts SET role = %s WHERE email = %s RETURNING *", (role, email)
        )
        row = cur.fetchone()
    if row is not None:
        _cells().revoke(email)
    return _row_to_account(row)


def set_stripe_customer(email: str, customer_id: str) -> Account | None:
    email = _normalize_email(email)
    customer_id = (customer_id or "").strip()
    with tx() as cur:
        cur.execute(
            "UPDATE accounts SET stripe_customer_id = %s WHERE email = %s RETURNING *",
            (customer_id, email),
        )
        row = cur.fetchone()
    return _row_to_account(row)


def set_subscription(
    email: str,
    *,
    subscription_id: str = "",
    subscription_status: str = "",
    price_id: str = "",
    subscribed_since: float | None = None,
    paid_through: float | None = None,
    is_edu: bool,
) -> Account | None:
    """Persist Stripe lifecycle state without changing approval status or plan.

    Empty identifiers leave their current value intact. ``subscribed_since`` is
    first-write-wins; ``paid_through`` only advances, so an older replay or an
    out-of-order invoice cannot shorten an entitlement.
    """
    email = _normalize_email(email)
    with tx() as cur:
        cur.execute(
            """
            UPDATE accounts
               SET subscription_id = CASE WHEN %s = '' THEN subscription_id ELSE %s END,
                   subscription_status = CASE WHEN %s = '' THEN subscription_status ELSE %s END,
                   price_id = CASE WHEN %s = '' THEN price_id ELSE %s END,
                   subscribed_since = COALESCE(subscribed_since, %s),
                   paid_through = CASE
                       WHEN CAST(%s AS DOUBLE PRECISION) IS NULL THEN paid_through
                       WHEN paid_through IS NULL OR paid_through < %s THEN %s
                       ELSE paid_through
                   END,
                   is_edu = %s
             WHERE email = %s
         RETURNING *
            """,
            (
                subscription_id,
                subscription_id,
                subscription_status,
                subscription_status,
                price_id,
                price_id,
                subscribed_since,
                paid_through,
                paid_through,
                paid_through,
                is_edu,
                email,
            ),
        )
        row = cur.fetchone()
    if row is not None:
        _cells().revoke(email)
    return _row_to_account(row)


def list_accounts(
    status: str | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Account], int]:
    """(page, total) for the admin queue. ``status=None`` lists everything."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    with tx() as cur:
        if status is None:
            cur.execute("SELECT COUNT(*) AS n FROM accounts")
            total = cur.fetchone()["n"]
            cur.execute(
                "SELECT * FROM accounts ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE status = %s", (status,)
            )
            total = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT * FROM accounts WHERE status = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
                """,
                (status, limit, offset),
            )
        rows = cur.fetchall()
    return [a for a in (_row_to_account(r) for r in rows) if a is not None], int(total)


def pending_count() -> int:
    with tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE status = 'pending'")
        row = cur.fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Pending/denied page data (rendering is the caller's job)
# ---------------------------------------------------------------------------


def pending_page_data(account: Account) -> dict[str, Any]:
    """What a non-active caller should be told. Pure data — no HTML here."""
    if account.status == "pending":
        headline = "Your account is awaiting approval"
        detail = (
            "Thanks for signing up. An administrator reviews new accounts; "
            "you'll get access as soon as yours is approved."
        )
        next_step = "Sit tight — no action needed. Check back later."
    elif account.status == "denied":
        headline = "Your account request was not approved"
        detail = "This registration was reviewed and denied."
        next_step = "Contact the site administrator if you believe this is a mistake."
    elif account.status == "suspended":
        headline = "Your account is suspended"
        detail = "Access to this account has been suspended."
        next_step = "Contact the site administrator to reinstate access."
    else:  # active — shouldn't reach a denial page, but stay truthful
        headline = "Your account is active"
        detail = "You have full access."
        next_step = "Reload the page to continue."
    return {
        "email": account.email,
        "display": account.display,
        "status": account.status,
        "registered_at": account.created_at,
        "decided_at": account.decided_at,
        "headline": headline,
        "detail": detail,
        "next_step": next_step,
    }


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def _current_user(request: Request) -> Any:
    """Local mirror of auth.get_current_user (duck-typed so accounts.py stays
    importable without the auth module). AuthMiddleware populates state.user."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def _require_admin(request: Request) -> Any:
    """Local equivalent of app._require_admin — same semantics, no app.py import."""
    user = _current_user(request)
    if getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _account_dict(a: Account) -> dict[str, Any]:
    return {
        "email": a.email,
        "uid": a.uid,
        "display": a.display,
        "status": a.status,
        "plan": a.plan,
        "role": a.role,
        "heavy": a.heavy,
        "created_at": a.created_at,
        "decided_at": a.decided_at,
        "decided_by": a.decided_by,
        "stripe_customer_id": a.stripe_customer_id,
        "trial_ends_at": a.trial_ends_at,
        "subscribed_since": a.subscribed_since,
        "paid_through": a.paid_through,
        "subscription_id": a.subscription_id,
        "subscription_status": a.subscription_status,
        "price_id": a.price_id,
        "credit_balance": a.credit_balance,
        "credit_ceiling_monthly": a.credit_ceiling_monthly,
        "ai_grant": a.ai_grant,
        "is_edu": a.is_edu,
        "notes": a.notes,
    }


@router.get("/api/admin/accounts")
async def admin_list_accounts(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: Any = Depends(_require_admin),
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(STATUSES)}")
    page, total = list_accounts(status, limit=limit, offset=offset)
    return {
        "accounts": [_account_dict(a) for a in page],
        "total": total,
        "limit": limit,
        "offset": offset,
        "pending": pending_count(),
    }


@router.get("/api/admin/accounts/pending-count")
async def admin_pending_count(_admin: Any = Depends(_require_admin)) -> dict[str, int]:
    return {"pending": pending_count()}


def _mutate(request: Request, email: str, fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Shared mutation wrapper: rate-limit keyed on the ADMIN's email, then apply."""
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin account writes")
    account = fn(email, *args, **kwargs)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"ok": True, "account": _account_dict(account), "pending": pending_count()}


@router.delete("/api/admin/accounts/{email}")
async def admin_delete_account(email: str, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin account writes")
    account = delete_account(email)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"ok": True, "email": account.email}


@router.post("/api/admin/accounts/{email}/approve")
async def admin_approve_account(email: str, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    return _mutate(request, email, set_status, "active", decided_by=admin.email)


@router.post("/api/admin/accounts/{email}/deny")
async def admin_deny_account(email: str, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    return _mutate(request, email, set_status, "denied", decided_by=admin.email)


@router.post("/api/admin/accounts/{email}/suspend")
async def admin_suspend_account(email: str, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    return _mutate(request, email, set_status, "suspended", decided_by=admin.email)


@router.post("/api/admin/accounts/{email}/plan")
async def admin_set_plan(
    email: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    plan = str(body.get("plan") or "").strip().lower()
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"plan must be one of {sorted(PLANS)}")
    return _mutate(request, email, set_plan, plan)


@router.get("/api/admin/accounts/{email}/security-policy")
async def admin_get_security_policy(email: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    account = get_account(email)
    if account is None or account.plan != "enterprise":
        raise HTTPException(status_code=404, detail="enterprise account not found")
    with tx() as cur:
        cur.execute(
            "SELECT a.policy_id, a.endpoint FROM account_security_policies a "
            "WHERE a.email = %s", (account.email,),
        )
        row = cur.fetchone()
    return {"email": account.email, "policy": "standard" if row is None else row["policy_id"],
            "endpoint": None if row is None else row["endpoint"]}


@router.put("/api/admin/accounts/{email}/security-policy")
async def admin_set_security_policy(email: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _require_admin(request)
    api_writes.enforce(admin.email, "admin security policy writes")
    account = get_account(email)
    if account is None or account.plan != "enterprise":
        raise HTTPException(status_code=404, detail="enterprise account not found")
    if not isinstance(body, dict) or set(body) != {"policy", "endpoint"}:
        raise HTTPException(status_code=400, detail="invalid security policy assignment")
    policy = body.get("policy")
    endpoint = body.get("endpoint")
    if policy not in {"standard", "minimized", "customer_endpoint"}:
        raise HTTPException(status_code=400, detail="invalid security policy")
    if policy == "customer_endpoint":
        parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="customer endpoint must be HTTPS")
    elif endpoint is not None:
        raise HTTPException(status_code=400, detail="endpoint is only valid for customer_endpoint")
    with tx() as cur:
        cur.execute(
            "INSERT INTO account_security_policies (email,policy_id,endpoint,assigned_at,assigned_by) "
            "VALUES (%s,%s,%s,now(),%s) ON CONFLICT (email) DO UPDATE SET "
            "policy_id=EXCLUDED.policy_id,endpoint=EXCLUDED.endpoint,"
            "assigned_at=EXCLUDED.assigned_at,assigned_by=EXCLUDED.assigned_by",
            (account.email, policy, endpoint, admin.email),
        )
    return {"ok": True, "email": account.email, "policy": policy, "endpoint": endpoint}


@router.get("/api/me/account")
async def my_account(request: Request) -> dict[str, Any]:
    """Self-service: the caller's own status/plan, and nothing about anyone else."""
    user = _current_user(request)
    account = get_account(user.email)
    if account is None:
        # Authenticated but never registered — report a truthful,
        # non-authorized shape without writing anything.
        return {"email": user.email, "registered": False, "status": None, "plan": None}
    return {
        "email": account.email,
        "display": account.display,
        "registered": True,
        "status": account.status,
        "plan": effective_plan(account),
        "role": effective_role(account),
        "created_at": account.created_at,
    }
