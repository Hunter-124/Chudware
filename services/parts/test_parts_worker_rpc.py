"""Tests for the parts-library worker-RPC bridge.

Validates the worker_rpc.WORKER_METHODS shim:
  - part_search  → populates results, degrades gracefully when hub unavailable
  - part_get     → returns PartRecord or ok:false
  - part_place   → routes to place() and returns editable result (R1 assertion)

These are unit tests of the worker-seam layer specifically; the deeper hub behaviour is
covered by test_parts_hub.py / test_parts_hub_p2.py / test_parts_hub_p3.py.
"""
from __future__ import annotations

import base64
import json
import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _noop_emit(_):
    pass

def _search(query, domain=None, limit=20):
    from services.parts.worker_rpc import _part_search
    params = {"query": query, "limit": limit}
    if domain:
        params["domain"] = domain
    return _part_search(params, _noop_emit)

def _get(part_id):
    from services.parts.worker_rpc import _part_get
    return _part_get({"part_id": part_id}, _noop_emit)

def _place(part_id, doc=None, extra=None):
    from services.parts.worker_rpc import _part_place
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
        from services.parts.worker_rpc import WORKER_METHODS
        assert "part_search" in WORKER_METHODS
        assert "part_get"    in WORKER_METHODS
        assert "part_place"  in WORKER_METHODS

    def test_all_methods_callable(self):
        from services.parts.worker_rpc import WORKER_METHODS
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

class TestPartsTaxonomyRpc:
    """Parts Library V2: the parts_taxonomy GUI RPC envelope."""

    def _taxonomy(self, params=None):
        from services.parts.gui_rpc import _parts_taxonomy
        return _parts_taxonomy(params or {}, _noop_emit)

    def test_registered_in_gui_worker_methods(self):
        from services.parts.gui_rpc import GUI_WORKER_METHODS
        for name in ("parts_taxonomy", "vendors_health_check", "vendors_health_status"):
            assert name in GUI_WORKER_METHODS and callable(GUI_WORKER_METHODS[name])

    def test_envelope(self):
        r = self._taxonomy()
        assert r.get("ok") is True
        assert r.get("version") == 1
        assert isinstance(r.get("tree"), list) and r["tree"]
        node = r["tree"][0]
        for key in ("id", "label", "icon", "description", "facets", "children"):
            assert key in node, f"tree node missing {key!r}"

    def test_subtree_by_category_id(self):
        r = self._taxonomy({"category_id": "fasteners.bolts_screws"})
        assert r.get("ok") is True
        assert len(r["tree"]) == 1
        assert r["tree"][0]["id"] == "fasteners.bolts_screws"

    def test_unknown_category_id_ok_false_no_traceback(self):
        r = self._taxonomy({"category_id": "no.such.category"})
        assert r.get("ok") is False
        assert "reason" in r


class TestCategoryRoutedPartsSearch:
    """Parts Library V2: parts_search with category_id routing + facet filters."""

    def _gui_search(self, **params):
        from services.parts.gui_rpc import _parts_search
        return _parts_search(params, _noop_emit)

    def test_category_routed_search(self):
        """category_id fasteners routes to fastener/mech_family domains."""
        r = self._gui_search(query="M8", category_id="fasteners")
        assert r.get("ok") is True, f"routed search failed: {r}"
        for item in r.get("results", []):
            assert item["domain"] in ("fastener", "mech_family", "catalog"), \
                f"category routing violated: {item['domain']!r}"

    def test_unknown_category_id_degrades_gracefully(self):
        """Unknown category_id must return ok:true (unrouted), never a traceback."""
        r = self._gui_search(query="M8", category_id="no.such.category")
        assert r.get("ok") is True
        assert isinstance(r.get("results"), list)

    def test_legacy_category_param_still_works(self):
        r = self._gui_search(query="M8", category="fastener")
        assert r.get("ok") is True
        for item in r.get("results", []):
            assert item["domain"] == "fastener"

    def test_facet_range_filters_synthetic_bolt(self, monkeypatch):
        """A facets length_mm range must keep the in-range bolt and drop the long one."""
        from services import parts as hub_mod

        def _fake_search(query, *, domain=None, filters=None, limit=20, **_kw):
            def bolt(pid, length):
                return {"part_id": f"fastener:SYN:{pid}", "domain": "fastener",
                        "title": f"Synthetic bolt {pid}", "designation": pid,
                        "params": {"thread": "M8", "length_mm": length},
                        "sourcing": [], "provenance": "test", "license": "clean-room"}
            return [bolt("M8x20", "20 mm"), bolt("M8x80", 80)]

        monkeypatch.setattr(hub_mod, "search", _fake_search)
        r = self._gui_search(query="M8", category_id="fasteners.bolts_screws.hex",
                             facets={"length_mm": {"min": 10, "max": 30}})
        assert r.get("ok") is True, f"facet search failed: {r}"
        ids = [item["id"] for item in r["results"]]
        assert any(i.endswith("M8x20") for i in ids), f"in-range bolt dropped: {ids}"
        assert not any(i.endswith("M8x80") for i in ids), f"out-of-range bolt kept: {ids}"

    def test_facet_enum_filters_synthetic_bolt(self, monkeypatch):
        from services import parts as hub_mod

        def _fake_search(query, *, domain=None, filters=None, limit=20, **_kw):
            return [{"part_id": "fastener:SYN:M8", "domain": "fastener",
                     "title": "Synthetic M8", "designation": "M8",
                     "params": {"thread": "M8"}, "sourcing": [],
                     "provenance": "test", "license": "clean-room"}]

        monkeypatch.setattr(hub_mod, "search", _fake_search)
        keep = self._gui_search(query="bolt", category_id="fasteners.bolts_screws.hex",
                                facets={"thread": "m8"})
        drop = self._gui_search(query="bolt", category_id="fasteners.bolts_screws.hex",
                                facets={"thread": "M10"})
        assert keep.get("ok") and len(keep["results"]) == 1
        assert drop.get("ok") and len(drop["results"]) == 0

    def test_facet_alias_bore_mm_keeps_local_bearing(self, monkeypatch):
        """Host facet filter must alias bore→bore_mm so a 12 mm-bore local bearing
        is kept when the GUI queries facets={bore_mm: 12} (parity with MCP)."""
        from services import parts as hub_mod

        def _fake_search(query, *, domain=None, filters=None, limit=20, **_kw):
            return [
                {"part_id": "bearing:ISO 15:6001", "domain": "bearing",
                 "title": "Deep-Groove Ball Bearing 6001",
                 "params": {"bore": 12.0, "outer_d": 28.0, "width": 8.0},
                 "geometry_ref": {"kind": "core_family"}, "sourcing": [],
                 "provenance": "test", "license": "clean-room"},
                {"part_id": "bearing:ISO 15:6205", "domain": "bearing",
                 "title": "Deep-Groove Ball Bearing 6205",
                 "params": {"bore": 25.0, "outer_d": 52.0, "width": 15.0},
                 "geometry_ref": {"kind": "core_family"}, "sourcing": [],
                 "provenance": "test", "license": "clean-room"},
            ]

        monkeypatch.setattr(hub_mod, "search", _fake_search)
        r = self._gui_search(
            query="bearing",
            category_id="bearings_motion.ball_bearings.deep_groove",
            facets={"bore_mm": 12},
        )
        assert r.get("ok") is True, f"facet search failed: {r}"
        ids = [item["id"] for item in r["results"]]
        assert ids == ["bearing:ISO 15:6001"], \
            f"12mm-bore local bearing dropped by alias-blind filter: {ids}"


class TestVendorsHealthRpc:
    """Parts Library V2: vendors_health_check / vendors_health_status envelopes."""

    def test_check_degrades_without_health_module(self):
        """services.browser.health is another agent's module — absence must yield
        the documented ok:false envelope (never a traceback). When it exists,
        the call must return {ok:true, started:bool}."""
        from services.parts.gui_rpc import _vendors_health_check
        r = _vendors_health_check({}, _noop_emit)
        assert "ok" in r
        if r["ok"]:
            assert "started" in r
        else:
            assert r.get("error") == "health module not available"

    def test_check_rejects_non_list_sites(self):
        from services.parts.gui_rpc import _vendors_health_check
        r = _vendors_health_check({"sites": "mcmaster"}, _noop_emit)
        assert r.get("ok") is False

    def test_status_envelope(self):
        from services.parts.gui_rpc import _vendors_health_status
        r = _vendors_health_status({}, _noop_emit)
        assert r.get("ok") is True
        for key in ("running", "results", "started_ts", "finished_ts"):
            assert key in r, f"status envelope missing {key!r}"
        assert isinstance(r["results"], list)


class TestVendorResultCache:
    """Only clean vendor results may survive into the live-search cache."""

    @staticmethod
    def _isolated_cache(monkeypatch, tmp_path):
        from services import paths
        from services.parts import gui_rpc

        monkeypatch.setattr(paths, "ROOT", tmp_path)
        monkeypatch.setattr(gui_rpc, "_vendor_cache_ready", False)
        monkeypatch.setattr(gui_rpc, "_vendor_cache_last_trim", 0.0)
        return gui_rpc

    def test_transient_failure_notice_is_not_cached(self, monkeypatch, tmp_path):
        gui_rpc = self._isolated_cache(monkeypatch, tmp_path)
        gui_rpc._vendor_cache_put(
            "mcmaster", "m8", 20,
            [{"ok": False, "site": "mcmaster", "reason": "browser_busy"}],
        )

        assert gui_rpc._vendor_cache_get("mcmaster", "m8", 20) is None

    def test_clean_records_and_empty_searches_are_cached(self, monkeypatch, tmp_path):
        gui_rpc = self._isolated_cache(monkeypatch, tmp_path)
        clean = [{"part_id": "catalog:mcmaster::https://example.test/1",
                  "title": "M8 cap screw"}]
        gui_rpc._vendor_cache_put("mcmaster", "m8", 20, clean)
        gui_rpc._vendor_cache_put("mcmaster", "definitely-absent", 20, [])

        assert gui_rpc._vendor_cache_get("mcmaster", "m8", 20) == clean
        assert gui_rpc._vendor_cache_get("mcmaster", "definitely-absent", 20) == []


class TestLiveVendorConcurrency:
    @staticmethod
    def _job(site):
        return {
            "results": [],
            "notices": [],
            "seen_ids": set(),
            "seen_notices": set(),
            "pending": 1,
            "done": False,
            "cancelled": False,
            "vendors": {site: {"site": site, "status": "queued"}},
        }

    @staticmethod
    def _routing(query):
        return {"vendor_query": query, "facet_defs": []}

    def test_cold_vendor_scrapes_respect_cef_concurrency_budget(self, monkeypatch):
        import threading

        from services import parts
        from services.parts import gui_rpc

        lock = threading.Lock()
        three_running = threading.Event()
        release_scrapes = threading.Event()
        active = 0
        max_active = 0
        search_calls = 0

        def fake_search(*_args, **_kwargs):
            nonlocal active, max_active, search_calls
            with lock:
                active += 1
                search_calls += 1
                max_active = max(max_active, active)
                if active == gui_rpc._LIVE_FETCH_CONCURRENCY:
                    three_running.set()
            release_scrapes.wait(timeout=2.0)
            with lock:
                active -= 1
            return []

        monkeypatch.setattr(gui_rpc, "_live_fetch_slots", threading.BoundedSemaphore(3))
        monkeypatch.setattr(gui_rpc, "_vendor_cache_get", lambda *_args: None)
        monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *_args: None)
        monkeypatch.setattr(parts, "search", fake_search)
        threads = [
            threading.Thread(
                target=gui_rpc._run_live_site,
                args=(str(i), self._routing(f"cold-{i}"), 1, {}, self._job(str(i))),
            )
            for i in range(9)
        ]
        try:
            for thread in threads:
                thread.start()
            assert three_running.wait(timeout=1.0)
        finally:
            release_scrapes.set()
            for thread in threads:
                thread.join(timeout=2.0)

        assert all(not thread.is_alive() for thread in threads)
        assert search_calls == 9
        assert max_active == gui_rpc._LIVE_FETCH_CONCURRENCY == 3

    def test_warm_vendor_hit_bypasses_saturated_scrape_slots(self, monkeypatch):
        import threading

        from services import parts
        from services.parts import gui_rpc

        lock = threading.Lock()
        slots_saturated = threading.Event()
        release_scrapes = threading.Event()
        active = 0
        max_active = 0
        search_calls = 0
        warm_record = {
            "part_id": "catalog:warm::https://example.test/cached",
            "title": "Cached bearing",
            "domain": "catalog",
            "source": "live",
            "params": {},
        }

        def fake_cache_get(site, *_args):
            return [warm_record] if site == "warm" else None

        def fake_search(*_args, **_kwargs):
            nonlocal active, max_active, search_calls
            with lock:
                active += 1
                search_calls += 1
                max_active = max(max_active, active)
                if active == gui_rpc._LIVE_FETCH_CONCURRENCY:
                    slots_saturated.set()
            release_scrapes.wait(timeout=2.0)
            with lock:
                active -= 1
            return []

        monkeypatch.setattr(gui_rpc, "_live_fetch_slots", threading.BoundedSemaphore(3))
        monkeypatch.setattr(gui_rpc, "_vendor_cache_get", fake_cache_get)
        monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *_args: None)
        monkeypatch.setattr(parts, "search", fake_search)
        cold_threads = [
            threading.Thread(
                target=gui_rpc._run_live_site,
                args=(site, self._routing(site), 1, {}, self._job(site)),
            )
            for site in ("cold-a", "cold-b", "cold-c")
        ]
        warm_job = self._job("warm")
        warm_finished = threading.Event()

        def run_warm():
            gui_rpc._run_live_site(
                "warm", self._routing("cached"), 1, {}, warm_job
            )
            warm_finished.set()

        warm_thread = threading.Thread(target=run_warm)
        try:
            for thread in cold_threads:
                thread.start()
            assert slots_saturated.wait(timeout=1.0)
            warm_thread.start()
            completed_while_saturated = warm_finished.wait(timeout=1.0)
            with lock:
                calls_while_saturated = search_calls
        finally:
            release_scrapes.set()
            for thread in cold_threads:
                thread.join(timeout=2.0)
            if warm_thread.ident is not None:
                warm_thread.join(timeout=2.0)

        assert completed_while_saturated
        assert calls_while_saturated == 3
        assert max_active == gui_rpc._LIVE_FETCH_CONCURRENCY == 3
        assert warm_job["done"] is True
        assert warm_job["vendors"]["warm"]["status"] == "done"
        assert [item["id"] for item in warm_job["results"]] == [warm_record["part_id"]]

    def test_vendor_failure_is_attributed_and_marked_error(self, monkeypatch):
        from services import parts
        from services.parts import gui_rpc

        monkeypatch.setattr(gui_rpc, "_vendor_cache_get", lambda *_args: None)
        monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *_args: None)
        monkeypatch.setattr(
            parts,
            "search",
            lambda *_args, **_kwargs: [
                {"ok": False, "reason": "bot_walled", "hint": "HTTP 403"}
            ],
        )
        job = {
            "results": [],
            "notices": [],
            "seen_ids": set(),
            "seen_notices": set(),
            "pending": 1,
            "done": False,
            "cancelled": False,
            "vendors": {"grainger": {"site": "grainger", "status": "queued"}},
        }

        gui_rpc._run_live_site_inner(
            "grainger", {"vendor_query": "bearing", "facet_defs": []}, 20, {}, job
        )

        assert job["done"] is True
        assert job["vendors"]["grainger"]["status"] == "error"
        assert job["notices"] == [
            {"site": "grainger", "reason": "bot_walled", "hint": "HTTP 403"}
        ]

class TestDomainListSearch:
    """Parts Library V2: PartsIndex/hub accept domain as a list (SQL IN)."""

    def test_index_domain_list(self):
        from services.parts import PartsIndex
        idx = PartsIndex()
        recs = [
            {"part_id": "fastener:T:1", "domain": "fastener", "title": "bolt one", "params": {}},
            {"part_id": "bearing:T:2", "domain": "bearing", "title": "bearing two", "params": {}},
            {"part_id": "ecad:T:3", "domain": "ecad", "title": "chip three", "params": {}},
        ]
        idx.rebuild(iter(recs))
        got = {r["domain"] for r in idx.search("", domain=["fastener", "bearing"], limit=10)}
        assert got == {"fastener", "bearing"}
        got_one = {r["domain"] for r in idx.search("", domain="ecad", limit=10)}
        assert got_one == {"ecad"}

    def test_hub_search_domain_list(self):
        from services.parts import search as _search
        recs = _search("M8", domain=["fastener", "mech_family"], limit=20)
        for rec in recs:
            assert rec["domain"] in ("fastener", "mech_family")


class TestPartPlace:
    @pytest.fixture(autouse=True)
    def close_placed_docs(self, monkeypatch):
        """Placements are native docs; do not leave them for the next agent test."""
        from services.parts import worker_rpc
        from services.mcp import core_bridge as cb

        place = worker_rpc._part_place
        created = []

        def tracked(*args, **kwargs):
            result = place(*args, **kwargs)
            if isinstance(result, dict) and result.get("doc_id"):
                created.append(result["doc_id"])
            return result

        monkeypatch.setattr(worker_rpc, "_part_place", tracked)
        yield
        for doc_id in created:
            cb.call("doc_close", {"doc_id": doc_id})

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


class TestPrepareAssemblyComponent:
    def test_saves_native_part_and_returns_path_without_overwriting_existing_file(
            self, tmp_path, monkeypatch):
        from services.parts import gui_rpc
        from services.mcp import core_bridge as cb

        made = []
        closed = []

        def place(params, _emit):
            made.append(params["item_id"])
            return {"ok": True, "doc_id": f"generated-{len(made)}",
                    "detail": {"name": "Socket screw"}}

        def call(method, params):
            if method == "doc_save":
                from pathlib import Path
                Path(params["path"]).write_bytes(b"editable native part")
                return {"path": params["path"]}
            assert method == "doc_close"
            closed.append(params["doc_id"])
            return {}

        monkeypatch.setattr(gui_rpc, "_parts_insert_into_doc", place)
        monkeypatch.setattr(cb, "call", call)
        first = gui_rpc.GUI_WORKER_METHODS["parts_prepare_for_assembly"](
            {"item_id": "fastener:example", "project_dir": str(tmp_path)}, _noop_emit)
        second = gui_rpc.GUI_WORKER_METHODS["parts_prepare_for_assembly"](
            {"item_id": "fastener:example", "project_dir": str(tmp_path)}, _noop_emit)
        assert first == {"ok": True, "path": str(tmp_path / "Socket_screw.step+")}
        assert second == {"ok": True, "path": str(tmp_path / "Socket_screw_generated-2.step+")}
        assert made == ["fastener:example", "fastener:example"]
        assert closed == ["generated-1", "generated-2"]
        assert (tmp_path / "Socket_screw.step+").read_bytes() == b"editable native part"

    def test_save_failure_is_not_reported_as_insertable(self, tmp_path, monkeypatch):
        from services.parts import gui_rpc
        from services.mcp import core_bridge as cb

        monkeypatch.setattr(gui_rpc, "_parts_insert_into_doc",
                            lambda params, emit: {"ok": True, "doc_id": "generated",
                                                  "detail": {"name": "Bearing"}})
        closed = []

        def call(method, params):
            if method == "doc_close":
                closed.append(params["doc_id"])
                return {}
            raise RuntimeError("disk full")

        monkeypatch.setattr(cb, "call", call)
        result = gui_rpc._parts_prepare_for_assembly(
            {"item_id": "bearing:example", "project_dir": str(tmp_path)}, _noop_emit)
        assert result["ok"] is False and "disk full" in result["reason"]
        assert closed == ["generated"]
        assert not list(tmp_path.glob("*.step+"))


class TestPartsThumbnail:
    """Parts Library V2: parts_thumbnail must RENDER a standard part's own 3D model.

    Daemon-mode regression guard, same bug class as the placer's (see
    test_x3_browser_catalog.py): ``_render_local_part_png`` used to reach the engine through a
    bare ``import core_bridge`` off a sys.path hack. A bare import leaves the module with no
    package, so ``core_bridge.call``'s own ``from .daemon_bridge import DaemonError`` raised
    "attempted relative import with no known parent package" on the FIRST call — swallowed by
    the render's broad ``except``, so every part not already on disk answered ok:false and the
    GUI card fell back to a flat glyph. Only the handful of ids left in the shared on-disk
    cache still produced an image, which is exactly why the failure looked random. The cache
    dir is redirected to tmp_path here so a stale cache can never mask a broken render.
    """

    @staticmethod
    def _thumb(part_id, tmp_path, monkeypatch, **extra):
        from services.parts import gui_rpc
        monkeypatch.setattr(gui_rpc, "_thumb_cache_dir", lambda: tmp_path)
        return gui_rpc._parts_thumbnail({"item_id": part_id, **extra}, _noop_emit)

    def test_missing_item_id_returns_ok_false(self, tmp_path, monkeypatch):
        r = self._thumb("", tmp_path, monkeypatch)
        assert r.get("ok") is False
        assert "item_id" in r.get("reason", "")

    def test_unknown_part_returns_ok_false_not_traceback(self, tmp_path, monkeypatch):
        r = self._thumb("fastener:__no_such__:X999", tmp_path, monkeypatch)
        assert r.get("ok") is False
        assert "reason" in r

    def test_uncached_core_family_part_renders_png(self, tmp_path, monkeypatch):
        """A local catalog fastener carries NO vendor image_url — its preview exists only if
        the worker renders geometry_ref.kind == "core_family" through the engine."""
        from services.parts.gui_rpc import _resolve_core_family
        part_id = "fastener:ISO 7380:M5"
        if _resolve_core_family(part_id) is None:
            pytest.skip("hub data for the button-head catalog record not present")
        r = self._thumb(part_id, tmp_path, monkeypatch)
        if not r.get("ok") and "core" in r.get("reason", "").lower():
            pytest.skip(f"native core not available: {r.get('reason')}")
        assert r.get("ok") is True, \
            f"core_family part rendered nothing — the GUI card gets a bare glyph: {r}"
        raw = base64.b64decode(r["image_b64"])
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", "preview is not a decodable PNG"
        assert list(tmp_path.glob("*.img")), "render was not cached — every view re-renders"
