"""Organizations, teams, memberships, and invitations.

Two distinct entities, deliberately:

* an **organization** is the ownership and billing boundary. Exactly one member
  holds ``owner``; ``admin`` runs it day to day; ``member`` is everybody else.
  The owner's plan is what pays for shared storage, so it is re-read live on
  every shared write — a downgrade or a suspension stops the whole org's team
  uploads at the next request, with nothing cached in between.
* a **team** is a collaboration boundary inside one organization and owns
  exactly one shared bucket tree. Org owner/admin reach every team; an ordinary
  org member reaches only the teams it actually belongs to.
  Team ``viewer`` membership grants read access only; explicit membership never
  reduces the organization owner/admin's manager authority.

Authorization is opaque by default: an org or team id that the caller may not
see answers 404, never 403, so the UUIDs cannot be probed to learn which
customers exist. 403 is reserved for "you are a member here, but this action
needs a higher role", which is not a leak because membership is already known
to the caller. The site-wide ``admin`` account role deliberately grants nothing
here: operating the gateway is not the same thing as being inside a customer's
organization, and ``accounts.role`` is never consulted below.
The operator-configured university team additionally requires active, verified
institutional identity even for an org manager. Domain admission enrolls
eligible accounts as ordinary members; it never changes account entitlements.

Storage is PostgreSQL via ``db.tx()`` (schema in migrations 0015 and 0022).
Every related mutation locks its organization row BEFORE checking permissions
and acts in that transaction, serializing ownership transfers, role changes,
removals, and deletions. Changes also revoke idle engine principals immediately.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Request

try:
    from . import accounts, mailer
    from .auth import User, get_current_user
    from .config import settings
    from .db import tx
    from .limits import api_writes
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    import mailer  # type: ignore
    from auth import User, get_current_user  # type: ignore
    from config import settings  # type: ignore
    from db import tx  # type: ignore
    from limits import api_writes  # type: ignore

log = logging.getLogger("chudware.gateway.orgs")

router = APIRouter(prefix="/api/orgs", tags=["organizations"])

ORG_ROLES = frozenset({"owner", "admin", "member"})
# Roles that may invite, manage members, and reach every team in the org.
ORG_MANAGER_ROLES = frozenset({"owner", "admin"})
# Roles an invitation may carry. Nobody is ever invited straight to 'owner';
# ownership only moves through an explicit transfer by the incumbent.
INVITABLE_ROLES = frozenset({"admin", "member"})
TEAM_ROLES = frozenset({"manager", "member", "viewer"})

# Plans that may own an organization and therefore fund its shared storage.
# Trial and single-seat desktop are personal products; letting one own a team
# bucket would hand an unlimited number of seats a personal-plan quota.
SHARED_PLANS = frozenset({"studio", "enterprise"})

GENERAL_TEAM_NAME = "General"

MAX_NAME_LEN = 80
# Abuse ceilings. api_writes already rate-limits the mutations; these bound the
# steady state a single account can create rather than the rate it does it at.
MAX_ORGS_PER_OWNER = 10
MAX_TEAMS_PER_ORG = 50


# ---------------------------------------------------------------------------
# Value handling
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value: datetime | None) -> float | None:
    """TIMESTAMPTZ -> epoch seconds, matching the portal's other timestamps."""
    return None if value is None else value.timestamp()


def canonical_uuid(value: Any) -> str:
    """Return ``value`` as canonical lowercase UUID text, or raise 404.

    Both an org/team id from a URL and the ``?team=`` bucket selector end up as
    filesystem path components, so nothing but the exact canonical form is
    accepted — not braces, not a URN, not uppercase, not a bare hex run. 404
    rather than 400 so a malformed id and an id belonging to somebody else are
    indistinguishable from outside.
    """
    raw = value.strip() if isinstance(value, str) else ""
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="not found") from None
    text = str(parsed)
    if text != raw.lower():
        raise HTTPException(status_code=404, detail="not found")
    return text


def _clean_name(raw: Any, what: str) -> str:
    """Collapse whitespace and bound the length of an org/team name."""
    name = " ".join(str(raw or "").split())
    if not name:
        raise HTTPException(status_code=400, detail=f"{what} is required")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(
            status_code=400, detail=f"{what} is limited to {MAX_NAME_LEN} characters"
        )
    return name


def _clean_email(raw: Any) -> str:
    email = str(raw or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise HTTPException(status_code=400, detail="a valid email address is required")
    if len(email) > 254:
        raise HTTPException(status_code=400, detail="email address is too long")
    return email


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invite_ttl() -> timedelta:
    return timedelta(days=max(1, int(settings.org_invite_ttl_days)))


def team_bucket_url(team_id: str) -> str:
    """Destination the portal links to for a team's shared files."""
    return f"/bucket?team={team_id}"


def _body(body: Any) -> dict[str, Any]:
    return body if isinstance(body, dict) else {}


def _chat_hub():
    # chat imports orgs for team authorization; defer the reverse import.
    try:
        from .chat import hub
    except ImportError:
        from chat import hub  # type: ignore
    return hub


def _cells():
    try:
        from . import cells
    except ImportError:
        import cells  # type: ignore
    return cells


def _lock_org(cur, org_id: str) -> None:
    # Every membership mutation locks the parent before reading permissions.
    # READ COMMITTED then observes transfers/removals that completed while waiting.
    cur.execute("SELECT id FROM organizations WHERE id = %s FOR UPDATE", (org_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="organization not found")

# ---------------------------------------------------------------------------
# Authorization primitives (cursor-level: they run inside the caller's own
# transaction, so nothing they assert can go stale before the write lands)
# ---------------------------------------------------------------------------


def _org_and_role(cur, org_id: str, email: str) -> tuple[dict[str, Any], str]:
    cur.execute(
        """
        SELECT o.id, o.name, o.created_at,
               m.role AS caller_role,
               owner.email AS owner_email,
               (SELECT count(*) FROM org_members c WHERE c.org_id = o.id) AS member_count
          FROM organizations o
          LEFT JOIN org_members m     ON m.org_id = o.id AND m.email = %s
          LEFT JOIN org_members owner ON owner.org_id = o.id AND owner.role = 'owner'
         WHERE o.id = %s
        """,
        (email, org_id),
    )
    row = cur.fetchone()
    if row is None or row["caller_role"] is None:
        # Not an org at all, or one this caller has no business knowing about.
        raise HTTPException(status_code=404, detail="organization not found")
    return row, row["caller_role"]


def _require_org(
    cur, org_id: str, email: str, *, manage: bool = False, owner: bool = False,
    lock: bool = False,
) -> tuple[dict[str, Any], str]:
    if lock:
        _lock_org(cur, org_id)
    row, role = _org_and_role(cur, org_id, email)
    if owner and role != "owner":
        raise HTTPException(status_code=403, detail="organization owner only")
    if manage and role not in ORG_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="organization admin only")
    return row, role


def _team_role(cur, team_id: str, email: str) -> str | None:
    cur.execute(
        "SELECT role FROM team_members WHERE team_id = %s AND email = %s",
        (team_id, email),
    )
    row = cur.fetchone()
    return None if row is None else row["role"]


def _require_team(
    cur, org_id: str, team_id: str, email: str, org_role: str, *, manage: bool = False
) -> tuple[dict[str, Any], str | None]:
    """Resolve a team the caller may see. Returns (team row, caller team role).

    Team role is None when access comes from org owner/admin authority instead
    of an explicit team_members row — those two roles manage every team.
    """
    cur.execute(
        """
        SELECT t.id, t.org_id, t.name, t.is_general, t.created_at,
               (SELECT count(*) FROM team_members x WHERE x.team_id = t.id) AS member_count
          FROM teams t
         WHERE t.id = %s AND t.org_id = %s
        """,
        (team_id, org_id),
    )
    team = cur.fetchone()
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")
    role = _team_role(cur, team_id, email)
    if org_role in ORG_MANAGER_ROLES:
        return team, role
    if role is None:
        # An ordinary org member cannot even confirm this team exists.
        raise HTTPException(status_code=404, detail="team not found")
    if manage and role != "manager":
        raise HTTPException(status_code=403, detail="team manager only")
    return team, role


def _effective_team_role(team_role: str | None, org_role: str) -> str:
    """What the portal shows for ``teams[].role``.

    An org owner/admin with no team_members row still manages the team, so
    reporting their absence as 'member' would understate what the UI may offer.
    """
    if team_role:
        return team_role
    return "manager" if org_role in ORG_MANAGER_ROLES else "member"


# ---------------------------------------------------------------------------
# Payload shaping
# ---------------------------------------------------------------------------


def _team_payload(row: dict[str, Any], org_role: str) -> dict[str, Any]:
    team_id = str(row["id"])
    return {
        "id": team_id,
        "org_id": str(row["org_id"]),
        "name": row["name"],
        "is_general": bool(row["is_general"]),
        "role": _effective_team_role(row.get("team_role"), org_role),
        "member_count": int(row.get("member_count") or 0),
        "bucket_url": team_bucket_url(team_id),
        "storage_provider": "box" if team_id == settings.university_team_id else "local",
        "created_at": _epoch(row.get("created_at")),
    }


def _invitation_payload(row: dict[str, Any], *, redacted: bool) -> dict[str, Any]:
    """Invitation as seen by an org member.

    Ordinary members get the fact that invitations are outstanding and nothing
    that identifies who was invited or who invited them; org admins get the
    detail they need to manage the list. Neither ever gets the token — the only
    copy of that left the process in the invitation email.
    """
    payload: dict[str, Any] = {
        "id": str(row["id"]),
        "role": row["role"],
        "created_at": _epoch(row["created_at"]),
    }
    if not redacted:
        payload["email"] = row["email"]
        payload["invited_by"] = row["invited_by"]
        payload["expires_at"] = _epoch(row["expires_at"])
    return payload


def _visible_teams(cur, email: str, org_id: str | None = None) -> list[dict[str, Any]]:
    """Every team the caller may see, across all their orgs or within one.

    One statement rather than a per-org loop: the portal's overview renders
    every destination at once and this is the query behind it.
    """
    cur.execute(
        """
        SELECT t.id, t.org_id, t.name, t.is_general, t.created_at,
               m.role AS org_role,
               tm.role AS team_role,
               (SELECT count(*) FROM team_members x WHERE x.team_id = t.id) AS member_count
          FROM teams t
          JOIN org_members m        ON m.org_id = t.org_id AND m.email = %(email)s
          LEFT JOIN team_members tm ON tm.team_id = t.id AND tm.email = %(email)s
         WHERE (%(org_id)s::uuid IS NULL OR t.org_id = %(org_id)s::uuid)
           AND (m.role IN ('owner', 'admin') OR tm.email IS NOT NULL)
         ORDER BY t.is_general DESC, lower(t.name)
        """,
        {"email": email, "org_id": org_id},
    )
    return [_team_payload(row, row["org_role"]) for row in cur.fetchall()]


def _pending_invitations_for(cur, email: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT i.id, i.org_id, o.name AS org_name, i.role, i.invited_by,
               i.created_at, i.expires_at
          FROM org_invitations i
          JOIN organizations o ON o.id = i.org_id
         WHERE lower(i.email) = %s
           AND i.accepted_at IS NULL
           AND i.revoked_at IS NULL
           AND i.expires_at > now()
         ORDER BY i.created_at
        """,
        (email,),
    )
    return [
        {
            "id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "org_name": row["org_name"],
            "role": row["role"],
            "invited_by": row["invited_by"],
            "created_at": _epoch(row["created_at"]),
            "expires_at": _epoch(row["expires_at"]),
        }
        for row in cur.fetchall()
    ]


# ---------------------------------------------------------------------------

def university_eligible(email: str) -> bool:
    """Only active, server-verified ua.edu identities qualify; never claims."""
    if not (settings.university_org_id and settings.university_team_id
            and settings.university_workspace_root):
        return False
    email = (email or "").strip().lower()
    if email.count("@") != 1:
        return False
    domain = email.split("@", 1)[1]
    if domain != "ua.edu" and not (
        domain.endswith(".ua.edu")
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", p)
                for p in domain.split("."))
    ):
        return False
    with tx() as cur:
        cur.execute(
            """SELECT 1 FROM accounts a WHERE a.email = %s AND a.status = 'active'
               AND (EXISTS (SELECT 1 FROM credentials c WHERE c.email = a.email
                            AND c.email_verified = TRUE)
                    OR EXISTS (SELECT 1 FROM oauth_identities i WHERE i.email = a.email))""",
            (email,),
        )
        return cur.fetchone() is not None


def university_team(email: str) -> str:
    """Enroll a proven university identity as an ordinary member, idempotently."""
    if not university_eligible(email):
        return ""
    org_id = canonical_uuid(settings.university_org_id)
    team_id = canonical_uuid(settings.university_team_id)
    with tx() as cur:
        try:
            _lock_org(cur, org_id)
        except HTTPException:
            raise HTTPException(503, "university workspace is not provisioned") from None
        cur.execute("SELECT 1 FROM teams WHERE id = %s AND org_id = %s", (team_id, org_id))
        if cur.fetchone() is None:
            raise HTTPException(503, "university workspace is not provisioned")
        cur.execute(
            "INSERT INTO org_members (org_id,email,role) VALUES (%s,%s,'member') ON CONFLICT DO NOTHING",
            (org_id, email),
        )
        cur.execute(
            "INSERT INTO team_members (team_id,email,role) VALUES (%s,%s,'member') ON CONFLICT DO NOTHING",
            (team_id, email),
        )
    return team_id


def selected_workspace_team(request, email: str, scope: str = "", team: str = "") -> str:
    """Explicit request selection, then browser selection, then university default."""
    selection = scope or team or request.query_params.get("team", "") or request.cookies.get("chudware_workspace", "")
    if selection in {"personal", "shared"}:
        return ""
    if selection:
        return canonical_uuid(selection)
    return university_team(email)


def university_root() -> Path:
    root = Path(settings.university_workspace_root).expanduser()
    # Fail closed when FUSE disappears: never write a disconnected local shadow.
    if not root.is_absolute() or not root.is_mount():
        raise HTTPException(503, "university Box workspace mount is unavailable")
    return root.absolute()

# Shared bucket resolution (consumed by bucket.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeamBucket:
    """One team's shared tree plus the authority that justified reaching it."""

    org_id: str
    org_name: str
    team_id: str
    team_name: str
    org_role: str
    team_role: str  # "" when access derives from org owner/admin authority
    owner_email: str
    owner_plan: str
    root: Path

    def context(self) -> dict[str, Any]:
        """Scope block added to /api/bucket/list."""
        return {
            "id": self.team_id,
            "name": self.team_name,
            "org_id": self.org_id,
            "org_name": self.org_name,
            "role": _effective_team_role(self.team_role or None, self.org_role),
            "org_role": self.org_role,
            "bucket_url": team_bucket_url(self.team_id),
            "storage_provider": "box" if self.team_id == settings.university_team_id else "local",
        }


def resolve_team_bucket(email: str, team_id: str, *, write: bool) -> TeamBucket:
    """Resolve ``team_id`` into a shared root this caller may use right now.

    Read access is team membership, or org owner/admin authority. Write access
    additionally requires the organization to still be funded: the owner
    account must exist, be active, and hold a plan that includes shared
    storage. That check is live on every call, which is what makes a downgrade,
    a suspension, or a removal take effect immediately rather than at the next
    login.
    """
    email = (email or "").strip().lower()
    team_id = canonical_uuid(team_id)
    university = team_id == settings.university_team_id
    if university:
        # Verified UA identities may auto-enroll; existing owners and invited
        # members use the same membership checks as every other team below.
        university_team(email)
    with tx() as cur:
        cur.execute(
            """
            SELECT t.id AS team_id, t.name AS team_name,
                   o.id AS org_id, o.name AS org_name,
                   m.role AS org_role, tm.role AS team_role,
                   owner.email AS owner_email
              FROM teams t
              JOIN organizations o        ON o.id = t.org_id
              LEFT JOIN org_members m     ON m.org_id = o.id AND m.email = %(email)s
              LEFT JOIN team_members tm   ON tm.team_id = t.id AND tm.email = %(email)s
              LEFT JOIN org_members owner ON owner.org_id = o.id AND owner.role = 'owner'
             WHERE t.id = %(team_id)s
            """,
            {"email": email, "team_id": team_id},
        )
        row = cur.fetchone()
    if row is None or row["org_role"] is None:
        raise HTTPException(status_code=404, detail="team not found")
    org_role = row["org_role"]
    team_role = row["team_role"]
    if org_role not in ORG_MANAGER_ROLES and team_role is None:
        raise HTTPException(status_code=404, detail="team not found")
    if write and org_role not in ORG_MANAGER_ROLES and team_role == "viewer":
        raise HTTPException(status_code=403, detail="forbidden_right")

    owner_email = row["owner_email"] or ""
    owner = accounts.get_account(owner_email) if owner_email else None
    owner_plan = owner.plan if owner is not None else ""
    if write and (
        owner is None or owner.status != "active" or owner_plan not in SHARED_PLANS
    ):
        raise HTTPException(
            status_code=402,
            detail=(
                "shared team storage is paused — the organization owner's "
                "account is no longer on an active Studio or Enterprise plan"
            ),
        )
    org_id = str(row["org_id"])
    return TeamBucket(
        org_id=org_id,
        org_name=row["org_name"],
        team_id=team_id,
        team_name=row["team_name"],
        org_role=org_role,
        team_role=team_role or "",
        owner_email=owner_email,
        owner_plan=owner_plan,
        root=university_root() if university else settings.team_bucket_root(org_id, team_id),
    )


# ---------------------------------------------------------------------------
# Invitation mail
# ---------------------------------------------------------------------------


def invite_url(token: str) -> str:
    """Link mailed to an invitee.

    The portal reads ``org_invite`` from the query string and posts the token
    back to /api/orgs/invitations/accept; the ``#team`` fragment lands the
    invitee on the Team & files page once accepted. Built from the configured
    origin, never from a request Host header.
    """
    return f"{settings.public_base_url.rstrip('/')}/?org_invite={token}#team"


def _send_invite_mail(
    *, to: str, org_name: str, inviter: str, role: str, token: str, expires: datetime
) -> None:
    mailer.send(
        to,
        "org_invite",
        {
            "email": to,
            "org_name": org_name,
            "inviter": inviter,
            "role": role,
            "invite_url": invite_url(token),
            "expires_days": max(1, int(settings.org_invite_ttl_days)),
        },
    )


# ---------------------------------------------------------------------------
# Routes — collection
# ---------------------------------------------------------------------------


def _caller(request: Request) -> User:
    return get_current_user(request)


@router.get("")
async def list_organizations(request: Request) -> dict[str, Any]:
    """Every org the caller belongs to, its visible teams, and open invites."""
    university_team(_caller(request).email)
    user = _caller(request)
    email = user.email.lower()
    with tx() as cur:
        cur.execute(
            """
            SELECT o.id, o.name, o.created_at, m.role,
                   owner.email AS owner_email,
                   (SELECT count(*) FROM org_members c WHERE c.org_id = o.id) AS member_count
              FROM org_members m
              JOIN organizations o        ON o.id = m.org_id
              LEFT JOIN org_members owner ON owner.org_id = o.id AND owner.role = 'owner'
             WHERE m.email = %s
             ORDER BY lower(o.name)
            """,
            (email,),
        )
        orgs = cur.fetchall()
        teams = _visible_teams(cur, email)
        invitations = _pending_invitations_for(cur, email)
    by_org: dict[str, list[dict[str, Any]]] = {}
    for team in teams:
        by_org.setdefault(team["org_id"], []).append(team)
    return {
        "organizations": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "role": row["role"],
                "owner_email": row["owner_email"] or "",
                "member_count": int(row["member_count"] or 0),
                "created_at": _epoch(row["created_at"]),
                "teams": by_org.get(str(row["id"]), []),
            }
            for row in orgs
        ],
        "invitations": invitations,
    }


@router.post("", status_code=201)
async def create_organization(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Create an org, its General team, and the caller's owner membership."""
    user = _caller(request)
    email = user.email.lower()
    name = _clean_name(_body(body).get("name"), "organization name")
    api_writes.enforce(email, "organization changes")

    account = accounts.get_account(email)
    if account is None or account.status != "active" or account.plan not in SHARED_PLANS:
        raise HTTPException(
            status_code=402,
            detail="creating an organization requires an active Studio or Enterprise plan",
        )

    org_id = str(uuid.uuid4())
    team_id = str(uuid.uuid4())
    with tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM org_members WHERE email = %s AND role = 'owner'",
            (email,),
        )
        if int((cur.fetchone() or {}).get("n") or 0) >= MAX_ORGS_PER_OWNER:
            raise HTTPException(
                status_code=409,
                detail=f"an account may own at most {MAX_ORGS_PER_OWNER} organizations",
            )
        cur.execute(
            "INSERT INTO organizations (id, name, created_by) VALUES (%s, %s, %s)",
            (org_id, name, email),
        )
        cur.execute(
            "INSERT INTO org_members (org_id, email, role) VALUES (%s, %s, 'owner')",
            (org_id, email),
        )
        cur.execute(
            "INSERT INTO teams (id, org_id, name, is_general, created_by)"
            " VALUES (%s, %s, %s, TRUE, %s)",
            (team_id, org_id, GENERAL_TEAM_NAME, email),
        )
        cur.execute(
            "INSERT INTO team_members (team_id, email, role) VALUES (%s, %s, 'manager')",
            (team_id, email),
        )
    log.info("org created: %s (%s) by %s", name, org_id, email)
    return {
        "id": org_id,
        "name": name,
        "role": "owner",
        "owner_email": email,
        "member_count": 1,
        "teams": [
            {
                "id": team_id,
                "org_id": org_id,
                "name": GENERAL_TEAM_NAME,
                "is_general": True,
                "role": "manager",
                "member_count": 1,
                "bucket_url": team_bucket_url(team_id),
            }
        ],
    }


# ---------------------------------------------------------------------------
# Routes — invitation redemption. Two static segments, so this never collides
# with the single-segment /{org_id} routes below.
# ---------------------------------------------------------------------------


@router.post("/invitations/accept")
async def accept_invitation(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Redeem an invitation addressed to the CALLER's own email.

    Either the emailed token or the invitation id from ``GET /api/orgs`` is
    accepted; both are matched against ``lower(email) = caller``, so knowing an
    id is not enough to join somebody else's organization. The claim, the
    membership, and the General-team join all happen in one transaction, and
    the ``accepted_at IS NULL`` predicate makes the redemption single-use even
    against two concurrent clicks.
    """
    user = _caller(request)
    email = user.email.lower()
    api_writes.enforce(email, "invitation redemptions")
    payload = _body(body)
    token = str(payload.get("token") or "").strip()
    invitation_id = str(payload.get("invitation_id") or "").strip()
    if token:
        predicate, key = "token_hash = %(key)s", _token_hash(token)
    elif invitation_id:
        predicate, key = "id = %(key)s", canonical_uuid(invitation_id)
    else:
        raise HTTPException(status_code=400, detail="token or invitation_id is required")

    with tx() as cur:
        # Resolve without locking the invitation first: all writers take the
        # organization lock before invitation/member locks, including deletion.
        cur.execute(
            f"SELECT org_id FROM org_invitations WHERE {predicate} AND lower(email) = %(email)s",
            {"key": key, "email": email},
        )
        invitation = cur.fetchone()
        if invitation is None:
            raise HTTPException(status_code=404, detail="invitation not found or no longer valid")
        _lock_org(cur, str(invitation["org_id"]))
        cur.execute(
            f"""
            UPDATE org_invitations
               SET accepted_at = now(), accepted_by = %(email)s
             WHERE {predicate}
               AND lower(email) = %(email)s
               AND accepted_at IS NULL
               AND revoked_at IS NULL
               AND expires_at > now()
         RETURNING id, org_id, role
            """,
            {"key": key, "email": email},
        )
        claimed = cur.fetchone()
        if claimed is None:
            raise HTTPException(
                status_code=404, detail="invitation not found or no longer valid"
            )
        org_id = str(claimed["org_id"])
        # An existing membership wins: re-accepting must never downgrade an
        # admin (or, worse, overwrite the owner) with the invitation's role.
        cur.execute(
            "INSERT INTO org_members (org_id, email, role) VALUES (%s, %s, %s)"
            " ON CONFLICT (org_id, email) DO NOTHING",
            (org_id, email, claimed["role"]),
        )
        cur.execute(
            "SELECT id, name FROM teams WHERE org_id = %s AND is_general", (org_id,)
        )
        general = cur.fetchone()
        if general is not None:
            cur.execute(
                "INSERT INTO team_members (team_id, email, role) VALUES (%s, %s, 'member')"
                " ON CONFLICT (team_id, email) DO NOTHING",
                (general["id"], email),
            )
        cur.execute(
            "SELECT o.name, m.role FROM organizations o"
            " JOIN org_members m ON m.org_id = o.id AND m.email = %s"
            " WHERE o.id = %s",
            (email, org_id),
        )
        org = cur.fetchone() or {}
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_id,))
        team_ids = [str(row["id"]) for row in cur.fetchall()]
    log.info("org invite accepted: org=%s by %s", org_id, email)
    for team_id in team_ids:
        _cells().revoke(email, team_id)
    team = (
        None
        if general is None
        else {
            "id": str(general["id"]),
            "name": general["name"],
            "bucket_url": team_bucket_url(str(general["id"])),
        }
    )
    return {
        "ok": True,
        "org": {
            "id": org_id,
            "name": org.get("name", ""),
            "role": org.get("role", "member"),
        },
        "team": team,
    }


# ---------------------------------------------------------------------------
# Routes — one organization
# ---------------------------------------------------------------------------


@router.get("/{org_id}")
async def get_organization(org_id: str, request: Request) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    with tx() as cur:
        org, role = _require_org(cur, org_uuid, email)
        cur.execute(
            """
            SELECT m.email, m.role, m.joined_at, COALESCE(a.display, '') AS display
              FROM org_members m
              LEFT JOIN accounts a ON a.email = m.email
             WHERE m.org_id = %s
             ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
                      m.email
            """,
            (org_uuid,),
        )
        members = [
            {
                "email": row["email"],
                "display": row["display"],
                "role": row["role"],
                "joined_at": _epoch(row["joined_at"]),
            }
            for row in cur.fetchall()
        ]
        teams = _visible_teams(cur, email, org_uuid)
        cur.execute(
            """
            SELECT id, email, role, invited_by, created_at, expires_at
              FROM org_invitations
             WHERE org_id = %s
               AND accepted_at IS NULL
               AND revoked_at IS NULL
               AND expires_at > now()
             ORDER BY created_at
            """,
            (org_uuid,),
        )
        redacted = role not in ORG_MANAGER_ROLES
        invitations = [
            _invitation_payload(row, redacted=redacted) for row in cur.fetchall()
        ]
    return {
        "organization": {
            "id": org_uuid,
            "name": org["name"],
            "role": role,
            "owner_email": org["owner_email"] or "",
            "member_count": int(org["member_count"] or 0),
            "created_at": _epoch(org["created_at"]),
        },
        "members": members,
        "teams": teams,
        "invitations": invitations,
    }


@router.patch("/{org_id}")
async def rename_organization(
    org_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    name = _clean_name(_body(body).get("name"), "organization name")
    api_writes.enforce(email, "organization changes")
    with tx() as cur:
        _require_org(cur, org_uuid, email, manage=True, lock=True)
        cur.execute("UPDATE organizations SET name = %s WHERE id = %s", (name, org_uuid))
    return {"ok": True, "id": org_uuid, "name": name}


@router.delete("/{org_id}")
async def delete_organization(org_id: str, request: Request) -> dict[str, bool]:
    """Delete the org, its teams, memberships, and invitations (owner only).

    The shared files themselves are left on disk. Cascading a customer's data
    off the filesystem from an HTTP DELETE is not recoverable, and the same
    conservative rule already governs the legacy shared tree.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    api_writes.enforce(email, "organization changes")
    with tx() as cur:
        _require_org(cur, org_uuid, email, owner=True, lock=True)
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_uuid,))
        team_ids = [str(row["id"]) for row in cur.fetchall()]
        cur.execute("DELETE FROM organizations WHERE id = %s", (org_uuid,))
    for team_id in team_ids:
        _chat_hub().revoke_team(team_id)
        _cells().revoke_team(team_id)
    log.warning(
        "org deleted: %s by %s — shared files under %s were left in place",
        org_uuid,
        email,
        settings.bucket_root / "orgs" / org_uuid,
    )
    return {"ok": True}


@router.post("/{org_id}/transfer-ownership")
async def transfer_ownership(
    org_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    """Hand the billing root to another member, atomically.

    The incumbent is demoted to admin and the successor promoted inside one
    transaction; the ``org_members_single_owner`` partial unique index means a
    half-applied transfer cannot leave two owners behind. The successor must be
    on a plan that funds shared storage, otherwise the transfer would silently
    freeze every team bucket in the org.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    target = _clean_email(_body(body).get("email"))
    api_writes.enforce(email, "organization changes")
    with tx() as cur:
        _require_org(cur, org_uuid, email, owner=True, lock=True)
        if target == email:
            raise HTTPException(status_code=400, detail="you already own this organization")
        account = accounts.get_account(target)
        if account is None or account.status != "active" or account.plan not in SHARED_PLANS:
            raise HTTPException(
                status_code=402,
                detail="the new owner must hold an active Studio or Enterprise plan",
            )
        cur.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail="that account is not a member of this organization",
            )
        cur.execute(
            "UPDATE org_members SET role = 'admin' WHERE org_id = %s AND role = 'owner'",
            (org_uuid,),
        )
        cur.execute(
            "UPDATE org_members SET role = 'owner' WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_uuid,))
        team_ids = [str(row["id"]) for row in cur.fetchall()]
    log.info("org ownership transferred: %s %s -> %s", org_uuid, email, target)
    for team_id in team_ids:
        _cells().revoke_team(team_id)
    return {"ok": True, "owner_email": target}


# ---------------------------------------------------------------------------
# Routes — invitations
# ---------------------------------------------------------------------------


@router.post("/{org_id}/invitations", status_code=201)
async def invite_member(
    org_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    payload = _body(body)
    invitee = _clean_email(payload.get("email"))
    role = str(payload.get("role") or "member").strip().lower()
    if role not in INVITABLE_ROLES:
        raise HTTPException(status_code=400, detail="role must be admin or member")
    api_writes.enforce(email, "organization invitations")

    token = secrets.token_urlsafe(32)
    expires = _now() + _invite_ttl()
    invitation_id = str(uuid.uuid4())
    with tx() as cur:
        org, caller_role = _require_org(cur, org_uuid, email, manage=True, lock=True)
        if role == "admin" and caller_role != "owner":
            raise HTTPException(
                status_code=403,
                detail="only the organization owner can invite administrators",
            )
        cur.execute(
            "SELECT 1 FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, invitee),
        )
        if cur.fetchone() is not None:
            raise HTTPException(status_code=409, detail="that address is already a member")
        # Resending supersedes: exactly one live link per address per org.
        cur.execute(
            "UPDATE org_invitations SET revoked_at = now()"
            " WHERE org_id = %s AND lower(email) = %s"
            "   AND accepted_at IS NULL AND revoked_at IS NULL",
            (org_uuid, invitee),
        )
        cur.execute(
            "INSERT INTO org_invitations"
            " (id, org_id, email, role, token_hash, invited_by, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (invitation_id, org_uuid, invitee, role, _token_hash(token), email, expires),
        )
        org_name = org["name"]
    # Outside the transaction: queuing mail must not hold the invitation's row
    # lock, and a mail failure must not roll back a resendable invitation.
    _send_invite_mail(
        to=invitee,
        org_name=org_name,
        inviter=email,
        role=role,
        token=token,
        expires=expires,
    )
    log.info("org invite sent: org=%s to=%s role=%s by %s", org_uuid, invitee, role, email)
    return {
        "ok": True,
        "invitation": {
            "id": invitation_id,
            "email": invitee,
            "role": role,
            "invited_by": email,
            "expires_at": expires.timestamp(),
        },
    }


@router.delete("/{org_id}/invitations/{invitation_id}")
async def revoke_invitation(
    org_id: str, invitation_id: str, request: Request
) -> dict[str, bool]:
    """Withdraw an outstanding invitation.

    The UPDATE is keyed on (id, org_id) together: an invitation id lifted from
    another organization matches nothing and answers 404, so the id is not a
    cross-tenant handle.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    invite_uuid = canonical_uuid(invitation_id)
    api_writes.enforce(email, "organization invitations")
    with tx() as cur:
        _require_org(cur, org_uuid, email, manage=True, lock=True)
        cur.execute(
            "UPDATE org_invitations SET revoked_at = now()"
            " WHERE id = %s AND org_id = %s"
            "   AND accepted_at IS NULL AND revoked_at IS NULL"
            " RETURNING id",
            (invite_uuid, org_uuid),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="invitation not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — organization members
# ---------------------------------------------------------------------------


@router.patch("/{org_id}/members/{member_email}")
async def set_member_role(
    org_id: str, member_email: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    """Move a member between 'admin' and 'member' (owner only).

    Role promotion is the one lever that hands somebody the ability to reach
    every team's files, so an admin cannot pull it — only the owner can.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    target = _clean_email(member_email)
    role = str(_body(body).get("role") or "").strip().lower()
    api_writes.enforce(email, "organization membership changes")
    if role == "owner":
        raise HTTPException(
            status_code=400, detail="use transfer-ownership to move the owner role"
        )
    if role not in INVITABLE_ROLES:
        raise HTTPException(status_code=400, detail="role must be admin or member")
    with tx() as cur:
        _require_org(cur, org_uuid, email, owner=True, lock=True)
        cur.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
        current = cur.fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="member not found")
        if current["role"] == "owner":
            raise HTTPException(
                status_code=400,
                detail="the owner's role cannot be changed; transfer ownership instead",
            )
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_uuid,))
        team_ids = [str(row["id"]) for row in cur.fetchall()]
        cur.execute(
            "UPDATE org_members SET role = %s WHERE org_id = %s AND email = %s",
            (role, org_uuid, target),
        )
    if role == "member" and current["role"] == "admin":
        # A former org admin may have been on teams without explicit membership.
        # Snapshot the affected teams in the same transaction as the demotion.
        for team_id in team_ids:
            _chat_hub().revoke(target, team_id)
    for team_id in team_ids:
        _cells().revoke(target, team_id)
    log.info("org member role: %s %s -> %s by %s", org_uuid, target, role, email)
    return {"ok": True, "email": target, "role": role}


@router.delete("/{org_id}/members/{member_email}")
async def remove_member(org_id: str, member_email: str, request: Request) -> dict[str, bool]:
    """Remove a member from the org and from every team inside it.

    Anyone may remove themselves (leaving), an admin may remove ordinary
    members, and the owner may remove admins too. The owner cannot be removed
    at all — ownership moves by transfer, never by deletion, so an org can
    never end up with no billable owner. Team rows go in the same transaction,
    so the removed account loses shared-file access on its very next request.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    target = _clean_email(member_email)
    api_writes.enforce(email, "organization membership changes")
    with tx() as cur:
        _, caller_role = _require_org(cur, org_uuid, email, lock=True)
        cur.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
        current = cur.fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="member not found")
        if current["role"] == "owner":
            raise HTTPException(
                status_code=400,
                detail="the owner cannot be removed; transfer ownership first",
            )
        if target != email:
            if caller_role not in ORG_MANAGER_ROLES:
                raise HTTPException(status_code=403, detail="organization admin only")
            if current["role"] == "admin" and caller_role != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="only the organization owner can remove an administrator",
                )
        cur.execute("SELECT id FROM teams WHERE org_id = %s", (org_uuid,))
        team_ids = [str(row["id"]) for row in cur.fetchall()]
        cur.execute(
            "DELETE FROM team_members USING teams"
            " WHERE team_members.team_id = teams.id"
            "   AND teams.org_id = %s AND team_members.email = %s",
            (org_uuid, target),
        )
        cur.execute(
            "DELETE FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
    for team_id in team_ids:
        _chat_hub().revoke(target, team_id)
        _cells().revoke(target, team_id)
    log.info("org member removed: %s %s by %s", org_uuid, target, email)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — teams
# ---------------------------------------------------------------------------


@router.post("/{org_id}/teams", status_code=201)
async def create_team(org_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    name = _clean_name(_body(body).get("name"), "team name")
    api_writes.enforce(email, "team changes")
    team_id = str(uuid.uuid4())
    with tx() as cur:
        _require_org(cur, org_uuid, email, manage=True, lock=True)
        cur.execute("SELECT count(*) AS n FROM teams WHERE org_id = %s", (org_uuid,))
        if int((cur.fetchone() or {}).get("n") or 0) >= MAX_TEAMS_PER_ORG:
            raise HTTPException(
                status_code=409,
                detail=f"an organization may hold at most {MAX_TEAMS_PER_ORG} teams",
            )
        # Inference on the case-insensitive unique index: a duplicate name is
        # refused by the database, so two simultaneous creates cannot both win.
        cur.execute(
            "INSERT INTO teams (id, org_id, name, created_by) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (org_id, lower(name)) DO NOTHING RETURNING id",
            (team_id, org_uuid, name, email),
        )
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=409, detail="a team with that name already exists"
            )
        cur.execute(
            "INSERT INTO team_members (team_id, email, role) VALUES (%s, %s, 'manager')",
            (team_id, email),
        )
    log.info("team created: %s (%s) in org %s by %s", name, team_id, org_uuid, email)
    return {
        "ok": True,
        "team": {
            "id": team_id,
            "org_id": org_uuid,
            "name": name,
            "is_general": False,
            "role": "manager",
            "member_count": 1,
            "bucket_url": team_bucket_url(team_id),
        },
    }


@router.get("/{org_id}/teams/{team_id}")
async def get_team(org_id: str, team_id: str, request: Request) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    with tx() as cur:
        _, org_role = _require_org(cur, org_uuid, email)
        team, team_role = _require_team(cur, org_uuid, team_uuid, email, org_role)
        cur.execute(
            """
            SELECT tm.email, tm.role, tm.added_at,
                   COALESCE(a.display, '') AS display,
                   m.role AS org_role
              FROM team_members tm
              LEFT JOIN accounts a    ON a.email = tm.email
              LEFT JOIN org_members m ON m.org_id = %s AND m.email = tm.email
             WHERE tm.team_id = %s
             ORDER BY CASE tm.role WHEN 'manager' THEN 0 ELSE 1 END, tm.email
            """,
            (org_uuid, team_uuid),
        )
        members = [
            {
                "email": row["email"],
                "display": row["display"],
                "role": row["role"],
                "org_role": row["org_role"] or "",
                "added_at": _epoch(row["added_at"]),
            }
            for row in cur.fetchall()
        ]
    return {
        "team": _team_payload({**team, "team_role": team_role}, org_role),
        "members": members,
    }


@router.patch("/{org_id}/teams/{team_id}")
async def rename_team(
    org_id: str, team_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    name = _clean_name(_body(body).get("name"), "team name")
    api_writes.enforce(email, "team changes")
    try:
        with tx() as cur:
            _, org_role = _require_org(cur, org_uuid, email, lock=True)
            _require_team(cur, org_uuid, team_uuid, email, org_role, manage=True)
            cur.execute(
                "UPDATE teams SET name = %s WHERE id = %s AND org_id = %s",
                (name, team_uuid, org_uuid),
            )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409, detail="a team with that name already exists"
        ) from None
    return {"ok": True, "id": team_uuid, "name": name}


@router.delete("/{org_id}/teams/{team_id}")
async def delete_team(org_id: str, team_id: str, request: Request) -> dict[str, bool]:
    """Delete a team and its memberships (org owner/admin only).

    Destroying a shared destination is an organization-level act, not something
    a team manager can do to their own team. General is refused outright: every
    accepted invitation joins it, so it must always exist. The files under the
    team's bucket root are left on disk for the same reason org deletion leaves
    them — an HTTP DELETE is not a safe place to make data unrecoverable.
    """
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    api_writes.enforce(email, "team changes")
    with tx() as cur:
        _require_org(cur, org_uuid, email, manage=True, lock=True)
        cur.execute(
            "SELECT is_general FROM teams WHERE id = %s AND org_id = %s",
            (team_uuid, org_uuid),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="team not found")
        if row["is_general"]:
            raise HTTPException(status_code=400, detail="the General team cannot be deleted")
        cur.execute("DELETE FROM teams WHERE id = %s AND org_id = %s", (team_uuid, org_uuid))
    _chat_hub().revoke_team(team_uuid)
    _cells().revoke_team(team_uuid)
    log.warning(
        "team deleted: %s in org %s by %s — shared files under %s were left in place",
        team_uuid,
        org_uuid,
        email,
        settings.team_bucket_root(org_uuid, team_uuid),
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — team members
# ---------------------------------------------------------------------------


@router.post("/{org_id}/teams/{team_id}/members", status_code=201)
async def add_team_member(
    org_id: str, team_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    payload = _body(body)
    target = _clean_email(payload.get("email"))
    role = str(payload.get("role") or "member").strip().lower()
    if role not in TEAM_ROLES:
        raise HTTPException(status_code=400, detail="role must be manager, member or viewer")
    api_writes.enforce(email, "team membership changes")
    with tx() as cur:
        _, org_role = _require_org(cur, org_uuid, email, lock=True)
        _require_team(cur, org_uuid, team_uuid, email, org_role, manage=True)
        cur.execute(
            "SELECT 1 FROM org_members WHERE org_id = %s AND email = %s",
            (org_uuid, target),
        )
        if cur.fetchone() is None:
            # Teams never span organizations: joining one means joining the org
            # first, which is what the invitation flow is for.
            raise HTTPException(
                status_code=400,
                detail="that account is not a member of this organization — invite them first",
            )
        cur.execute(
            "INSERT INTO team_members (team_id, email, role) VALUES (%s, %s, %s)"
            " ON CONFLICT (team_id, email) DO NOTHING RETURNING email",
            (team_uuid, target, role),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=409, detail="already on this team")
    _cells().revoke(target, team_uuid)
    log.info("team member added: %s %s role=%s by %s", team_uuid, target, role, email)
    return {"ok": True, "email": target, "role": role}


@router.patch("/{org_id}/teams/{team_id}/members/{member_email}")
async def set_team_member_role(
    org_id: str, team_id: str, member_email: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    target = _clean_email(member_email)
    role = str(_body(body).get("role") or "").strip().lower()
    if role not in TEAM_ROLES:
        raise HTTPException(status_code=400, detail="role must be manager, member or viewer")
    api_writes.enforce(email, "team membership changes")
    with tx() as cur:
        _, org_role = _require_org(cur, org_uuid, email, lock=True)
        _require_team(cur, org_uuid, team_uuid, email, org_role, manage=True)
        cur.execute(
            "UPDATE team_members SET role = %s WHERE team_id = %s AND email = %s"
            " RETURNING email",
            (role, team_uuid, target),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="team member not found")
    _chat_hub().revoke(target, team_uuid)
    _cells().revoke(target, team_uuid)
    return {"ok": True, "email": target, "role": role}


@router.delete("/{org_id}/teams/{team_id}/members/{member_email}")
async def remove_team_member(
    org_id: str, team_id: str, member_email: str, request: Request
) -> dict[str, bool]:
    """Remove one account from a team. Managers manage; anyone may leave."""
    user = _caller(request)
    email = user.email.lower()
    org_uuid = canonical_uuid(org_id)
    team_uuid = canonical_uuid(team_id)
    target = _clean_email(member_email)
    api_writes.enforce(email, "team membership changes")
    with tx() as cur:
        _, org_role = _require_org(cur, org_uuid, email, lock=True)
        _require_team(cur, org_uuid, team_uuid, email, org_role, manage=target != email)
        cur.execute(
            "DELETE FROM team_members WHERE team_id = %s AND email = %s RETURNING email",
            (team_uuid, target),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="team member not found")
    _chat_hub().revoke(target, team_uuid)
    _cells().revoke(target, team_uuid)
    log.info("team member removed: %s %s by %s", team_uuid, target, email)
    return {"ok": True}


__all__ = [
    "router",
    "TeamBucket",
    "resolve_team_bucket",
    "canonical_uuid",
    "team_bucket_url",
    "invite_url",
    "SHARED_PLANS",
    "GENERAL_TEAM_NAME",
]
