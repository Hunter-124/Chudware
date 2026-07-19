"""Firecrawl key-pool rotation — no network; transport is injected.

Covers the observable contract: keys merge/dedup from env+config, 429 rotates to the
next ready key (Retry-After honored), 401/402/403 parks a key for a day, request-level
4xx raises without rotating, an all-cooling pool waits then raises PoolExhaustedError,
and cooldown state persists across processes (state file).
"""
from __future__ import annotations

import json
import time

import pytest

from engine.research import firecrawl_pool as pool


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every test gets a private state file and a clean key environment."""
    monkeypatch.setenv(pool.ENV_STATE, str(tmp_path / "state.json"))
    monkeypatch.setenv(pool.ENV_CONFIG, str(tmp_path / "config.json"))
    monkeypatch.delenv(pool.ENV_KEYS, raising=False)
    monkeypatch.delenv(pool.ENV_KEY, raising=False)
    monkeypatch.delenv(pool.ENV_MAX_WAIT, raising=False)
    yield


def _write_config(tmp_path, firecrawl):
    cfg = {"keys": {"openrouter": "sk-or-keep-me", "firecrawl": firecrawl}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return p


def _ok_transport(body=b'{"data": {"markdown": "md", "metadata": {"title": "T"}}}'):
    def transport(key, path, payload, timeout):
        return 200, None, body
    return transport


# --------------------------------------------------------------------------- load_keys


def test_load_keys_merges_env_and_config_deduped(monkeypatch, tmp_path):
    _write_config(tmp_path, ["fc-bbb", "fc-ccc", "fc-aaa"])  # fc-aaa dup of env
    monkeypatch.setenv(pool.ENV_KEYS, "fc-aaa, fc-bbb\nfc-ddd")
    monkeypatch.setenv(pool.ENV_KEY, "fc-eee")
    keys = pool.load_keys()
    assert keys == ["fc-aaa", "fc-bbb", "fc-ddd", "fc-eee", "fc-ccc"]


def test_load_keys_config_string_form(monkeypatch, tmp_path):
    _write_config(tmp_path, "fc-one, fc-two\nfc-three")
    assert pool.load_keys() == ["fc-one", "fc-two", "fc-three"]


def test_load_keys_empty_when_unconfigured():
    assert pool.load_keys() == []


# --------------------------------------------------------------------------- rotation


def test_rotates_on_429_and_records_cooldown():
    seen = []

    def transport(key, path, payload, timeout):
        seen.append(key)
        if key == "fc-hot":
            return 429, 30.0, b'{"error": "rate limit"}'
        return 200, None, b'{"ok": true}'

    out = pool.request("/v1/scrape", {"url": "https://x"}, transport=transport,
                       keys=["fc-hot", "fc-cold"])
    assert out == {"ok": True}
    assert seen == ["fc-hot", "fc-cold"]
    cd = pool._cooldowns()[pool._kid("fc-hot")]
    assert cd["reason"] == "rate_limited" and cd["status"] == 429
    assert 25 < cd["until"] - time.time() <= 30.0  # Retry-After honored, not the 60s default


def test_cooled_key_is_skipped_on_later_calls():
    calls = []

    def transport(key, path, payload, timeout):
        calls.append(key)
        if len(calls) == 1:
            return 429, None, b""
        return 200, None, b'{"ok": true}'

    keys = ["fc-a", "fc-b"]
    pool.request("/v1/scrape", {}, transport=transport, keys=keys)
    assert calls == ["fc-a", "fc-b"]
    # Second request: fc-a is cooling (default 60s) so only fc-b is tried.
    pool.request("/v1/scrape", {}, transport=transport, keys=keys)
    assert calls == ["fc-a", "fc-b", "fc-b"]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_dead_key_statuses_park_key_for_a_day(status):
    def transport(key, path, payload, timeout):
        if key == "fc-dead":
            return status, None, b""
        return 200, None, b'{"ok": true}'

    pool.request("/v1/scrape", {}, transport=transport, keys=["fc-dead", "fc-live"])
    cd = pool._cooldowns()[pool._kid("fc-dead")]
    assert cd["reason"] == "key_dead" and cd["status"] == status
    assert cd["until"] - time.time() > 23 * 3600


def test_request_level_4xx_raises_without_rotating():
    seen = []

    def transport(key, path, payload, timeout):
        seen.append(key)
        return 400, None, b'{"error": "bad request"}'

    with pytest.raises(pool.FirecrawlAPIError) as ei:
        pool.request("/v1/scrape", {}, transport=transport, keys=["fc-a", "fc-b"])
    assert ei.value.status == 400
    assert seen == ["fc-a"]  # rotation cannot fix a malformed request


def test_server_error_cools_briefly_and_rotates():
    def transport(key, path, payload, timeout):
        if key == "fc-a":
            return 503, None, b""
        return 200, None, b'{"ok": true}'

    pool.request("/v1/scrape", {}, transport=transport, keys=["fc-a", "fc-b"])
    cd = pool._cooldowns()[pool._kid("fc-a")]
    assert cd["reason"] == "server_error"
    assert cd["until"] - time.time() <= pool.COOLDOWN_SERVER_S


def test_all_keys_cooling_waits_then_succeeds(monkeypatch):
    # fc-a cooled for 0.2s up front; pool must sleep for it instead of failing.
    pool._set_cooldown("fc-a", 0.2, "rate_limited", 429)
    slept = []
    monkeypatch.setattr(pool.time, "sleep", lambda s: slept.append(s))

    out = pool.request("/v1/scrape", {}, transport=_ok_transport(),
                       keys=["fc-a"], max_wait_s=5.0)
    assert "data" in out
    assert slept and slept[0] > 0


def test_all_keys_cooling_beyond_cap_raises():
    pool._set_cooldown("fc-a", 3600, "rate_limited", 429)
    with pytest.raises(pool.PoolExhaustedError, match="cooling"):
        pool.request("/v1/scrape", {}, transport=_ok_transport(),
                     keys=["fc-a"], max_wait_s=0.0)


def test_no_keys_raises_pool_exhausted():
    with pytest.raises(pool.PoolExhaustedError, match="no Firecrawl API keys"):
        pool.request("/v1/scrape", {}, transport=_ok_transport(), keys=[])


def test_cooldown_state_persists_across_reads():
    """State file is the cross-process contract: a cooldown written by one 'process'
    (state mutation) is visible to a fresh read."""
    pool._set_cooldown("fc-x", 120, "rate_limited", 429)
    fresh = pool._cooldowns()
    assert pool._kid("fc-x") in fresh
    # success clears the cooldown
    pool._record_success("fc-x")
    assert pool._kid("fc-x") not in pool._cooldowns()


def test_success_clears_prior_cooldown():
    pool._set_cooldown("fc-a", 300, "server_error", 503)
    # Wait out nothing — simulate the cooldown expiring by forcing success path:
    # transport succeeds, and _record_success must clear the parked entry.
    pool._update_state(lambda s: s["cooldowns"].__setitem__(
        pool._kid("fc-a"), {"until": 0.0, "reason": "server_error", "status": 503}))
    pool.request("/v1/scrape", {}, transport=_ok_transport(), keys=["fc-a"])
    assert pool._kid("fc-a") not in pool._cooldowns()


# --------------------------------------------------------------------------- helpers


def test_scrape_returns_markdown_and_metadata():
    md, meta = pool.scrape("https://example.org", transport=_ok_transport(), keys=["fc-a"])
    assert md == "md" and meta == {"title": "T"}


def test_search_clamps_limit():
    captured = {}

    def transport(key, path, payload, timeout):
        captured.update(payload)
        return 200, None, b'{"data": []}'

    pool.search("q", limit=5000, transport=transport, keys=["fc-a"])
    assert captured["limit"] == 100
    assert captured["query"] == "q"
    assert captured["sources"] == [{"type": "web"}]


def test_pool_status_masks_keys_and_reports_readiness(monkeypatch):
    pool._set_cooldown("fc-secretkey1234", 60, "rate_limited", 429)
    monkeypatch.setenv(pool.ENV_KEYS, "fc-secretkey1234,fc-other")
    rows = pool.pool_status()
    assert len(rows) == 2
    hot, cold = rows
    assert "secret" not in hot["key"] and hot["key"].startswith("fc-sec")
    assert hot["ready"] is False and hot["cooldown_remaining_s"] > 0
    assert cold["ready"] is True
