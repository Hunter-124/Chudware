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
    entry = on_disk["subscription_auth"]["cursor"]
    assert entry["active"] == sub_auth.DEFAULT_ACCOUNT_ID
    assert entry["accounts"][sub_auth.DEFAULT_ACCOUNT_ID]["access_token"] == "t"


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



# ── Multi-account credential store ──────────────────────────────────────────

def test_legacy_flat_cache_migrates_transparently(tmp_config):
    """Existing single-account cache files still resolve; migrate writes multi shape."""
    # Write a legacy flat credential directly (pre-multi-account format).
    tmp_config.write_text(json.dumps({
        "subscription_auth": {
            "xai-sub": {
                "access_token": "legacy-tok",
                "refresh_token": "legacy-rt",
                "expires_at_ms": 999,
                "email": "old@x.ai",
                "account_id": "acct-legacy",
            }
        }
    }))
    # Transparent read without rewrite.
    cred = sub_auth.load_credential("xai-sub")
    assert cred is not None
    assert cred["access_token"] == "legacy-tok"
    assert cred["email"] == "old@x.ai"
    # Active pointer resolves to the legacy account_id when present.
    assert sub_auth.get_active_account_id("xai-sub") == "acct-legacy"

    # Explicit migrate rewrites on disk.
    assert sub_auth.migrate_legacy_provider("xai-sub") is True
    on_disk = json.loads(tmp_config.read_text())
    entry = on_disk["subscription_auth"]["xai-sub"]
    assert "accounts" in entry and entry["active"] == "acct-legacy"
    assert entry["accounts"]["acct-legacy"]["access_token"] == "legacy-tok"
    # Second migrate is a no-op.
    assert sub_auth.migrate_legacy_provider("xai-sub") is False

    # Legacy without account_id becomes "default".
    tmp_config.write_text(json.dumps({
        "subscription_auth": {
            "cursor": {"access_token": "c-tok", "refresh_token": "c-rt"}
        }
    }))
    assert sub_auth.load_credential("cursor")["access_token"] == "c-tok"
    assert sub_auth.get_active_account_id("cursor") == sub_auth.DEFAULT_ACCOUNT_ID
    sub_auth.migrate_legacy_provider("cursor")
    entry = json.loads(tmp_config.read_text())["subscription_auth"]["cursor"]
    assert entry["active"] == "default"
    assert "default" in entry["accounts"]


def test_two_accounts_same_provider_and_switch(tmp_config):
    """Two accounts under one provider; switch flips active token resolution."""
    sub_auth.save_credential("xai-sub", {
        "access_token": "tok-a", "refresh_token": "rt-a",
        "email": "a@x.ai", "account_id": "acct-a",
        "expires_at_ms": 111,
    }, account_id="acct-a")
    sub_auth.save_credential("xai-sub", {
        "access_token": "tok-b", "refresh_token": "rt-b",
        "email": "b@x.ai", "account_id": "acct-b",
        "expires_at_ms": 222,
    }, account_id="acct-b")

    accts = sub_auth.list_accounts("xai-sub")
    assert {a["account_id"] for a in accts} == {"acct-a", "acct-b"}
    # Last save is active.
    assert sub_auth.get_active_account_id("xai-sub") == "acct-b"
    assert sub_auth.load_credential("xai-sub")["access_token"] == "tok-b"

    assert sub_auth.set_active_account("xai-sub", "acct-a") is True
    assert sub_auth.get_active_account_id("xai-sub") == "acct-a"
    assert sub_auth.load_credential("xai-sub")["access_token"] == "tok-a"
    assert sub_auth.load_credential("xai-sub", account_id="acct-b")["access_token"] == "tok-b"

    # Logout active only — other account remains.
    sub_auth.clear_credential("xai-sub")  # active = acct-a
    assert sub_auth.load_credential("xai-sub")["access_token"] == "tok-b"
    assert sub_auth.get_active_account_id("xai-sub") == "acct-b"

    # Logout remaining → disconnected.
    sub_auth.clear_credential("xai-sub")
    assert sub_auth.load_credential("xai-sub") is None
    assert sub_auth.list_accounts("xai-sub") == []


def test_active_token_resolution_via_sub_providers(tmp_config):
    """*_sub_provider constructors resolve the ACTIVE account through load_credential."""
    from agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID

    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "first", "refresh_token": "r1",
        "email": "one@x.ai", "account_id": "one",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
    }, account_id="one")
    sub_auth.save_credential(PROVIDER_ID, {
        "access_token": "second", "refresh_token": "r2",
        "email": "two@x.ai", "account_id": "two",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
    }, account_id="two")

    p = XAISubProvider()
    assert p._cred["access_token"] == "second"

    assert sl.switch("xai", "one")["ok"] is True
    p2 = XAISubProvider()
    assert p2._cred["access_token"] == "first"

    st = sl.status()["xai"]
    assert st["connected"] is True
    assert st["email"] == "one@x.ai"
    assert st["account_id"] in ("one", "one")  # active pointer
    assert len(st["accounts"]) == 2
    assert sum(1 for a in st["accounts"] if a["active"]) == 1


def test_login_derives_account_id_and_sets_active(tmp_config, monkeypatch):
    """login() derives account_id from cred metadata and marks it active."""
    from agent.providers import xai_sub_provider as xsp

    def fake_login_device(self, open_browser=True, progress=None):
        cred = {
            "access_token": "new-tok",
            "refresh_token": "new-rt",
            "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
            "account_id": "user-xyz",
            "email": "user@x.ai",
        }
        # Mimic provider save (legacy path may write under default first).
        sub_auth.save_credential(xsp.PROVIDER_ID, cred)
        self._cred = cred
        return cred

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", fake_login_device)
    out = sl.login("xai", open_browser=False)
    assert out["ok"] is True
    assert out["account_id"] == "user-xyz"
    assert sub_auth.get_active_account_id("xai-sub") == "user-xyz"
    assert sub_auth.load_credential("xai-sub")["access_token"] == "new-tok"


def test_dispatcher_accounts_and_switch(tmp_config):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t1", "email": "a@c.com", "account_id": "a1",
    }, account_id="a1")
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t2", "email": "b@c.com", "account_id": "a2",
    }, account_id="a2")

    all_accts = sl.accounts()
    claude = [a for a in all_accts if a["provider"] == "claude"]
    assert len(claude) == 2
    assert all(a["connected"] for a in claude)
    assert sum(1 for a in claude if a["active"]) == 1

    filtered = sl.accounts("claude")
    assert len(filtered) == 2
    assert sl.accounts("nope") == []

    bad = sl.switch("claude", "missing")
    assert bad["ok"] is False

    ok = sl.switch("claude", "a1")
    assert ok["ok"] is True and ok["account_id"] == "a1"
    assert sub_auth.load_credential("anthropic-sub")["access_token"] == "t1"

    # logout(None) clears active only.
    assert sl.logout("claude") == {"ok": True}
    assert sub_auth.load_credential("anthropic-sub")["access_token"] == "t2"
    assert sl.logout("claude", account_id="a2") == {"ok": True}
    assert sl.status()["claude"]["connected"] is False


def test_subscription_rpc_multi_account_shapes(tmp_config):
    """RPC shapes for accounts/switch + backward-compat status/logout."""
    from engine_oracle.parts_hub import subscription_rpc as rpc

    emit = lambda *_a, **_k: None  # noqa: E731

    # Empty accounts list.
    out = rpc.SUBSCRIPTION_METHODS["subscription_accounts"]({}, emit)
    assert out["ok"] is True and out["accounts"] == []

    sub_auth.save_credential("xai-sub", {
        "access_token": "tok1", "email": "one@x.ai", "account_id": "one",
        "expires_at_ms": 1,
    }, account_id="one")
    sub_auth.save_credential("xai-sub", {
        "access_token": "tok2", "email": "two@x.ai", "account_id": "two",
        "expires_at_ms": 2,
    }, account_id="two")

    out = rpc.SUBSCRIPTION_METHODS["subscription_accounts"]({"provider": "xai"}, emit)
    assert out["ok"] is True
    assert len(out["accounts"]) == 2
    for a in out["accounts"]:
        assert set(a) >= {"provider", "account_id", "label", "email", "active", "connected"}
        assert a["provider"] == "xai"
        assert "access_token" not in a

    # status still works (GUI in the wild).
    st = rpc.SUBSCRIPTION_METHODS["subscription_status"]({}, emit)
    assert st["ok"] is True
    assert st["providers"]["xai"]["connected"] is True
    assert "accounts" in st["providers"]["xai"]

    sw = rpc.SUBSCRIPTION_METHODS["subscription_switch"](
        {"provider": "xai", "account_id": "one"}, emit
    )
    assert sw["ok"] is True and sw["account_id"] == "one"
    assert sub_auth.load_credential("xai-sub")["access_token"] == "tok1"

    # logout without account_id = active.
    lo = rpc.SUBSCRIPTION_METHODS["subscription_logout"]({"provider": "xai"}, emit)
    assert lo["ok"] is True
    assert sub_auth.get_active_account_id("xai-sub") == "two"

    # logout with explicit account_id.
    lo2 = rpc.SUBSCRIPTION_METHODS["subscription_logout"](
        {"provider": "xai", "account_id": "two"}, emit
    )
    assert lo2["ok"] is True
    assert sub_auth.load_credential("xai-sub") is None

    # Unknown provider / missing account_id contract shapes.
    bad = rpc.SUBSCRIPTION_METHODS["subscription_switch"]({"provider": "xai"}, emit)
    assert bad["ok"] is False and "account_id" in bad["reason"]
    bad2 = rpc.SUBSCRIPTION_METHODS["subscription_accounts"]({"provider": "nope"}, emit)
    assert bad2["ok"] is False

    # Methods registry includes new RPCs.
    assert "subscription_accounts" in rpc.SUBSCRIPTION_METHODS
    assert "subscription_switch" in rpc.SUBSCRIPTION_METHODS


@pytest.mark.parametrize(
    ("friendly", "provider_name", "cred_id"),
    [
        ("claude", "anthropic-sub", "anthropic-sub"),
        ("codex", "codex-sub", "openai-codex"),
        ("cursor", "cursor-sub", "cursor"),
        ("xai", "xai-sub", "xai-sub"),
    ],
)
def test_each_provider_login_persists_and_rpc_manages_only_selected_account(
    tmp_config, monkeypatch, friendly, provider_name, cred_id
):
    """Mock the live browser boundary, then exercise the real store and RPC seam."""
    credentials = [
        {
            "access_token": f"{friendly}-token-one",
            "refresh_token": f"{friendly}-refresh-one",
            "account_id": f"{friendly}-one",
            "email": f"one@{friendly}.example",
            "display_name": f"{friendly.title()} One",
            "plan": "pro",
            "expires_at_ms": int(time.time() * 1000) + 10**9,
        },
        {
            "access_token": f"{friendly}-token-two",
            "refresh_token": f"{friendly}-refresh-two",
            "account_id": f"{friendly}-two",
            "email": f"two@{friendly}.example",
            "display_name": f"{friendly.title()} Two",
            "plan": "max",
            "expires_at_ms": int(time.time() * 1000) + 10**9,
        },
    ]

    class FakeProvider:
        def login(self):
            return credentials.pop(0)

        def login_device(self, open_browser=True, progress=None):
            if progress:
                progress({"stage": "approved", "browser_opened": open_browser})
            return credentials.pop(0)

    monkeypatch.setattr(sl, "_instantiate", lambda name: FakeProvider())
    assert next(p for p in sl.providers() if p["id"] == friendly)["name"] == provider_name

    from engine_oracle.parts_hub import subscription_rpc as rpc

    emit = lambda *_a, **_k: None  # noqa: E731
    with rpc._login_lock:
        rpc._login_state.pop(friendly, None)
    for _ in range(2):
        started = rpc.SUBSCRIPTION_METHODS["subscription_login"](
            {"provider": friendly, "open_browser": False}, emit
        )
        assert started["ok"] is True
        for _attempt in range(200):
            status = rpc.SUBSCRIPTION_METHODS["subscription_status"]({}, emit)
            if not status["providers"][friendly]["login_in_progress"]:
                break
            time.sleep(0.005)
        assert status["providers"][friendly]["login_in_progress"] is False
        assert status["providers"][friendly]["last_error"] is None

    on_disk = json.loads(tmp_config.read_text())["subscription_auth"][cred_id]
    assert on_disk["active"] == f"{friendly}-two"
    assert set(on_disk["accounts"]) == {f"{friendly}-one", f"{friendly}-two"}

    status = rpc.SUBSCRIPTION_METHODS["subscription_status"]({}, emit)
    active = status["providers"][friendly]
    assert active["connected"] is True
    assert active["email"] == f"two@{friendly}.example"
    assert active["display_name"] == f"{friendly.title()} Two"
    assert active["plan"] == "max"

    listed = rpc.SUBSCRIPTION_METHODS["subscription_accounts"](
        {"provider": friendly}, emit
    )["accounts"]
    assert len(listed) == 2
    assert {a["display_name"] for a in listed} == {
        f"{friendly.title()} One", f"{friendly.title()} Two"
    }
    assert {a["plan"] for a in listed} == {"pro", "max"}

    switched = rpc.SUBSCRIPTION_METHODS["subscription_switch"](
        {"provider": friendly, "account_id": f"{friendly}-one"}, emit
    )
    assert switched["ok"] is True
    assert sub_auth.load_credential(cred_id)["access_token"] == f"{friendly}-token-one"

    signed_out = rpc.SUBSCRIPTION_METHODS["subscription_logout"](
        {"provider": friendly, "account_id": f"{friendly}-two"}, emit
    )
    assert signed_out["ok"] is True
    assert sub_auth.load_credential(cred_id, f"{friendly}-two") is None
    assert sub_auth.load_credential(cred_id)["access_token"] == f"{friendly}-token-one"


def test_subscription_login_thread_failure_does_not_leave_status_stuck(
    tmp_config, monkeypatch
):
    from engine_oracle.parts_hub import subscription_rpc as rpc

    with rpc._login_lock:
        rpc._login_state.clear()

    def explode(*_args, **_kwargs):
        raise RuntimeError("mock browser callback crashed")

    monkeypatch.setattr(sl, "login", explode)
    emit = lambda *_a, **_k: None  # noqa: E731
    started = rpc.SUBSCRIPTION_METHODS["subscription_login"](
        {"provider": "claude"}, emit
    )
    assert started["ok"] is True

    for _ in range(100):
        status = rpc.SUBSCRIPTION_METHODS["subscription_status"]({}, emit)
        claude = status["providers"]["claude"]
        if not claude["login_in_progress"]:
            break
        time.sleep(0.01)
    assert claude["login_in_progress"] is False
    assert "mock browser callback crashed" in claude["last_error"]


def test_cpp_settings_allowlist_merge_preserves_subscription_store(tmp_config):
    """Model saveSettings' merge/erase/update rewrite against a populated fixture."""
    sub_auth.save_credential("xai-sub", {
        "access_token": "x-token",
        "refresh_token": "x-refresh",
        "account_id": "x-account",
        "email": "x@example.com",
    }, account_id="x-account")
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "a-token",
        "refresh_token": "a-refresh",
        "account_id": "a-account",
        "email": "a@example.com",
    }, account_id="a-account")

    fixture = json.loads(tmp_config.read_text())
    fixture.update({
        "provider": "openai",
        "model": "old-model",
        "api_key": "old-key",
        "appearance": {"glass": 0.5},
        "plm": {"server": "keep.example"},
    })
    tmp_config.write_text(json.dumps(fixture))

    # Mirrors the fixed C++ pattern: load the whole object, erase only GUI-owned
    # conditionals, then write updated GUI keys back into the merged object.
    gui_owned = {
        "provider", "model", "api_key", "openai", "llamacpp", "keys",
        "reasoning_effort", "max_iters", "max_output_tokens", "context_window",
        "system_prompt", "appearance", "workspace_dir", "hidden_models",
        "disabled_providers", "experimental",
    }
    rewritten = json.loads(tmp_config.read_text())
    for key in gui_owned:
        rewritten.pop(key, None)
    rewritten.update({
        "provider": "xai-sub",
        "model": "grok-4.5",
        "appearance": {"glass": 0.8},
    })
    tmp_config.write_text(json.dumps(rewritten))

    assert sub_auth.load_credential("xai-sub", "x-account")["access_token"] == "x-token"
    assert sub_auth.load_credential("anthropic-sub", "a-account")["access_token"] == "a-token"
    round_trip = json.loads(tmp_config.read_text())
    assert round_trip["subscription_auth"] == fixture["subscription_auth"]
    assert round_trip["plm"] == {"server": "keep.example"}
