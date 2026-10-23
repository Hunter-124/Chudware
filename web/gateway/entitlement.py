"""Desktop browser authorization and signed offline entitlements.

Tokens are deliberately not JWTs. They are one canonical JSON payload and one
Ed25519 signature: ``base64url(payload) + '.' + base64url(signature)``. There is
no algorithm field, parser registry, or negotiation surface.
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import os
import re
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
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
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from . import accounts
    from .auth import get_current_user
    from .db import tx
    from .limits import RateLimiter, read_bounded_body, request_network
    from .plans import PLANS, limits_for
except ImportError:  # uvicorn app:app from package dir
    import accounts  # type: ignore
    from auth import get_current_user  # type: ignore
    from db import tx  # type: ignore
    from limits import RateLimiter, read_bounded_body, request_network  # type: ignore
    from plans import PLANS, limits_for  # type: ignore

router = APIRouter(tags=["entitlement"])

MAX_REFRESH_BYTES = 64 * 1024
ENTITLEMENT_KEY_ENV = "CHUDWARE_ENTITLEMENT_KEY_PATH"
EMBEDDED_PUBLIC_KEY = bytes.fromhex(
    "bdb8869038e8274a32a6f14b86f0fbdbe599c6c78e49dc495cfa1275b01490f3"
)
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_DEVICE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_VALID_PLANS = frozenset({"trial", "desktop", "studio", "enterprise"})
_PAID_STATUSES = frozenset({"active", "trialing"})
_refresh_by_subject = RateLimiter(20, 60.0)
_refresh_by_ip = RateLimiter(30, 60.0)
_REFRESH_FIELDS = frozenset({
    "token", "request_nonce", "sequence", "app_version", "platform",
    "integrity_digest", "last_acked_event_sequence", "customer_receipt", "signature",
})
_POLICY_MODES = frozenset({"standard", "minimized", "customer_endpoint"})
_CUSTOMER_RECEIPT_FIELDS = frozenset({"subject", "sequence", "request_nonce", "verdict", "reason", "issued_at", "signature"})

def _device_public_key(value: str) -> bytes:
    try:
        raw = _unb64(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "invalid device public key") from exc
    if len(raw) != 32 or _b64(raw) != value:
        raise HTTPException(400, "invalid device public key")
    return raw

def _sign_object(payload: dict[str, Any]) -> dict[str, Any]:
    signed = dict(payload)
    signed["signature"] = _b64(_load_private_key().sign(_canonical(payload)))
    return signed

def _verify_device_object(payload: dict[str, Any], public_key: bytes) -> None:
    signature = payload.get("signature")
    try:
        if not isinstance(signature, str):
            raise ValueError("missing signature")
        unsigned = dict(payload)
        unsigned.pop("signature")
        Ed25519PublicKey.from_public_bytes(public_key).verify(_unb64(signature), _canonical(unsigned))
    except (ValueError, InvalidSignature) as exc:
        raise HTTPException(401, "invalid device signature") from exc

def _policy_assignment(account: Any) -> dict[str, Any]:
    if str(getattr(account, "plan", "")) != "enterprise":
        return {"policy_id": "standard", "endpoint": None, "config": {}}
    with tx() as cur:
        cur.execute(
            "SELECT a.policy_id, a.endpoint, a.config "
            "FROM account_security_policies a WHERE a.email = %s", (account.email,),
        )
        row = cur.fetchone()
    if row is None or str(row["policy_id"]) not in _POLICY_MODES:
        return {"policy_id": "standard", "endpoint": None, "config": {}}
    return {"policy_id": str(row["policy_id"]), "endpoint": row["endpoint"], "config": dict(row["config"] or {})}


def _security_policy(account: Any, entitlement_payload: dict[str, Any], request_nonce: str,
                     policy_id: str, now: int | None = None) -> dict[str, Any]:
    issued_at = int(time.time()) if now is None else int(now)
    assignment = _policy_assignment(account)
    mode = assignment["policy_id"]
    if policy_id != mode:
        policy_id = mode
    expires_at = min(
        int(entitlement_payload["paid_through"]),
        int(entitlement_payload["issued_at"]) + int(entitlement_payload["grace_days"]) * 86400,
    )
    payload = {
        "id": policy_id, "subject": entitlement_payload["sub"],
        "email": entitlement_payload["email"], "entitlement_nonce": entitlement_payload["nonce"],
        "request_nonce": request_nonce, "issued_at": issued_at, "expires_at": expires_at,
        "telemetry_mode": mode,
    }
    if mode == "customer_endpoint":
        payload["customer_endpoint"] = assignment["endpoint"]
    return _sign_object(payload)

def _enforcement_state(subject: str, state: str, expires_at: int, *,
                       reason_codes: list[str] | None = None,
                       action_id: str | None = None, now: int | None = None) -> dict[str, Any]:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "subject": subject,
        "state": state,
        "issued_at": issued_at,
        "expires_at": int(expires_at),
        "reason_codes": list(reason_codes or []),
        "action_id": action_id or str(uuid.uuid4()),
        "nonce": _b64(secrets.token_bytes(16)),
    }
    return _sign_object(payload)


def _verify_customer_receipt(receipt: Any, public_key: bytes, *, subject: str,
                             sequence: int, request_nonce: str) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _CUSTOMER_RECEIPT_FIELDS:
        raise HTTPException(409, "valid customer security receipt required")
    _verify_device_object(receipt, public_key)
    if (receipt.get("subject") != subject or receipt.get("sequence") != sequence
            or receipt.get("request_nonce") != request_nonce
            or receipt.get("verdict") != "allow" or receipt.get("reason") != "heartbeat"):
        raise HTTPException(409, "valid customer security receipt required")
    issued_at = receipt.get("issued_at")
    if type(issued_at) is not int or abs(int(time.time()) - issued_at) > 900:
        raise HTTPException(409, "valid customer security receipt required")


def _forward_customer_receipt(endpoint: str, receipt: dict[str, Any]) -> None:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(409, "customer security endpoint is not configured")
    request = urllib.request.Request(
        endpoint, data=_canonical(receipt), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Chudware-Security/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = response.read(4097)
            if response.status != 200 or len(body) > 4096:
                raise HTTPException(409, "customer security receipt was not accepted")
            answer = json.loads(body)
    except HTTPException:
        raise
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "customer security receipt was not accepted") from exc
    if answer != {"accepted": True}:
        raise HTTPException(409, "customer security receipt was not accepted")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value or len(value) % 4 == 1:
        raise ValueError("non-canonical base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _verification_key() -> bytes:
    environment = os.environ.get("CHUDWARE_DEPLOY_ENV", "prod")
    if environment == "prod":
        return EMBEDDED_PUBLIC_KEY
    if environment != "staging":
        raise RuntimeError("unknown entitlement deployment environment")
    raw = os.environ.get("CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        raise RuntimeError("staging requires an independent entitlement public key")
    public = bytes.fromhex(raw)
    if public == EMBEDDED_PUBLIC_KEY:
        raise RuntimeError("staging must not hold the production entitlement signing key")
    return public


def _key_file() -> Path:
    value = os.environ.get(ENTITLEMENT_KEY_ENV, "").strip()
    if not value:
        raise RuntimeError(
            f"{ENTITLEMENT_KEY_ENV} must name the systemd entitlement credential"
        )
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"{ENTITLEMENT_KEY_ENV} must be an absolute path")
    return path


def _load_private_key(path: Path | None = None) -> Ed25519PrivateKey:
    key_path = path or _key_file()
    try:
        metadata = key_path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"entitlement signing credential is missing: {key_path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"entitlement signing credential is not a regular file: {key_path}")
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(
            f"entitlement signing credential must not be accessible by group or others: {key_path}"
        )
    try:
        raw = key_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"entitlement signing credential is unreadable: {key_path}") from exc
    if len(raw) != 32:
        raise RuntimeError(f"entitlement signing key must be 32 raw bytes: {key_path}")
    key = Ed25519PrivateKey.from_private_bytes(raw)
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if public != _verification_key():
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
    payload = _verify_signed_json(token, public_key or _verification_key())
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




def _subject_for(email: str, device_name: str, device_public_key: str) -> str:
    """Rotate one named installation onto a new normalized Ed25519 identity."""
    public_key = _device_public_key(device_public_key)
    subject = str(uuid.uuid4())
    with tx() as cur:
        cur.execute(
            "SELECT device_public_key, revoked_at FROM desktop_entitlements "
            "WHERE email = %s AND device_name = %s FOR UPDATE", (email, device_name),
        )
        existing = cur.fetchone()
        if (existing is not None and existing["revoked_at"] is not None
                and bytes(existing["device_public_key"] or b"") == public_key):
            raise HTTPException(409, "revoked device key cannot be reactivated")
        cur.execute(
            """
            INSERT INTO desktop_entitlements
                (email, subject, device_name, device_public_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (email, device_name) DO UPDATE
                    SET subject = EXCLUDED.subject,
                        device_public_key = EXCLUDED.device_public_key,
                        created_at = now(),
                        last_checkin_at = NULL,
                        last_security_checkin_at = NULL,
                        last_refresh_sequence = 0,
                        last_event_sequence = 0,
                        policy_id = 'standard',
                        revoked_at = NULL
            RETURNING subject::text AS subject
            """,
            (email, subject, device_name, public_key),
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
        "grace_days": 30,
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
    signed = _verify_signed_json(proof, public_key or _verification_key())
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


# The sign-in page the user just came from is the design language; this page is
# the last step of that same flow, so it reuses the same content-hashed
# stylesheet and the same two-panel shell (brand copy left, action card right)
# instead of carrying a second look. Only the summary rows are page-specific,
# and they are the whole point of a consent screen: name the device, the
# account, the tier, and where the credential is about to go.
_AUTH_CSS = "/auth-assets/login.dc48293a5489.css"
# Quote every attribute: an unquoted `height=42/>` swallows the slash into the
# value, and the whole mark silently renders as nothing.
_BRAND_MARK = (
    '<svg viewBox="0 0 64 64" aria-hidden="true">'
    '<g fill="none" stroke="currentColor" stroke-width="2.4">'
    '<rect x="6" y="16" width="42" height="42"/>'
    '<rect x="16" y="6" width="42" height="42"/>'
    '<rect x="21" y="25" width="18" height="18"/>'
    '<rect x="25" y="21" width="18" height="18"/>'
    '<path d="M6 16L16 6M48 16L58 6M48 58L58 48M6 58L16 48M21 25L25 21'
    'M39 25L43 21M39 43L43 39M21 43L25 39"/></g></svg>'
)
_PAGE_CSS = """
    .auth-card h2 .device{color:var(--teal)}
    .facts{display:grid;gap:1px;margin:8px 0 4px;border:1px solid var(--edge);
      border-radius:11px;background:var(--edge);overflow:hidden}
    .facts div{display:flex;align-items:baseline;justify-content:space-between;
      gap:16px;padding:13px 16px;background:#0b1118}
    .facts dt{color:var(--muted);font:650 .68rem ui-monospace,monospace;
      letter-spacing:.11em;text-transform:uppercase}
    .facts dd{margin:0;font-size:.86rem;font-weight:650;text-align:right;
      word-break:break-word}
    /* A <button> shrink-fits even as a flex box, so the action stack is an
       explicit grid; that also centres the cancel link under it. */
    .auth-card form{display:grid;gap:14px}
    a.primary{text-decoration:none}
    /* Stacked layout puts the brand mark directly above the card, and the
       absolutely positioned wordmark then lands on top of a fact row. */
    @media(max-width:850px){.wordmark{display:none}}
"""


def _authorize_shell(
    title: str, brand: str, card: str, status_code: int = 200
) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=color-scheme content=dark>'
        f"<title>{html.escape(title)} · Chudware</title>"
        f'<link rel=stylesheet href="{_AUTH_CSS}">'
        f"<style>{_PAGE_CSS}</style></head><body>"
        '<main class="auth-shell"><section class="brand-panel">'
        f"{_BRAND_MARK}{brand}</section>"
        '<section class="auth-card">'
        '<a class="wordmark" href="https://chudware.com">Chudware</a>'
        f'<div class="auth-form">{card}</div>'
        "</section></main></body></html>",
        status_code=status_code,
    )


@router.get("/desktop/authorize", response_class=HTMLResponse)
async def desktop_authorize_page(
    request: Request, port: int, state: str, device: str, device_public_key: str
) -> HTMLResponse:
    user = get_current_user(request)
    _validate_authorize_params(port, state, device)
    _device_public_key(device_public_key)
    account = accounts.get_account(user.email)
    window = _entitlement_window(account, int(time.time()))
    if window is None:
        return _authorize_shell(
            "Desktop access is not active",
            '<p class="eyebrow">Desktop authorization</p>'
            "<h1>Nothing to hand the app yet.</h1>"
            "<p>A desktop entitlement is issued from an active plan. Pick one up"
            " and this device can be authorized straight away.</p>",
            '<p class="eyebrow">Not active</p>'
            "<h2>Desktop access is not active</h2>"
            "<p>This account has no current desktop entitlement, so the device"
            f" <strong>{html.escape(device)}</strong> cannot be authorized.</p>"
            '<dl class="facts"><div><dt>Account</dt>'
            f"<dd>{html.escape(user.email)}</dd></div></dl>"
            '<a class="primary" href="/#pricing">Manage subscription</a>',
            status_code=403,
        )
    escaped_device = html.escape(device)
    plan = str(getattr(account, "plan", ""))
    plan_label = PLANS[plan].display_name if plan in PLANS else plan
    # The token expires with the subscription; grace_days in _issue keeps the app
    # working offline past that, and saying so here is why the user can trust it.
    entitled_through = datetime.datetime.fromtimestamp(
        window[0], datetime.timezone.utc
    )
    through = f"{entitled_through:%B} {entitled_through.day}, {entitled_through:%Y}"
    return _authorize_shell(
        "Authorize Chudware",
        '<p class="eyebrow">Desktop authorization</p>'
        "<h1>Hand this computer a signed entitlement.</h1>"
        "<p>The offline entitlement is delivered only to the Chudware app waiting"
        " on this machine, and you can revoke the device at any time from your"
        " account portal.</p>"
        "<ul><li>Device-bound credential</li><li>Works offline</li>"
        "<li>Revocable any time</li></ul>",
        '<p class="eyebrow">Confirm device</p>'
        f'<h2>Authorize Chudware on <span class="device">{escaped_device}</span>?</h2>'
        "<dl class=\"facts\">"
        f"<div><dt>Account</dt><dd>{html.escape(user.email)}</dd></div>"
        f"<div><dt>Plan</dt><dd>{html.escape(plan_label)}</dd></div>"
        f"<div><dt>Entitled through</dt><dd>{through}</dd></div>"
        f"<div><dt>Device</dt><dd>{escaped_device}</dd></div>"
        f"<div><dt>Delivered to</dt><dd>127.0.0.1:{port}</dd></div>"
        "</dl>"
        '<form method="post" action="/desktop/authorize">'
        f'<input type="hidden" name="port" value="{port}">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        f'<input type="hidden" name="device" value="{escaped_device}">'
        f'<input type="hidden" name="device_public_key" value="{html.escape(device_public_key)}">'
        '<button class="primary" type="submit">Authorize Chudware</button>'
        '<a class="text-button" href="/">Cancel and return to my account</a>'
        "</form>"
        '<p class="legal">Not expecting this? Close this tab — nothing is issued'
        " until you authorize.</p>",
    )


def _handoff_page(target: str) -> HTMLResponse:
    """Hand the browser to the loopback listener without a form redirect.

    The app's CSP is ``form-action 'self'``, and Chromium enforces form-action
    against every hop of a form submission, including the redirect target. A
    303 from this POST to ``http://127.0.0.1:<port>/callback`` is therefore
    aborted before it leaves the browser, which is exactly the "clicking
    Authorize does nothing" symptom. A meta refresh is an ordinary document
    navigation, so it is not covered by form-action, and the manual link is the
    fallback when the app is not actually listening yet.
    """
    escaped = html.escape(target, quote=True)
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=color-scheme content=dark>'
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        "<title>Authorized · Chudware</title>"
        f'<link rel=stylesheet href="{_AUTH_CSS}">'
        f"<style>{_PAGE_CSS}</style></head><body>"
        '<main class="auth-shell"><section class="brand-panel">'
        f"{_BRAND_MARK}"
        '<p class="eyebrow">Desktop authorization</p>'
        "<h1>Authorized. Returning to the app.</h1>"
        "<p>The signed entitlement is being delivered to the Chudware app on"
        " this computer. You can close this tab once the app shows your"
        " account.</p></section>"
        '<section class="auth-card">'
        '<a class="wordmark" href="https://chudware.com">Chudware</a>'
        '<div class="auth-form"><p class="eyebrow">Handing off</p>'
        "<h2>Delivering your entitlement</h2>"
        "<p>If the app does not pick it up, make sure Chudware is still waiting"
        " for sign-in on this computer, then use the button below.</p>"
        f'<a class="primary" href="{escaped}">Deliver to the app</a>'
        '<a class="text-button" href="/">Return to my account</a>'
        "</div></section></main></body></html>",
    )


@router.post("/desktop/authorize")
async def desktop_authorize_confirm(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    form = await request.form()
    state_value = str(form.get("state", ""))
    device = str(form.get("device", ""))
    device_public_key = str(form.get("device_public_key", ""))
    port = _validate_authorize_params(form.get("port"), state_value, device)
    account = accounts.get_account(user.email)
    subject = _subject_for(user.email, device, device_public_key)
    token = _issue(account, subject)
    payload = verify_compact(token)
    policy = _security_policy(account, payload, state_value, "standard")
    enforcement = _enforcement_state(subject, "active", min(
        int(payload["paid_through"]), int(payload["issued_at"]) + int(payload["grace_days"]) * 86400))
    query = urllib.parse.urlencode({
        "token": token, "state": state_value,
        "security_policy": json.dumps(policy, separators=(",", ":"), sort_keys=True),
        "enforcement_state": json.dumps(enforcement, separators=(",", ":"), sort_keys=True),
    })
    return _handoff_page(f"http://127.0.0.1:{port}/callback?{query}")


@router.post("/api/entitlement/refresh")
async def refresh_entitlement(request: Request) -> JSONResponse:
    body = await read_bounded_body(request, MAX_REFRESH_BYTES, "refresh request too large")
    refresh_nonce = request.headers.get("x-chudware-refresh-nonce", "")
    if not _STATE_RE.fullmatch(refresh_nonce):
        raise HTTPException(400, "invalid refresh nonce")

    # One-release migration path: old clients sent only the compact token. It
    # is authenticated before receiving a signed, nonce-bound reauthorization.
    try:
        envelope = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            legacy = verify_compact(body.decode("ascii"))
        except (HTTPException, UnicodeDecodeError):
            key = request_network(request)
            _refresh_by_ip.enforce(key, "entitlement refreshes")
            raise HTTPException(401, "invalid entitlement token")
        _refresh_by_subject.enforce(legacy["sub"], "entitlement refreshes")
        return JSONResponse(_signed_refusal("device reauthorization required", refresh_nonce))

    if not isinstance(envelope, dict) or set(envelope) != _REFRESH_FIELDS:
        raise HTTPException(400, "invalid refresh envelope")
    if envelope.get("request_nonce") != refresh_nonce:
        raise HTTPException(401, "refresh nonce mismatch")
    sequence = envelope.get("sequence")
    ack = envelope.get("last_acked_event_sequence")
    if type(sequence) is not int or sequence <= 0 or type(ack) is not int or ack < 0:
        raise HTTPException(400, "invalid refresh sequence")
    if not all(isinstance(envelope.get(k), str) for k in (
        "token", "app_version", "platform", "integrity_digest", "signature"
    )):
        raise HTTPException(400, "invalid refresh envelope")
    if envelope["customer_receipt"] is not None and not isinstance(envelope["customer_receipt"], dict):
        raise HTTPException(400, "invalid refresh envelope")
    token_payload = verify_compact(envelope["token"])
    _refresh_by_subject.enforce(token_payload["sub"], "entitlement refreshes")

    account = accounts.get_account(token_payload["email"])
    with tx() as cur:
        cur.execute(
            """
            SELECT device_public_key, last_refresh_sequence, last_event_sequence,
                   last_security_checkin_at, policy_id, revoked_at
              FROM desktop_entitlements
             WHERE email = %s AND subject = %s
             FOR UPDATE
            """,
            (token_payload["email"], token_payload["sub"]),
        )
        device = cur.fetchone()
        if device is None or device["revoked_at"] is not None or device["device_public_key"] is None:
            return JSONResponse(_signed_refusal("account identity is no longer valid", refresh_nonce))
        _verify_device_object(envelope, bytes(device["device_public_key"]))
        if sequence <= int(device["last_refresh_sequence"]):
            return JSONResponse(_signed_refusal("device reauthorization required", refresh_nonce))
        if ack != int(device["last_event_sequence"]):
            raise HTTPException(409, "security event acknowledgement mismatch")
        assignment = _policy_assignment(account) if account is not None else {"policy_id": "standard", "endpoint": None}
        if assignment["policy_id"] == "customer_endpoint":
            receipt = envelope["customer_receipt"]
            _verify_customer_receipt(
                receipt, bytes(device["device_public_key"]), subject=token_payload["sub"],
                sequence=sequence, request_nonce=refresh_nonce,
            )
            _forward_customer_receipt(str(assignment["endpoint"] or ""), receipt)
        elif (device["last_security_checkin_at"] is None
                or device["last_security_checkin_at"].timestamp() < time.time() - 900):
            raise HTTPException(409, "recent security check-in required")
        if account is None or _entitlement_window(account, int(time.time())) is None:
            return JSONResponse(_signed_refusal("desktop entitlement is inactive", refresh_nonce))
        fresh = _issue(account, token_payload["sub"])
        fresh_payload = verify_compact(fresh)
        policy_id = str(assignment["policy_id"])
        policy = _security_policy(account, fresh_payload, refresh_nonce, policy_id)
        cur.execute(
            """
            UPDATE desktop_entitlements
               SET last_refresh_sequence = %s, last_checkin_at = now(), policy_id = %s
             WHERE email = %s AND subject = %s
            """,
            (sequence, policy["id"], token_payload["email"], token_payload["sub"]),
        )
    enforcement = _enforcement_state(token_payload["sub"], "active", min(
        int(fresh_payload["paid_through"]),
        int(fresh_payload["issued_at"]) + int(fresh_payload["grace_days"]) * 86400,
    ))
    return JSONResponse({"entitled": True, "token": fresh, "security_policy": policy,
                         "enforcement_state": enforcement})
