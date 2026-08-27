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


def test_fetch_page_rejects_private_targets_before_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(fp_mod.socket, "getaddrinfo", lambda *a, **k: calls.append(a) or [])
    for url in ("https://127.0.0.1/x", "https://[::1]/x",
                "https://[::ffff:127.0.0.1]/x", "http://example.com/x"):
        out = research.fetch_page(url)
        assert out["reason"] == "bad_url", (url, out)
    assert calls == []


def test_public_https_policy_accepts_public_dns(monkeypatch):
    monkeypatch.setattr(fp_mod.socket, "getaddrinfo", lambda *a, **k: [
        (fp_mod.socket.AF_INET, fp_mod.socket.SOCK_STREAM, fp_mod.socket.IPPROTO_TCP,
         "", ("93.184.216.34", 443))
    ])
    fp_mod._validate_public_url("https://example.com/path")


def test_http_get_revalidates_private_redirect_before_next_connection(monkeypatch):
    requested = []

    class _Response:
        is_redirect = True
        headers = {"location": "https://127.0.0.1/metadata"}

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def get(self, url):
            requested.append(url)
            return _Response()

    monkeypatch.setattr(fp_mod.socket, "getaddrinfo", lambda *a, **k: [
        (fp_mod.socket.AF_INET, fp_mod.socket.SOCK_STREAM, fp_mod.socket.IPPROTO_TCP,
         "", ("93.184.216.34", 443))
    ])
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    try:
        fp_mod._http_get("https://example.com/start")
        assert False, "private redirect should be refused"
    except fp_mod.OutboundPolicyError:
        pass
    assert requested == ["https://example.com/start"]


# ------------------------------------------------------- redirect-walk budget
# A fresh request per hop means a per-hop timeout MULTIPLIES. These pin the
# WHOLE-walk bound that keeps this tool's worst case inside the outer harness
# deadline (services/agent/tool_guidance/research.py research_fetch_page
# timeout_s), rather than the current numbers.


class _FakeClock:
    """Stand-in for the ``time`` module inside fetch_page (only ``monotonic`` is
    used), so a full-scale 60-90 s walk is measured without sleeping."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


def _stub_public_dns(monkeypatch) -> None:
    monkeypatch.setattr(fp_mod.socket, "getaddrinfo", lambda *a, **k: [
        (fp_mod.socket.AF_INET, fp_mod.socket.SOCK_STREAM, fp_mod.socket.IPPROTO_TCP,
         "", ("93.184.216.34", 443))
    ])


def _stub_slow_redirect_chain(monkeypatch, clock, *, hop_cost: float) -> list:
    """httpx stub whose every hop is an eligible public redirect that burns
    ``hop_cost`` seconds — the pathological vendor/catalog chain. Returns the
    growing list of requested URLs."""
    requested: list = []

    class _Redirect:
        is_redirect = True

        def __init__(self, hop: int):
            self.headers = {"location": f"https://example.com/hop{hop + 1}"}

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"] == fp_mod._HOP_TIMEOUT_S

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            requested.append(url)
            clock.t += hop_cost            # this hop ran to its own deadline
            return _Redirect(len(requested))

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    return requested


def test_redirect_walk_stops_on_its_whole_walk_budget(monkeypatch):
    """Full scale, deterministic (fake clock, no sleeping): a chain where EVERY
    hop burns its whole _HOP_TIMEOUT_S is exactly the shape that made the old
    per-hop-only bound (10 + 1) * 30 = 330 s against a 120 s harness ceiling.
    The walk must stop on the WHOLE-walk budget — not on _MAX_REDIRECTS — and
    say which hop it gave up on."""
    clock = _FakeClock()
    monkeypatch.setattr(fp_mod, "time", clock)
    _stub_public_dns(monkeypatch)
    requested = _stub_slow_redirect_chain(
        monkeypatch, clock, hop_cost=fp_mod._HOP_TIMEOUT_S)

    try:
        fp_mod._http_get("https://example.com/hop0")
        assert False, "an all-slow redirect chain must not walk to completion"
    except fp_mod.RedirectBudgetError as exc:
        message = str(exc)

    assert len(requested) < fp_mod._MAX_REDIRECTS + 1, requested
    assert f"hop {len(requested)}" in message, message
    assert "redirect budget" in message, message
    # Measured walk + the ONE uncancellable getaddrinfo the budget check cannot
    # clamp is this tool's worst case, and it must fit inside the declared one.
    assert clock.t <= fp_mod._WALK_BUDGET_S + fp_mod._HOP_TIMEOUT_S, clock.t
    assert clock.t + fp_mod._DNS_STALL_RESERVE_S <= fp_mod.FETCH_WORST_CASE_S


def test_fetch_page_reports_a_too_slow_redirect_chain_actionably(monkeypatch):
    """The tool boundary must return the §6 degradation shape naming the hop it
    quit on, so the model has a remedy — never leave the harness to abandon the
    call with "still running in the background, may still mutate state"."""
    clock = _FakeClock()
    monkeypatch.setattr(fp_mod, "time", clock)
    monkeypatch.setattr(fp_mod, "is_offline", lambda: False)
    _stub_public_dns(monkeypatch)
    _stub_slow_redirect_chain(monkeypatch, clock, hop_cost=fp_mod._HOP_TIMEOUT_S)

    with tempfile.TemporaryDirectory() as td:
        out = fp_mod.fetch_page("https://example.com/hop0", cache=_tmp_cache(td))
    assert out["ok"] is False and out["reason"] == "network", out
    assert "too slow" in out["hint"] and "redirect budget" in out["hint"], out
    assert "redirect hop" in out["hint"], out


def test_fetch_page_worst_case_fits_research_fetch_page_harness_ceiling():
    """Arithmetic lockstep with the harness: 60 s walk budget + 15 s in-flight
    hop + 15 s uncancellable DNS stall = 90 s, under the outer deadline
    agent_turn._dispatch_tool takes from tool_guidance."""
    from services.agent.tool_guidance import research as research_guidance

    ceiling = research_guidance.ENTRIES["research_fetch_page"]["timeout_s"]
    assert fp_mod.FETCH_WORST_CASE_S == (
        fp_mod._WALK_BUDGET_S + fp_mod._HOP_TIMEOUT_S + fp_mod._DNS_STALL_RESERVE_S)
    assert fp_mod.FETCH_WORST_CASE_S == 90.0
    assert fp_mod.FETCH_WORST_CASE_S < ceiling, (
        f"fetch_page worst case {fp_mod.FETCH_WORST_CASE_S}s does not fit "
        f"research_fetch_page timeout_s={ceiling}s")
    # The per-hop bound ALONE is still 11 * 15 = 165 s > the ceiling, so the
    # whole-walk budget is the load-bearing part: lowering _MAX_REDIRECTS or the
    # hop timeout without it would not make this tool fit.
    assert (fp_mod._MAX_REDIRECTS + 1) * fp_mod._HOP_TIMEOUT_S > ceiling


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
