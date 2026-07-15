"""Tests for engine_oracle/quote_providers.py — JLCPCB signed-request auth (T-BL24) + degrade.

No network: the single HTTP seam (``_http_post_json``) is monkeypatched, so CI stays offline.
The signing tests pin a known-input → known-signature FIXTURE so any change to the string-to-sign
composition (field order / separators / encoding) is caught deterministically.
"""
from __future__ import annotations

import re

import pytest

from . import quote_providers as qp

# HMAC-SHA256(base64) over "POST\n/v1/quote\n1700000000000\ntestnonce123\n{\"q\": 5}" keyed with
# b"test-secret-key" — recomputed independently; pins the exact string-to-sign.
_FIXTURE_SIG = "bJKUIiHgLxFFHT1kr/XAGCBHbA/RtWNNl79y7JG5UrM="


# --- deterministic signing fixture (T-BL24) ---------------------------------------------------

def test_jlcpcb_sign_pins_known_signature():
    sig = qp._jlcpcb_sign("POST", "/v1/quote", "1700000000000", "testnonce123",
                          '{"q": 5}', "test-secret-key")
    assert sig == _FIXTURE_SIG


def test_jlcpcb_sign_uppercases_method_and_reacts_to_every_field():
    base = qp._jlcpcb_sign("POST", "/p", "1", "n", "b", "k")
    assert qp._jlcpcb_sign("post", "/p", "1", "n", "b", "k") == base  # method upper-cased
    assert qp._jlcpcb_sign("POST", "/p", "2", "n", "b", "k") != base  # timestamp matters
    assert qp._jlcpcb_sign("POST", "/p", "1", "m", "b", "k") != base  # nonce matters
    assert qp._jlcpcb_sign("POST", "/p", "1", "n", "c", "k") != base  # body matters
    assert qp._jlcpcb_sign("POST", "/p", "1", "n", "b", "j") != base  # secret matters


def test_jlcpcb_auth_header_format_and_signature():
    creds = {"appid": "APP1", "accesskey": "AK1", "secretkey": "test-secret-key"}
    hdr = qp._jlcpcb_auth_header(creds, "POST", "/v1/quote", '{"q": 5}',
                                 timestamp="1700000000000", nonce="testnonce123")
    assert hdr == (
        'JOP appid="APP1", accesskey="AK1", nonce="testnonce123", '
        f'timestamp="1700000000000", signature="{_FIXTURE_SIG}"')


# --- credential resolution (config → env fallback), mirrors parts_providers._resolve_creds -----

def test_resolve_jlcpcb_creds_from_env(monkeypatch):
    monkeypatch.setattr(qp, "_provider_config", lambda key: {})
    monkeypatch.setenv("JLCPCB_APP_ID", "app-e")
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", "ak-e")
    monkeypatch.setenv("JLCPCB_SECRET_KEY", "sk-e")
    assert qp._resolve_jlcpcb_creds() == {"appid": "app-e", "accesskey": "ak-e", "secretkey": "sk-e"}


def test_resolve_jlcpcb_creds_config_wins_over_env(monkeypatch):
    monkeypatch.setattr(qp, "_provider_config",
                        lambda key: {"appid": "app-c", "accesskey": "ak-c", "secretkey": "sk-c"})
    monkeypatch.setenv("JLCPCB_APP_ID", "app-e")
    assert qp._resolve_jlcpcb_creds()["appid"] == "app-c"


def test_resolve_jlcpcb_creds_absent_is_empty(monkeypatch):
    monkeypatch.setattr(qp, "_provider_config", lambda key: {})
    for v in ("JLCPCB_APP_ID", "JLCPCB_ACCESS_KEY", "JLCPCB_SECRET_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert qp._resolve_jlcpcb_creds() == {"appid": "", "accesskey": "", "secretkey": ""}


# --- JLCPCBProvider.quote(): degrade closed / signed request ----------------------------------

def test_quote_degrades_without_creds_and_never_calls_network(monkeypatch):
    monkeypatch.setattr(qp, "_resolve_jlcpcb_creds",
                        lambda: {"appid": "", "accesskey": "", "secretkey": ""})
    calls = []
    monkeypatch.setattr(qp, "_http_post_json", lambda *a, **k: calls.append(1))
    out = qp.JLCPCBProvider().quote("board.zip")
    assert out["ok"] is False and out["provider"] == "jlcpcb"
    assert "signing" in out["reason"] or "HMAC" in out["reason"]
    assert calls == []  # fail-closed: no network without credentials


def test_quote_sends_valid_signed_jop_header(monkeypatch):
    monkeypatch.setattr(qp, "_resolve_jlcpcb_creds",
                        lambda: {"appid": "APP1", "accesskey": "AK1", "secretkey": "test-secret-key"})
    captured = {}

    def fake_post(url, payload, *, headers=None, timeout=qp.HTTP_TIMEOUT):
        captured["headers"] = headers
        captured["payload"] = payload
        return {"unit_price": 2.5, "currency": "USD"}

    monkeypatch.setattr(qp, "_http_post_json", fake_post)
    out = qp.JLCPCBProvider().quote("board.zip", layers=4, quantity=10)
    assert out["ok"] is True and out["unit_price"] == 2.5
    auth = captured["headers"]["Authorization"]
    assert auth.startswith('JOP appid="APP1", accesskey="AK1", nonce="')
    # The signature in the header must verify against a re-sign over the SAME body the post sent.
    import json as _json
    body = _json.dumps(captured["payload"])
    nonce = re.search(r'nonce="([^"]+)"', auth).group(1)
    ts = re.search(r'timestamp="([^"]+)"', auth).group(1)
    expected = qp._jlcpcb_sign("POST", "/v1/quote", ts, nonce, body, "test-secret-key")
    assert f'signature="{expected}"' in auth
