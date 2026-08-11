"""Desktop browser authorization and signed offline entitlements.

Tokens are deliberately not JWTs. They are one canonical JSON payload and one
Ed25519 signature: ``base64url(payload) + '.' + base64url(signature)``. There is
no algorithm field, parser registry, or negotiation surface.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import secrets
import stat
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

try:
    from . import accounts
    from .auth import get_current_user
    from .db import tx
    from .limits import RateLimiter
    from .plans import limits_for
except ImportError:  # uvicorn app:app from package dir
    import accounts  # type: ignore
    from auth import get_current_user  # type: ignore
    from db import tx  # type: ignore
    from limits import RateLimiter  # type: ignore
    from plans import limits_for  # type: ignore

router = APIRouter(tags=["entitlement"])

MAX_REFRESH_BYTES = 64 * 1024
KEY_PATH = Path("~/chudware-web-data/entitlement_ed25519.key").expanduser()
EMBEDDED_PUBLIC_KEY = bytes.fromhex(
    "bdb8869038e8274a32a6f14b86f0fbdbe599c6c78e49dc495cfa1275b01490f3"
)
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_DEVICE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_VALID_PLANS = frozenset({"trial", "desktop", "studio", "enterprise"})
_PAID_STATUSES = frozenset({"active", "trialing"})
_refresh_by_subject = RateLimiter(20, 60.0)
_refresh_by_ip = RateLimiter(30, 60.0)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value or len(value) % 4 == 1:
        raise ValueError("non-canonical base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _key_file() -> Path:
    override = os.environ.get("CHUDWARE_ENTITLEMENT_KEY_PATH", "").strip()
    return Path(override).expanduser() if override else KEY_PATH


def _load_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    key_path = path or _key_file()
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        raw = key_path.read_bytes()
    except FileNotFoundError:
        generated = Ed25519PrivateKey.generate()
        raw = generated.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another gateway worker won first-start generation
            raw = key_path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
    if len(raw) != 32:
        raise RuntimeError(f"entitlement signing key must be 32 raw bytes: {key_path}")
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    key = Ed25519PrivateKey.from_private_bytes(raw)
    # An override is a test/rotation seam. The production default must match the
    # public key compiled into the desktop or every valid customer is locked out.
    if path is None and not os.environ.get("CHUDWARE_ENTITLEMENT_KEY_PATH"):
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if public != EMBEDDED_PUBLIC_KEY:
            raise RuntimeError("entitlement private key does not match the shipped public key")
    return key


def _compact(payload: dict[str, Any], key: Ed25519PrivateKey | None = None) -> str:
    encoded = _b64(_canonical(payload))
    signature = (key or _load_private_key()).sign(encoded.encode("ascii"))
    return f"{encoded}.{_b64(signature)}"


def _verify_signed_json(token: str, public_key: bytes) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".")
        if token.count(".") != 1:
            raise ValueError("too many sections")
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            _unb64(signature), encoded.encode("ascii")
        )
        payload = json.loads(_unb64(encoded))
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "invalid signed payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(401, "invalid signed payload")
    return payload


def verify_compact(token: str, public_key: bytes | None = None) -> dict[str, Any]:
    """Verify before parsing the signed JSON and require the exact token shape."""
    payload = _verify_signed_json(token, public_key or EMBEDDED_PUBLIC_KEY)
    expected = {
        "v", "sub", "email", "plan", "issued_at", "paid_through",
        "subscribed_since", "grace_days", "nonce",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise HTTPException(401, "invalid entitlement payload")
    if (
        payload.get("v") != 1
        or not all(isinstance(payload.get(k), str) and payload[k] for k in ("sub", "email", "plan", "nonce"))
        or not all(type(payload.get(k)) is int for k in ("issued_at", "paid_through", "subscribed_since", "grace_days"))
        or not 7 <= payload["grace_days"] <= 30
    ):
        raise HTTPException(401, "invalid entitlement payload")
    return payload


def grace_days(issued_at: int, subscribed_since: int) -> int:
    tenure_months = max(0, (issued_at - subscribed_since) // 2_592_000)
    return max(7, min(30, 7 + tenure_months))


def _subject_for(email: str, device_name: str) -> str:
    """Mint a subject for one named installation, replacing stale authorization.

    Re-authorizing the same machine intentionally rotates its subject and clears
    a prior revocation. Any token copied from the old installation stops
    refreshing immediately.
    """
    subject = str(uuid.uuid4())
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO desktop_entitlements (email, subject, device_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (email, device_name) DO UPDATE
                    SET subject = EXCLUDED.subject,
                        created_at = now(),
                        last_checkin_at = NULL,
                        revoked_at = NULL
            RETURNING subject::text AS subject
            """,
            (email, subject, device_name),
        )
        row = cur.fetchone()
    return str(row["subject"])


def _subject_matches(email: str, subject: str) -> bool:
    """Validate an active device and record this authenticated check-in."""
    with tx() as cur:
        cur.execute(
            """
            UPDATE desktop_entitlements
               SET last_checkin_at = now()
             WHERE email = %s AND subject = %s AND revoked_at IS NULL
         RETURNING 1
            """,
            (email, subject),
        )
        return cur.fetchone() is not None


def list_devices(email: str) -> list[dict[str, Any]]:
    """Portal-safe device rows for one authenticated account."""
    with tx() as cur:
        cur.execute(
            """
            SELECT subject::text AS subject, device_name,
                   EXTRACT(EPOCH FROM created_at) AS created_at,
                   EXTRACT(EPOCH FROM last_checkin_at) AS last_checkin_at,
                   EXTRACT(EPOCH FROM revoked_at) AS revoked_at
              FROM desktop_entitlements
             WHERE email = %s
             ORDER BY created_at DESC
            """,
            (email,),
        )
        return [dict(row) for row in cur.fetchall()]


def revoke_device(email: str, subject: str) -> bool:
    """Revoke exactly one device belonging to *email*."""
    with tx() as cur:
        cur.execute(
            """
            UPDATE desktop_entitlements
               SET revoked_at = COALESCE(revoked_at, now())
             WHERE email = %s AND subject = %s
            """,
            (email, subject),
        )
        return cur.rowcount == 1


def _entitlement_window(account: Any, now: int) -> tuple[int, int] | None:
    if account is None or getattr(account, "status", "") != "active":
        return None
    plan = str(getattr(account, "plan", ""))
    if plan not in _VALID_PLANS or not limits_for(plan).desktop:
        return None
    if plan == "trial":
        paid_through = int(getattr(account, "trial_ends_at", 0) or 0)
        subscribed_since = int(getattr(account, "created_at", 0) or 0)
    else:
        if str(getattr(account, "subscription_status", "")) not in _PAID_STATUSES:
            return None
        paid_through = int(getattr(account, "paid_through", 0) or 0)
        subscribed_since = int(getattr(account, "subscribed_since", 0) or 0)
    if paid_through <= now or subscribed_since <= 0:
        return None
    return paid_through, subscribed_since


def _issue(account: Any, subject: str, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    window = _entitlement_window(account, issued_at)
    if window is None:
        raise HTTPException(403, "desktop entitlement inactive")
    paid_through, subscribed_since = window
    payload = {
        "v": 1,
        "sub": subject,
        "email": str(account.email),
        "plan": str(account.plan),
        "issued_at": issued_at,
        "paid_through": paid_through,
        "subscribed_since": subscribed_since,
        "grace_days": grace_days(issued_at, subscribed_since),
        "nonce": _b64(secrets.token_bytes(16)),
    }
    return _compact(payload)


def _signed_refusal(reason: str, request_nonce: str, now: int | None = None) -> dict[str, Any]:
    issued_at = int(time.time()) if now is None else int(now)
    response: dict[str, Any] = {
        "entitled": False,
        "issued_at": issued_at,
        "nonce": _b64(secrets.token_bytes(16)),
        "reason": reason,
        "request_nonce": request_nonce,
    }
    response["proof"] = _compact(response)
    return response

def verify_refusal(
    response: dict[str, Any], expected_request_nonce: str, now: int,
    public_key: bytes | None = None,
) -> str:
    if not isinstance(response, dict) or response.get("entitled") is not False:
        raise HTTPException(401, "invalid refusal")
    proof = response.get("proof")
    if not isinstance(proof, str):
        raise HTTPException(401, "unsigned refusal")
    signed = _verify_signed_json(proof, public_key or EMBEDDED_PUBLIC_KEY)
    unsigned = dict(response)
    unsigned.pop("proof", None)
    if signed != unsigned:
        raise HTTPException(401, "refusal payload mismatch")
    issued_at = signed.get("issued_at")
    _ = now  # signed telemetry only; replay defense is the one-use refresh nonce
    if type(issued_at) is not int or issued_at <= 0:
        raise HTTPException(401, "invalid refusal timestamp")
    if signed.get("request_nonce") != expected_request_nonce or not signed.get("nonce"):
        raise HTTPException(401, "refusal does not match this refresh")
    return str(signed.get("reason") or "desktop entitlement inactive")


def _validate_authorize_params(port: Any, state_value: str, device: str) -> int:
    try:
        parsed_port = int(port)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "invalid callback port") from exc
    if not 1024 <= parsed_port <= 65535:
        raise HTTPException(400, "invalid callback port")
    if not _STATE_RE.fullmatch(state_value or ""):
        raise HTTPException(400, "invalid authorization state")
    if not _DEVICE_RE.fullmatch(device or ""):
        raise HTTPException(400, "invalid device name")
    return parsed_port


@router.get("/desktop/authorize", response_class=HTMLResponse)
async def desktop_authorize_page(
    request: Request, port: int, state: str, device: str
) -> HTMLResponse:
    user = get_current_user(request)
    _validate_authorize_params(port, state, device)
    account = accounts.get_account(user.email)
    if _entitlement_window(account, int(time.time())) is None:
        return HTMLResponse(
            "<h1>Desktop access is not active</h1>"
            '<p><a href="/#pricing">Manage subscription</a></p>',
            status_code=403,
        )
    escaped_device = html.escape(device)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8><title>Authorize Chudware</title></head>"
        f"<body><main><h1>Authorize Chudware on <strong>{escaped_device}</strong>?</h1>"
        "<p>The desktop app will receive an offline entitlement for this account.</p>"
        '<form method="post" action="/desktop/authorize">'
        f'<input type="hidden" name="port" value="{port}">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        f'<input type="hidden" name="device" value="{escaped_device}">'
        '<button type="submit">Authorize Chudware</button></form></main></body></html>'
    )


@router.post("/desktop/authorize")
async def desktop_authorize_confirm(request: Request) -> RedirectResponse:
    user = get_current_user(request)
    form = await request.form()
    state_value = str(form.get("state", ""))
    device = str(form.get("device", ""))
    port = _validate_authorize_params(form.get("port"), state_value, device)
    account = accounts.get_account(user.email)
    token = _issue(account, _subject_for(user.email, device))
    query = urllib.parse.urlencode({"token": token, "state": state_value})
    return RedirectResponse(f"http://127.0.0.1:{port}/callback?{query}", status_code=303)


@router.post("/api/entitlement/refresh")
async def refresh_entitlement(request: Request) -> JSONResponse:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_REFRESH_BYTES:
                raise HTTPException(413, "refresh request too large")
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
    body = await request.body()
    if len(body) > MAX_REFRESH_BYTES:
        raise HTTPException(413, "refresh request too large")
    try:
        token = body.decode("ascii")
    except UnicodeDecodeError as exc:
        key = request.client.host if request.client else "unknown"
        _refresh_by_ip.enforce(key, "entitlement refreshes")
        raise HTTPException(401, "invalid entitlement token") from exc

    try:
        payload = verify_compact(token)
    except HTTPException:
        key = request.client.host if request.client else "unknown"
        _refresh_by_ip.enforce(key, "entitlement refreshes")
        raise
    _refresh_by_subject.enforce(payload["sub"], "entitlement refreshes")
    refresh_nonce = request.headers.get("x-chudware-refresh-nonce", "")
    if not _STATE_RE.fullmatch(refresh_nonce):
        raise HTTPException(400, "invalid refresh nonce")

    account = accounts.get_account(payload["email"])
    identity_matches = _subject_matches(payload["email"], payload["sub"])
    if account is None or not identity_matches:
        return JSONResponse(_signed_refusal("account identity is no longer valid", refresh_nonce))
    if _entitlement_window(account, int(time.time())) is None:
        return JSONResponse(_signed_refusal("desktop entitlement is inactive", refresh_nonce))
    fresh = _issue(account, payload["sub"])
    return JSONResponse({"entitled": True, "token": fresh})
