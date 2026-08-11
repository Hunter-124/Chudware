"""mailer: rendering, queueing, and the retry semantics behind them.

The load-bearing property here is not "mail gets delivered" — it is that
*nothing about mail can fail a request*. Verification, password reset and
dunning all hang off HTTP handlers, so a refused SMTP connection, a Resend
4xx, an unrenderable template or an unreachable database must all end as a log
line and a queue row, never as a 500. The retry tests exist to prove the
failure has somewhere else to live.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402

VERIFY_CTX = {
    # An ampersand in the URL is deliberate: it must survive plain text intact
    # and come out escaped in the HTML part.
    "email": "ada@example.com",
    "verify_url": "https://chudware.com/verify?token=abc123&next=/app",
    "expires_hours": 24,
}


def _rows() -> list[dict]:
    """Every outbox row, oldest first. Goes through db.tx directly so it still
    works in the tests that break mailer.tx on purpose."""
    with db.tx() as cur:
        cur.execute("SELECT * FROM mail_outbox ORDER BY id")
        return list(cur.fetchall())


def _make_due() -> None:
    """Expire the delivery lease so the next drain pass claims the row again,
    instead of the test sleeping out a real backoff."""
    with db.tx() as cur:
        cur.execute("UPDATE mail_outbox SET next_attempt_at = now()")


def _lease_seconds(row: dict) -> float:
    return (row["next_attempt_at"] - datetime.now(timezone.utc)).total_seconds()


@pytest.fixture()
def mail(pg, monkeypatch):
    """mailer on a virgin schema, console backend, fresh rate-limit budget."""
    monkeypatch.setattr(mailer, "outbound", mailer.RateLimiter(5, 3600.0))
    monkeypatch.setattr(mailer.settings, "mail_backend", "console")
    return mailer


@pytest.fixture()
def dead_smtp(monkeypatch):
    """Point the SMTP backend at a port nothing listens on.

    A real connection-refused rather than a stubbed exception, so the test
    exercises smtplib and the error text an operator would actually read.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(mailer.settings, "mail_backend", "smtp")
    monkeypatch.setattr(mailer.settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(mailer.settings, "smtp_port", port)
    monkeypatch.setattr(mailer.settings, "smtp_user", "")
    return port


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_console_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("CHUDWARE_MAIL_BACKEND", raising=False)
    assert config.Settings().mail_backend == "console"


def test_unknown_backend_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_BACKEND", "sendmail")
    with pytest.raises(ValueError, match="CHUDWARE_MAIL_BACKEND"):
        config.Settings()


def test_backend_without_its_credential_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_BACKEND", "resend")
    monkeypatch.delenv("CHUDWARE_RESEND_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CHUDWARE_RESEND_API_KEY"):
        config.Settings()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_verify_email_substitutes_and_escapes():
    message = mailer.render("verify_email", VERIFY_CTX)

    assert message.subject == "Confirm your email to finish setting up Chudware"
    assert "ada@example.com" in message.text
    assert VERIFY_CTX["verify_url"] in message.text
    assert "24 hours" in message.text
    # Nothing left unsubstituted, in either part.
    assert "$" not in message.text
    assert "$" not in message.html
    # The HTML part says the same thing, with ctx escaped so a query string
    # cannot terminate an attribute or swallow the rest of the document.
    assert "ada@example.com" in message.html
    assert "https://chudware.com/verify?token=abc123&amp;next=/app" in message.html
    assert "token=abc123&next=" not in message.html


@pytest.mark.parametrize("template", sorted(mailer.TEMPLATES))
def test_every_template_has_all_three_parts_and_matching_placeholders(template):
    """The plain-text and HTML parts must be the same message: same variables,
    both present, subject non-empty."""
    parts = {}
    for ext in ("subject", "txt", "html"):
        path = mailer.TEMPLATE_DIR / f"{template}.{ext}"
        assert path.exists(), path
        parts[ext] = Template(path.read_text(encoding="utf-8"))
        assert parts[ext].is_valid(), f"{template}.{ext} has a stray dollar sign"

    text_vars = set(parts["txt"].get_identifiers())
    assert text_vars == set(parts["html"].get_identifiers())
    assert set(parts["subject"].get_identifiers()) <= text_vars

    ctx = {name: f"<{name}>" for name in text_vars}
    rendered = mailer.render(template, ctx)
    assert rendered.subject.strip()
    assert rendered.text.strip()
    assert rendered.html.lstrip().startswith("<!doctype html>")


def test_render_requires_every_placeholder():
    with pytest.raises(KeyError):
        mailer.render("verify_email", {"email": "ada@example.com"})


def test_render_rejects_an_unlisted_template():
    with pytest.raises(FileNotFoundError):
        mailer.render("../../config", {})


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


def test_send_queues_instead_of_delivering(mail):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)

    (row,) = _rows()
    assert row["to_addr"] == "ada@example.com"
    assert row["template"] == "verify_email"
    assert json.loads(row["ctx_json"])["verify_url"] == VERIFY_CTX["verify_url"]
    assert row["attempts"] == 0
    assert row["sent_at"] is None
    assert row["last_error"] is None


def test_send_accepts_a_display_name_and_stores_the_bare_address(mail):
    mailer.send("Ada Lovelace <ada@example.com>", "verify_email", VERIFY_CTX)
    assert _rows()[0]["to_addr"] == "ada@example.com"


def test_drain_delivers_and_marks_sent(mail):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)

    assert asyncio.run(mailer.drain_once()) == 1
    (row,) = _rows()
    assert row["sent_at"] is not None
    assert row["attempts"] == 1
    assert row["last_error"] is None
    # A delivered row is never claimed again.
    assert asyncio.run(mailer.drain_once()) == 0
    assert _rows()[0]["attempts"] == 1


def test_unrenderable_mail_is_dropped_without_burning_budget(mail):
    mailer.send("ada@example.com", "no_such_template", {})
    mailer.send("ada@example.com", "verify_email", {"email": "ada@example.com"})
    mailer.send("not-an-address", "verify_email", VERIFY_CTX)
    assert _rows() == []

    # None of those consumed the recipient's hourly allowance.
    for _ in range(5):
        mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 5


def test_outbound_rate_limit_is_per_recipient(mail):
    grace_ctx = dict(VERIFY_CTX, email="grace@example.com")
    for _ in range(7):
        mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    mailer.send("grace@example.com", "verify_email", grace_ctx)

    queued = [row["to_addr"] for row in _rows()]
    assert queued.count("ada@example.com") == 5
    assert queued.count("grace@example.com") == 1


# ---------------------------------------------------------------------------
# Failure containment — the reason the queue exists
# ---------------------------------------------------------------------------


def test_dead_transport_never_turns_a_request_into_a_500(mail, dead_smtp):
    """The actual requirement: a handler that sends mail still returns 200
    when the mail server is unreachable, and the failure lands on the drain
    task instead."""
    app = FastAPI()

    @app.post("/signup")
    def signup() -> dict[str, str]:
        mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
        return {"status": "check your email"}

    with TestClient(app) as client:
        response = client.post("/signup")

    assert response.status_code == 200
    assert response.json() == {"status": "check your email"}
    assert len(_rows()) == 1

    # The drain absorbs the refused connection; still no exception anywhere.
    assert asyncio.run(mailer.drain_once()) == 0
    assert "smtp delivery failed" in _rows()[0]["last_error"]


def test_dead_database_never_turns_a_request_into_a_500(mail, monkeypatch):
    def exploding_tx():
        raise db.DatabaseUnavailable("postgres is gone")

    monkeypatch.setattr(mailer, "tx", exploding_tx)
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)  # must not raise
    assert _rows() == []


def test_failed_delivery_records_the_error_and_backs_off(mail, dead_smtp):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)

    assert asyncio.run(mailer.drain_once()) == 0
    (row,) = _rows()
    assert row["attempts"] == 1
    assert row["sent_at"] is None
    assert "smtp delivery failed" in row["last_error"]
    # The first retry is a minute out, so an immediate second pass is a no-op
    # rather than a hot loop against a dead host.
    assert _lease_seconds(row) == pytest.approx(mailer.BASE_BACKOFF_SEC, abs=5.0)
    assert asyncio.run(mailer.drain_once()) == 0
    assert _rows()[0]["attempts"] == 1


def test_backoff_doubles_and_delivery_stops_at_five_attempts(mail, dead_smtp):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)

    for attempt, expected_wait in enumerate([60.0, 120.0, 240.0, 480.0, 960.0], 1):
        _make_due()
        assert asyncio.run(mailer.drain_once()) == 0
        (row,) = _rows()
        assert row["attempts"] == attempt
        assert _lease_seconds(row) == pytest.approx(expected_wait, abs=5.0)

    # Sixth pass: due, but over the cap, so it is never claimed again.
    _make_due()
    assert asyncio.run(mailer.drain_once()) == 0
    (row,) = _rows()
    assert row["attempts"] == mailer.MAX_ATTEMPTS == 5
    assert row["sent_at"] is None
    assert "smtp delivery failed" in row["last_error"]


def test_a_recovered_transport_delivers_a_previously_failed_message(
    mail, dead_smtp, monkeypatch
):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert asyncio.run(mailer.drain_once()) == 0
    assert _rows()[0]["last_error"]

    monkeypatch.setattr(mailer.settings, "mail_backend", "console")
    _make_due()
    assert asyncio.run(mailer.drain_once()) == 1
    (row,) = _rows()
    assert row["sent_at"] is not None
    assert row["last_error"] is None


def test_resend_rejection_is_recorded_not_raised(mail, monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["auth"] = kwargs["headers"]["Authorization"]
        return httpx.Response(422, text="the chudware.com domain is not verified")

    monkeypatch.setattr(mailer.settings, "mail_backend", "resend")
    monkeypatch.setattr(mailer.settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(httpx, "post", fake_post)

    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert asyncio.run(mailer.drain_once()) == 0

    assert captured["url"] == mailer.RESEND_ENDPOINT
    assert captured["auth"] == "Bearer re_test_key"
    assert captured["json"]["to"] == ["ada@example.com"]
    assert captured["json"]["from"] == mailer.settings.mail_from
    assert captured["json"]["subject"].startswith("Confirm your email")
    assert captured["json"]["html"].lstrip().startswith("<!doctype html>")

    last_error = _rows()[0]["last_error"]
    assert "422" in last_error
    assert "domain is not verified" in last_error


def test_resend_without_an_api_key_says_what_to_set(mail, monkeypatch):
    """An unset key must not become a bare 401 from Resend — the operator has
    to be told which variable to set."""

    def refuse(*args, **kwargs):
        raise AssertionError("no request may be sent without an API key")

    monkeypatch.setattr(mailer.settings, "mail_backend", "resend")
    monkeypatch.setattr(mailer.settings, "resend_api_key", "")
    monkeypatch.setattr(httpx, "post", refuse)

    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert asyncio.run(mailer.drain_once()) == 0

    last_error = _rows()[0]["last_error"]
    assert "CHUDWARE_RESEND_API_KEY is unset" in last_error
    assert "CHUDWARE_MAIL_BACKEND" in last_error


# ---------------------------------------------------------------------------
# Backlog visibility — an undrained queue must not look healthy
# ---------------------------------------------------------------------------


def test_backlog_separates_pending_from_abandoned(mail, dead_smtp):
    assert mailer.backlog() == mailer.Backlog(0, 0, 0.0)

    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    depth = mailer.backlog()
    assert (depth.pending, depth.abandoned) == (1, 0)
    assert depth.oldest_age_sec >= 0.0

    for _ in range(mailer.MAX_ATTEMPTS):
        _make_due()
        asyncio.run(mailer.drain_once())

    depth = mailer.backlog()
    assert (depth.pending, depth.abandoned) == (0, 1)


def test_backlog_ignores_delivered_mail(mail):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert asyncio.run(mailer.drain_once()) == 1
    assert mailer.backlog() == mailer.Backlog(0, 0, 0.0)
