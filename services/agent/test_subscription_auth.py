"""Subscription-auth providers (§4.4) — unit-level, no network, no secrets.

Contract: contracts/providers.md §4.4. Exercises the shared plumbing (PKCE, callback
server, credential store, JWT decode, CCH), the login dispatcher, and offline-mode
refusal. Network calls are monkeypatched; nothing leaves the machine.
"""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from services.agent.providers import sub_auth
from services.agent.providers import subscription_login as sl


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

def _serve_on_ephemeral_port(server, target):
    """Start ``target`` in a thread and return ``(thread, base_url)`` for ``server``.

    These tests used to hardcode ports 54999/54998 and sleep 0.3 s hoping the
    listener had bound. Both are wrong on the CI fleet: several jobs share one
    runner, so a fixed port collides and the loser dies in ``_make_server`` with
    EADDRINUSE. Pass ``port=0``, let the kernel assign, then read the assigned
    address back off the live listener. ``CallbackServer`` binds (and listens) in
    ``_make_server`` before serving, so once ``_httpd`` is visible a connect is
    already accepted into the backlog -- no sleep needed either.
    """
    t = threading.Thread(target=target)
    t.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        httpd = server._httpd
        if httpd is not None:
            host, port = httpd.server_address[0], httpd.server_address[1]
            return t, "http://{0}:{1}".format(
                "[{0}]".format(host) if ":" in host else host, port
            )
        time.sleep(0.005)
    raise AssertionError("callback server never bound a port")


def test_callback_server_captures_code():
    server = sub_auth.CallbackServer(port=0, path="/callback", timeout=10.0)
    result_holder = {}

    def run():
        result_holder["r"] = server.wait_for_callback(expected_state="xyz")

    t, base = _serve_on_ephemeral_port(server, run)
    with urlopen(base + "/callback?code=abc123&state=xyz") as resp:
        assert resp.status == 200
    t.join(timeout=10)
    assert result_holder["r"]["code"] == "abc123"


def test_callback_server_state_mismatch_raises():
    server = sub_auth.CallbackServer(port=0, path="/callback", timeout=10.0)
    err_holder = {}

    def run():
        try:
            server.wait_for_callback(expected_state="expected")
        except Exception as e:  # noqa: BLE001
            err_holder["e"] = e

    t, base = _serve_on_ephemeral_port(server, run)
    with urlopen(base + "/callback?code=abc&state=WRONG") as resp:
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
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider
    p = AnthropicSubProvider()
    h1 = p._compute_cch(b"some-body-bytes")
    h2 = p._compute_cch(b"some-body-bytes")
    assert h1 == h2
    assert len(h1) == 5 and all(c in "0123456789abcdef" for c in h1)
    assert p._compute_cch(b"different") != h1


def test_anthropic_billing_suffix_3hex(tmp_config):
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider
    p = AnthropicSubProvider()
    s = p._billing_suffix("hello world this is a long enough message")
    assert len(s) == 3 and all(c in "0123456789abcdef" for c in s)
    # Short message (out-of-range indices default to '0') still yields 3 hex chars.
    assert len(p._billing_suffix("hi")) == 3


# ── Anthropic request body shape (Opus-4.8 safe, CCH patched) ───────────────

def test_anthropic_body_is_opus48_safe_and_cch_patched(tmp_config, monkeypatch):
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider
    from services.agent.providers import base

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
    from services.agent.providers.codex_sub_provider import CodexSubProvider
    assert CodexSubProvider().supports_vision is False


def test_codex_build_input_drops_images(tmp_config):
    from services.agent.providers.codex_sub_provider import CodexSubProvider
    from services.agent.providers import base

    p = CodexSubProvider()
    msgs = [base.user([base.text_part("look"), base.image_part("/tmp/x.png")])]
    items = p._build_input("sys", msgs)
    assert items[0]["role"] == "developer" and items[0]["content"] == "sys"
    # Image part is dropped; only the text survives (no crash reading a missing file).
    assert items[1]["role"] == "user" and "look" in items[1]["content"]


# ── Login dispatcher ────────────────────────────────────────────────────────

def test_dispatcher_lists_providers():
    ids = {p["id"] for p in sl.providers()}
    assert ids == {"codex", "claude", "cursor", "xai", "kimi"}


def test_dispatcher_status_all_disconnected(tmp_config):
    st = sl.status()
    assert set(st) == {"codex", "claude", "cursor", "xai", "kimi"}
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
    from services.agent.providers import cursor_sub_provider as csp

    def boom(self):
        raise RuntimeError("no browser")

    monkeypatch.setattr(csp.CursorSubProvider, "login", boom)
    out = sl.login("cursor")
    assert out["ok"] is False
    assert "login failed" in out["reason"]


# ── Offline mode refuses subscription providers ─────────────────────────────

@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"])
def test_subscription_providers_refused_offline(name):
    from services.agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider(name, {"offline_mode": True})


@pytest.mark.parametrize("name", ["anthropic-sub", "codex-sub", "cursor-sub", "xai-sub"])
def test_subscription_providers_never_offline_safe(name):
    from services.agent import config

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
    from services.agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID

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
    from services.agent import config
    from services.agent.providers.xai_provider import XAIProvider, BASE_URL

    p = config.get_provider("xai", {"api_key": "xai-test-key", "model": "grok-4"})
    assert isinstance(p, XAIProvider)
    assert p.name == "xai"
    assert p.model == "grok-4"
    assert p.base_url == BASE_URL


def test_xai_offline_refuses_api_key_path():
    from services.agent import config
    with pytest.raises(config.OfflineModeError):
        config.get_provider("xai", {"offline_mode": True, "api_key": "k"})


def test_xai_tier_denied_error_is_contract_shaped():
    from services.agent.providers.xai_sub_provider import (
        XaiTierDeniedError, TIER_DENIED_REASON, TIER_DENIED_HINT,
    )
    err = XaiTierDeniedError("permission denied")
    assert err.reason == TIER_DENIED_REASON
    assert "console.x.ai" in err.hint or "console.x.ai" in TIER_DENIED_HINT
    assert "Heavy" in str(err)
    assert "traceback" not in str(err).lower()


def test_xai_login_403_is_contract_shaped(tmp_config, monkeypatch):
    """Device-code HTTP 403 surfaces the Heavy-gating reason, never a traceback."""
    from services.agent.providers import xai_sub_provider as xsp

    def boom(self, open_browser=True, progress=None):
        raise xsp.XaiTierDeniedError("caller does not have permission")

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", boom)
    monkeypatch.setattr(xsp.XAISubProvider, "login", boom)
    out = sl.login("xai")
    assert out["ok"] is False
    assert "Heavy-only" in out["reason"] or "not API-enabled" in out["reason"]
    assert "console.x.ai" in (out.get("hint") or "")


def test_xai_store_tokens_roundtrip(tmp_config, monkeypatch):
    from services.agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID
    from services.agent.providers import usage as usage_mod

    # Offline unit test: no live userinfo.
    monkeypatch.setattr(usage_mod, "_http_get", lambda *a, **k: (404, None, {}))

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
    assert cred["display_name"] == "grok@x.ai"
    assert sub_auth.load_credential(PROVIDER_ID)["refresh_token"] == "rt"
    assert sub_auth.load_credential(PROVIDER_ID)["display_name"] == "grok@x.ai"


def test_xai_store_tokens_uuid_only_gets_friendly_name(tmp_config, monkeypatch):
    """JWT with sub UUID and no email still sets a non-UUID display_name."""
    from services.agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID
    from services.agent.providers import usage as usage_mod

    monkeypatch.setattr(usage_mod, "_http_get", lambda *a, **k: (404, None, {}))

    p = XAISubProvider()
    uuid = "06dc3359-aaaa-bbbb-cccc-ddddeeeeffff"
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": uuid, "plan": "SuperGrok"}).encode()
    ).decode().rstrip("=")
    token = f"hdr.{payload}.sig"
    cred = p._store_tokens({
        "access_token": token,
        "refresh_token": "rt",
        "expires_in": 3600,
    }, token_endpoint="https://auth.x.ai/oauth2/token")
    assert cred["account_id"] == uuid
    assert cred["display_name"]
    assert cred["display_name"] != uuid
    assert "Grok" in cred["display_name"] or "SuperGrok" in cred["display_name"]
    stored = sub_auth.load_credential(PROVIDER_ID)
    assert stored["display_name"] == cred["display_name"]


def test_xai_login_device_true_still_opens_browser(tmp_config, monkeypatch):
    """GUI passes device=True; that must NOT suppress the browser open (regression)."""
    from services.agent.providers import xai_sub_provider as xsp

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
    from services.agent.providers import xai_sub_provider as xsp

    seen = {}

    def fake_login_device(self, open_browser=True, progress=None):
        seen["open_browser"] = open_browser
        return {"access_token": "t", "refresh_token": "r", "email": None, "account_id": None}

    monkeypatch.setattr(xsp.XAISubProvider, "login_device", fake_login_device)
    out = sl.login("xai", device=True, open_browser=False)
    assert out["ok"] is True
    assert seen["open_browser"] is False


def test_xai_login_progress_callback_surfaces_code(tmp_config, monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

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
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.setattr(xsp.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no display")))
    monkeypatch.setattr(xsp.shutil, "which", lambda name: None)
    assert xsp.open_verification_url("https://accounts.x.ai/activate") is False


def test_xai_device_url_always_embeds_user_code(tmp_config, monkeypatch):
    """Bare verification_uri without user_code must be completed before browser open.

    Without this, accounts.x.ai/oauth2/device bounces signed-out browsers to the
    generic X "Sign in to X to generate tweets with Grok" page.
    """
    from services.agent.providers import xai_sub_provider as xsp

    opened = {}

    def fake_post_form(url, data, headers=None, timeout=30.0):
        if "device/code" in url:
            return {
                "device_code": "dev-1",
                "user_code": "QWER-5678",
                # Issuer omitted verification_uri_complete — the footgun path.
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "expires_in": 600,
                "interval": 1,
            }
        raise AssertionError(f"unexpected post_form {url}")

    def fake_open(url):
        opened["url"] = url
        return True

    monkeypatch.setattr(xsp.sub_auth, "post_form", fake_post_form)
    monkeypatch.setattr(xsp, "open_verification_url", fake_open)
    # Never reach token poll — we only care about the URL handed to the browser.
    monkeypatch.setattr(
        xsp.XAISubProvider,
        "_poll_device_token",
        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("stop-after-url")),
    )
    monkeypatch.setattr(
        xsp.XAISubProvider,
        "_discover_token_endpoint",
        lambda self: "https://auth.x.ai/oauth2/token",
    )

    prov = xsp.XAISubProvider()
    try:
        prov.login_device(open_browser=True)
    except RuntimeError as exc:
        assert "stop-after-url" in str(exc)
    assert "url" in opened
    assert "user_code=QWER-5678" in opened["url"]
    assert opened["url"].startswith("https://accounts.x.ai/oauth2/device")


def test_xai_default_model_is_grok_45():
    from services.agent.providers.xai_sub_provider import DEFAULT_MODEL as sub_default
    from services.agent.providers.xai_provider import DEFAULT_MODEL as key_default
    assert sub_default == "grok-4.5"
    assert key_default == "grok-4.5"


def test_parse_xai_tier_and_provider_resolution(tmp_config, monkeypatch):
    """xai:<model> gym tiers prefer subscription when connected, else API key."""
    from services.agent.tiers import parse_xai_tier, profile_tier_for, provider_for_tier
    from services.agent import config as agent_config

    assert parse_xai_tier("xai:grok-4.5") == "grok-4.5"
    assert parse_xai_tier("xai:grok-4-5") == "grok-4-5"
    assert parse_xai_tier("xai:") is None
    assert parse_xai_tier("openrouter:x") is None
    assert profile_tier_for("xai:grok-4.5") == "frontier"

    monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)

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
    from services.agent.providers.xai_sub_provider import XAISubProvider, PROVIDER_ID

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
    from services.agent.providers import xai_sub_provider as xsp

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
    from services.agent import worker_rpc as rpc

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


# ── ensure_fresh (pre-run token refresh) ─────────────────────────────────────

def test_ensure_fresh_stale_token_refreshes_and_persists(tmp_config, monkeypatch):
    """Stale access token → real provider.refresh via mocked token endpoint + persist."""
    now = time.time() * 1000
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "old-at",
        "refresh_token": "rt-1",
        "expires_at_ms": now - 60_000,  # already expired
        "email": "me@x.com",
        "account_id": "acct-1",
    })

    def fake_post_json(url, body, headers=None, timeout=30.0):  # noqa: ARG001
        assert body.get("grant_type") == "refresh_token"
        assert body.get("refresh_token") == "rt-1"
        return {
            "access_token": "new-at",
            "refresh_token": "rt-2",
            "expires_in": 3600,
        }

    monkeypatch.setattr(sub_auth, "post_json", fake_post_json)

    out = sl.ensure_fresh("claude")
    assert out["ok"] is True
    assert out["refreshed"] is True
    assert out.get("error") in (None, "")
    stored = sub_auth.load_credential("anthropic-sub")
    assert stored["access_token"] == "new-at"
    assert stored["refresh_token"] == "rt-2"
    assert out["expires_in"] is not None and out["expires_in"] > 0

    # Naming variants all resolve the same way; second call is fresh → no refresh.
    for alias in ("anthropic-sub", "anthropic", "claude"):
        out2 = sl.ensure_fresh(alias)
        assert out2["ok"] is True and out2["refreshed"] is False


def test_ensure_fresh_fresh_token_skips_refresh(tmp_config, monkeypatch):
    """Token valid beyond 5 min skew → no network refresh."""
    from services.agent.providers.xai_sub_provider import XAISubProvider

    now = time.time() * 1000
    sub_auth.save_credential("xai-sub", {
        "access_token": "live-at",
        "refresh_token": "rt",
        "expires_at_ms": now + 60 * 60 * 1000,  # 1h left
        "email": "g@x.ai",
    })

    def boom(self):
        raise AssertionError("refresh must not be called for a fresh token")

    monkeypatch.setattr(XAISubProvider, "refresh", boom)

    out = sl.ensure_fresh("xai-sub")
    assert out == {
        "ok": True,
        "refreshed": False,
        "expires_in": out["expires_in"],  # dynamic
        "error": None,
    } or (out["ok"] is True and out["refreshed"] is False and out["error"] is None)
    assert out["expires_in"] is not None and out["expires_in"] > 50 * 60


def test_ensure_fresh_refresh_failure_clean_error(tmp_config, monkeypatch):
    """Refresh exception → clean error dict, no traceback, nothing half-written."""
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider

    now = time.time() * 1000
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "old-at",
        "refresh_token": "bad-rt",
        "expires_at_ms": now - 1,
    })

    def fail(self):
        raise RuntimeError("OAuth access token has expired (mock)")

    monkeypatch.setattr(AnthropicSubProvider, "refresh", fail)

    out = sl.ensure_fresh("claude")
    assert out["ok"] is False
    assert out["refreshed"] is False
    assert "error" in out and out["error"]
    assert "traceback" not in out["error"].lower()
    assert "re-authenticate" in out["error"].lower() or "refresh failed" in out["error"].lower()
    # Original token still on disk (refresh never persisted).
    assert sub_auth.load_credential("anthropic-sub")["access_token"] == "old-at"



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

    from services.agent import worker_rpc as rpc

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
    from services.agent import worker_rpc as rpc

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


# ── xAI multi-account rotation + usable gate ─────────────────────────────────


def test_xai_provider_usable_with_stored_credentials(tmp_config):
    """Signed-in xAI credentials make the provider usable (cold restore)."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    assert xsp.provider_is_usable() is False
    assert sl.status()["xai"]["connected"] is False
    assert sl.status()["xai"].get("usable") is False

    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok",
        "refresh_token": "rt",
        "account_id": "acct-a",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
    }, account_id="acct-a")

    assert xsp.provider_is_usable() is True
    st = sl.status()["xai"]
    assert st["connected"] is True
    assert st["usable"] is True
    p = xsp.XAISubProvider()
    assert p.model == "grok-4.5"
    assert p._cred["access_token"] == "tok"


def test_xai_pick_skips_blocked_account(tmp_config):
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-a", "refresh_token": "r-a",
        "account_id": "a", "expires_at_ms": now,
    }, account_id="a", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-b", "refresh_token": "r-b",
        "account_id": "b", "expires_at_ms": now,
    }, account_id="b", set_active=False)

    xsp.mark_account_blocked("a", seconds=600, reason="test")
    picked = xsp.pick_usable_account(prefer="a")
    assert picked is not None
    assert picked[0] == "b"
    assert picked[1]["access_token"] == "tok-b"

    # Constructor also prefers the non-blocked account and flips active.
    p = xsp.XAISubProvider()
    assert p._cred["access_token"] == "tok-b"
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "b"


def test_xai_complete_ex_rotates_on_tier_denied(tmp_config, monkeypatch):
    """HTTP 403 on account A falls through to account B before failing."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-a", "refresh_token": "r-a",
        "account_id": "acct-a", "expires_at_ms": now,
    }, account_id="acct-a", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-b", "refresh_token": "r-b",
        "account_id": "acct-b", "expires_at_ms": now,
    }, account_id="acct-b", set_active=False)

    seen_tokens: list[str] = []

    def fake_post(self, body):
        tok = (self._cred or {}).get("access_token")
        seen_tokens.append(tok)
        if tok == "tok-a":
            raise xsp.XaiTierDeniedError('{"code":"personal-team-blocked:spending-limit"}')
        return {
            "choices": [{"message": {"content": "ok-from-b", "reasoning_content": ""}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(xsp.XAISubProvider, "_post_once", fake_post)
    # complete_ex now live-pulls usage before each hop — keep unit tests offline.
    monkeypatch.setattr(
        xsp, "pull_account_usage",
        lambda account_id, cred=None, force=True: dict(cred or {}) | {"account_id": account_id},
    )
    # Avoid real refresh paths if anything looks expired.
    monkeypatch.setattr(xsp.XAISubProvider, "refresh", lambda self: self._cred or {})

    p = xsp.XAISubProvider()
    # Force start on blocked-path account A.
    p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    sub_auth.set_active_account(xsp.PROVIDER_ID, "acct-a")

    content, _thinking = p.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert content == "ok-from-b"
    assert "tok-a" in seen_tokens
    assert "tok-b" in seen_tokens
    assert xsp.is_account_blocked("acct-a") is True
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "acct-b"
    assert p.model == "grok-4.5"


def test_xai_complete_ex_all_accounts_403_clear_error(tmp_config, monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    for aid, tok in (("a", "t1"), ("b", "t2")):
        sub_auth.save_credential(xsp.PROVIDER_ID, {
            "access_token": tok, "refresh_token": f"r-{aid}",
            "account_id": aid, "expires_at_ms": now,
        }, account_id=aid)

    def always_403(self, body):
        raise xsp.XaiTierDeniedError("spending-limit")

    monkeypatch.setattr(xsp.XAISubProvider, "_post_once", always_403)
    monkeypatch.setattr(xsp.XAISubProvider, "refresh", lambda self: self._cred or {})
    monkeypatch.setattr(
        xsp, "pull_account_usage",
        lambda account_id, cred=None, force=True: dict(cred or {}) | {"account_id": account_id},
    )

    p = xsp.XAISubProvider()
    with pytest.raises(xsp.XaiTierDeniedError) as ei:
        p.complete_ex("sys", [{"role": "user", "content": "hi"}])
    msg = str(ei.value)
    assert "all 2 signed-in account" in msg or "signed-in account" in msg
    assert "console.x.ai" in msg or "Hint" in msg
    assert "traceback" not in msg.lower()


def test_xai_model_hyphen_normalized_in_request_body(tmp_config, monkeypatch):
    """grok-4-5 (gym/hyphen form) is rewritten to dotted API id grok-4.5."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok", "refresh_token": "rt",
        "account_id": "a",
        "expires_at_ms": int(time.time() * 1000) + 10 ** 9,
    }, account_id="a")

    captured = {}

    def fake_post(self, body):
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(xsp.XAISubProvider, "_post_once", fake_post)
    monkeypatch.setattr(xsp.XAISubProvider, "refresh", lambda self: self._cred or {})
    monkeypatch.setattr(
        xsp, "pull_account_usage",
        lambda account_id, cred=None, force=True: dict(cred or {}) | {"account_id": account_id},
    )

    p = xsp.XAISubProvider(model="grok-4-5")
    out, _ = p.complete_ex("s", [{"role": "user", "content": "x"}])
    assert out == "ok"
    assert captured["body"]["model"] == "grok-4.5"
    assert p.model == "grok-4.5"


def test_xai_complete_ex_rotates_on_timeout(tmp_config, monkeypatch):
    """Read timeout on account A falls through to account B (pool hang)."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-a", "refresh_token": "r-a",
        "account_id": "acct-a", "expires_at_ms": now,
    }, account_id="acct-a", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-b", "refresh_token": "r-b",
        "account_id": "acct-b", "expires_at_ms": now,
    }, account_id="acct-b", set_active=False)

    seen: list[str] = []

    def fake_post(self, body):
        tok = (self._cred or {}).get("access_token")
        seen.append(tok)
        if tok == "tok-a":
            raise TimeoutError("The read operation timed out")
        return {
            "choices": [{"message": {"content": "ok-b"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(xsp.XAISubProvider, "_post_once", fake_post)
    monkeypatch.setattr(xsp.XAISubProvider, "refresh", lambda self: self._cred or {})
    monkeypatch.setattr(
        xsp, "pull_account_usage",
        lambda account_id, cred=None, force=True: dict(cred or {}) | {"account_id": account_id},
    )
    # No same-account timeout retries in this unit test — rotate immediately.
    monkeypatch.setenv("CHUDWARE_XAI_READ_TIMEOUT_RETRIES", "0")
    xsp._reset_request_semaphore_for_tests()

    p = xsp.XAISubProvider()
    p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    sub_auth.set_active_account(xsp.PROVIDER_ID, "acct-a")

    content, _ = p.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert content == "ok-b"
    assert "tok-a" in seen and "tok-b" in seen
    assert xsp.is_account_blocked("acct-a") is True



def test_account_usage_exhausted_filters_full_credits():
    from services.agent.providers import xai_sub_provider as xsp

    assert xsp.account_usage_exhausted({
        "last_usage": [
            {"window": "credits", "used_fraction": 1.0, "status": "blocked"},
        ],
    }) is True
    assert xsp.account_usage_exhausted({
        "last_usage": [
            {"window": "credits", "used_fraction": 0.58, "status": "ok"},
        ],
    }) is False
    assert xsp.account_usage_exhausted({"usage_exhausted": True}) is True
    assert xsp.account_usage_exhausted({"last_usage": []}) is False


def test_list_candidates_skips_100pct_usage_and_orders_by_remaining(tmp_config):
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    # Active is fully spent — must not be first candidate.
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-spent", "refresh_token": "r-s",
        "account_id": "acct-spent", "expires_at_ms": now,
        "last_usage": [
            {"window": "credits", "label": "Credits",
             "used_fraction": 1.0, "status": "blocked"},
        ],
    }, account_id="acct-spent", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-mid", "refresh_token": "r-m",
        "account_id": "acct-mid", "expires_at_ms": now,
        "last_usage": [
            {"window": "credits", "used_fraction": 0.8, "status": "warn"},
        ],
    }, account_id="acct-mid", set_active=False)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-fresh", "refresh_token": "r-f",
        "account_id": "acct-fresh", "expires_at_ms": now,
        "last_usage": [
            {"window": "credits", "used_fraction": 0.1, "status": "ok"},
        ],
    }, account_id="acct-fresh", set_active=False)

    cands = xsp.list_candidate_accounts(include_blocked=False, include_exhausted=False)
    ids = [a for a, _ in cands]
    assert "acct-spent" not in ids
    assert ids[0] == "acct-fresh"  # lowest usage first among non-preferred
    assert "acct-mid" in ids

    # pick_usable + constructor skip spent active.
    picked = xsp.pick_usable_account(prefer="acct-spent")
    assert picked is not None and picked[0] == "acct-fresh"
    p = xsp.XAISubProvider()
    assert p._cred["access_token"] == "tok-fresh"


def test_complete_ex_pulls_usage_before_try_and_skips_spent(tmp_config, monkeypatch):
    """Before each hop, live usage is pulled; 100% accounts are not POSTed."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    now = int(time.time() * 1000) + 10 ** 9
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-a", "refresh_token": "r-a",
        "account_id": "acct-a", "expires_at_ms": now,
        # Stale disk says ok — live pull will flip to spent.
        "last_usage": [
            {"window": "credits", "used_fraction": 0.2, "status": "ok"},
        ],
        "last_usage_fetched_at": 0,
    }, account_id="acct-a", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "tok-b", "refresh_token": "r-b",
        "account_id": "acct-b", "expires_at_ms": now,
        "last_usage": [
            {"window": "credits", "used_fraction": 0.3, "status": "ok"},
        ],
        "last_usage_fetched_at": 0,
    }, account_id="acct-b", set_active=False)

    pulls: list[str] = []

    def fake_pull(account_id, cred=None, *, force=True):  # noqa: ARG001
        pulls.append(str(account_id))
        c = dict(cred or {})
        c["account_id"] = account_id
        if account_id == "acct-a":
            c["last_usage"] = [
                {"window": "credits", "used_fraction": 1.0, "status": "blocked"},
            ]
            c["usage_exhausted"] = True
            # Mirror production side-effect so filters stick.
            xsp.mark_account_usage_exhausted("acct-a", detail="test", used_fraction=1.0)
        else:
            c["last_usage"] = [
                {"window": "credits", "used_fraction": 0.3, "status": "ok"},
            ]
        c["last_usage_fetched_at"] = time.time()
        return c

    seen_tokens: list[str] = []

    def fake_post(self, body):  # noqa: ARG001
        tok = (self._cred or {}).get("access_token")
        seen_tokens.append(tok)
        if tok == "tok-a":
            raise AssertionError("must not POST spent account A")
        return {
            "choices": [{"message": {"content": "ok-b", "reasoning_content": ""}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(xsp, "pull_account_usage", fake_pull)
    monkeypatch.setattr(xsp.XAISubProvider, "_post_once", fake_post)
    monkeypatch.setattr(xsp.XAISubProvider, "refresh", lambda self: self._cred or {})

    p = xsp.XAISubProvider()
    # Force start pointing at A; ensure_auth must pull + skip to B.
    p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    sub_auth.set_active_account(xsp.PROVIDER_ID, "acct-a")

    content, _ = p.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert content == "ok-b"
    assert "tok-a" not in seen_tokens
    assert "tok-b" in seen_tokens
    assert "acct-a" in pulls and "acct-b" in pulls


# ── xAI refresh-token rotation safety ────────────────────────────────────────


def test_save_credential_identity_patch_does_not_clobber_rotated_rt(tmp_config):
    """Identity/usage writes must not rewind a just-rotated refresh token."""
    now = time.time() * 1000
    sub_auth.save_credential("xai-sub", {
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expires_at_ms": now - 1000,
        "email": "a@x.ai",
        "account_id": "acct-a",
    }, account_id="acct-a")

    # Simulate a refresh that landed a rotated RT.
    sub_auth.save_credential("xai-sub", {
        "access_token": "at-new",
        "refresh_token": "rt-new",
        "expires_at_ms": now + 3600_000,
        "email": "a@x.ai",
        "account_id": "acct-a",
    }, account_id="acct-a", replace_tokens=True)

    # Stale identity snapshot from before the refresh (classic usage-poll race).
    sub_auth.save_credential(
        "xai-sub",
        {
            "access_token": "at-old",
            "refresh_token": "rt-old",
            "expires_at_ms": now - 1000,
            "display_name": "Alice",
            "plan": "SuperGrok",
            "email": "a@x.ai",
            "account_id": "acct-a",
            "last_usage": [{"window": "total", "label": "Credits", "used_fraction": 0.1}],
        },
        account_id="acct-a",
        set_active=False,
        replace_tokens=False,
    )

    stored = sub_auth.load_credential("xai-sub", account_id="acct-a")
    assert stored["refresh_token"] == "rt-new"
    assert stored["access_token"] == "at-new"
    assert stored["display_name"] == "Alice"
    assert stored["plan"] == "SuperGrok"
    assert stored["last_usage"][0]["label"] == "Credits"


def test_parallel_account_writes_do_not_drop_sibling_tokens(tmp_config):
    """Two accounts written under lock both survive (no full-snapshot clobber)."""
    import threading

    now = time.time() * 1000
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(aid: str, rt: str) -> None:
        try:
            barrier.wait(timeout=5)
            sub_auth.save_credential(
                "xai-sub",
                {
                    "access_token": f"at-{aid}",
                    "refresh_token": rt,
                    "expires_at_ms": now + 3600_000,
                    "account_id": aid,
                    "email": f"{aid}@x.ai",
                },
                account_id=aid,
                set_active=False,
                replace_tokens=True,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("acct-a", "rt-a"))
    t2 = threading.Thread(target=writer, args=("acct-b", "rt-b"))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert not errors, errors
    a = sub_auth.load_credential("xai-sub", account_id="acct-a")
    b = sub_auth.load_credential("xai-sub", account_id="acct-b")
    assert a and a["refresh_token"] == "rt-a"
    assert b and b["refresh_token"] == "rt-b"


def test_xai_refresh_serializes_and_reuses_rotated_token(tmp_config, monkeypatch):
    """Two concurrent refresh() calls share one network exchange; both get new RT."""
    import threading
    from services.agent.providers import xai_sub_provider as xsp

    now = time.time() * 1000
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expires_at_ms": now - 1000,
        "account_id": "acct-a",
        "email": "a@x.ai",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-a")

    calls = {"n": 0}
    gate = threading.Barrier(2)

    def fake_post_form(url, fields, headers=None, timeout=30.0):  # noqa: ARG001
        calls["n"] += 1
        # Let both threads enter refresh(); the lock should serialize so only one posts.
        assert fields.get("refresh_token") == "rt-old"
        return {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 3600,
        }

    monkeypatch.setattr(sub_auth, "post_form", fake_post_form)

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            p = xsp.XAISubProvider()
            p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
            gate.wait(timeout=5)
            results.append(p.refresh())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert not errors, errors
    assert len(results) == 2
    # Only one network refresh — the second short-circuits on the fresh disk cred.
    assert calls["n"] == 1
    stored = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    assert stored["refresh_token"] == "rt-new"
    assert stored["access_token"] == "at-new"
    assert all(r["refresh_token"] == "rt-new" for r in results)


def test_xai_refresh_invalid_grant_marks_revoked(tmp_config, monkeypatch):
    import urllib.error
    from services.agent.providers import xai_sub_provider as xsp

    now = time.time() * 1000
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-old",
        "refresh_token": "rt-dead",
        "expires_at_ms": now - 1000,
        "account_id": "acct-a",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-a")
    xsp.clear_blocked_accounts_for_tests()

    def boom(url, fields, headers=None, timeout=30.0):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", hdrs=None, fp=__import__("io").BytesIO(
                b'{"error":"invalid_grant","error_description":"Refresh token has been revoked"}'
            ),
        )

    monkeypatch.setattr(sub_auth, "post_form", boom)
    p = xsp.XAISubProvider()
    p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    try:
        p.refresh()
        raise AssertionError("expected refresh to fail")
    except RuntimeError as exc:
        assert "invalid_grant" in str(exc) or "revoked" in str(exc).lower()
    stored = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    assert stored.get("refresh_revoked") is True
    assert xsp.is_account_blocked("acct-a") is True


def test_ensure_fresh_xai_rotates_past_revoked_active(tmp_config, monkeypatch):
    """Pre-run gate must not fail closed on a dead active account when siblings live."""
    from services.agent.providers import xai_sub_provider as xsp

    now = time.time() * 1000
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-dead",
        "refresh_token": "rt-dead",
        "expires_at_ms": now - 1000,
        "account_id": "acct-dead",
        "email": "dead@x.ai",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-dead", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expires_at_ms": now - 1000,
        "account_id": "acct-live",
        "email": "live@x.ai",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-live", set_active=False)
    # Active pointer is the dead one.
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "acct-dead"
    xsp.clear_blocked_accounts_for_tests()

    def fake_post_form(url, fields, headers=None, timeout=30.0):  # noqa: ARG001
        rt = fields.get("refresh_token")
        if rt == "rt-dead":
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", hdrs=None, fp=__import__("io").BytesIO(
                    b'{"error":"invalid_grant","error_description":"Refresh token has been revoked"}'
                ),
            )
        if rt == "rt-live":
            return {
                "access_token": "at-live-new",
                "refresh_token": "rt-live-new",
                "expires_in": 7200,
            }
        raise AssertionError(f"unexpected rt {rt!r}")

    import urllib.error
    monkeypatch.setattr(sub_auth, "post_form", fake_post_form)

    out = sl.ensure_fresh("xai")
    assert out["ok"] is True, out
    assert out["refreshed"] is True
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "acct-live"
    live = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-live")
    assert live["refresh_token"] == "rt-live-new"
    assert live["access_token"] == "at-live-new"


def test_ensure_fresh_skips_revoked_prefix_beyond_old_cap(tmp_config, monkeypatch):
    """Revoked leaders must not hide a live account past the old 5-slot cap.

    Regression: _ENSURE_FRESH_MAX_ACCOUNTS sliced candidates BEFORE skipping
    refresh_revoked, so pools with many dead actives always failed closed.
    """
    from services.agent.providers import xai_sub_provider as xsp

    now = time.time() * 1000
    # 7 revoked/expired leaders, then one live account.
    for i in range(7):
        aid = f"acct-dead-{i}"
        sub_auth.save_credential(xsp.PROVIDER_ID, {
            "access_token": f"at-dead-{i}",
            "refresh_token": f"rt-dead-{i}",
            "expires_at_ms": now - 1000,
            "account_id": aid,
            "email": f"dead{i}@x.ai",
            "refresh_revoked": True,
            "auth_error": "invalid_grant",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }, account_id=aid, set_active=(i == 0))
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expires_at_ms": now - 1000,
        "account_id": "acct-live-deep",
        "email": "live@x.ai",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-live-deep", set_active=False)
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "acct-dead-0"
    xsp.clear_blocked_accounts_for_tests()

    posts = {"n": 0}

    def fake_post_form(url, fields, headers=None, timeout=30.0):  # noqa: ARG001
        posts["n"] += 1
        rt = fields.get("refresh_token")
        if rt == "rt-live":
            return {
                "access_token": "at-live-new",
                "refresh_token": "rt-live-new",
                "expires_in": 7200,
            }
        raise AssertionError(f"must not POST refresh for {rt!r}")

    monkeypatch.setattr(sub_auth, "post_form", fake_post_form)

    out = sl.ensure_fresh("xai")
    assert out["ok"] is True, out
    assert out["refreshed"] is True
    assert posts["n"] == 1  # only the live account
    assert sub_auth.get_active_account_id(xsp.PROVIDER_ID) == "acct-live-deep"
    live = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-live-deep")
    assert live["refresh_token"] == "rt-live-new"




def test_xai_tier_denied_spending_limit_reason_distinct():
    """403 body personal-team-blocked:spending-limit must surface as
    out-of-credits / needs-subscription, not the Heavy-only tier gate."""
    from services.agent.providers import xai_sub_provider as xsp

    exc = xsp.XaiTierDeniedError('{"code":"personal-team-blocked:spending-limit"}')
    msg = str(exc)
    assert "out of credits" in msg
    assert "spending limit" in msg
    assert "tier not API-enabled" not in msg
    assert exc.reason == xsp.SPENDING_LIMIT_REASON
    # Hint retained so the chat card still points at a way out.
    assert "Hint" in msg

    # Non-spend 403 keeps the tier-gate reason.
    exc2 = xsp.XaiTierDeniedError('{"code":"tier-gated"}')
    assert exc2.reason == xsp.TIER_DENIED_REASON
    assert "tier not API-enabled" in str(exc2)


def test_xai_ensure_auth_all_revoked_pool_says_relogin(tmp_config, monkeypatch):
    """When every stored account is refresh_revoked, _ensure_auth must say
    plainly that re-login is required — not a generic all-accounts-failed."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    past = time.time() * 1000 - 1000  # expired access JWTs
    for aid in ("acct-dead-1", "acct-dead-2"):
        sub_auth.save_credential(xsp.PROVIDER_ID, {
            "access_token": f"at-{aid}",
            "refresh_token": f"rt-{aid}",
            "expires_at_ms": past,
            "account_id": aid,
            "refresh_revoked": True,
            "auth_error": "invalid_grant",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }, account_id=aid, set_active=(aid == "acct-dead-1"))

    p = xsp.XAISubProvider()
    with pytest.raises(RuntimeError) as ei:
        p._ensure_auth()
    msg = str(ei.value)
    assert "revoked refresh tokens" in msg
    assert "Re-login required" in msg
    assert "Sign in with xAI" in msg
    # Not the misleading generic messages.
    assert "spent or temporarily blocked" not in msg
    assert "Not logged in" not in msg


def test_xai_ensure_auth_mixed_pool_not_flagged_all_revoked(tmp_config, monkeypatch):
    """One viable account among revoked ones must NOT trip the re-login error."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    past = time.time() * 1000 - 1000
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-dead",
        "refresh_token": "rt-dead",
        "expires_at_ms": past,
        "account_id": "acct-dead",
        "refresh_revoked": True,
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-dead", set_active=True)
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expires_at_ms": time.time() * 1000 + 10 ** 9,
        "account_id": "acct-live",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-live", set_active=False)

    assert xsp._stored_accounts_all_refresh_revoked() is False

    monkeypatch.setattr(
        xsp, "pull_account_usage",
        lambda account_id, cred=None, force=True: dict(cred or {}) | {"account_id": account_id},
    )
    p = xsp.XAISubProvider()
    p._ensure_auth()  # must not raise
    assert (p._cred or {}).get("account_id") == "acct-live"


def test_xai_all_revoked_predicate_empty_store(tmp_config):
    """No stored accounts → predicate is False (plain 'not logged in' path)."""
    from services.agent.providers import xai_sub_provider as xsp

    xsp.clear_blocked_accounts_for_tests()
    assert xsp._stored_accounts_all_refresh_revoked() is False


# ── 0600 credential-store permissions ───────────────────────────────────────

def test_credential_store_file_is_0600(tmp_config):
    """OAuth tokens must never persist world/group readable (default umask)."""
    sub_auth.save_credential("prov", {"access_token": "x"}, account_id="a")
    mode = stat.S_IMODE(tmp_config.stat().st_mode)
    assert mode == 0o600


def test_load_cfg_tightens_existing_insecure_permissions(tmp_config):
    """A config.json left over from before this fix gets chmod'd on load."""
    tmp_config.write_text("{}", encoding="utf-8")
    tmp_config.chmod(0o644)
    sub_auth._load_cfg()
    mode = stat.S_IMODE(tmp_config.stat().st_mode)
    assert mode == 0o600


def test_account_lock_file_is_0600(tmp_config):
    with sub_auth.account_lock("prov", "acct-a"):
        pass
    lock_p = sub_auth._account_lock_path("prov", "acct-a")
    mode = stat.S_IMODE(lock_p.stat().st_mode)
    assert mode == 0o600


# ── account_lock (interprocess flock) ───────────────────────────────────────

def test_account_lock_serializes_across_independent_opens(tmp_config):
    """Two independent lock acquisitions (simulating separate processes, since
    flock is per open-file-description) must serialize — the second blocks
    until the first releases, even with no in-process threading.Lock held."""
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def first() -> None:
        with sub_auth.account_lock("prov", "acct-a"):
            order.append("first-enter")
            entered.set()
            release.wait(timeout=5)
            order.append("first-exit")

    t1 = threading.Thread(target=first)
    t1.start()
    assert entered.wait(timeout=5)

    def second() -> None:
        with sub_auth.account_lock("prov", "acct-a"):
            order.append("second-enter")

    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.15)
    assert order == ["first-enter"]  # second is still blocked on the flock

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first-enter", "first-exit", "second-enter"]


def test_xai_refresh_invalid_grant_retries_once_when_peer_rotated(tmp_config, monkeypatch):
    """Defense-in-depth: invalid_grant on the token we sent must not revoke a
    healthy account when the on-disk refresh_token already moved on (a peer
    rotated it) — retry once with the newer token instead."""
    import io
    import urllib.error
    from services.agent.providers import xai_sub_provider as xsp

    now = time.time() * 1000
    sub_auth.save_credential(xsp.PROVIDER_ID, {
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expires_at_ms": now - 1000,
        "account_id": "acct-a",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
    }, account_id="acct-a")
    xsp.clear_blocked_accounts_for_tests()

    p = xsp.XAISubProvider()
    p._cred = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")

    calls = {"n": 0}

    def fake_post_form(url, fields, headers=None, timeout=30.0):  # noqa: ARG001
        calls["n"] += 1
        if fields.get("refresh_token") == "rt-old":
            # Simulate a peer process having already rotated this RT: land the
            # newer token on disk (bypassing our own lock, as a true peer
            # process would) before we discover our token is stale.
            sub_auth.save_credential(xsp.PROVIDER_ID, {
                "access_token": "at-peer",
                "refresh_token": "rt-peer",
                "expires_at_ms": now + 3600_000,
                "account_id": "acct-a",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            }, account_id="acct-a", replace_tokens=True)
            raise urllib.error.HTTPError(
                url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(
                    b'{"error":"invalid_grant","error_description":"revoked"}'
                ),
            )
        assert fields.get("refresh_token") == "rt-peer"
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}

    monkeypatch.setattr(sub_auth, "post_form", fake_post_form)
    result = p.refresh()
    assert calls["n"] == 2  # one failed attempt + one retry with the rotated token
    assert result["refresh_token"] == "rt-new"
    stored = sub_auth.load_credential(xsp.PROVIDER_ID, account_id="acct-a")
    assert stored.get("refresh_revoked") is not True
    assert xsp.is_account_blocked("acct-a") is False
