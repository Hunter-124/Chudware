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
    assert ids == {"codex", "claude", "cursor", "xai"}


def test_dispatcher_status_all_disconnected(tmp_config):
    st = sl.status()
    assert set(st) == {"codex", "claude", "cursor", "xai"}
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

@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"])
def test_subscription_providers_refused_offline(name):
    from agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider(name, {"offline_mode": True})


@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"])
def test_subscription_providers_never_offline_safe(name):
    from agent import config

    class _Stub:
        pass

    stub = _Stub()
    stub.name = name
    assert config.provider_offline_safe(stub) is False


# ── xAI registration ────────────────────────────────────────────────────────

def test_xai_credential_roundtrip_and_status(tmp_config):
    """Token cache + status shape for the xai friendly id (no network)."""
    assert sub_auth.load_credential("xai-sub") is None
    cred = {
        "access_token": "xai-access",
        "refresh_token": "xai-refresh",
        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        "account_id": "acct-xai",
        "email": "user@x.ai",
    }
    sub_auth.save_credential("xai-sub", cred)
    assert sub_auth.load_credential("xai-sub") == cred

    st = sl.status()
    assert st["xai"]["connected"] is True
    assert st["xai"]["email"] == "user@x.ai"
    assert st["xai"]["account_id"] == "acct-xai"
    assert st["xai"]["label"]
    assert st["xai"]["expires_at_ms"] == cred["expires_at_ms"]

    assert sl.logout("xai") == {"ok": True}
    assert sub_auth.load_credential("xai-sub") is None
    assert sl.status()["xai"]["connected"] is False


def test_xai_sub_provider_loads_cached_token(tmp_config):
    from agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID

    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
        "email": "a@b.com",
    })
    p = XAISubProvider()
    assert p.name == "xai-sub"
    assert p.supports_vision is True
    assert p._cred["access_token"] == "tok"


def test_xai_api_key_provider_resolves(tmp_config):
    from agent import config
    from agent.providers.xai_provider import XAIProvider, BASE_URL

    p = config.get_provider("xai", {"api_key": "xai-test-key", "model": "grok-4"})
    assert isinstance(p, XAIProvider)
    assert p.name == "xai"
    assert p.model == "grok-4"
    assert p.base_url == BASE_URL


def test_xai_offline_refuses_api_key_path():
    from agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider("xai", {"offline_mode": True, "api_key": "k"})


def test_xai_tier_denied_error_is_contract_shaped():
    from agent.providers.xai_sub_provider import (
        XaiTierDeniedError, TIER_DENIED_REASON, TIER_DENIED_HINT,
    )
    err = XaiTierDeniedError("permission denied")
    assert err.reason == TIER_DENIED_REASON
    assert "console.x.ai" in err.hint or "console.x.ai" in TIER_DENIED_HINT
    assert "Heavy" in str(err)
    assert "traceback" not in str(err).lower()


def test_xai_login_403_is_contract_shaped(tmp_config, monkeypatch):
    """Device-code HTTP 403 surfaces the Heavy-gating reason, never a traceback."""
    from agent.providers import xai_sub_provider as xsp

    def boom(self, open_browser=True, progress=None):
        raise xsp.XaiTierDeniedError("caller does not have permission")

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", boom)
    monkeypatch.setattr(xsp.XAISubProvider, "login", boom)
    out = sl.login("xai")
    assert out["ok"] is False
    assert "Heavy-only" in out["reason"] or "not API-enabled" in out["reason"]
    assert "console.x.ai" in (out.get("hint") or "")


def test_xai_store_tokens_roundtrip(tmp_config, monkeypatch):
    from agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID

    p = XAISubProvider()
    # Fake JWT with email claim.
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": "Grok@X.AI", "sub": "user-1"}).encode()
    ).decode().rstrip("=")
    token = f"hdr.{payload}.sig"
    cred = p._store_tokens({
        "access_token": token,
        "refresh_token": "rt",
        "expires_in": 3600,
    }, token_endpoint="https://auth.x.ai/oauth2/token")
    assert cred["email"] == "grok@x.ai"
    assert cred["account_id"] == "user-1"
    assert sub_auth.load_credential(PROVIDER_ID)["refresh_token"] == "rt"


def test_xai_login_device_true_still_opens_browser(tmp_config, monkeypatch):
    """GUI passes device=True; that must NOT suppress the browser open (regression)."""
    from agent.providers import xai_sub_provider as xsp

    seen = {}

    def fake_login_device(self, open_browser=True, progress=None):
        seen["open_browser"] = open_browser
        if progress:
            progress({
                "stage": "awaiting_approval",
                "user_code": "ABCD-1234",
                "verification_url": "https://accounts.x.ai/activate?user_code=ABCD-1234",
                "browser_opened": open_browser,
            })
        return {
            "access_token": "t",
            "refresh_token": "r",
            "email": "u@x.ai",
            "account_id": "a1",
        }

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", fake_login_device)
    out = sl.login("xai", device=True)
    assert out["ok"] is True
    assert seen["open_browser"] is True  # the bug was open_browser=not device → False


def test_xai_login_open_browser_false_for_headless(tmp_config, monkeypatch):
    from agent.providers import xai_sub_provider as xsp

    seen = {}

    def fake_login_device(self, open_browser=True, progress=None):
        seen["open_browser"] = open_browser
        return {"access_token": "t", "refresh_token": "r", "email": None, "account_id": None}

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", fake_login_device)
    out = sl.login("xai", device=True, open_browser=False)
    assert out["ok"] is True
    assert seen["open_browser"] is False


def test_xai_login_progress_callback_surfaces_code(tmp_config, monkeypatch):
    from agent.providers import xai_sub_provider as xsp

    events = []

    def fake_login_device(self, open_browser=True, progress=None):
        if progress:
            progress({
                "stage": "awaiting_approval",
                "user_code": "WXYZ-9999",
                "verification_url": "https://accounts.x.ai/activate",
                "browser_opened": False,
            })
        return {"access_token": "t", "refresh_token": "r", "email": "e@x.ai", "account_id": "id"}

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", fake_login_device)
    out = sl.login("xai", progress=lambda p: events.append(p))
    assert out["ok"] is True
    assert any(e.get("user_code") == "WXYZ-9999" for e in events)


def test_open_verification_url_fallback_does_not_raise(monkeypatch):
    from agent.providers import xai_sub_provider as xsp

    monkeypatch.setattr(xsp.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no display")))
    monkeypatch.setattr(xsp.shutil, "which", lambda name: None)
    assert xsp.open_verification_url("https://accounts.x.ai/activate") is False


def test_xai_default_model_is_grok_45():
    from agent.providers.xai_sub_provider import DEFAULT_MODEL as sub_default
    from agent.providers.xai_provider import DEFAULT_MODEL as key_default
    assert sub_default == "grok-4.5"
    assert key_default == "grok-4.5"


def test_parse_xai_tier_and_provider_resolution(tmp_config, monkeypatch):
    """xai:<model> gym tiers prefer subscription when connected, else API key."""
    from services.agent.tiers import parse_xai_tier, effort_tier_for, provider_for_tier
    from services.agent import config as agent_config

    assert parse_xai_tier("xai:grok-4.5") == "grok-4.5"
    assert parse_xai_tier("xai:grok-4-5") == "grok-4-5"
    assert parse_xai_tier("xai:") is None
    assert parse_xai_tier("openrouter:x") is None
    assert effort_tier_for("xai:grok-4.5") == "frontier"

    monkeypatch.delenv("AI_CAD_PROVIDER", raising=False)

    # No subscription → API-key path.
    calls = []

    def tracked(name=None, cfg=None):
        calls.append((name, (cfg or {}).get("model")))
        return type("P", (), {"name": name, "model": (cfg or {}).get("model")})()

    monkeypatch.setattr(agent_config, "get_provider", tracked)
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    prov = provider_for_tier("xai:grok-4-5", cfg={})
    assert calls[-1][0] == "xai"
    assert calls[-1][1] == "grok-4.5"  # hyphen normalised
    assert prov.name == "xai"

    # With subscription credential → xai-sub path.
    calls.clear()
    sub_auth.save_credential("xai-sub", {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
    })
    prov = provider_for_tier("xai:grok-4.5", cfg={})
    assert calls[-1][0] == "xai-sub"
    assert calls[-1][1] == "grok-4.5"
    assert prov.name == "xai-sub"
