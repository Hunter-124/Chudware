"""Subscription-auth providers (§4.4) — unit-level, no network, no secrets.

Contract: contracts/providers.md §4.4. Exercises the shared plumbing (PKCE, callback
server, credential store, JWT decode, CCH), the login dispatcher, and offline-mode
refusal. Network calls are monkeypatched; nothing leaves the machine.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from agent.providers import sub_auth
from agent.providers import subscription_login as sl


# ── config.json isolation ───────────────────────────────────────────────────

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point the credential store at a throwaway config.json."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(sub_auth.paths, "CONFIG", cfg)
    return cfg


# ── PKCE ────────────────────────────────────────────────────────────────────

def test_verifier_is_96_bytes_b64url():
    v = sub_auth.make_verifier()
    # 96 random bytes -> 128 base64 chars, unpadded, url-safe.
    assert len(v) == 128
    assert "=" not in v and "+" not in v and "/" not in v


def test_challenge_is_s256():
    v = sub_auth.make_verifier()
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert sub_auth.challenge_for(v) == expect


# ── Callback server ─────────────────────────────────────────────────────────

def test_callback_server_captures_code():
    server = sub_auth.CallbackServer(port=54999, path="/callback", timeout=10.0)
    result_holder = {}

    def run():
        result_holder["r"] = server.wait_for_callback(expected_state="xyz")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)  # let the listener bind
    with urlopen("http://127.0.0.1:54999/callback?code=abc123&state=xyz") as resp:
        assert resp.status == 200
    t.join(timeout=10)
    assert result_holder["r"]["code"] == "abc123"


def test_callback_server_state_mismatch_raises():
    server = sub_auth.CallbackServer(port=54998, path="/callback", timeout=10.0)
    err_holder = {}

    def run():
        try:
            server.wait_for_callback(expected_state="expected")
        except Exception as e:  # noqa: BLE001
            err_holder["e"] = e

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    with urlopen("http://127.0.0.1:54998/callback?code=abc&state=WRONG") as resp:
        resp.read()
    t.join(timeout=10)
    assert "state mismatch" in str(err_holder["e"]).lower()


# ── Credential store ────────────────────────────────────────────────────────

def test_credential_roundtrip(tmp_config):
    assert sub_auth.load_credential("codex") is None
    cred = {"access_token": "a", "refresh_token": "r", "expires_at_ms": 123,
            "account_id": "acc", "email": "u@x.com"}
    sub_auth.save_credential("codex", cred)
    assert sub_auth.load_credential("codex") == cred
    sub_auth.clear_credential("codex")
    assert sub_auth.load_credential("codex") is None


def test_save_credential_preserves_other_keys(tmp_config):
    tmp_config.write_text(json.dumps({"provider": "anthropic", "api_key": "keep-me"}))
    sub_auth.save_credential("cursor", {"access_token": "t"})
    on_disk = json.loads(tmp_config.read_text())
    # Non-auth keys survive the merge-write.
    assert on_disk["provider"] == "anthropic"
    assert on_disk["api_key"] == "keep-me"
    assert on_disk["subscription_auth"]["cursor"]["access_token"] == "t"


def test_is_expired_with_skew():
    now = time.time() * 1000
    assert sub_auth.is_expired({"expires_at_ms": now - 1000}) is True
    assert sub_auth.is_expired({"expires_at_ms": now + 10_000}) is False
    # 5-min skew makes a token that expires in 1 min count as expired.
    assert sub_auth.is_expired({"expires_at_ms": now + 60_000}, skew_ms=5 * 60 * 1000) is True


# ── JWT decode ──────────────────────────────────────────────────────────────

def test_decode_jwt_payload():
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"},
               "https://api.openai.com/profile": {"email": "U@X.com"}}
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token = f"header.{seg}.sig"
    out = sub_auth.decode_jwt_payload(token)
    assert out["https://api.openai.com/auth"]["chatgpt_account_id"] == "acc-1"


def test_decode_jwt_garbage_returns_empty():
    assert sub_auth.decode_jwt_payload("not-a-jwt") == {}


# ── Anthropic CCH + billing suffix ──────────────────────────────────────────

def test_anthropic_cch_is_deterministic_5hex(tmp_config):
    from agent.providers.anthropic_sub_provider import AnthropicSubProvider
    p = AnthropicSubProvider()
    h1 = p._compute_cch(b"some-body-bytes")
    h2 = p._compute_cch(b"some-body-bytes")
    assert h1 == h2
    assert len(h1) == 5 and all(c in "0123456789abcdef" for c in h1)
    assert p._compute_cch(b"different") != h1


def test_anthropic_billing_suffix_3hex(tmp_config):
    from agent.providers.anthropic_sub_provider import AnthropicSubProvider
    p = AnthropicSubProvider()
    s = p._billing_suffix("hello world this is a long enough message")
    assert len(s) == 3 and all(c in "0123456789abcdef" for c in s)
    # Short message (out-of-range indices default to '0') still yields 3 hex chars.
    assert len(p._billing_suffix("hi")) == 3


# ── Anthropic request body shape (Opus-4.8 safe, CCH patched) ───────────────

def test_anthropic_body_is_opus48_safe_and_cch_patched(tmp_config, monkeypatch):
    from agent.providers.anthropic_sub_provider import AnthropicSubProvider
    from agent.providers import base

    p = AnthropicSubProvider()
    p._cred = {"access_token": "t", "refresh_token": "r",
               "expires_at_ms": time.time() * 1000 + 10 ** 9}

    captured = {}

    def fake_post_once(body_str, cch):
        captured["body"] = body_str
        captured["cch"] = cch
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(p, "_post_once", fake_post_once)
    text, _ = p.complete_ex("sys", [base.user([base.text_part("hello")])])
    assert text == "ok"
    body = captured["body"]
    # Adaptive thinking, NOT budget_tokens (would 400 on Opus 4.8).
    assert '"thinking":{"type":"adaptive"}' in body
    assert "budget_tokens" not in body
    # Compact JSON (no delimiter whitespace) to match the reference client byte-for-byte:
    # re-serializing the parsed body with compact separators reproduces it exactly.
    assert json.dumps(json.loads(body), separators=(",", ":")) == body
    # The placeholder was replaced with the real 5-hex CCH.
    assert "cch=00000;" not in body
    assert f"cch={captured['cch']};" in body
    assert len(captured["cch"]) == 5


# ── Codex is honest about vision + drops images ─────────────────────────────

def test_codex_reports_no_vision(tmp_config):
    from agent.providers.codex_sub_provider import CodexSubProvider
    assert CodexSubProvider().supports_vision is False


def test_codex_build_input_drops_images(tmp_config):
    from agent.providers.codex_sub_provider import CodexSubProvider
    from agent.providers import base

    p = CodexSubProvider()
    msgs = [base.user([base.text_part("look"), base.image_part("/tmp/x.png")])]
    items = p._build_input("sys", msgs)
    assert items[0]["role"] == "developer" and items[0]["content"] == "sys"
    # Image part is dropped; only the text survives (no crash reading a missing file).
    assert items[1]["role"] == "user" and "look" in items[1]["content"]


# ── Login dispatcher ────────────────────────────────────────────────────────

def test_dispatcher_lists_providers():
    ids = {p["id"] for p in sl.providers()}
    assert ids == {"codex", "claude", "cursor"}


def test_dispatcher_status_all_disconnected(tmp_config):
    st = sl.status()
    assert set(st) == {"codex", "claude", "cursor"}
    assert all(not v["connected"] for v in st.values())


def test_dispatcher_status_reflects_stored_credential(tmp_config):
    sub_auth.save_credential("anthropic-sub", {"access_token": "t", "email": "me@x.com",
                                               "expires_at_ms": 999})
    st = sl.status()
    assert st["claude"]["connected"] is True
    assert st["claude"]["email"] == "me@x.com"


def test_dispatcher_unknown_provider():
    out = sl.login("gemini")
    assert out["ok"] is False and "unknown" in out["reason"]


def test_dispatcher_logout(tmp_config):
    sub_auth.save_credential("openai-codex", {"access_token": "t"})
    assert sl.logout("codex") == {"ok": True}
    assert sub_auth.load_credential("openai-codex") is None


def test_dispatcher_login_failure_is_contract_shaped(tmp_config, monkeypatch):
    # Force the provider's login() to blow up; the dispatcher must catch and shape it.
    from agent.providers import cursor_sub_provider as csp

    def boom(self):
        raise RuntimeError("no browser")

    monkeypatch.setattr(csp.CursorSubProvider, "login", boom)
    out = sl.login("cursor")
    assert out["ok"] is False
    assert "login failed" in out["reason"]


# ── Offline mode refuses subscription providers ─────────────────────────────

@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub"])
def test_subscription_providers_refused_offline(name):
    from agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider(name, {"offline_mode": True})


@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub"])
def test_subscription_providers_never_offline_safe(name):
    from agent import config

    class _Stub:
        pass

    stub = _Stub()
    stub.name = name
    assert config.provider_offline_safe(stub) is False
