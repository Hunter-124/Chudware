"""Tests for services/browser/page_cache — the bounded on-disk page cache.

No real network and no real CEF: ``browser_fetch``'s ``_cef`` injection seam
(same pattern as test_browser_session.py) counts how many times the "network" was
actually touched. Isolation is by ``APPDATA`` + reload, also as in
test_browser_session.py, so the cache lands in ``tmp_path`` and nothing escapes
into the user's real data dir.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import importlib
import os
import time

import pytest


@pytest.fixture()
def cache_mods(tmp_path, monkeypatch):
    """Reload services.browser.* under an isolated APPDATA base."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_PAGE_CACHE", raising=False)
    monkeypatch.delenv("CHUDWARE_PAGE_CACHE_TTL_S", raising=False)
    monkeypatch.delenv("CHUDWARE_PAGE_CACHE_MAX_MB", raising=False)
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import page_cache, profiles, ratelimit, session
    importlib.reload(profiles)
    importlib.reload(ratelimit)
    importlib.reload(page_cache)
    importlib.reload(session)
    page_cache.reset_state()
    yield page_cache, session, ratelimit
    page_cache.close()
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


class _CountingCef:
    """Stands in for the CEF daemon; records every fetch that reaches it."""

    def __init__(self, html: str = "<html>product page</html>", status: int = 200):
        self._html, self._status = html, status
        self.calls: list[str] = []

    def fetch(self, url, settle_ms=None):
        self.calls.append(url)
        return {"ok": True, "status": self._status, "html": self._html}


#: How far in the past the fixture payload claims to have been fetched.
#: page_cache judges freshness from the payload's OWN ``fetched_ts`` (when the
#: page really came off the wire), not from when the row was inserted — the
#: right behaviour for a page cache, but it means a fixture that hardcodes a
#: literal instant silently rots: once wall-clock passes the 6 h default TTL,
#: every put/get round-trip in this file degrades into a miss. So anchor to now,
#: one minute back — comfortably inside the TTL, yet distinguishably not "now",
#: which keeps the "original instant is preserved" assertion meaningful.
_FETCHED_AGO_S = 60.0


def _fetched_ts(ago_s: float = _FETCHED_AGO_S) -> str:
    """The contract's ISO-8601 ``Z`` instant, ``ago_s`` seconds before now."""
    return _dt.datetime.fromtimestamp(time.time() - ago_s,
                                      _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(content: str = "<html>hi</html>", status: int = 200,
             fetched_ts: str | None = None) -> dict:
    return {"ok": True, "url": "https://x/1", "status": status, "content": content,
            "fetched_ts": fetched_ts or _fetched_ts(),
            "session_profile_used": "grabcad"}


# --- unit: get/put --------------------------------------------------------------

def test_miss_then_hit_roundtrips_the_payload(cache_mods):
    page_cache, _session, _rl = cache_mods
    assert page_cache.get("https://x/1", "grabcad") is None

    sent = _payload("<html>body</html>")
    page_cache.put("https://x/1", "grabcad", sent)
    hit = page_cache.get("https://x/1", "grabcad")

    assert hit is not None
    assert hit["ok"] is True
    assert hit["cached"] is True
    assert hit["content"] == "<html>body</html>"
    assert hit["status"] == 200
    assert hit["url"] == "https://x/1"
    assert hit["fetched_ts"] == sent["fetched_ts"]  # original instant, not now
    # age is measured from that original instant, not from the insert, so a
    # payload fetched a minute ago reports ~60 s — never ~0.
    assert hit["age_s"] >= _FETCHED_AGO_S - 1.0


def test_key_is_scoped_by_site(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload("<html>a</html>"))
    assert page_cache.get("https://x/1", "mcmaster") is None
    assert page_cache.get("https://x/1", "grabcad")["content"] == "<html>a</html>"


@pytest.mark.parametrize("payload", [
    {"ok": False, "reason": "challenge_page", "hint": "captcha"},
    {"ok": False, "reason": "bot_walled", "hint": "403"},
    {"ok": True, "content": "", "status": 200},
    {"ok": True, "content": "   ", "status": 200},
    {"ok": True, "content": "<html>error</html>", "status": 404},
    {"ok": True, "content": "<html>error</html>", "status": 403},
])
def test_never_caches_failures_or_error_pages(cache_mods, payload):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", payload)
    assert page_cache.get("https://x/1", "grabcad") is None
    assert page_cache.stats()["entries"] == 0


def test_oversized_pages_are_skipped(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    monkeypatch.setattr(page_cache, "_MAX_ENTRY_BYTES", 64)
    page_cache.put("https://x/1", "grabcad", _payload("y" * 4096))
    assert page_cache.get("https://x/1", "grabcad") is None


def test_ttl_expiry_is_a_miss_and_deletes_the_entry(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    assert page_cache.stats()["entries"] == 1

    monkeypatch.setenv("CHUDWARE_PAGE_CACHE_TTL_S", "0.0001")
    time.sleep(0.01)
    assert page_cache.get("https://x/1", "grabcad") is None
    assert page_cache.stats()["entries"] == 0
    # blob gone too, not just the row
    assert not list(page_cache.cache_dir().rglob("*.html.gz"))


def test_max_age_override_beats_the_env_ttl(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    time.sleep(0.01)
    assert page_cache.get("https://x/1", "grabcad", max_age_s=0.001) is None


def test_size_cap_evicts_least_recently_used(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    # Incompressible bodies, so the on-disk budget is actually meaningful.
    for i in range(6):
        page_cache.put(f"https://x/{i}", "grabcad", _payload(os.urandom(16384).hex()))
    total = page_cache.stats()["bytes"]
    assert page_cache.stats()["entries"] == 6 and total > 0

    # Touch the newest so it is unambiguously the most-recently-used, then halve
    # the budget and force the sweep the 60 s throttle would otherwise skip.
    assert page_cache.get("https://x/5", "grabcad") is not None
    page_cache.close()          # force-flushes the batched last_used touch
    monkeypatch.setenv("CHUDWARE_PAGE_CACHE_MAX_MB", repr(total * 0.5 / (1024 * 1024)))
    page_cache._force_evict_for_tests()

    after = page_cache.stats()
    assert 0 < after["entries"] < 6                          # partial, not a wipe
    assert after["bytes"] <= total * 0.5
    assert page_cache.get("https://x/0", "grabcad") is None  # coldest went first
    assert page_cache.get("https://x/5", "grabcad") is not None  # hottest survived
    # no orphaned blobs left behind by eviction
    assert len(list(page_cache.cache_dir().rglob("*.html.gz"))) == after["entries"]


def test_last_used_writes_are_batched(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    page_cache._flush_locked(force=True)

    writes = {"n": 0}
    real = page_cache._flush_locked

    def _counting(*, force=False):
        pending = bool(page_cache._dirty)
        real(force=force)
        if pending and not page_cache._dirty:
            writes["n"] += 1        # the dirty set actually reached sqlite

    monkeypatch.setattr(page_cache, "_flush_locked", _counting)
    for _ in range(50):
        assert page_cache.get("https://x/1", "grabcad") is not None

    # 50 hits inside one flush window must coalesce — not 50 UPDATEs.
    assert writes["n"] <= 1
    assert page_cache._dirty                      # the touch is pending, not lost
    page_cache._flush_locked(force=True)
    assert not page_cache._dirty                  # ...and a forced flush lands it


def test_purge_clears_by_site_and_wholesale(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    page_cache.put("https://x/2", "mcmaster", _payload())

    assert page_cache.purge("grabcad") == 1
    assert page_cache.get("https://x/1", "grabcad") is None
    assert page_cache.get("https://x/2", "mcmaster") is not None

    assert page_cache.purge() == 1
    assert page_cache.stats()["entries"] == 0


def test_stats_counts_hits_and_misses(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.get("https://x/1", "grabcad")            # miss
    page_cache.put("https://x/1", "grabcad", _payload())
    page_cache.get("https://x/1", "grabcad")            # hit
    s = page_cache.stats()
    assert s["hits"] == 1 and s["misses"] == 1
    assert s["enabled"] is True and s["entries"] == 1 and s["bytes"] > 0


# --- degradation ----------------------------------------------------------------

def test_kill_switch_writes_nothing_to_disk(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    monkeypatch.setenv("CHUDWARE_PAGE_CACHE", "0")

    page_cache.put("https://x/1", "grabcad", _payload())
    assert page_cache.get("https://x/1", "grabcad") is None
    assert page_cache.purge() == 0
    assert page_cache.stats() == {"entries": 0, "bytes": 0, "hits": 0, "misses": 0,
                                  "enabled": False}
    assert not page_cache.cache_dir().exists()


def test_corrupt_index_degrades_to_misses(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    page_cache.close()

    db = page_cache.cache_dir() / "index.sqlite"
    db.write_bytes(b"this is not a database, not even close" * 64)
    page_cache.reset_state()

    assert page_cache.get("https://x/1", "grabcad") is None   # no raise
    page_cache.put("https://x/2", "grabcad", _payload())      # no raise
    assert page_cache.stats()["entries"] == 0


def test_missing_blob_degrades_to_a_miss(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    for blob in page_cache.cache_dir().rglob("*.html.gz"):
        blob.unlink()
    assert page_cache.get("https://x/1", "grabcad") is None
    assert page_cache.stats()["entries"] == 0


def test_truncated_blob_degrades_to_a_miss(cache_mods):
    page_cache, _session, _rl = cache_mods
    page_cache.put("https://x/1", "grabcad", _payload())
    for blob in page_cache.cache_dir().rglob("*.html.gz"):
        blob.write_bytes(gzip.compress(b"partial")[:5])
    assert page_cache.get("https://x/1", "grabcad") is None


def test_unwritable_cache_dir_is_not_fatal(cache_mods, monkeypatch):
    page_cache, _session, _rl = cache_mods
    from services import paths as engine_paths
    monkeypatch.setattr(engine_paths, "ROOT", engine_paths.ROOT / "nope" / "\0bad")
    page_cache.reset_state()
    page_cache.put("https://x/1", "grabcad", _payload())   # no raise
    assert page_cache.get("https://x/1", "grabcad") is None


# --- session.browser_fetch integration -------------------------------------------

def test_browser_fetch_second_call_is_served_from_cache(cache_mods):
    _page_cache, session, _rl = cache_mods
    cef = _CountingCef()

    first = session.browser_fetch("https://grabcad.com/p/1", "grabcad",
                                  _cef=cef, min_interval=0.0)
    second = session.browser_fetch("https://grabcad.com/p/1", "grabcad",
                                   _cef=cef, min_interval=0.0)

    assert first["ok"] is True and "cached" not in first
    assert second["ok"] is True and second["cached"] is True
    assert second["content"] == first["content"]
    assert second["fetched_ts"] == first["fetched_ts"]
    assert cef.calls == ["https://grabcad.com/p/1"]  # network touched exactly once


def test_cache_hit_skips_the_profile_lock_and_rate_limiter(cache_mods, monkeypatch):
    _page_cache, session, ratelimit = cache_mods
    cef = _CountingCef()
    session.browser_fetch("https://grabcad.com/p/2", "grabcad", _cef=cef, min_interval=0.0)

    waits: list[str] = []
    monkeypatch.setattr(ratelimit, "wait",
                        lambda site, *, min_interval=0.0, url=None, max_wait_s=None:
                        (waits.append(site), 0.0)[1])
    monkeypatch.setattr(session.profiles, "profile_lock",
                        lambda site: pytest.fail("cache hit must not take the profile lock"))
    monkeypatch.setattr(session.profiles, "ensure_profile",
                        lambda site: pytest.fail("cache hit must not touch the profile"))

    hit = session.browser_fetch("https://grabcad.com/p/2", "grabcad",
                                _cef=cef, min_interval=0.0)
    assert hit["cached"] is True
    assert waits == []
    assert cef.calls == ["https://grabcad.com/p/2"]


def test_no_cache_flag_skips_lookup_and_store(cache_mods):
    _page_cache, session, _rl = cache_mods
    cef = _CountingCef()

    a = session.browser_fetch("https://grabcad.com/p/3", "grabcad",
                              _cef=cef, min_interval=0.0, no_cache=True)
    b = session.browser_fetch("https://grabcad.com/p/3", "grabcad",
                              _cef=cef, min_interval=0.0, no_cache=True)

    assert "cached" not in a and "cached" not in b
    assert len(cef.calls) == 2
    assert _page_cache.stats()["entries"] == 0


def test_browser_fetch_never_caches_an_http_error(cache_mods, monkeypatch):
    _page_cache, session, ratelimit = cache_mods
    cef = _CountingCef(html="<html>Access Denied</html>", status=403)
    # A 403 arms the rate limiter's exponential backoff; neutralise it so the second
    # call measures the CACHE, not the (correct) 30 s penalty. Returning 0.0 is
    # "no wait was needed" — session.py reads that return value now, because a wait
    # longer than session.COOLDOWN_WAIT_BUDGET_S short-circuits to a cooldown_active
    # refusal instead of a second fetch.
    monkeypatch.setattr(ratelimit, "wait",
                        lambda site, *, min_interval=0.0, url=None, max_wait_s=None: 0.0)

    first = session.browser_fetch("https://grabcad.com/p/4", "grabcad",
                                  _cef=cef, min_interval=0.0)
    second = session.browser_fetch("https://grabcad.com/p/4", "grabcad",
                                   _cef=cef, min_interval=0.0)

    assert first["ok"] is False and second["ok"] is False
    assert len(cef.calls) == 2  # re-fetched, not served from cache
    assert _page_cache.stats()["entries"] == 0


def test_browser_fetch_never_caches_a_challenge_page(cache_mods):
    _page_cache, session, _rl = cache_mods
    cef = _CountingCef(html='<div class="g-recaptcha" data-sitekey="k"></div>')

    r = session.browser_fetch("https://grabcad.com/p/5", "grabcad",
                              _cef=cef, min_interval=0.0)
    assert r["ok"] is False and r["reason"] == "challenge_page"
    assert _page_cache.stats()["entries"] == 0
