"""OpenRouter PKCE login — end-to-end behind the feature flag, no network.

Contract: contracts/providers.md §4.1. Drives the full flow with a fake transport so CI
needs no network and no secrets. Lives under agent/ (gate runs `pytest engine/doc agent`).
"""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from agent.providers import oauth_pkce as pk


def test_flag_default_off():
    assert pk.pkce_enabled({}) is False
    out = pk.begin_login("http://127.0.0.1:8765/cb", cfg={})
    assert out["ok"] is False and "disabled" in out["reason"]


def test_flag_env_on(monkeypatch):
    monkeypatch.setenv(pk.FLAG_ENV, "1")
    assert pk.pkce_enabled({}) is True


def test_flag_cfg_on():
    assert pk.pkce_enabled({pk.FLAG_CFG: True}) is True


def test_challenge_is_s256():
    v = pk.make_verifier()
    assert 43 <= len(v) <= 128
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert pk.challenge_for(v) == expect
    assert "=" not in pk.challenge_for(v)  # url-safe, unpadded


def test_begin_login_builds_auth_url(monkeypatch):
    monkeypatch.setenv(pk.FLAG_ENV, "1")
    cb = "http://127.0.0.1:8765/cb"
    out = pk.begin_login(cb)
    assert out["ok"] is True
    q = parse_qs(urlparse(out["auth_url"]).query)
    assert q["callback_url"] == [cb]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [pk.challenge_for(out["code_verifier"])]
    assert out["auth_url"].startswith(pk.AUTH_URL)


def test_complete_login_end_to_end(monkeypatch):
    monkeypatch.setenv(pk.FLAG_ENV, "1")
    begun = pk.begin_login("http://127.0.0.1:8765/cb")
    seen = {}

    def fake_transport(url, payload, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return {"key": "sk-or-v1-test"}

    out = pk.complete_login("auth-code-xyz", begun["code_verifier"], transport=fake_transport)
    assert out == {"ok": True, "key": "sk-or-v1-test"}
    assert seen["url"] == pk.KEYS_URL
    assert seen["payload"]["code"] == "auth-code-xyz"
    assert seen["payload"]["code_verifier"] == begun["code_verifier"]
    assert seen["payload"]["code_challenge_method"] == "S256"


def test_complete_login_disabled_by_default():
    out = pk.complete_login("code", "verifier", transport=lambda *a: {"key": "x"})
    assert out["ok"] is False and "disabled" in out["reason"]


def test_complete_login_missing_code(monkeypatch):
    monkeypatch.setenv(pk.FLAG_ENV, "1")
    out = pk.complete_login("", "verifier", transport=lambda *a: {"key": "x"})
    assert out["ok"] is False and "code" in out["reason"]


def test_complete_login_no_key_in_response(monkeypatch):
    monkeypatch.setenv(pk.FLAG_ENV, "1")
    out = pk.complete_login("code", "verifier", transport=lambda *a: {"error": "nope"})
    assert out["ok"] is False and "no key" in out["reason"].lower()


def test_openrouter_refused_offline():
    """Offline mode refuses the OpenRouter cloud provider before construction."""
    from agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider("openrouter", {"offline_mode": True})
