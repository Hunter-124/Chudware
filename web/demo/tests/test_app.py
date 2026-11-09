"""The front door's own surface: waiting page, cookie, status/join/end, abuse limits."""

from __future__ import annotations

import re
import time

import pytest

from demo.cookies import COOKIE_NAME
from demo.podman import NAME_PREFIX

from .support import LOW, PLENTY, front_door


def test_waiting_page_is_static_strict_and_self_contained(tmp_path):
    with front_door(tmp_path) as fd:
        page = fd.client.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
        assert page.headers["x-frame-options"] == "DENY" and page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["cache-control"] == "no-store"
        html = page.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "style=" not in html
        assert not re.search(r"https?://", html)
        # What the page tells visitors (the owner's required disclosures).
        for phrase in ("Nothing persists", "45 minutes", "15 minutes", "AI models", "Privacy",
                       "deleted"):
            assert phrase in html
        js = fd.client.get("/demo/static/waiting.js")
        css = fd.client.get("/demo/static/waiting.css")
        assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        for asset in (js.text, css.text):
            assert not re.search(r"https?://", asset) and "eval(" not in asset and "innerHTML" not in asset
        assert fd.client.get("/demo/static/secret.txt").status_code == 404
        assert fd.client.get("/demo/static/..%2fapp.py").status_code == 404


def test_join_sets_a_hardened_cookie_reaches_active_and_root_redirects_to_the_app(tmp_path):
    with front_door(tmp_path) as fd:
        visitor = fd.visitor()
        joined = visitor.join()
        assert joined.status_code == 200
        cookie = joined.headers["set-cookie"]
        assert cookie.startswith(COOKIE_NAME + "=")
        lowered = cookie.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
        assert "path=/" in lowered and "domain=" not in lowered
        status = visitor.wait_for("active")
        assert status["position"] is None and status["warn"] is False
        assert status["hard_expires_at"] - status["idle_expires_at"] == pytest.approx(1800, abs=2)
        assert fd.runtime.started and fd.runtime.started[0].name.startswith(NAME_PREFIX)
        root = visitor.get("/")
        assert root.status_code == 307 and root.headers["location"] == "/app/"
        # A stranger still gets the waiting page.
        assert fd.visitor("198.51.100.77").get("/").status_code == 200


def test_status_document_matches_the_contract(tmp_path):
    with front_door(tmp_path, capacity=LOW) as fd:
        stranger = fd.visitor("198.51.100.9").get("/demo/status").json()
        assert stranger["state"] == "none" and stranger["free_ai_available"] is True
        visitor = fd.visitor()
        visitor.join()
        status = visitor.get("/demo/status").json()
        deadline = time.time() + 3                      # the first tick flags the capacity hold
        while status["note"] != "capacity" and time.time() < deadline:
            time.sleep(0.03)
            status = visitor.get("/demo/status").json()
        assert status["state"] == "queued" and status["position"] == 1 and status["queue_length"] == 1
        assert status["eta_seconds"] is None and status["note"] == "capacity"
        assert status["idle_expires_at"] is None and status["hard_expires_at"] is None
        assert status["warn"] is False and status["max_sessions"] == 3
        for key in ("state", "position", "queue_length", "eta_seconds", "idle_expires_at",
                    "hard_expires_at", "warn"):
            assert key in status
        assert visitor.get("/demo/status").headers["cache-control"] == "no-store"
        fd.state["capacity"] = PLENTY
        assert visitor.wait_for("active")["position"] is None


def test_status_wait_holds_the_request_until_the_deadline_when_nothing_changes(tmp_path):
    with front_door(tmp_path, capacity=LOW, status_wait_max=2) as fd:
        visitor = fd.visitor()
        visitor.join()
        begun = time.monotonic()
        held = visitor.get("/demo/status?wait=1").json()
        assert held["state"] == "queued" and time.monotonic() - begun >= 0.9
        assert visitor.get("/demo/status?wait=abc").json()["state"] == "queued"
        begun = time.monotonic()
        visitor.get("/demo/status?wait=9999")          # clamped to status_wait_max (2 s here)
        assert 1.5 <= time.monotonic() - begun < 4


def test_status_wait_returns_early_on_admission(tmp_path):
    with front_door(tmp_path, capacity=LOW) as fd:
        visitor = fd.visitor()
        visitor.join()
        fd.state["capacity"] = PLENTY
        begun = time.monotonic()
        result = visitor.get("/demo/status?wait=5").json()
        assert result["state"] in ("starting", "active") and time.monotonic() - begun < 3


def test_method_and_origin_refusals(tmp_path):
    with front_door(tmp_path) as fd:
        visitor = fd.visitor()
        assert visitor.get("/demo/join").status_code == 405
        assert visitor.get("/demo/end").status_code == 405
        assert visitor.post("/demo/status").status_code == 405
        assert visitor.post("/").status_code == 405
        assert visitor.post("/demo/health").status_code == 405
        cross = visitor.post("/demo/join", headers={"origin": "https://evil.example"})
        assert cross.status_code == 403 and "set-cookie" not in cross.headers
        assert visitor.post("/demo/join", headers={"origin": "http://app.chudware.com.evil.example"}).status_code == 403
        cross_end = visitor.post("/demo/end", headers={"origin": "https://evil.example"})
        assert cross_end.status_code == 403
        assert fd.manager.entries == {}                  # nothing was created by refused requests


def test_unknown_host_header_is_refused(tmp_path):
    with front_door(tmp_path) as fd:
        assert fd.client.get("/", headers={"host": "evil.example"}).status_code == 400
        assert fd.client.get("/demo/health", headers={"host": "app.chudware.com"}).status_code == 200


def test_one_entry_per_client_address_trusting_cf_header_only_from_loopback(tmp_path):
    with front_door(tmp_path) as fd:
        first, second, third = fd.visitor("203.0.113.5"), fd.visitor("203.0.113.5"), fd.visitor("203.0.113.6")
        assert first.join().status_code == 200
        refused = second.join()
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "ip_limit"
        assert third.join().status_code == 200
        assert first.join().status_code == 200           # same cookie: idempotent, not a second entry
        # IPv6 clients are grouped by /64.
        a, b = fd.visitor("2001:db8:1:2::1"), fd.visitor("2001:db8:1:2:ffff::9")
        assert a.join().status_code == 200 and b.join().status_code == 409

    with front_door(tmp_path / "direct", peer="203.0.113.9") as fd:   # not behind the tunnel
        one, two = fd.visitor("198.51.100.1"), fd.visitor("198.51.100.2")
        assert one.join().status_code == 200
        # The spoofable header is ignored from a non-loopback peer: both are 203.0.113.9.
        assert two.join().status_code == 409


def test_join_rate_limit_and_queue_cap_through_the_api(tmp_path):
    with front_door(tmp_path, join_limit=2) as fd:
        visitor = fd.visitor("198.51.100.1")
        for _ in range(2):
            assert visitor.join().status_code == 200
            assert visitor.end().status_code == 200
            visitor.cookie = None
        limited = visitor.join()
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1

    with front_door(tmp_path / "q", capacity=LOW, max_queue=1) as fd:
        assert fd.visitor("198.51.100.1").join().status_code == 200
        full = fd.visitor("198.51.100.2").join()
        assert full.status_code == 503 and full.json()["error"]["code"] == "queue_full"


def test_end_destroys_the_session_immediately(tmp_path):
    with front_door(tmp_path) as fd:
        visitor = fd.visitor()
        visitor.join()
        visitor.wait_for("active")
        sid = next(iter(fd.manager.entries))
        ended = visitor.end()
        assert ended.status_code == 200 and ended.json()["state"] == "expired" and ended.json()["note"] == "ended"
        assert fd.runtime.removed == [NAME_PREFIX + sid] and not fd.runtime.containers
        assert not (fd.cfg.session_root / sid).exists() and fd.broker.closed == [sid]
        assert visitor.get("/workspace/list").status_code == 401
        assert visitor.get("/demo/status").json()["state"] == "expired"
        # A visitor with no session can call end harmlessly.
        assert fd.visitor("198.51.100.9").end().json()["state"] == "none"


def test_tampered_or_foreign_cookies_are_treated_as_no_session(tmp_path):
    with front_door(tmp_path) as fd:
        owner = fd.visitor("198.51.100.1")
        owner.join()
        owner.wait_for("active")
        genuine = owner.cookie
        sid, mac = genuine.split(".")
        forged = [sid + "." + mac[:-1] + ("A" if mac[-1] != "A" else "B"),    # bad MAC
                  "0" * 24 + "." + mac,                                          # other sid, borrowed MAC
                  sid,                                                           # unsigned
                  sid + "." + "A" * 43]
        for value in forged:
            thief = fd.visitor("198.51.100.99", cookie=value)
            assert thief.get("/demo/status").json()["state"] == "none"
            assert thief.get("/workspace/list").status_code == 401
            assert thief.get("/app/").status_code == 307 and thief.get("/app/").headers["location"] == "/"
            assert thief.get("/").status_code == 200            # waiting page, not the app
            assert thief.end().json()["state"] == "none"
        assert owner.state() == "active"                        # forged calls never touched the real session


def test_health_endpoint_reports_counts_only(tmp_path):
    with front_door(tmp_path, capacity=LOW) as fd:
        fd.visitor().join()
        health = fd.client.get("/demo/health").json()
        assert health == {"ok": True, "active": 0, "queued": 1, "max": 3}


def test_idle_expiry_through_the_running_loop(tmp_path):
    from .support import FakeClock
    clock = FakeClock()
    with front_door(tmp_path, clock=clock) as fd:
        visitor = fd.visitor()
        visitor.join()
        visitor.wait_for("active")
        clock.advance(901)
        status = visitor.wait_for("expired")
        assert status["note"] == "idle"
        assert visitor.get("/workspace/list").status_code == 401
        assert visitor.get("/").status_code == 200                      # may rejoin from the waiting page
        assert visitor.join().json()["state"] in ("queued", "starting", "active")


def test_factory_reads_the_environment_and_scrubs_secrets_from_it(tmp_path, monkeypatch):
    import os

    from demo.app import create_app
    from demo.broker import ProviderBroker
    from demo.podman import PodmanRuntime

    monkeypatch.setenv("CHUDWARE_DEMO_OPENROUTER_KEY", "sk-or-from-env")
    monkeypatch.setenv("CHUDWARE_DEMO_COOKIE_KEY", "c" * 40)
    monkeypatch.setenv("CHUDWARE_DEMO_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHUDWARE_DEMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHUDWARE_DEMO_MAX_ACTIVE", "2")
    app = create_app()
    demo = app.state.demo
    assert demo.cfg.ai_enabled and demo.cfg.cookie_key_persistent and demo.cfg.max_active == 2
    assert isinstance(demo.broker, ProviderBroker) and isinstance(demo.runtime, PodmanRuntime)
    # Neither secret remains in the environment that podman/conmon would inherit.
    assert "CHUDWARE_DEMO_OPENROUTER_KEY" not in os.environ and "CHUDWARE_DEMO_COOKIE_KEY" not in os.environ
    assert (tmp_path / "state").is_dir() and ((tmp_path / "state").stat().st_mode & 0o777) == 0o700
    assert "sk-or-from-env" not in repr(demo.cfg)
