"""Transactional email: render, queue, drain.

Ordinary mail uses :func:`send`; credential transitions reserve eligibility with
:func:`prepare` and persist with :func:`enqueue` in their token transaction.
Templates use :class:`string.Template`; queued context is AES-256-GCM ciphertext
bound to recipient/template with a key outside PostgreSQL. It does not talk to a mail
server. A single asyncio task started by :func:`start_drain` claims due rows and
delivers them through the backend named by ``CHUDWARE_MAIL_BACKEND``.

That split is the whole point: verification mail, password resets and dunning
notices all hang off HTTP handlers, and a Resend outage or a wrong SMTP port
must never turn a signup into a 500. Everything :func:`send` can go wrong at is
logged and swallowed; everything the *transport* can go wrong at is retried
with exponential backoff and abandoned after ``MAX_ATTEMPTS`` with the failure
recorded in ``mail_outbox.last_error`` for an operator to read.
Context is erased immediately on successful delivery or final failure, and the
drain erases expired or abandoned envelopes before claiming any due messages.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import smtplib
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from pathlib import Path
from string import Template
from typing import Any, Mapping

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from .config import settings
    from . import deployment_drain
    from .db import tx
    from .limits import RateLimiter
except ImportError:  # uvicorn app:app from the package dir
    from config import settings
    import deployment_drain
    from db import tx
    from limits import RateLimiter


log = logging.getLogger("chudware-web.mail")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "email"

# Every template the product may send. An unlisted name is a caller bug, not a
# missing file, and is rejected before anything reaches the queue.
TEMPLATES = frozenset(
    {
        "verify_email",
        "password_reset",
        "register_exists",
        "oauth_only_reset",
        "trial_ending",
        "trial_ended",
        "payment_failed",
        "receipt",
        "org_invite",
    }
)

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Retry schedule. Attempt n is made BASE * 2**(n-1) seconds after attempt n-1:
# 1m, 2m, 4m, 8m, then the row is abandoned. Five attempts spread over a
# quarter hour covers a relay restart or a transient 5xx; anything longer-lived
# is an operator problem and sitting in the queue forever only hides it.
MAX_ATTEMPTS = 5
BASE_BACKOFF_SEC = 60.0
MAX_BACKOFF_SEC = 3600.0

DRAIN_BATCH = 20
DRAIN_INTERVAL_SEC = 10.0
HTTP_TIMEOUT_SEC = 15.0
SMTP_TIMEOUT_SEC = 20.0

# Aliases share the five-per-hour recipient budget. A separate, single-key
# budget bounds reservations across recipients even if their LRU entries churn.
# Retrying a queued message never makes another reservation.
outbound = RateLimiter(5, 3600.0)
outbound_global = RateLimiter(settings.mail_global_per_hour, 3600.0, max_keys=1)
_reservation_lock = threading.Lock()


class MailDeliveryError(RuntimeError):
    """A backend refused the message or could not reach its transport."""


@dataclass(frozen=True, slots=True)
class Rendered:
    subject: str
    text: str
    html: str


def render(template: str, ctx: Mapping[str, Any]) -> Rendered:
    """Substitute ``ctx`` into the three files of ``template``.

    HTML gets escaped values, plain text gets raw ones — a display name with an
    ampersand in it must not produce broken markup. Raises ``KeyError`` for a
    placeholder the caller did not supply and ``FileNotFoundError`` for an
    unknown template; both are caller bugs and both are caught by :func:`send`
    so neither can reach an HTTP response.
    """
    if template not in TEMPLATES:
        raise FileNotFoundError(f"unknown mail template {template!r}")
    plain = {key: "" if value is None else str(value) for key, value in ctx.items()}
    escaped = {key: escape(value) for key, value in plain.items()}
    return Rendered(
        subject=_template(template, "subject").substitute(plain).strip(),
        text=_template(template, "txt").substitute(plain),
        html=_template(template, "html").substitute(escaped),
    )


def _template(template: str, ext: str) -> Template:
    # Read per render rather than caching: the files are a kilobyte each, and
    # fixing a typo in customer-facing copy should not need a gateway restart.
    return Template((TEMPLATE_DIR / f"{template}.{ext}").read_text(encoding="utf-8"))


def _context_key() -> bytes:
    key = settings.mail_context_encryption_key
    if len(key) != 32:
        raise RuntimeError("CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY must encode 32 bytes")
    return key


def _context_aad(to: str, template: str) -> bytes:
    return json.dumps(["chudware-mail-context-v1", to, template]).encode()


def decrypt_context(to: str, template: str, envelope: str) -> dict[str, Any]:
    """Open an authenticated envelope; legacy plaintext is never accepted."""
    encoded = json.loads(envelope)["encrypted_v1"]
    raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    plaintext = AESGCM(_context_key()).decrypt(
        raw[:12], raw[12:], _context_aad(to, template)
    )
    return json.loads(plaintext)


@dataclass(frozen=True, slots=True)
class Prepared:
    to: str
    template: str
    envelope: str
    expires_at: datetime


def _recipient_key(address: str) -> str:
    """Mailbox aliases share abuse budget without rewriting delivery addresses."""
    local, domain = address.strip().lower().rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        domain = "gmail.com"
        local = local.replace(".", "")
    return f"{local}@{domain}"


def prepare(to: str, template: str, ctx: Mapping[str, Any], *,
            expires_at: datetime | None = None) -> Prepared | None:
    """Validate/encrypt and reserve mail budget BEFORE replacing any capability."""
    addr = parseaddr(to)[1].strip()
    if "@" not in addr:
        return None
    try:
        render(template, ctx)
        nonce = os.urandom(12)
        encrypted = AESGCM(_context_key()).encrypt(
            nonce, json.dumps(dict(ctx), default=str).encode(), _context_aad(addr, template)
        )
        envelope = json.dumps({"encrypted_v1": base64.urlsafe_b64encode(nonce + encrypted).decode()})
    except Exception:
        # Context and rendered bodies may contain bearer capabilities.
        log.warning("mail: cannot prepare template=%s", template)
        return None
    key = _recipient_key(addr)
    # Both decisions are one reservation even when callers prepare in threads.
    # Check the recipient without recording first: rejected aliases must not
    # consume the service-wide allowance, nor vice versa.
    with _reservation_lock:
        if not outbound.allowed(key):
            log.warning("mail: rate limited %s template=%s", addr, template)
            return None
        outbound_global.limit = settings.mail_global_per_hour
        if not outbound_global.check("all"):
            log.warning("mail: global reservation limit template=%s", template)
            return None
        outbound.check(key)
    if expires_at is None:
        hours = 1 if template == "password_reset" else 24
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    return Prepared(addr, template, envelope, expires_at)


def enqueue(message: Prepared) -> None:
    """Persist accepted mail in the caller's transaction; failures MUST unwind it."""
    with tx() as cur:
        cur.execute(
            "INSERT INTO mail_outbox (to_addr, template, ctx_json, next_attempt_at, context_expires_at)"
            " VALUES (%s, %s, %s, now(), %s)",
            (message.to, message.template, message.envelope, message.expires_at),
        )


def send(to: str, template: str, ctx: dict[str, Any]) -> None:
    """Best-effort queueing for mail without a coupled credential transition."""
    message = prepare(to, template, ctx)
    if message is None:
        return
    try:
        enqueue(message)
    except Exception:
        log.warning("mail: enqueue failed template=%s", template)


# Claim and schedule in one statement so a worker that dies mid-delivery still
# burns exactly one attempt: the increment is committed before the send starts,
# and next_attempt_at doubles as the lease. `attempts` on the right-hand side is
# the pre-increment value, so attempt n waits BASE * 2**(n-1).
_CLAIM_SQL = """
UPDATE mail_outbox
   SET attempts = attempts + 1,
       next_attempt_at = now()
           + make_interval(secs => LEAST(%s * power(2, attempts), %s))
 WHERE id IN (
           SELECT id
             FROM mail_outbox
            WHERE sent_at IS NULL
              AND attempts < %s
              AND next_attempt_at <= now()
              AND ctx_json <> '{}'
              AND context_expires_at > now()
            ORDER BY id
            LIMIT %s
       )
RETURNING id, to_addr, template, ctx_json, attempts
"""


def _claim(limit: int) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(_CLAIM_SQL, (BASE_BACKOFF_SEC, MAX_BACKOFF_SEC, MAX_ATTEMPTS, limit))
        return list(cur.fetchall())


@dataclass(frozen=True, slots=True)
class Backlog:
    """Undelivered outbox depth.

    ``pending`` is still retriable and a working drain keeps it near zero;
    ``abandoned`` is over the attempt cap and only grows when mail is being
    lost. ``oldest_age_sec`` is the tell for a drain that is not running at
    all, because from the outside that failure is invisible — every ``send``
    succeeded, the rows just sit there.
    """

    pending: int
    abandoned: int
    oldest_age_sec: float


_BACKLOG_SQL = """
SELECT count(*) FILTER (WHERE attempts < %s)  AS pending,
       count(*) FILTER (WHERE attempts >= %s) AS abandoned,
       COALESCE(
           EXTRACT(
               EPOCH FROM now() - min(created_at) FILTER (WHERE attempts < %s)
           ),
           0
       ) AS oldest_age_sec
  FROM mail_outbox
 WHERE sent_at IS NULL
"""


def backlog() -> Backlog:
    """Current outbox depth, for the drain's boot log and /metrics."""
    with tx() as cur:
        cur.execute(_BACKLOG_SQL, (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS))
        row = cur.fetchone() or {}
    return Backlog(
        pending=int(row.get("pending") or 0),
        abandoned=int(row.get("abandoned") or 0),
        oldest_age_sec=float(row.get("oldest_age_sec") or 0.0),
    )


def _record_sent(row_id: int) -> None:
    with tx() as cur:
        cur.execute(
            "UPDATE mail_outbox SET sent_at = now(), last_error = NULL, ctx_json = '{}' WHERE id = %s",
            (row_id,),
        )


def scrub_expired_context() -> int:
    """Erase sent, expired, and abandoned envelopes before claiming due mail."""
    with tx() as cur:
        cur.execute(
            "UPDATE mail_outbox SET ctx_json = '{}'"
            " WHERE ctx_json <> '{}' AND (sent_at IS NOT NULL OR attempts >= %s"
            " OR context_expires_at <= now())",
            (MAX_ATTEMPTS,),
        )
        return cur.rowcount


def _record_failure(row_id: int, attempts: int, exc: BaseException) -> None:
    # Transport exceptions can contain a message body or provider response.
    detail = f"{type(exc).__name__}: delivery failed"
    with tx() as cur:
        cur.execute(
            "UPDATE mail_outbox SET last_error = %s,"
            " ctx_json = CASE WHEN %s >= %s THEN '{}' ELSE ctx_json END WHERE id = %s",
            (detail, attempts, MAX_ATTEMPTS, row_id),
        )
    if attempts >= MAX_ATTEMPTS:
        log.error(
            "mail: giving up on id=%s after %d attempts — %s", row_id, attempts, detail
        )
    else:
        log.warning(
            "mail: attempt %d/%d failed for id=%s — %s",
            attempts,
            MAX_ATTEMPTS,
            row_id,
            detail,
        )


def _deliver(to: str, message: Rendered) -> None:
    """Hand one rendered message to the configured transport. Raises on failure."""
    backend = settings.mail_backend
    if backend == "console":
        _deliver_console(to, message)
    elif backend == "resend":
        _deliver_resend(to, message)
    elif backend == "smtp":
        _deliver_smtp(to, message)
    else:
        raise MailDeliveryError(f"unknown mail backend {backend!r}")


def _deliver_console(to: str, message: Rendered) -> None:
    # Console is a development transport, not a safe place for bearer links.
    # Log only routing metadata; never the rendered body.
    log.info(
        "mail[console] from=%s to=%s subject=%s body=[SUPPRESSED]",
        settings.mail_from,
        to,
        message.subject,
    )


def _deliver_resend(to: str, message: Rendered) -> None:
    if not settings.resend_api_key:
        # Reachable when the key is cleared after boot; config.py rejects the
        # combination at startup. Say what to do rather than let Resend answer
        # with a bare 401 that reads like a Chudware bug.
        raise MailDeliveryError(
            "CHUDWARE_RESEND_API_KEY is unset — set it to the API key of the "
            "verified chudware.com sending domain, or set CHUDWARE_MAIL_BACKEND "
            "to smtp or console"
        )
    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.mail_from,
                "to": [to],
                "subject": message.subject,
                "text": message.text,
                "html": message.html,
            },
            timeout=HTTP_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        raise MailDeliveryError(f"resend request failed: {exc}") from exc
    if response.status_code >= 400:
        raise MailDeliveryError(
            f"resend returned {response.status_code}: {response.text[:200]}"
        )


def _smtp_client() -> smtplib.SMTP:
    """Open an encrypted submission session.

    "implicit" wraps the socket before the greeting, which is the only thing a
    465-only listener accepts. "starttls" upgrades in-band and REQUIRES the
    server to advertise the extension: silently continuing in plaintext would
    put the relay password and every reset link on the wire.
    """
    context = ssl.create_default_context()
    if settings.smtp_tls == "implicit":
        return smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=SMTP_TIMEOUT_SEC,
            context=context,
        )
    client = smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SEC
    )
    client.ehlo()
    if not client.has_extn("starttls"):
        client.close()
        raise MailDeliveryError(
            f"{settings.smtp_host}:{settings.smtp_port} does not advertise STARTTLS; "
            "set CHUDWARE_SMTP_TLS=implicit for a TLS-only submission port such "
            "as 465"
        )
    client.starttls(context=context)
    client.ehlo()
    return client


def _deliver_smtp(to: str, message: Rendered) -> None:
    msg = EmailMessage()
    msg["Subject"] = message.subject
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg.set_content(message.text)
    msg.add_alternative(message.html, subtype="html")
    try:
        with _smtp_client() as client:
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_pass)
            client.send_message(msg)
    except MailDeliveryError:
        raise
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError(f"smtp delivery failed: {exc}") from exc


async def drain_once() -> int:
    # Reserve before claiming rows. Closed maintenance leaves the durable
    # outbox untouched, while a previously accepted batch finishes normally.
    try:
        admission = deployment_drain.controller().acquire(inherit=False)
    except deployment_drain.HTTPException as exc:
        if exc.status_code != 503:
            raise
        return 0
    try:
        with admission.bind():
            task = asyncio.create_task(_drain_once_accepted())
    except BaseException:
        admission.close()
        raise
    task.add_done_callback(lambda done: admission.close() if not done.cancelled() else None)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


async def _drain_once_accepted() -> int:
    """Attempt every message that is due. Returns how many were delivered.

    Blocking work — psycopg calls and the transports themselves — runs in a
    worker thread so the drain never stalls the event loop the gateway serves.
    """
    await asyncio.to_thread(scrub_expired_context)
    claimed = await asyncio.to_thread(_claim, DRAIN_BATCH)
    delivered = 0
    for row in claimed:
        try:
            message = render(row["template"], decrypt_context(
                row["to_addr"], row["template"], row["ctx_json"]
            ))
            await asyncio.to_thread(_deliver, row["to_addr"], message)
        except Exception as exc:  # noqa: BLE001 — every failure is retriable
            await asyncio.to_thread(_record_failure, row["id"], row["attempts"], exc)
            continue
        await asyncio.to_thread(_record_sent, row["id"])
        delivered += 1
    return delivered


_drain_task: asyncio.Task[None] | None = None
_drain_stop: asyncio.Event | None = None


async def _drain_loop(stop: asyncio.Event) -> None:
    try:
        depth = await asyncio.to_thread(backlog)
    except Exception:  # noqa: BLE001 — a boot-time read must not kill the loop
        log.exception("mail: could not read outbox backlog at startup")
    else:
        log.log(
            logging.WARNING if depth.pending or depth.abandoned else logging.INFO,
            "mail: outbox backlog pending=%d abandoned=%d oldest=%.0fs",
            depth.pending,
            depth.abandoned,
            depth.oldest_age_sec,
        )
    while not stop.is_set():
        try:
            await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any single pass
            log.exception("mail: drain pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=DRAIN_INTERVAL_SEC)
        except TimeoutError:
            pass


def start_drain() -> None:
    """Start the one outbox drain task. Idempotent; call from app startup."""
    global _drain_task, _drain_stop
    if _drain_task is not None and not _drain_task.done():
        return
    _drain_stop = asyncio.Event()
    _drain_task = asyncio.create_task(_drain_loop(_drain_stop), name="mail-drain")
    log.info("mail: drain started backend=%s", settings.mail_backend)


async def stop_drain() -> None:
    """Stop new passes and wait for accepted deliveries, including their threads."""
    global _drain_task, _drain_stop
    task, stop = _drain_task, _drain_stop
    if task is None:
        return
    stop.set()
    await task
    if _drain_task is task:
        _drain_task = _drain_stop = None


__all__ = [
    "Backlog",
    "MAX_ATTEMPTS",
    "MailDeliveryError",
    "Rendered",
    "TEMPLATES",
    "backlog",
    "drain_once",
    "decrypt_context",
    "enqueue",
    "prepare",
    "outbound",
    "render",
    "send",
    "start_drain",
    "stop_drain",
]
