"""Customer and administrator lifecycle for reversible device-security appeals."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

try:
    from .auth import get_current_user
    from .db import tx
    from .limits import api_writes
    from .metrics import registry
except ImportError:  # allow ``uvicorn app:app`` from the package directory
    from auth import get_current_user  # type: ignore
    from db import tx  # type: ignore
    from limits import api_writes  # type: ignore
    from metrics import registry  # type: ignore

router = APIRouter(tags=["security appeals"])
MAX_MESSAGE_BYTES = 4096
_DECISIONS = frozenset({"restore_device", "uphold"})


def _require_admin(request: Request) -> Any:
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _uuid(value: str, detail: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(404, detail) from exc
    if str(parsed) != value:
        raise HTTPException(404, detail)
    return parsed


def _message(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, "message is required")
    clean = value.strip()
    if not clean:
        raise HTTPException(400, "message is required")
    if len(clean.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HTTPException(413, "appeal message exceeds 4 KiB")
    return clean


def _note(value: Any) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, "decision note is required")
    clean = value.strip()
    if not clean:
        raise HTTPException(400, "decision note is required")
    if len(clean.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HTTPException(413, "decision note exceeds 4 KiB")
    return clean


def _row(row: Any, *, admin: bool) -> dict[str, Any]:
    value = {
        "id": str(row["id"]),
        "subject": str(row["subject"]),
        "device_name": str(row["device_name"] or ""),
        "state": str(row["state"]),
        "reason_codes": list(row["reason_codes"]),
        "action_at": float(row["action_at"]),
        "created_at": float(row["created_at"]),
        "resolved_at": None if row["resolved_at"] is None else float(row["resolved_at"]),
        "decision_note": str(row["decision_note"] or ""),
    }
    if admin:
        value.update({"email": str(row["email"]), "message": str(row["message"]),
                      "decided_by": str(row["decided_by"] or "")})
    return value


_SELECT = """
SELECT p.id, p.email, p.subject, p.message, p.state,
       EXTRACT(EPOCH FROM p.created_at) AS created_at,
       EXTRACT(EPOCH FROM p.resolved_at) AS resolved_at,
       p.decided_by, p.decision_note, d.device_name,
       a.reason_codes, EXTRACT(EPOCH FROM a.created_at) AS action_at
  FROM security_appeals p
  JOIN security_actions a ON a.id = p.action_id
  LEFT JOIN desktop_entitlements d ON d.email = p.email AND d.subject = p.subject
"""


def list_customer_appeals(email: str) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(_SELECT + " WHERE p.email = %s ORDER BY p.created_at DESC", (email,))
        return [_row(row, admin=False) for row in cur.fetchall()]


def list_admin_appeals(state: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    if state is not None and state not in {"pending", "restored", "upheld"}:
        raise HTTPException(400, "invalid appeal state")
    where = " WHERE p.state = %s" if state else ""
    args: list[Any] = [state] if state else []
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM security_appeals p" + where, args)
        total = int(cur.fetchone()["n"])
        cur.execute(_SELECT + where + " ORDER BY p.created_at DESC LIMIT %s OFFSET %s", args + [limit, offset])
        return [_row(row, admin=True) for row in cur.fetchall()], total


@router.get("/api/portal/security-appeals")
async def customer_appeals(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    return {"appeals": list_customer_appeals(user.email)}


@router.post("/api/portal/security-appeals")
async def create_customer_appeal(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = get_current_user(request)
    subject = _uuid(str(body.get("subject") or ""), "eligible device action not found")
    message = _message(body.get("message"))
    api_writes.enforce(user.email, "security appeal submissions")
    appeal_id = uuid.uuid4()
    with tx() as cur:
        cur.execute(
            """
            SELECT a.id
              FROM security_actions a
              JOIN desktop_entitlements d ON d.email = a.email AND d.subject = a.subject
             WHERE a.email = %s AND a.subject = %s AND a.action = 'revoke_device'
               AND a.reversed_at IS NULL AND d.revoked_at IS NOT NULL
             ORDER BY a.created_at DESC LIMIT 1
             FOR UPDATE OF a
            """,
            (user.email, subject),
        )
        action = cur.fetchone()
        if action is None:
            raise HTTPException(404, "eligible device action not found")
        cur.execute("SELECT 1 FROM security_appeals WHERE action_id = %s AND state = 'pending'", (action["id"],))
        if cur.fetchone() is not None:
            raise HTTPException(409, "an appeal is already pending")
        cur.execute(
            "INSERT INTO security_appeals (id,action_id,email,subject,message,state) VALUES (%s,%s,%s,%s,%s,'pending')",
            (appeal_id, action["id"], user.email, subject, message),
        )
        cur.execute(_SELECT + " WHERE p.id = %s", (appeal_id,))
        result = _row(cur.fetchone(), admin=False)
    registry.security_appeal("submitted")
    return {"ok": True, "appeal": result}


@router.get("/api/admin/security/appeals")
async def admin_appeals(
    _admin: Any = Depends(_require_admin),
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows, total = list_admin_appeals(state, limit, offset)
    return {"appeals": rows, "total": total, "limit": limit, "offset": offset}


@router.post("/api/admin/security/appeals/{appeal_id}/resolve")
async def resolve_appeal(appeal_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    admin = _require_admin(request)
    parsed_id = _uuid(appeal_id, "appeal not found")
    decision = str(body.get("decision") or "").strip()
    if decision not in _DECISIONS:
        raise HTTPException(400, "decision must be restore_device or uphold")
    note = _note(body.get("note"))
    api_writes.enforce(admin.email, "security appeal decisions")
    state = "restored" if decision == "restore_device" else "upheld"
    with tx() as cur:
        cur.execute(
            "SELECT action_id, email, subject, state FROM security_appeals WHERE id = %s FOR UPDATE",
            (parsed_id,),
        )
        appeal = cur.fetchone()
        if appeal is None:
            raise HTTPException(404, "appeal not found")
        if appeal["state"] != "pending":
            raise HTTPException(409, "appeal has already been resolved")
        cur.execute(
            "UPDATE security_appeals SET state=%s,resolved_at=now(),decided_by=%s,decision_note=%s WHERE id=%s",
            (state, admin.email, note, parsed_id),
        )
        if state == "restored":
            cur.execute(
                "UPDATE security_actions SET reversed_at=now(),reversed_by=%s WHERE id=%s AND reversed_at IS NULL",
                (admin.email, appeal["action_id"]),
            )
            # Deliberately do not clear revoked_at or replace device_public_key. The
            # normal browser authorization flow must rotate subject and key first.
            registry.security_action("reversed")
        cur.execute(_SELECT + " WHERE p.id = %s", (parsed_id,))
        result = _row(cur.fetchone(), admin=True)
    registry.security_appeal(state)
    return {"ok": True, "appeal": result}


__all__ = ["MAX_MESSAGE_BYTES", "router"]
