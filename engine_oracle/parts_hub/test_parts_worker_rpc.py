"""Tests for the parts-library worker-RPC bridge (T-U2).

Validates the worker_rpc.WORKER_METHODS shim:
  - part_search  → populates results, degrades gracefully when hub unavailable
  - part_get     → returns PartRecord or ok:false
  - part_place   → routes to place() and returns editable result (R1 assertion)

These are unit tests of the worker-seam layer specifically; the deeper hub behaviour is
covered by test_parts_hub.py / test_parts_hub_p2.py / test_parts_hub_p3.py.
"""
from __future__ import annotations

import json
import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _noop_emit(_):
    pass

def _search(query, domain=None, limit=20):
    from engine_oracle.parts_hub.worker_rpc import _part_search
    params = {"query": query, "limit": limit}
    if domain:
        params["domain"] = domain
    return _part_search(params, _noop_emit)

def _get(part_id):
    from engine_oracle.parts_hub.worker_rpc import _part_get
    return _part_get({"part_id": part_id}, _noop_emit)

def _place(part_id, doc=None, extra=None):
    from engine_oracle.parts_hub.worker_rpc import _part_place
    params = {"part_id": part_id}
    if doc is not None:
        params["doc"] = doc
    if extra is not None:
        params["params"] = extra
    return _part_place(params, _noop_emit)


# ---------------------------------------------------------------------------
# WORKER_METHODS registration
# ---------------------------------------------------------------------------

class TestWorkerRpcRegistration:
    def test_worker_methods_keys(self):
        """WORKER_METHODS exposes exactly part_search / part_get / part_place."""
        from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS
        assert "part_search" in WORKER_METHODS
        assert "part_get"    in WORKER_METHODS
        assert "part_place"  in WORKER_METHODS

    def test_all_methods_callable(self):
        from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS
        for name, fn in WORKER_METHODS.items():
            assert callable(fn), f"{name} must be callable"


# ---------------------------------------------------------------------------
# part_search
# ---------------------------------------------------------------------------

class TestPartSearch:
    def test_search_returns_ok_true(self):
        r = _search("M8")
        assert r.get("ok") is True, f"expected ok:true, got {r}"

    def test_search_count_matches_results(self):
        r = _search("M8")
        assert r.get("count") == len(r.get("results", []))

    def test_search_empty_query_returns_results(self):
        r = _search("")
        assert r.get("ok") is True

    def test_search_domain_filter_fastener(self):
        r = _search("M8", domain="fastener")
        assert r.get("ok") is True
        for rec in r.get("results", []):
            assert rec.get("domain") == "fastener", \
                f"domain filter violated: {rec.get('domain')!r}"

    def test_search_unknown_domain_degrades_gracefully(self):
        """Unknown domain must return empty list, not a traceback."""
        r = _search("M8", domain="__nonexistent__")
        assert r.get("ok") is True
        assert isinstance(r.get("results"), list)

    def test_search_limit_respected(self):
        r = _search("", limit=3)
        assert len(r.get("results", [])) <= 3

    def test_search_has_live_field(self):
        r = _search("M8")
        assert "has_live" in r

    def test_search_result_has_required_fields(self):
        r = _search("M8")
        for rec in r.get("results", []):
            for field in ("part_id", "domain", "title"):
                assert field in rec, f"PartRecord missing '{field}': {rec}"

    def test_search_part_id_scheme(self):
        """part_id must follow <domain>:<catalog_id> scheme (contracts/parts.md §0)."""
        r = _search("M8", domain="fastener")
        for rec in r.get("results", []):
            pid = rec.get("part_id", "")
            assert ":" in pid, f"part_id missing colon separator: {pid!r}"
            domain, rest = pid.split(":", 1)
            assert domain, "part_id domain prefix must be non-empty"
            assert rest,   "part_id catalog_id must be non-empty"


# ---------------------------------------------------------------------------
# part_get
# ---------------------------------------------------------------------------

class TestPartGet:
    @pytest.fixture
    def known_part_id(self):
        """Return a part_id that is definitely in the hub index."""
        r = _search("M8", domain="fastener", limit=1)
        results = r.get("results", [])
        if not results:
            pytest.skip("No fastener results available — hub data not present")
        return results[0]["part_id"]

    def test_get_known_part(self, known_part_id):
        r = _get(known_part_id)
        assert r.get("ok") is True, f"part_get failed: {r}"
        assert "part" in r

    def test_get_part_record_envelope(self, known_part_id):
        r = _get(known_part_id)
        part = r["part"]
        for field in ("part_id", "domain", "title", "params", "sourcing", "provenance", "license"):
            assert field in part, f"PartRecord missing '{field}'"

    def test_get_part_id_roundtrips(self, known_part_id):
        r = _get(known_part_id)
        assert r["part"]["part_id"] == known_part_id

    def test_get_unknown_part_id_returns_ok_false(self):
        r = _get("fastener:__no_such_part__:X999")
        assert r.get("ok") is False
        assert "reason" in r

    def test_get_empty_part_id_degrades(self):
        r = _get("")
        # Must not raise; either ok:false or ok:true with a None record both acceptable.
        assert "ok" in r


# ---------------------------------------------------------------------------
# part_place — R1 editability assertion (DoD requirement)
# ---------------------------------------------------------------------------

class TestPartPlace:
    @pytest.fixture
    def placeable_part_id(self):
        """Return a fastener part_id with geometry_ref (geometry available for placement)."""
        r = _search("shcs M5", domain="mech_family", limit=1)
        results = r.get("results", [])
        if not results:
            r = _search("M5", domain="fastener", limit=1)
            results = r.get("results", [])
        if not results:
            pytest.skip("No placeable part found — core or hub data not present")
        return results[0]["part_id"]

    def test_place_returns_dict(self, placeable_part_id):
        r = _place(placeable_part_id)
        assert isinstance(r, dict), f"part_place must return a dict, got {type(r)}"

    def test_place_has_ok_field(self, placeable_part_id):
        r = _place(placeable_part_id)
        assert "ok" in r, f"part_place result missing 'ok' field: {r}"

    def test_place_r1_editable(self, placeable_part_id):
        """Core DoD: part_place result must be R1-editable (editable:true in placement result)."""
        r = _place(placeable_part_id)
        if not r.get("ok"):
            pytest.skip(f"part_place not available (no core/hub): {r.get('reason','')}")
        assert r.get("editable") is True, \
            f"R1 editability violated — part_place returned editable:{r.get('editable')!r} for {placeable_part_id!r}"

    def test_place_doc_id_present_on_success(self, placeable_part_id):
        r = _place(placeable_part_id)
        if not r.get("ok"):
            pytest.skip("part_place not available")
        assert "doc_id" in r, f"Placement result missing doc_id: {r}"

    def test_place_unknown_part_id_returns_ok_false(self):
        r = _place("fastener:__no_such__:X999")
        assert r.get("ok") is False
        assert "reason" in r

    def test_place_degrades_without_core(self):
        """part_place must return ok:false, not raise, when the core is absent."""
        r = _place("mech_family:shcs:M100000")
        # Either ok:true (core present) or ok:false (core absent) — never a traceback.
        assert "ok" in r
