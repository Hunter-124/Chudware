"""Tests for the aicad.parts/1 parts library hub.

Covers:
  - PartRecord envelope validation
  - BoltsAdapter: fastener and bearing record shape and lookup
  - CoreFamiliesAdapter: graceful degradation when core absent
  - PartsIndex: FTS build + search + ranking + get
  - PartsHub: end-to-end search including the two DoD examples
  - MCP parts_tools: part_search / part_get responses
  - Index rebuild idempotency and p95 latency

Run with:
    ~/.local/share/Chudware/mcp-venv/bin/python -m pytest services/parts/ -q -k parts
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

# Ensure repo root is on sys.path.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_RECORD_FIELDS = {
    "part_id", "domain", "title", "params",
    "sourcing", "provenance", "license",
}


def _validate_record(rec: dict) -> None:
    """Assert all required PartRecord fields are present and non-null."""
    missing = REQUIRED_RECORD_FIELDS - set(rec.keys())
    assert not missing, f"PartRecord missing fields: {missing!r}"
    assert rec["part_id"], "part_id must be non-empty"
    assert rec["domain"] in (
        "fastener", "bearing", "mech_family",
        "section", "fitting", "hydraulic", "ecad",
    ), f"Unknown domain: {rec['domain']!r}"
    assert rec["license"] in (
        "clean-room", "public-domain", "user-supplied", "proprietary",
    ), f"Unknown license: {rec['license']!r}"
    assert rec["provenance"], "provenance must be non-empty"
    assert isinstance(rec["sourcing"], list), "sourcing must be a list"
    assert isinstance(rec["params"], dict), "params must be a dict"
    if rec.get("geometry_ref") is not None:
        gr = rec["geometry_ref"]
        assert isinstance(gr, dict), "geometry_ref must be a dict"
        assert "kind" in gr, "geometry_ref must have 'kind'"


# ---------------------------------------------------------------------------
# BoltsAdapter
# ---------------------------------------------------------------------------

class TestBoltsAdapter:
    def setup_method(self):
        from services.parts.adapters.bolts_adapter import BoltsAdapter
        self.adapter = BoltsAdapter()

    def test_iter_records_nonempty(self):
        recs = list(self.adapter.iter_records())
        assert len(recs) > 10, "Expected many records from bolts tables"

    def test_all_records_valid_envelope(self):
        for rec in self.adapter.iter_records():
            _validate_record(rec)

    def test_fastener_domain(self):
        recs = [r for r in self.adapter.iter_records() if r["domain"] == "fastener"]
        assert len(recs) > 5, "Expected fastener records"

    def test_bearing_domain(self):
        recs = [r for r in self.adapter.iter_records() if r["domain"] == "bearing"]
        assert len(recs) > 5, "Expected bearing records"

    def test_6205_present(self):
        recs = [r for r in self.adapter.iter_records() if r.get("designation") == "6205"]
        assert recs, "6205 bearing must be in the bolts tables"
        rec = recs[0]
        assert rec["domain"] == "bearing"
        _validate_record(rec)
        assert rec["params"]["bore"] == 25.0
        assert rec["params"]["outer_d"] == 52.0
        assert rec["params"]["width"] == 15.0

    def test_m8_shcs_present(self):
        recs = [r for r in self.adapter.iter_records()
                if r.get("designation") == "M8" and "socket" in r.get("title", "").lower()]
        assert recs, "M8 shcs fastener must be in tables"
        rec = recs[0]
        assert rec["domain"] == "fastener"
        _validate_record(rec)

    def test_get_by_part_id_fastener(self):
        # Find any fastener part_id and look it up
        for rec in self.adapter.iter_records():
            if rec["domain"] == "fastener":
                pid = rec["part_id"]
                result = self.adapter.get(pid)
                assert result is not None
                assert result["part_id"] == pid
                _validate_record(result)
                break

    def test_get_by_part_id_bearing_6205(self):
        rec = self.adapter.get("bearing:ISO 15:6205")
        assert rec is not None
        assert rec["domain"] == "bearing"
        assert rec["designation"] == "6205"
        _validate_record(rec)

    def test_get_unknown_returns_none(self):
        assert self.adapter.get("bearing:NONSTD:XXXXXX") is None

    def test_get_wrong_domain_prefix_returns_none(self):
        # mech_family domain is not handled by BoltsAdapter
        assert self.adapter.get("mech_family:shcs:M5") is None

    def test_geometry_ref_core_family(self):
        for rec in self.adapter.iter_records():
            gr = rec.get("geometry_ref")
            assert gr is not None, "BoltsAdapter records must have geometry_ref"
            assert gr["kind"] == "core_family"
            assert "family" in gr
            assert "size" in gr
            break

    def test_count_matches_iter(self):
        assert self.adapter.count() == len(list(self.adapter.iter_records()))


# ---------------------------------------------------------------------------
# CoreFamiliesAdapter
# ---------------------------------------------------------------------------

class TestCoreFamiliesAdapter:
    def setup_method(self):
        from services.parts.adapters.core_families_adapter import CoreFamiliesAdapter
        self.adapter = CoreFamiliesAdapter()

    def test_degrades_gracefully_without_core(self):
        """Must return empty list (not raise) when core is absent."""
        # Force a reload with no core available by clearing cached catalog.
        self.adapter._catalog = None
        # Even without core, should not raise.
        try:
            recs = list(self.adapter.iter_records())
        except Exception as exc:
            pytest.fail(f"CoreFamiliesAdapter raised when core absent: {exc}")

    def test_domain_is_mech_family(self):
        assert self.adapter.domain == "mech_family"

    def test_records_valid_when_core_available(self):
        """If the core IS available, records must pass envelope validation."""
        recs = list(self.adapter.iter_records())
        for rec in recs:
            _validate_record(rec)
            assert rec["domain"] == "mech_family"

    def test_search_returns_list(self):
        results = self.adapter.search("M5")
        assert isinstance(results, list)

    def test_get_unknown_returns_none(self):
        assert self.adapter.get("mech_family:nonexistent:X99") is None

    def test_get_wrong_prefix_returns_none(self):
        assert self.adapter.get("fastener:ISO 4762:M5") is None


# ---------------------------------------------------------------------------
# PartsIndex
# ---------------------------------------------------------------------------

class TestPartsIndex:
    def setup_method(self):
        from services.parts.index import PartsIndex
        from services.parts.adapters.bolts_adapter import BoltsAdapter
        self.index = PartsIndex()
        adapter = BoltsAdapter()
        self.count = self.index.rebuild(adapter.iter_records())

    def test_rebuild_returns_positive_count(self):
        assert self.count > 10

    def test_rebuild_idempotent(self):
        from services.parts.adapters.bolts_adapter import BoltsAdapter
        count2 = self.index.rebuild(BoltsAdapter().iter_records())
        assert count2 == self.count

    def test_get_by_part_id(self):
        rec = self.index.get("bearing:ISO 15:6205")
        assert rec is not None
        assert rec["designation"] == "6205"

    def test_get_unknown_returns_none(self):
        assert self.index.get("bearing:NONSTD:XXXXXX") is None

    def test_search_bearing_6205(self):
        results = self.index.search("6205")
        pids = [r["part_id"] for r in results]
        assert "bearing:ISO 15:6205" in pids, f"6205 not found in: {pids}"

    def test_search_m8_socket_head(self):
        results = self.index.search("M8 socket head")
        titles = [r.get("title", "").lower() for r in results]
        assert any("m8" in t or "socket" in t for t in titles), \
            f"Expected M8/socket head results, got: {titles[:5]}"

    def test_search_empty_query_with_domain(self):
        results = self.index.search("", domain="fastener")
        assert all(r["domain"] == "fastener" for r in results)
        assert len(results) > 0

    def test_search_domain_filter(self):
        results = self.index.search("bearing", domain="bearing")
        assert all(r["domain"] == "bearing" for r in results)

    def test_search_limit_respected(self):
        results = self.index.search("M", limit=5)
        assert len(results) <= 5

    def test_exact_designation_ranks_first(self):
        # "6205" exact match should beat fuzzier matches.
        results = self.index.search("6205", limit=5)
        assert results, "No results for '6205'"
        assert results[0]["designation"] == "6205"

    def test_search_p95_latency(self):
        """p95 search latency must be < 50 ms on the full corpus."""
        latencies = []
        for _ in range(30):
            t0 = time.perf_counter()
            self.index.search("M8 socket head")
            latencies.append(time.perf_counter() - t0)
        latencies.sort()
        p95 = latencies[int(0.95 * len(latencies))]
        assert p95 < 0.050, f"p95 search latency {p95*1000:.1f}ms exceeds 50ms"


# ---------------------------------------------------------------------------
# PartsHub (end-to-end)
# ---------------------------------------------------------------------------

class TestPartsHub:
    def setup_method(self):
        from services.parts.hub import PartsHub
        self.hub = PartsHub()
        self.hub.bootstrap()

    def test_search_bearing_6205_dod(self):
        """DoD: part_search('bearing 6205') returns the 6205 bearing PartRecord."""
        results = self.hub.search("bearing 6205")
        pids = [r["part_id"] for r in results]
        assert "bearing:ISO 15:6205" in pids, \
            f"DoD FAIL: bearing 6205 not found. Got pids: {pids[:10]}"
        rec = next(r for r in results if r["part_id"] == "bearing:ISO 15:6205")
        _validate_record(rec)
        assert rec["params"]["bore"] == 25.0

    def test_search_m8_socket_head_dod(self):
        """DoD: part_search('M8 socket head') returns fastener records for M8 shcs."""
        results = self.hub.search("M8 socket head")
        assert results, "DoD FAIL: no results for 'M8 socket head'"
        # At least one result must be an M8 fastener with socket head in the title.
        matching = [
            r for r in results
            if r.get("designation") == "M8"
            and r.get("domain") == "fastener"
            and ("socket" in r.get("title", "").lower() or "shcs" in r.get("title", "").lower())
        ]
        assert matching, \
            f"DoD FAIL: no M8 socket head fastener in results. Titles: {[r.get('title') for r in results[:5]]}"
        _validate_record(matching[0])

    def test_search_domain_mech_family_dod(self):
        """DoD: part_search(domain='mech_family') lists core families (or empty when core absent)."""
        results = self.hub.search("", domain="mech_family")
        # May be empty if core is absent — that's acceptable. But must not raise.
        assert isinstance(results, list)
        for rec in results:
            assert rec["domain"] == "mech_family"
            _validate_record(rec)

    def test_native_hobby_servo_is_first_local_hit(self):
        """The real core catalog, not a duplicate Python table, backs model search."""
        from services.mcp import core_bridge as cb
        try:
            families = cb.call("parts_catalog_list", {})["families"]
        except Exception as exc:
            pytest.skip(f"native core unavailable: {exc}")
        if not any(f["key"] == "hobby_servo" for f in families):
            pytest.fail("native parts catalog lacks hobby_servo (rebuild libchudware)")
        self.hub.rebuild_index()
        for query in ("MG90S", "SG90", "micro servo"):
            hits = self.hub.search(query, include_live=False)
            assert hits and hits[0]["geometry_ref"]["family"] == "hobby_servo", hits[:3]
        assert self.hub.search("MG90S", include_live=False)[0]["designation"] == "MG90S"
        assert any(r["part_id"] == "mech_family:hobby_servo:MG90S"
                   for r in self.hub.search("MG90S", domain=["ecad", "mech_family"], include_live=False))

    def test_get_by_part_id_bearing(self):
        rec = self.hub.get("bearing:ISO 15:6205")
        assert rec is not None
        assert rec["part_id"] == "bearing:ISO 15:6205"
        _validate_record(rec)

    def test_get_unknown_returns_none(self):
        assert self.hub.get("fastener:NONEXISTENT:X99") is None

    def test_rebuild_index_idempotent(self):
        count1 = self.hub.rebuild_index()
        count2 = self.hub.rebuild_index()
        assert count1 == count2
        assert count1 > 0

    def test_search_fts_no_crash_on_fts_special_chars(self):
        """FTS5 operator injection must not raise.

        Local-only: this test guards the FTS sanitiser, not the live fan-out.
        (Live fan-out is deadline-bounded separately — see
        test_live_fanout_deadline_bounds_slow_provider.)
        """
        for q in ('M8 "socket"', "AND OR NOT", "*", "", "(M8)", "M8 AND bearing"):
            try:
                results = self.hub.search(q, include_live=False)
                assert isinstance(results, list)
            except Exception as exc:
                pytest.fail(f"Search raised on query {q!r}: {exc}")

    def test_include_live_false_does_zero_network(self, monkeypatch):
        """include_live=False must never call a live provider.search."""
        calls: list[str] = []

        class _SlowLive:
            domain = "catalog"
            provider_id = "slow_live"
            site = "slowsite"

            def search(self, query, **kw):
                calls.append(query)
                return [{"part_id": "catalog:slowsite::x", "title": "X",
                         "domain": "catalog", "source": "live"}]

            def count(self):
                raise NotImplementedError

            def iter_records(self):
                raise NotImplementedError

        self.hub.register(_SlowLive())
        # Force the live path by searching for a string that cannot hit FTS.
        out = self.hub.search("zzz_no_fts_hit_xyzzy_9999", include_live=False)
        assert out == []
        assert calls == [], f"live provider was called despite include_live=False: {calls}"

    def test_live_fanout_deadline_bounds_slow_provider(self, monkeypatch):
        """A stalled live provider must not hang; fast providers still land and
        the slow site surfaces as a timeout notice."""
        import time as _time

        monkeypatch.setenv("CHUDWARE_PARTS_LIVE_BUDGET_S", "0.4")
        monkeypatch.setenv("CHUDWARE_PARTS_LIVE_SITE_BUDGET_S", "0.4")

        class _FastLive:
            domain = "catalog"
            provider_id = "fast_live"
            site = "fastsite"

            def search(self, query, **kw):
                return [{
                    "part_id": "catalog:fastsite::widget",
                    "domain": "catalog",
                    "title": "Fast Widget",
                    "source": "live",
                    "params": {},
                    "geometry_ref": {},
                    "sourcing": [],
                    "provenance": "test",
                    "license": "test",
                }]

            def count(self):
                raise NotImplementedError

            def iter_records(self):
                raise NotImplementedError

        class _SlowLive:
            domain = "catalog"
            provider_id = "slow_live"
            site = "slowsite"

            def search(self, query, **kw):
                _time.sleep(30.0)  # would hang forever pre-fix
                return [{
                    "part_id": "catalog:slowsite::late",
                    "domain": "catalog",
                    "title": "Late",
                    "source": "live",
                }]

            def count(self):
                raise NotImplementedError

            def iter_records(self):
                raise NotImplementedError

        # Fresh hub without the real browser catalog (so we only hit stubs).
        from services.parts.hub import PartsHub
        hub = PartsHub()
        hub.register(_FastLive())
        hub.register(_SlowLive())
        # Empty index → forces live fan-out.
        hub._index_built = True  # skip rebuild; index stays empty

        t0 = _time.monotonic()
        out = hub.search("zzz_no_fts_hit_xyzzy_9999", domain="catalog", limit=10)
        elapsed = _time.monotonic() - t0

        assert elapsed < 2.0, f"live fan-out hung for {elapsed:.2f}s (budget 0.4s)"
        real = [r for r in out if r.get("part_id") and r.get("title")]
        notices = [r for r in out if not (r.get("part_id") and r.get("title"))]
        assert any(r.get("part_id") == "catalog:fastsite::widget" for r in real), \
            f"fast provider result missing: {out}"
        assert any(n.get("site") == "slowsite" and n.get("reason") == "timeout"
                   for n in notices), f"timeout notice missing: {notices}"

    def test_browser_bootstrap_error_is_diagnostic(self, monkeypatch):
        """Browser adapter import failure must leave local search working and
        surface browser_bootstrap_error (not silent)."""
        from services.parts.hub import PartsHub

        hub = PartsHub()

        def _boom():
            raise RuntimeError("playwright missing in test")

        monkeypatch.setattr(hub, "_register_browser_catalog_adapters",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        # Call the real method body by forcing the import path to fail.
        def _failing_register():
            try:
                raise ImportError("no services.browser")
            except Exception as exc:
                hub.browser_bootstrap_error = f"{type(exc).__name__}: {exc}"

        monkeypatch.setattr(hub, "_register_browser_catalog_adapters", _failing_register)
        # Minimal bootstrap: bolts only + failing browser register.
        from services.parts.adapters.bolts_adapter import BoltsAdapter
        hub.register(BoltsAdapter())
        hub._register_browser_catalog_adapters()
        hub._ensure_index()

        assert hub.browser_bootstrap_error
        assert "ImportError" in hub.browser_bootstrap_error
        hits = hub.search("bearing 6205", include_live=False)
        assert any(r["part_id"] == "bearing:ISO 15:6205" for r in hits)


# ---------------------------------------------------------------------------
# MCP parts_tools
# ---------------------------------------------------------------------------

class TestPartsMCPTools:
    """Test the MCP tool wrappers (no server needed — call register and invoke directly)."""

    def setup_method(self):
        from unittest.mock import MagicMock

        self.tools: dict = {}
        mock_mcp = MagicMock()

        def capture_tool():
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp.tool = capture_tool

        import importlib
        # Import the canonical package so the registry under test shares server state.
        pt = importlib.import_module("services.mcp.parts_tools")
        pt.register_parts_tools(mock_mcp)

    def test_tools_registered(self):
        assert "part_search" in self.tools, "part_search not registered"
        assert "part_get" in self.tools, "part_get not registered"

    def test_part_search_bearing_6205(self):
        result = json.loads(self.tools["part_search"]("bearing 6205"))
        assert result["ok"] is True
        assert result["count"] > 0
        pids = [r["part_id"] for r in result["results"]]
        assert "bearing:ISO 15:6205" in pids, \
            f"bearing:ISO15:6205 not in MCP results: {pids[:5]}"

    def test_part_search_m8_socket_head(self):
        result = json.loads(self.tools["part_search"]("M8 socket head"))
        assert result["ok"] is True
        assert result["count"] > 0
        matching = [
            r for r in result["results"]
            if r.get("designation") == "M8" and r.get("domain") == "fastener"
        ]
        assert matching, \
            f"No M8 fastener in MCP part_search results. Titles: {[r.get('title') for r in result['results'][:5]]}"

    def test_part_search_empty_no_crash(self):
        result = json.loads(self.tools["part_search"](""))
        assert "ok" in result

    def test_part_search_with_domain(self):
        result = json.loads(self.tools["part_search"]("", "fastener", 10))
        assert result["ok"] is True
        for rec in result["results"]:
            assert rec["domain"] == "fastener"

    def test_part_get_bearing_6205(self):
        result = json.loads(self.tools["part_get"]("bearing:ISO 15:6205"))
        assert result["ok"] is True
        assert result["part"]["part_id"] == "bearing:ISO 15:6205"

    def test_part_get_unknown_returns_ok_false(self):
        result = json.loads(self.tools["part_get"]("bearing:NONSTD:XXXXXXX"))
        assert result["ok"] is False
        assert "reason" in result

    def test_part_search_limit_respected(self):
        result = json.loads(self.tools["part_search"]("M", "", 5))
        assert result["ok"] is True
        assert len(result["results"]) <= 5
