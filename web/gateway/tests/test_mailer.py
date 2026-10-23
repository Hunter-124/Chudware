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
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import limits  # noqa: E402
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
    monkeypatch.setattr(mailer.settings, "mail_global_per_hour", 20)
    monkeypatch.setattr(mailer, "outbound_global", mailer.RateLimiter(20, 3600.0, max_keys=1))
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


def test_global_mail_budget_has_a_sane_default(monkeypatch):
    monkeypatch.delenv("CHUDWARE_MAIL_GLOBAL_PER_HOUR", raising=False)
    assert config.Settings().mail_global_per_hour == 20


def test_global_mail_budget_reads_the_environment(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_GLOBAL_PER_HOUR", "7")
    assert config.Settings().mail_global_per_hour == 7


@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_global_mail_budget_refuses_to_boot(monkeypatch, value):
    monkeypatch.setenv("CHUDWARE_MAIL_GLOBAL_PER_HOUR", value)
    with pytest.raises(ValueError, match="CHUDWARE_MAIL_GLOBAL_PER_HOUR"):
        config.Settings()


def test_unknown_backend_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_BACKEND", "sendmail")
    with pytest.raises(ValueError, match="CHUDWARE_MAIL_BACKEND"):
        config.Settings()


def test_backend_without_its_credential_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_BACKEND", "resend")
    monkeypatch.delenv("CHUDWARE_RESEND_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CHUDWARE_RESEND_API_KEY"):
        config.Settings()


def test_starttls_is_the_default_tls_mode(monkeypatch):
    monkeypatch.delenv("CHUDWARE_SMTP_TLS", raising=False)
    assert config.Settings().smtp_tls == "starttls"


def test_unknown_tls_mode_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("CHUDWARE_MAIL_BACKEND", "smtp")
    monkeypatch.setenv("CHUDWARE_SMTP_HOST", "mx.example.com")
    monkeypatch.setenv("CHUDWARE_SMTP_TLS", "plaintext")
    with pytest.raises(ValueError, match="CHUDWARE_SMTP_TLS"):
        config.Settings()


@pytest.mark.parametrize("name", [
    "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY", "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY",
])
def test_settings_rejects_malformed_encryption_keys_without_echoing_values(monkeypatch, name):
    raw = "fixture-not-base64-key-secret%"
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=name) as refused:
        config.Settings()
    assert raw not in str(refused.value)


def test_settings_refuses_reusing_the_same_key_for_mail_and_provider_context(monkeypatch):
    import base64

    encoded = base64.urlsafe_b64encode(b"same-fixture-key-material-32bytes!"[:32]).decode()
    monkeypatch.setenv("CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY", encoded)
    monkeypatch.setenv("CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY", encoded)
    with pytest.raises(ValueError, match="independent"):
        config.Settings()


def test_a_relay_without_starttls_is_refused_rather_than_sent_in_the_clear(
    mail, monkeypatch
):
    """The security property: no silent plaintext fallback.

    A relay that does not advertise STARTTLS used to be used anyway, putting the
    relay password and every verification link on the wire. It must now fail and
    say which knob fixes it.
    """
    class NoStartTls:
        def ehlo(self) -> None:
            return None

        def has_extn(self, name: str) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    stub = NoStartTls()
    monkeypatch.setattr(mailer.settings, "mail_backend", "smtp")
    monkeypatch.setattr(mailer.settings, "smtp_host", "mx.example.com")
    monkeypatch.setattr(mailer.settings, "smtp_tls", "starttls")
    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *a, **k: stub)

    with pytest.raises(mailer.MailDeliveryError, match="CHUDWARE_SMTP_TLS=implicit"):
        mailer._deliver_smtp("ada@example.com", mailer.render("verify_email", VERIFY_CTX))
    assert stub.closed is True


def test_implicit_mode_opens_a_tls_socket_instead_of_upgrading(mail, monkeypatch):
    """465-only listeners never see a plaintext greeting or a STARTTLS probe."""
    calls: dict[str, object] = {}

    class Wrapped:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc) -> None:  # noqa: ANN002
            return None

        def login(self, user: str, password: str) -> None:
            calls["login"] = user

        def send_message(self, msg) -> None:  # noqa: ANN001
            calls["sent"] = msg["To"]

    def fake_ssl(host, port, timeout=None, context=None):  # noqa: ANN001, ANN202
        calls["host"] = host
        calls["port"] = port
        calls["verified"] = context.verify_mode
        return Wrapped()

    def explode(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("implicit mode must not open a plaintext SMTP session")

    monkeypatch.setattr(mailer.settings, "mail_backend", "smtp")
    monkeypatch.setattr(mailer.settings, "smtp_host", "mx.example.com")
    monkeypatch.setattr(mailer.settings, "smtp_port", 465)
    monkeypatch.setattr(mailer.settings, "smtp_tls", "implicit")
    monkeypatch.setattr(mailer.settings, "smtp_user", "noreply@example.com")
    monkeypatch.setattr(mailer.settings, "smtp_pass", "pw")
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(mailer.smtplib, "SMTP", explode)

    mailer._deliver_smtp("ada@example.com", mailer.render("verify_email", VERIFY_CTX))

    assert calls["host"] == "mx.example.com"
    assert calls["port"] == 465
    assert calls["login"] == "noreply@example.com"
    assert calls["sent"] == "ada@example.com"
    # A context that skips verification would happily talk to any impostor relay.
    assert calls["verified"] == ssl.CERT_REQUIRED


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
    assert mailer.decrypt_context(row["to_addr"], row["template"], row["ctx_json"])["verify_url"] == VERIFY_CTX["verify_url"]
    assert VERIFY_CTX["verify_url"] not in row["ctx_json"]
    assert "abc123" not in row["ctx_json"]
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
    assert row["ctx_json"] == "{}"
    # A delivered row is never claimed again.
    assert asyncio.run(mailer.drain_once()) == 0
    assert _rows()[0]["attempts"] == 1


def test_console_delivery_never_logs_bearer_context(mail, caplog):
    secret = "single-use-token-very-secret"
    ctx = dict(VERIFY_CTX, verify_url=f"https://example.test/verify?token={secret}")
    with caplog.at_level("INFO"):
        mailer.send("ada@example.com", "verify_email", ctx)
        assert asyncio.run(mailer.drain_once()) == 1
    assert secret not in caplog.text


def test_unrenderable_mail_is_dropped_without_burning_budget(mail):
    mailer.send("ada@example.com", "no_such_template", {})
    mailer.send("ada@example.com", "verify_email", {"email": "ada@example.com"})
    mailer.send("not-an-address", "verify_email", VERIFY_CTX)
    assert _rows() == []

    # None of those consumed the recipient's hourly allowance.
    for _ in range(5):
        mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 5
    assert mailer.outbound_global.allowed("all")
    assert len(mailer.outbound_global._hits["all"]) == 5


def test_outbound_rate_limit_is_per_recipient(mail):
    grace_ctx = dict(VERIFY_CTX, email="grace@example.com")
    for _ in range(7):
        mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    mailer.send("grace@example.com", "verify_email", grace_ctx)

    queued = [row["to_addr"] for row in _rows()]
    assert queued.count("ada@example.com") == 5
    assert queued.count("grace@example.com") == 1


@pytest.mark.parametrize("aliases", [
    ["Ada@example.com", "ada+signup@example.com", "ADA+reset@EXAMPLE.COM"],
    ["ada.lovelace@gmail.com", "adalovelace+signup@googlemail.com",
     "A.D.A.L.O.V.E.L.A.C.E+reset@GMAIL.COM"],
])
def test_recipient_aliases_share_budget_without_rewriting_delivery(mail, aliases):
    destinations = [aliases[i % len(aliases)] for i in range(5)]
    for address in destinations:
        mailer.send(f"  {address}  ", "verify_email", VERIFY_CTX)
    mailer.send(aliases[-1], "verify_email", VERIFY_CTX)

    rows = _rows()
    assert [row["to_addr"] for row in rows] == destinations
    for row in rows:
        assert mailer.decrypt_context(
            row["to_addr"], row["template"], row["ctx_json"]
        ) == VERIFY_CTX


def test_dots_in_non_gmail_recipients_do_not_merge_mailboxes(mail):
    for _ in range(5):
        mailer.send("ada.lovelace@example.com", "verify_email", VERIFY_CTX)
    mailer.send("adalovelace@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 6


def test_recipient_denials_do_not_spend_global_budget(mail, monkeypatch):
    monkeypatch.setattr(mailer.settings, "mail_global_per_hour", 6)
    for i in range(15):
        mailer.send(f"a.d.a+{i}@googlemail.com", "verify_email", VERIFY_CTX)
    mailer.send("grace@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 6
    assert _rows()[-1]["to_addr"] == "grace@example.com"


def test_global_mail_budget_is_configurable_and_survives_recipient_lru_churn(
    mail, monkeypatch
):
    monkeypatch.setenv("CHUDWARE_MAIL_GLOBAL_PER_HOUR", "3")
    monkeypatch.setattr(
        mailer.settings, "mail_global_per_hour", config.Settings().mail_global_per_hour
    )
    monkeypatch.setattr(mailer, "outbound", mailer.RateLimiter(5, 3600.0, max_keys=2))
    now = [1000.0]
    monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: now[0]))
    for i in range(8):
        mailer.send(f"person{i}@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 3
    assert mailer.outbound_global.window == 3600.0
    # Global refusals have not spent the new recipient's budget.
    for _ in range(5):
        assert mailer.outbound.check("person7@example.com")

    now[0] += 3601.0
    mailer.send("another@example.com", "verify_email", VERIFY_CTX)
    assert len(_rows()) == 4


def test_global_mail_reservations_are_atomic_across_preparing_threads(mail, monkeypatch):
    monkeypatch.setattr(mailer.settings, "mail_global_per_hour", 3)
    with ThreadPoolExecutor(max_workers=8) as workers:
        prepared = list(workers.map(
            lambda i: mailer.prepare(f"person{i}@example.com", "verify_email", VERIFY_CTX),
            range(20),
        ))
    assert sum(message is not None for message in prepared) == 3
    assert _rows() == [], "preparing must not enqueue outside the token transaction"


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
    assert "MailDeliveryError: delivery failed" == _rows()[0]["last_error"]


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
    assert row["last_error"] == "MailDeliveryError: delivery failed"
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
    assert row["last_error"] == "MailDeliveryError: delivery failed"
    assert row["ctx_json"] == "{}"


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
    assert last_error == "MailDeliveryError: delivery failed"


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
    assert last_error == "MailDeliveryError: delivery failed"


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


def test_context_envelope_is_authenticated_and_requires_external_key(mail, monkeypatch):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    row = _rows()[0]
    assert set(json.loads(row["ctx_json"])) == {"encrypted_v1"}
    assert "token=" not in row["ctx_json"]
    assert mailer.decrypt_context(row["to_addr"], row["template"], row["ctx_json"]) == VERIFY_CTX
    with pytest.raises(InvalidTag):
        mailer.decrypt_context("other@example.com", row["template"], row["ctx_json"])
    with pytest.raises(InvalidTag):
        mailer.decrypt_context(row["to_addr"], "password_reset", row["ctx_json"])
    monkeypatch.setattr(mailer.settings, "mail_context_encryption_key", b"x" * 32)
    with pytest.raises(InvalidTag):
        mailer.decrypt_context(row["to_addr"], row["template"], row["ctx_json"])
    with pytest.raises(KeyError):
        mailer.decrypt_context(row["to_addr"], row["template"], json.dumps(VERIFY_CTX))


@pytest.mark.parametrize("terminal", ["sent", "expired", "abandoned"])
def test_terminal_context_is_erased_before_claim(mail, monkeypatch, terminal):
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    with db.tx() as cur:
        if terminal == "sent":
            cur.execute("UPDATE mail_outbox SET sent_at = now()")
        elif terminal == "expired":
            cur.execute("UPDATE mail_outbox SET context_expires_at = now() - interval '1 second'")
        else:
            cur.execute("UPDATE mail_outbox SET attempts = %s", (mailer.MAX_ATTEMPTS,))
    def no_delivery(*args):
        raise AssertionError("terminal context must never be delivered")
    monkeypatch.setattr(mailer, "_deliver", no_delivery)
    assert asyncio.run(mailer.drain_once()) == 0
    assert _rows()[0]["ctx_json"] == "{}"


def test_failure_never_retains_or_logs_bearer_exception_text(mail, monkeypatch, caplog):
    secret = "fixture-bearer-capability"
    mailer.send("ada@example.com", "verify_email", dict(VERIFY_CTX, verify_url=f"https://example.test/?token={secret}"))
    def unsafe_transport(*args):
        raise mailer.MailDeliveryError(secret)
    monkeypatch.setattr(mailer, "_deliver", unsafe_transport)
    with caplog.at_level("WARNING"):
        assert asyncio.run(mailer.drain_once()) == 0
    assert secret not in caplog.text
    assert secret not in json.dumps(_rows(), default=str)


@pytest.fixture
def disposable_tls_smtp(tmp_path, monkeypatch):
    """Real loopback TLS/auth SMTP capture; no external DNS/provider or credentials."""
    import base64
    import ipaddress
    import socketserver
    import threading
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "disposable SMTP fixture")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=True, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                                    encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "smtp-cert.pem", tmp_path / "smtp-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    mailbox = []
    credentials = ("fixture-user", "fixture-smtp-password")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            self.wfile.write(b"220 fixture.local ESMTP\r\n")
            authenticated = False
            while True:
                line = self.rfile.readline(8193)
                if not line or len(line) > 8192:
                    return
                command, _, arguments = line.strip().partition(b" ")
                command = command.upper()
                if command == b"EHLO":
                    self.wfile.write(b"250-fixture.local\r\n250 AUTH PLAIN\r\n")
                elif command == b"AUTH":
                    mechanism, _, response = arguments.partition(b" ")
                    expected = b"\0" + credentials[0].encode() + b"\0" + credentials[1].encode()
                    authenticated = mechanism.upper() == b"PLAIN" and base64.b64decode(response, validate=True) == expected
                    self.wfile.write(b"235 2.7.0 authenticated\r\n" if authenticated else b"535 5.7.8 refused\r\n")
                elif command in {b"MAIL", b"RCPT"}:
                    self.wfile.write(b"250 2.1.0 accepted\r\n" if authenticated else b"530 authentication required\r\n")
                elif command == b"DATA" and authenticated:
                    self.wfile.write(b"354 send message\r\n")
                    message = bytearray()
                    while True:
                        row = self.rfile.readline(8193)
                        if not row or len(row) > 8192 or len(message) + len(row) > 262144:
                            return
                        if row == b".\r\n":
                            break
                        message.extend(row[1:] if row.startswith(b"..") else row)
                    mailbox.append(bytes(message))
                    self.wfile.write(b"250 2.0.0 fixture receipt\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 2.0.0 bye\r\n")
                    return
                else:
                    self.wfile.write(b"502 unsupported fixture command\r\n")

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            try:
                return context.wrap_socket(connection, server_side=True), address
            except BaseException:
                connection.close()
                raise

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
    try:
        yield server.server_address[1], credentials, mailbox
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_disposable_pg_tls_smtp_receipt_does_not_turn_offline_readiness_into_deliverability(
    mail, pg, tmp_path, monkeypatch, disposable_tls_smtp,
):
    import base64
    import os
    import subprocess
    from email import policy
    from email.parser import BytesParser

    port, credentials, mailbox = disposable_tls_smtp
    monkeypatch.setattr(mailer.settings, "mail_backend", "smtp")
    monkeypatch.setattr(mailer.settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(mailer.settings, "smtp_port", port)
    monkeypatch.setattr(mailer.settings, "smtp_tls", "implicit")
    monkeypatch.setattr(mailer.settings, "smtp_user", credentials[0])
    monkeypatch.setattr(mailer.settings, "smtp_pass", credentials[1])
    values = {
        "CHUDWARE_DEPLOY_ENV": "prod", "CHUDWARE_DB_URL": pg,
        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
        "CHUDWARE_MAIL_BACKEND": "smtp", "CHUDWARE_SMTP_HOST": "127.0.0.1",
        "CHUDWARE_SMTP_PORT": str(port), "CHUDWARE_SMTP_TLS": "implicit",
        "CHUDWARE_SMTP_USER": credentials[0], "CHUDWARE_SMTP_PASS": credentials[1],
    }
    for label, name in (("data", "CHUDWARE_WEB_DATA_ROOT"), ("workspace", "CHUDWARE_WORKSPACE_ROOT"),
                        ("bucket", "CHUDWARE_WEB_BUCKET_ROOT"), ("cells", "CHUDWARE_CELLS_ROOT")):
        values[name] = str(tmp_path / label)
        Path(values[name]).mkdir()
    path = tmp_path / "gateway.env"
    path.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))
    path.chmod(0o600)
    helper = Path(__file__).resolve().parents[2] / "deploy/gateway_preflight.py"
    def validate():
        result = subprocess.run([sys.executable, "-I", str(helper), "validate", "--env-file", str(path),
                                 "--environment", "prod"], env={"PATH": os.defpath},
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["configuration"]["status"] == "PASS"
        assert report["deliverability"]["status"] == "UNKNOWN"
        assert credentials[1] not in result.stdout + result.stderr
    validate()
    mailer.send("ada@example.com", "verify_email", VERIFY_CTX)
    assert asyncio.run(mailer.drain_once()) == 1
    assert len(mailbox) == 1
    message = BytesParser(policy=policy.default).parsebytes(mailbox[0])
    assert message["To"] == "ada@example.com"
    assert VERIFY_CTX["verify_url"] in message.get_body(preferencelist=("plain",)).get_content()
    row = _rows()[0]
    assert row["sent_at"] is not None and row["ctx_json"] == "{}"
    # A disposable SMTP receipt proves only this fixture path; it cannot attest
    # external DNS/SPF/DKIM or customer/provider delivery, even after a sent row.
    validate()
