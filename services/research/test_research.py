"""Tests for services.research (Pillar 8, contract contracts/research_tools.md).

No network: every provider/fetch is mocked or injected via a test seam, and the cache
is redirected to a tempdir. Runnable via pytest OR as a script (``python -m
services.research.test_research``) — same convention as the other services/research test files.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import importlib

from services import research
from services.research import common
from services.research.cache import Cache

# Submodules (the package re-exports fetch_page/web_search as *functions*, shadowing the
# module attributes — import_module returns the real module objects for monkeypatching).
cache_mod = importlib.import_module("services.research.cache")
fp_mod = importlib.import_module("services.research.fetch_page")
ws_mod = importlib.import_module("services.research.web_search")
from services.research.providers import (
    DDGProvider,
    MockSearchProvider,
    TavilyProvider,
    parse_ddg_html,
    select_provider,
)


# --------------------------------------------------------------------------- helpers


class _OfflineToggle:
    """Force is_offline() in the web_search + fetch_page modules (they bound the name at
    import). Restores on exit; works under pytest and the __main__ runner alike."""

    def __init__(self, value: bool):
        self.value = value

    def __enter__(self):
        self._orig = (ws_mod.is_offline, fp_mod.is_offline)
        ws_mod.is_offline = lambda: self.value
        fp_mod.is_offline = lambda: self.value
        return self

    def __exit__(self, *a):
        ws_mod.is_offline, fp_mod.is_offline = self._orig


def _tmp_cache(td: str) -> Cache:
    return Cache(root=Path(td))


# --------------------------------------------------------------------------- envelope


def test_envelope_and_fail_shape():
    env = common.envelope("42 MPa", units="MPa", source_url="https://x", quote="42 MPa",
                          confidence=common.HIGH)
    assert env["ok"] is True
    for k in ("answer", "units", "source_url", "quote", "fetched_ts", "confidence", "cached"):
        assert k in env
    assert env["fetched_ts"].endswith("Z")
    bad = common.fail("network", "retry")
    assert bad == {"ok": False, "reason": "network", "hint": "retry"}


# --------------------------------------------------------------------------- web_search


def test_web_search_mock_provider():
    prov = MockSearchProvider(
        results=[{"title": "6061", "url": "https://ex/6061", "snippet": "276 MPa"}],
        answer="6061-T6 yield ~276 MPa", confidence="high")
    with _OfflineToggle(False):
        out = research.web_search("6061 yield", provider=prov)
    assert out["ok"] and out["provider"] == "mock"
    assert out["results"][0]["url"] == "https://ex/6061"
    assert out["source_url"] == "https://ex/6061"
    assert out["answer"] == "6061-T6 yield ~276 MPa"


def test_web_search_offline_degrades():
    with _OfflineToggle(True):
        out = research.web_search("anything", provider=MockSearchProvider())
    assert out == {"ok": False, "reason": "offline", "hint": out["hint"]}
    assert out["ok"] is False and out["reason"] == "offline"


def test_web_search_empty_query():
    with _OfflineToggle(False):
        out = research.web_search("   ")
    assert out["ok"] is False and out["reason"] == "bad_query"


def test_web_search_provider_error_degrades():
    class _Boom(MockSearchProvider):
        def search(self, query, max_results=5):
            raise RuntimeError("boom")

    with _OfflineToggle(False):
        out = research.web_search("q", provider=_Boom())
    assert out["ok"] is False and out["reason"] == "network"


# --------------------------------------------------------------------------- fetch_page


def _counting_fetcher(body="# Title\n\n276 MPa", title="T"):
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        return body, url, title

    return fetch, calls


def test_fetch_page_live_then_cache_hit():
    with tempfile.TemporaryDirectory() as td:
        c = _tmp_cache(td)
        fetch, calls = _counting_fetcher()
        with _OfflineToggle(False):
            first = research.fetch_page("https://ex/p", cache=c, _fetcher=fetch)
            assert first["ok"] and first["cached"] is False and calls["n"] == 1
            assert "276 MPa" in first["content"]
            second = research.fetch_page("https://ex/p", cache=c, _fetcher=fetch)
        assert second["cached"] is True and calls["n"] == 1  # served from cache, no refetch
        assert second["fetched_ts"] == first["fetched_ts"]  # original fetch time preserved


def test_fetch_page_cache_expiry_refetches():
    with tempfile.TemporaryDirectory() as td:
        c = _tmp_cache(td)
        fetch, calls = _counting_fetcher()
        with _OfflineToggle(False):
            research.fetch_page("https://ex/price", cache=c, tier="pricing", _fetcher=fetch)
            assert calls["n"] == 1
            # age the stored record past the pricing TTL
            p = c._path("https://ex/price")
            import json
            rec = json.loads(p.read_text())
            rec["_stored_at"] = time.time() - (cache_mod.TTL_SECONDS["pricing"] + 10)
            p.write_text(json.dumps(rec))
            out = research.fetch_page("https://ex/price", cache=c, tier="pricing", _fetcher=fetch)
        assert calls["n"] == 2 and out["cached"] is False  # stale -> refetched


def test_fetch_page_offline_serves_cache_only():
    with tempfile.TemporaryDirectory() as td:
        c = _tmp_cache(td)
        fetch, _ = _counting_fetcher()
        with _OfflineToggle(False):
            research.fetch_page("https://ex/cached", cache=c, _fetcher=fetch)  # warm cache online
        with _OfflineToggle(True):
            hit = research.fetch_page("https://ex/cached", cache=c, _fetcher=fetch)
            miss = research.fetch_page("https://ex/never", cache=c, _fetcher=fetch)
        assert hit["ok"] and hit["cached"] is True and hit["confidence"] == "offline"
        assert miss["ok"] is False and miss["reason"] == "offline"


def test_fetch_page_bad_url():
    out = research.fetch_page("not-a-url")
    assert out["ok"] is False and out["reason"] == "bad_url"


def test_fetch_page_network_error_degrades():
    def boom(url):
        raise ConnectionError("down")

    with tempfile.TemporaryDirectory() as td:
        with _OfflineToggle(False):
            out = research.fetch_page("https://ex/x", cache=_tmp_cache(td), _fetcher=boom)
    assert out["ok"] is False and out["reason"] == "network"


# --------------------------------------------------------------------------- extraction


def test_html_to_markdown_strips_script_style():
    html = (
        "<html><head><title>Doc</title><style>.a{}</style></head>"
        "<body><script>evil()</script><h1>Head</h1><p>Yield 276 MPa</p></body></html>"
    )
    md, title = fp_mod.html_to_markdown(html)
    assert title == "Doc"
    assert "evil" not in md and ".a{" not in md
    assert "# Head" in md and "276 MPa" in md


def test_parse_ddg_html():
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.org%2Fp">'
        "6061 Alloy</a>"
        '<a class="result__snippet">Yield strength 276 MPa</a>'
    )
    rows = parse_ddg_html(html, max_results=5)
    assert rows and rows[0]["url"] == "https://ex.org/p"
    assert rows[0]["title"] == "6061 Alloy" and "276 MPa" in rows[0]["snippet"]


# --------------------------------------------------------------------------- providers / cache root


def test_select_provider_fallback_and_key(monkeypatch=None):
    import os

    saved = os.environ.pop("TAVILY_API_KEY", None)
    try:
        assert isinstance(select_provider(), DDGProvider)  # no key -> zero-key fallback
        assert select_provider("mock").name == "mock"
        os.environ["TAVILY_API_KEY"] = "k"
        assert isinstance(select_provider(), TavilyProvider)  # key present -> tavily
    finally:
        os.environ.pop("TAVILY_API_KEY", None)
        if saved is not None:
            os.environ["TAVILY_API_KEY"] = saved


def test_knowledge_root_env_override():
    import os

    saved = os.environ.get("CHUDWARE_KNOWLEDGE_DIR")
    try:
        os.environ["CHUDWARE_KNOWLEDGE_DIR"] = "/tmp/chudware-knowledge-test"
        assert str(cache_mod.knowledge_root()) == "/tmp/chudware-knowledge-test"
    finally:
        if saved is None:
            os.environ.pop("CHUDWARE_KNOWLEDGE_DIR", None)
        else:
            os.environ["CHUDWARE_KNOWLEDGE_DIR"] = saved


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"PASSED  {len(fns)} research tests")


if __name__ == "__main__":
    main()
