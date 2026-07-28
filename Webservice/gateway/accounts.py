"""Account store: registration, approval queue, and entitlement decisions.

Replaces the hand-edited users.yaml allowlist as the authorization source of
truth for public sign-up. An unknown-but-authenticated identity (Cloudflare
Access OIDC already proved the email) becomes a *pending* account with NO
session rights until an admin approves it, an auto-approve domain matches, or
a billing entitlement activates it.

Storage is a single SQLite database at ``settings.data_root / "accounts.db"``
using stdlib ``sqlite3`` with ``check_same_thread=False`` and one module-level
lock — the same store pattern as chat.py, but synchronous so it is callable
from both the ASGI middleware and request handlers without an event loop.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request

try:
    from .config import settings
    from .limits import api_writes
except ImportError:  # allow `uvicorn app:app` from the package dir
    from config import settings  # type: ignore
    from limits import api_writes  # type: ignore

log = logging.getLogger("chudware.gateway.accounts")

STATUSES = frozenset({"pending", "active", "denied", "suspended"})
PLANS = frozenset({"free", "pro", "team"})
ROLES = frozenset({"user", "admin"})

router = APIRouter(tags=["accounts"])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    email: str           # PK, lowercased
    display: str
    status: str          # pending | active | denied | suspended
    plan: str            # free | pro | team
    role: str            # user | admin — ONLY meaningful when status == active
    created_at: float    # epoch seconds
    decided_at: float | None
    decided_by: str
    stripe_customer_id: str
    notes: str
    heavy: bool = False  # imported from users.yaml; new accounts never heavy


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
        return "free"
    return account.plan if account.plan in PLANS else "free"


# ---------------------------------------------------------------------------
# SQLite store (single connection, module-level lock)
# ---------------------------------------------------------------------------

# RLock: public functions hold it across read-modify-write while _db() also
# acquires it internally; a plain Lock would self-deadlock.
_DB_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_CONN_PATH: Path | None = None


def _db_path() -> Path:
    path = settings.data_root / "accounts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            email TEXT PRIMARY KEY,
            display TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            plan TEXT NOT NULL DEFAULT 'free',
            role TEXT NOT NULL DEFAULT 'user',
            created_at REAL NOT NULL,
            decided_at REAL,
            decided_by TEXT NOT NULL DEFAULT '',
            stripe_customer_id TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            heavy INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # status: admin queue listing + pending-count badge filter by status.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
    # stripe_customer_id: billing webhook resolves customer → account.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_accounts_stripe ON accounts(stripe_customer_id)"
    )
    conn.commit()
    return conn


def _db() -> sqlite3.Connection:
    """Process-wide connection, re-opened if data_root moves (tests)."""
    global _CONN, _CONN_PATH
    path = _db_path()
    with _DB_LOCK:
        if _CONN is None or _CONN_PATH != path:
            if _CONN is not None:
                try:
                    _CONN.close()
                except Exception:
                    pass
            _CONN = _connect(path)
            _CONN_PATH = path
        return _CONN


def _row_to_account(row: sqlite3.Row | None) -> Account | None:
    if row is None:
        return None
    return Account(
        email=row["email"],
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
    )


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# Registration + lookups
# ---------------------------------------------------------------------------


# Legacy parity: auth.py hard-coded AUTO_APPROVE_DOMAINS = ("ua.edu",) before
# this store existed, and the Cloudflare Access policy gates the same domains
# at the edge. Defaulting to it keeps the existing user base working across the
# change; set the env var to an EMPTY string to close it and route every new
# identity through the approval queue instead.
_LEGACY_AUTO_APPROVE = "ua.edu"


def auto_approve_domains() -> tuple[str, ...]:
    """Domains whose identities self-activate as standard users.

    Read from the environment (CHUDWARE_WEB_AUTO_APPROVE_DOMAINS, comma-
    separated) rather than config.py so this module ships without touching the
    settings dataclass — wire it into Settings later. An explicitly empty value
    disables auto-approval; an UNSET value keeps the legacy behaviour.
    """
    raw = os.environ.get("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS")
    if raw is None:
        raw = _LEGACY_AUTO_APPROVE
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


def _domain_auto_approved(email: str) -> bool:
    at = email.rfind("@")
    if at < 0:
        return False
    domain = email[at + 1:]
    return any(domain == d or domain.endswith("." + d) for d in auto_approve_domains())


def ensure_account(email: str, display: str = "") -> Account:
    """Return the account for ``email``, registering it on first sight.

    Idempotent and cheap: no write when the row already exists. New rows are
    ``pending`` (or ``active`` when the domain is auto-approved) and are ALWAYS
    role ``user`` — admin is only ever granted through the admin API or the
    users.yaml migration, never self-assigned. Never resurrects or overwrites
    an existing (e.g. denied) account.
    """
    email = _normalize_email(email)
    display = (display or "").strip() or email.split("@")[0] or "user"
    with _DB_LOCK:
        conn = _db()
        row = conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            auto = _domain_auto_approved(email)
            now = time.time()
            conn.execute(
                """
                INSERT OR IGNORE INTO accounts
                    (email, display, status, plan, role, created_at, decided_at, decided_by)
                VALUES (?, ?, ?, 'free', 'user', ?, ?, ?)
                """,
                (
                    email,
                    display,
                    "active" if auto else "pending",
                    now,
                    now if auto else None,
                    "auto-domain" if auto else "",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()
    account = _row_to_account(row)
    assert account is not None  # row exists: we just wrote it or it pre-existed
    return account


def get_account(email: str) -> Account | None:
    with _DB_LOCK:
        row = _db().execute(
            "SELECT * FROM accounts WHERE email = ?", (_normalize_email(email),)
        ).fetchone()
    return _row_to_account(row)


def get_account_by_stripe_customer(customer_id: str) -> Account | None:
    customer_id = (customer_id or "").strip()
    if not customer_id:
        return None
    with _DB_LOCK:
        row = _db().execute(
            "SELECT * FROM accounts WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
    return _row_to_account(row)


# ---------------------------------------------------------------------------
# Mutations (admin / billing)
# ---------------------------------------------------------------------------


def adopt_listed(
    email: str,
    *,
    role: str = "user",
    heavy: bool = False,
    display: str = "",
) -> Account | None:
    """Activate a legacy ``users.yaml`` identity that predates this store.

    Used by the auth middleware when an allowlisted teammate shows up before
    (or after) the bulk startup migration ran — they are already an approved
    human, so stranding them in the pending queue would be a regression.

    Deliberately narrow: it only ever moves a ``pending`` row to ``active``. A
    ``denied`` or ``suspended`` account is NOT resurrected, so an admin
    decision always outranks the YAML file.
    """
    email = _normalize_email(email)
    role = role if role in ROLES else "user"
    with _DB_LOCK:
        conn = _db()
        cur = conn.execute(
            """
            UPDATE accounts
               SET status = 'active', role = ?, heavy = ?,
                   display = COALESCE(NULLIF(?, ''), display),
                   decided_at = ?, decided_by = 'users.yaml'
             WHERE email = ? AND status = 'pending'
            """,
            (role, 1 if heavy else 0, display.strip(), time.time(), email),
        )
        conn.commit()
        if cur.rowcount:
            log.info("accounts: adopted %s from users.yaml as %s", email, role)
        row = conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        ).fetchone()
    return _row_to_account(row) if row else None


def set_status(
    email: str,
    status: str,
    *,
    decided_by: str = "",
    notes: str | None = None,
) -> Account | None:
    """Transition an account's status. Role/plan are untouched; a suspended or
    denied admin keeps the role column but loses all rights via is_authorized.
    Role can never change here — self-service escalation is impossible."""
    email = _normalize_email(email)
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with _DB_LOCK:
        conn = _db()
        cur = conn.execute(
            """
            UPDATE accounts
               SET status = ?, decided_at = ?, decided_by = ?,
                   notes = COALESCE(?, notes)
             WHERE email = ?
            """,
            (status, time.time(), decided_by.strip(), notes, email),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        ).fetchone()
    return _row_to_account(row)


def set_plan(email: str, plan: str) -> Account | None:
    email = _normalize_email(email)
    if plan not in PLANS:
        raise ValueError(f"unknown plan: {plan}")
    with _DB_LOCK:
        conn = _db()
        cur = conn.execute(
            "UPDATE accounts SET plan = ? WHERE email = ?", (plan, email)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        ).fetchone()
    return _row_to_account(row)


def set_stripe_customer(email: str, customer_id: str) -> Account | None:
    email = _normalize_email(email)
    customer_id = (customer_id or "").strip()
    with _DB_LOCK:
        conn = _db()
        cur = conn.execute(
            "UPDATE accounts SET stripe_customer_id = ? WHERE email = ?",
            (customer_id, email),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM accounts WHERE email = ?", (email,)
        ).fetchone()
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
    with _DB_LOCK:
        conn = _db()
        if status is None:
            total = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE status = ?", (status,)
            ).fetchone()["n"]
            rows = conn.execute(
                """
                SELECT * FROM accounts WHERE status = ?
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            ).fetchall()
    return [a for a in (_row_to_account(r) for r in rows) if a is not None], int(total)


def pending_count() -> int:
    with _DB_LOCK:
        row = _db().execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE status = 'pending'"
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# users.yaml migration / adoption
# ---------------------------------------------------------------------------


def import_users_yaml(
    path: Path | None = None,
    *,
    decided_by: str = "migration",
) -> dict[str, Any]:
    """Adopt the existing users.yaml allowlist as ACTIVE accounts.

    Preserves role (so current admins keep admin) and heavy. Idempotent:
    existing rows are never touched, so re-running after go-live cannot
    clobber approvals, denials, plans, or stripe ids recorded since.
    """
    path = path or settings.users_file
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    users = data.get("users") if isinstance(data, dict) else data
    if not isinstance(users, list):
        users = []
    imported: list[str] = []
    skipped: list[str] = []
    now = time.time()
    with _DB_LOCK:
        conn = _db()
        for row in users:
            if not isinstance(row, dict):
                continue
            email = _normalize_email(str(row.get("email", "")))
            if not email:
                continue
            display = str(row.get("display") or email.split("@")[0]).strip()
            role = str(row.get("role") or "user").strip().lower()
            if role not in ROLES:
                role = "user"
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO accounts
                    (email, display, status, plan, role, created_at,
                     decided_at, decided_by, notes, heavy)
                VALUES (?, ?, 'active', 'free', ?, ?, ?, ?, ?, ?)
                """,
                (
                    email,
                    display,
                    role,
                    now,
                    now,
                    decided_by,
                    str(row.get("notes") or "").strip(),
                    1 if row.get("heavy") else 0,
                ),
            )
            (imported if cur.rowcount else skipped).append(email)
        conn.commit()
    log.info(
        "users.yaml import: %d imported as active, %d already present",
        len(imported),
        len(skipped),
    )
    return {"imported": imported, "skipped": skipped}


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
        "display": a.display,
        "status": a.status,
        "plan": a.plan,
        "role": a.role,
        "heavy": a.heavy,
        "created_at": a.created_at,
        "decided_at": a.decided_at,
        "decided_by": a.decided_by,
        "stripe_customer_id": a.stripe_customer_id,
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


@router.get("/api/me/account")
async def my_account(request: Request) -> dict[str, Any]:
    """Self-service: the caller's own status/plan, and nothing about anyone else."""
    user = _current_user(request)
    account = get_account(user.email)
    if account is None:
        # Authenticated but never registered (pre-migration users.yaml identity):
        # report a truthful, non-authorized shape without writing anything.
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
