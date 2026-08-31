"""Tests for the Parts Library V2 MCP surface (services/mcp/parts_tools.py).

Covers the module-level implementations shared with the copilot's mission
dispatch: envelope shapes, taxonomy routing + facet forwarding, facet_summary,
post-filters, vendors_health degradation/needs_human_login, and the
cam_tool_import mapping with an injected add_tool seam. Mocked — no network,
no libchudware, no CAM job.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.mcp import parts_tools as pt  # noqa: E402


# ---------------------------------------------------------------------------
# taxonomy_browse
# ---------------------------------------------------------------------------

class TestTaxonomyBrowse:
    def test_roots(self):
        out = pt.taxonomy_browse("")
        assert out["ok"] is True
        ids = [c["id"] for c in out["children"]]
        assert "fasteners" in ids and "bearings_motion" in ids
        assert all("has_children" in c for c in out["children"])

    def test_node_with_facets_and_breadcrumbs(self):
        out = pt.taxonomy_browse("bearings_motion.ball_bearings.deep_groove")
        assert out["ok"] is True
        assert [b["id"] for b in out["breadcrumbs"]] == [
            "bearings_motion", "bearings_motion.ball_bearings",
            "bearings_motion.ball_bearings.deep_groove"]
        assert {f["key"] for f in out["facets"]} == {
            "bore_mm", "od_mm", "width_mm", "seals"}
        assert out["node"]["path"][-1] == "bearings_motion.ball_bearings.deep_groove"

    def test_depth_limits_children_nesting(self):
        shallow = pt.taxonomy_browse("fasteners", depth=1)
        deep = pt.taxonomy_browse("fasteners", depth=2)
        assert "children" not in shallow["children"][0]
        nested = next(c for c in deep["children"] if c["has_children"])
        assert nested["children"]

    def test_unknown_id_degrades(self):
        out = pt.taxonomy_browse("no.such.category")
        assert out["ok"] is False and "unknown" in out["reason"]

    def test_include_facets_false(self):
        out = pt.taxonomy_browse("fasteners", include_facets=False)
        assert out["ok"] is True and out["facets"] == []


# ---------------------------------------------------------------------------
# search_parts_v2
# ---------------------------------------------------------------------------

_BEARINGS = [
    {"part_id": "bearing:ISO 15:6001", "domain": "bearing",
     "title": "Deep-Groove Ball Bearing 6001",
     "params": {"bore": 12.0, "outer_d": 28.0, "width": 8.0},
     "geometry_ref": {"kind": "core_family"}, "sourcing": []},
    {"part_id": "bearing:ISO 15:6205", "domain": "bearing",
     "title": "Deep-Groove Ball Bearing 6205",
     "params": {"bore": 25.0, "outer_d": 52.0, "width": 15.0},
     "geometry_ref": {"kind": "core_family"},
     "sourcing": [{"price": 6.5, "currency": "USD", "stock": 3}]},
    {"part_id": "catalog:mcmaster::https://x/6205", "domain": "catalog",
     "title": "6205 Sealed Bearing", "source": "live",
     "params": {"bore_mm": 25.0, "seals": "2RS"},
     "sourcing": [{"price": 12.0, "currency": "USD", "stock": 0}]},
]


@pytest.fixture()
def hub_search(monkeypatch):
    calls: list[dict] = []

    def _search(query, domain=None, limit=20, **kw):
        calls.append({"query": query, "domain": domain, "limit": limit})
        if domain == "catalog":
            return [r for r in _BEARINGS if r["domain"] == "catalog"]
        return list(_BEARINGS)

    import services.parts as hub
    monkeypatch.setattr(hub, "search", _search)
    return calls


class TestSearchPartsV2:
    def test_legacy_envelope_preserved(self, hub_search):
        out = pt.search_parts_v2("bearing")
        assert out["ok"] is True
        assert out["count"] == 3
        assert out["has_live"] is True
        assert isinstance(out["results"], list)
        assert "facet_summary" in out and "applied" in out

    def test_category_routing_forwards_domains(self, hub_search):
        pt.search_parts_v2("bearing",
                           category_id="bearings_motion.ball_bearings.deep_groove")
        assert hub_search[0]["domain"] == ["bearing"]  # taxonomy-routed

    def test_facet_filter_with_alias_tolerance(self, hub_search):
        out = pt.search_parts_v2(
            "bearing", category_id="bearings_motion.ball_bearings.deep_groove",
            facets='{"bore_mm": 12}')
        assert [r["part_id"] for r in out["results"]] == ["bearing:ISO 15:6001"]
        assert out["applied"]["facets"] == {"bore_mm": 12}

    def test_facet_summary_counts_values(self, hub_search):
        out = pt.search_parts_v2(
            "bearing", category_id="bearings_motion.ball_bearings.deep_groove")
        summary = out["facet_summary"]
        bores = {e["value"]: e["count"] for e in summary["bore_mm"]}
        assert bores == {"12.0": 1, "25.0": 2}

    def test_price_max_filter(self, hub_search):
        out = pt.search_parts_v2("bearing", price_max=7.0)
        assert [r["part_id"] for r in out["results"]] == ["bearing:ISO 15:6205"]

    def test_vendor_csv_filter(self, hub_search):
        out = pt.search_parts_v2("bearing", vendors="mcmaster")
        assert [r["part_id"] for r in out["results"]] == \
            ["catalog:mcmaster::https://x/6205"]

    def test_in_stock_drops_only_known_zero(self, hub_search):
        out = pt.search_parts_v2("bearing", in_stock=True)
        ids = [r["part_id"] for r in out["results"]]
        # catalog record has KNOWN stock 0 → dropped; unknown-stock 6001 passes.
        assert "catalog:mcmaster::https://x/6205" not in ids
        assert "bearing:ISO 15:6001" in ids and "bearing:ISO 15:6205" in ids

    def test_bad_facets_json_is_contract_error(self, hub_search):
        out = pt.search_parts_v2("bearing", facets="{not json")
        assert out["ok"] is False and "JSON" in out["reason"]

    def test_backend_failure_degrades(self, monkeypatch):
        import services.parts as hub

        def _boom(*a, **k):
            raise RuntimeError("index down")
        monkeypatch.setattr(hub, "search", _boom)
        out = pt.search_parts_v2("bearing")
        assert out["ok"] is False and "hint" in out

    def test_default_row_projection_is_compact(self, hub_search):
        """Default detail=summary projects rows; full records stay behind detail=full."""
        from services.mcp import result_serving as rs

        out = pt.search_parts_v2("bearing")
        assert out["ok"] is True and out["results"]
        for r in out["results"]:
            assert "part_id" in r and "title" in r
            # Compact projection — no full PartRecord bloat.
            assert "params" not in r
            assert "geometry_ref" not in r
            assert "sourcing" not in r
            assert "provenance" not in r
            assert "license" not in r
            for k in ("part_id", "title", "domain", "vendor", "price",
                      "designation", "score"):
                assert k in r, f"missing projected key {k} in {r}"

        # Token bound on a 20-row synthetic fan-out of fat PartRecords.
        fat = []
        for i in range(20):
            fat.append({
                "part_id": f"bearing:ISO 15:6{i:03d}",
                "domain": "bearing",
                "title": f"Deep-Groove Ball Bearing 6{i:03d} with a long descriptive title",
                "designation": f"6{i:03d}",
                "params": {"bore": 25.0, "outer_d": 52.0, "width": 15.0,
                           "extra": "x" * 200},
                "geometry_ref": {"kind": "core_family", "family": "ball"},
                "sourcing": [{"price": 6.5, "currency": "USD", "stock": 3,
                              "vendor": "local", "url": "https://example/" + "u" * 80}],
                "provenance": {"src": "index", "blob": "y" * 200},
                "license": "CC-BY-4.0",
            })
        payload = {
            "ok": True, "count": 20, "has_live": False,
            "results": fat, "notices": [], "facet_summary": {"bore": []},
            "applied": {},
        }
        compact = rs.project_part_search(payload, rs.ServeOpts(detail="summary"))
        tokens = rs.count_tokens(compact)
        assert tokens <= 1500, f"part_search default rows too large: {tokens} tokens"
        for r in compact["results"]:
            assert r["part_id"] and r["title"]

        full = rs.project_part_search(payload, rs.ServeOpts(detail="full"))
        assert "params" in full["results"][0]
        assert "geometry_ref" in full["results"][0]


# ---------------------------------------------------------------------------
# part_source_impl
# ---------------------------------------------------------------------------

class TestPartSourceImpl:
    def test_envelope(self, hub_search):
        out = pt.part_source_impl("bearing 12mm bore", "", 3)
        assert out["ok"] is True
        assert out["interpreted"]["facets"] == {"bore_mm": 12.0}
        assert out["candidates"][0]["part_id"] == "bearing:ISO 15:6001"
        assert out["candidates"][0]["artifact_plan"]["how"] == "part_place"

    def test_empty_need(self):
        out = pt.part_source_impl("", "", 5)
        assert out["ok"] is False


# ---------------------------------------------------------------------------
# vendors_health_impl
# ---------------------------------------------------------------------------

class TestVendorsHealth:
    def test_needs_human_login_flag_and_csv(self, monkeypatch):
        seen = {}

        def _run(sites=None):
            seen["sites"] = sites
            return [
                {"site": "mcmaster", "ok": True, "latency_ms": 90.0,
                 "logged_in": True, "hit_count": 4, "error": None},
                {"site": "grabcad", "ok": False, "latency_ms": 40.0,
                 "logged_in": False, "hit_count": 0,
                 "error": "login_required", "expected": True},
                {"site": "lowes", "ok": False, "latency_ms": 0,
                 "logged_in": False, "hit_count": 0, "error": "parse failure"},
            ]

        from services.browser import health as h
        monkeypatch.setattr(h, "run_health_check", _run)
        out = pt.vendors_health_impl("mcmaster, grabcad,lowes")
        assert out["ok"] is True and out["count"] == 3
        assert seen["sites"] == ["mcmaster", "grabcad", "lowes"]
        flags = {r["site"]: r["needs_human_login"] for r in out["sites"]}
        assert flags == {"mcmaster": False, "grabcad": True, "lowes": False}
        assert out["needs_human_login"] == ["grabcad"]

    def test_blank_sites_means_all(self, monkeypatch):
        seen = {}

        def _run(sites=None):
            seen["sites"] = sites
            return []
        from services.browser import health as h
        monkeypatch.setattr(h, "run_health_check", _run)
        out = pt.vendors_health_impl("")
        assert out["ok"] is True and seen["sites"] is None


# ---------------------------------------------------------------------------
# cam_tool_import_impl (injected seams — no CAM engine)
# ---------------------------------------------------------------------------

_END_MILL = {
    "part_id": "catalog:mcmaster::https://x/endmill",
    "domain": "catalog",
    "title": "1/4in Carbide Square End Mill 4 Flute AlTiN",
    "params": {"tool_diameter_mm": 6.35, "flutes": "4", "coating": "AlTiN",
               "material": "carbide", "shank_diameter_mm": 6.35, "loc_mm": 19.0},
    "sourcing": [{"price": 32.5, "currency": "USD"}],
    "source": "live",
    "provenance": "vendor listing",
}
_DRILL = {
    "part_id": "catalog:mcmaster::https://x/drill",
    "domain": "catalog",
    "title": "5mm Jobber Drill Bit HSS",
    "params": {"diameter_mm": 5.0, "material": "HSS"},
    "sourcing": [],
}


class TestCamToolImport:
    def _run(self, rec, number=0, material="", existing=(1, 2)):
        added = {}

        def _add(tool):
            added.update(tool)
            return {"ok": True, "revision": 7}
        out = pt.cam_tool_import_impl(
            rec["part_id"], number, material,
            _get_fn=lambda pid: rec if pid == rec["part_id"] else None,
            _add_tool_fn=_add,
            _existing_numbers_fn=lambda: list(existing))
        return out, added

    def test_end_mill_mapping_and_next_free_number(self):
        out, added = self._run(_END_MILL)
        assert out["ok"] is True
        assert out["tool_number"] == 3          # next free after [1, 2]
        assert added["type"] == "endmill_flat"
        assert added["diameter"] == 6.35
        assert added["flutes"] == 4
        assert added["tool_material"] == "carbide"
        assert out["machining"]["coating"] == "AlTiN"
        assert out["machining"]["price"] == 32.5
        assert out["machining"]["provenance"] == "vendor listing"
        assert "CuttingDB" in out["note"]
        sug = out["op_params_suggestion"]
        assert sug["stepover"] == pytest.approx(0.4 * 6.35, abs=1e-3)
        assert sug["stepdown"] == pytest.approx(0.5 * 6.35, abs=1e-3)

    def test_drill_gets_tip_angle_118(self):
        out, added = self._run(_DRILL, number=9)
        assert out["tool_number"] == 9
        assert added["type"] == "drill"
        assert added["tip_angle"] == 118.0
        assert added["tool_material"] == "hss"

    def test_material_fallback_arg(self):
        rec = dict(_DRILL)
        rec["params"] = {"diameter_mm": 5.0}
        out, added = self._run(rec, material="cobalt")
        assert added["tool_material"] == "cobalt"

    def test_unknown_part_id(self):
        out = pt.cam_tool_import_impl(
            "nope:1", _get_fn=lambda pid: None,
            _add_tool_fn=lambda t: {}, _existing_numbers_fn=lambda: [])
        assert out["ok"] is False and "not found" in out["reason"]

    def test_missing_part_id(self):
        out = pt.cam_tool_import_impl("")
        assert out["ok"] is False

    def test_no_cam_job_is_contract_error(self):
        def _no_job():
            raise ValueError("No CAM job. Call cam_new_job(...) first.")
        out = pt.cam_tool_import_impl(
            _END_MILL["part_id"],
            _get_fn=lambda pid: _END_MILL,
            _add_tool_fn=lambda t: t, _existing_numbers_fn=_no_job)
        assert out["ok"] is False and "cam_new_job" in out["hint"]


# ---------------------------------------------------------------------------
# MCP registration: the tools register and return JSON-string envelopes
# ---------------------------------------------------------------------------

class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class TestRegistration:
    def test_v2_tools_registered_and_json(self, hub_search):
        m = _FakeMCP()
        pt.register_parts_tools(m)
        for name in ("part_search", "parts_taxonomy", "part_source",
                     "vendors_health", "cam_tool_import", "part_get",
                     "part_place", "vendor_status", "vendor_login",
                     "browser_profiles", "match_model",
                     "reverse_engineer_product", "scrape_enqueue",
                     "scrape_queue_status", "import_artifact",
                     "vendor_part_register"):
            assert name in m.tools, name
        out = json.loads(m.tools["parts_taxonomy"]())
        assert out["ok"] is True
        out = json.loads(m.tools["part_search"]("bearing", facets='{"bore_mm": 12}'))
        assert out["ok"] is True and out["count"] == 1


@pytest.fixture()
def registered_parts_tools():
    mcp = _FakeMCP()
    pt.register_parts_tools(mcp)
    return mcp.tools


class TestBrowserProfiles:
    def test_list_projects_frozen_shape_and_checks_every_profile(
            self, monkeypatch, registered_parts_tools):
        from services.browser import profiles

        saved = [
            {"site": "alpha", "created_ts": 1.0, "last_used_ts": 2.0,
             "last_login_ts": 3.0, "login_verified_ts": 4.0, "extra": "omit"},
            {"site": "beta", "created_ts": 5.0, "last_used_ts": 6.0,
             "last_login_ts": None},
        ]
        checked = []
        monkeypatch.setattr(profiles, "list_profiles", lambda: saved)
        monkeypatch.setattr(
            profiles, "check_profile_status",
            lambda site: checked.append(site) or
            {"alpha": "active", "beta": "expired"}[site])

        out = json.loads(registered_parts_tools["browser_profiles"](action="list"))

        assert set(out) == {"ok", "profiles"}
        assert out == {
            "ok": True,
            "profiles": [
                {"site": "alpha", "created_ts": 1.0, "last_used_ts": 2.0,
                 "last_login_ts": 3.0, "status": "active"},
                {"site": "beta", "created_ts": 5.0, "last_used_ts": 6.0,
                 "last_login_ts": None, "status": "expired"},
            ],
        }
        assert checked == ["alpha", "beta"]

    def test_refresh_aliases_delegate_to_browser_login(
            self, monkeypatch, registered_parts_tools):
        from services.browser import session

        seen = []
        monkeypatch.setattr(
            session, "browser_login",
            lambda site: seen.append(site) or {
                "ok": True, "site": site, "last_login_ts": 123.0,
                "challenge_detected": False,
            })

        out = json.loads(registered_parts_tools["browser_profiles"](
            operation="refresh", vendor="McMaster"))

        assert seen == ["mcmaster"]
        assert out["ok"] is True and out["site"] == "mcmaster"
        assert out["last_login_ts"] == 123.0
        assert len(out["interpreted"]) == 2

    def test_busy_delete_refuses_without_mutation(
            self, monkeypatch, registered_parts_tools):
        from services.browser import profiles

        deleted = []

        class BusyLock:
            def acquire(self, *, blocking):
                assert blocking is False
                return False

            def release(self):
                raise AssertionError("an unacquired lock must not be released")

        monkeypatch.setattr(profiles, "profile_lock", lambda site: BusyLock())
        monkeypatch.setattr(
            profiles, "delete_profile", lambda site: deleted.append(site) or True)

        out = json.loads(registered_parts_tools["browser_profiles"](
            action="delete", site="alpha"))

        assert out["ok"] is False and out["reason"] == "profile_busy"
        assert deleted == []

    def test_delete_holds_lock_and_returns_structured_result(
            self, monkeypatch, registered_parts_tools):
        from services.browser import profiles

        events = []

        class FreeLock:
            def acquire(self, *, blocking):
                events.append(("acquire", blocking))
                return True

            def release(self):
                events.append(("release",))

        monkeypatch.setattr(
            profiles, "profile_lock",
            lambda site: events.append(("lock", site)) or FreeLock())
        monkeypatch.setattr(
            profiles, "delete_profile",
            lambda site: events.append(("delete", site)) or True)

        out = json.loads(registered_parts_tools["browser_profiles"](
            action="delete", site="Alpha"))

        assert out == {"ok": True, "site": "alpha", "deleted": True}
        assert events == [
            ("lock", "alpha"), ("acquire", False),
            ("delete", "alpha"), ("release",),
        ]

    @pytest.mark.parametrize("site", [".", ".."])
    def test_dot_sites_are_rejected_before_profile_access(
            self, monkeypatch, registered_parts_tools, site):
        from services.browser import profiles

        def touched(*_args, **_kwargs):
            raise AssertionError("invalid site reached profile storage")

        monkeypatch.setattr(profiles, "profile_lock", touched)
        monkeypatch.setattr(profiles, "delete_profile", touched)

        out = json.loads(registered_parts_tools["browser_profiles"](
            action="delete", site=site))

        assert out["ok"] is False and out["reason"] == "invalid_site"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({}, "action"),
            ({"action": "inspect"}, "action"),
            ({"action": "refresh"}, "site"),
            ({"action": "list", "site": "alpha"}, "does not accept a site"),
        ],
    )
    def test_action_and_site_combinations_are_validated(
            self, registered_parts_tools, kwargs, message):
        out = json.loads(registered_parts_tools["browser_profiles"](**kwargs))

        assert out["ok"] is False
        assert message in out["reason"]

    def test_mixed_action_surface_is_explicit_write_risk(self):
        from services.mcp import tool_registry

        assert (
            tool_registry._RISK_OVERRIDES["browser_profiles"]
            == tool_registry.RISK_WRITE
        )
        assert (
            tool_registry.classify_risk("browser_profiles")
            == tool_registry.RISK_WRITE
        )


def _queue_job(artifact_path: str, **spec_data) -> dict:
    return {
        "ok": True,
        "jobs": [{
            "id": "job-vendor-1",
            "site": "mcmaster",
            "query": "socket head screw",
            "status": "done",
            "artifact_path": artifact_path,
            "spec_data": spec_data,
        }],
    }


class TestVendorAcquisitionBridge:
    def test_import_resolves_done_queue_source_and_stamps_doc(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        artifact.write_text("STEP")
        queued_path = tmp_path / "queued.step"
        queued_path.symlink_to(artifact)

        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(queued_path), sku="91290A115",
            source_url="https://www.mcmaster.com/91290A115/",
            fetched_ts="2026-08-03T12:00:00Z"))
        calls = []

        from services.mcp import core_bridge as cb

        def _call(method, params):
            calls.append((method, params))
            if method == "part_import_file":
                return {"ok": True, "doc_id": "doc-import-1"}
            if method == "doc_set_meta":
                return {"ok": True, "key": "vendor_source"}
            raise AssertionError(f"unexpected rpc {method}")

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["import_artifact"](
            artifact_path=str(artifact)))

        assert out["ok"] is True
        metadata_calls = [params for method, params in calls
                          if method == "doc_set_meta"]
        assert len(metadata_calls) == 1
        assert metadata_calls[0]["key"] == "vendor_source"
        assert metadata_calls[0]["value"]["sku"] == "91290A115"
        assert (metadata_calls[0]["value"]["source_url"]
                == "https://www.mcmaster.com/91290A115/")
        assert out["vendor_source"] == metadata_calls[0]["value"]

    def test_import_without_matching_job_skips_metadata(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        artifact.write_text("STEP")
        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(tmp_path / "different.step"), sku="OTHER"))
        calls = []

        from services.mcp import core_bridge as cb

        def _call(method, params):
            calls.append((method, params))
            return {"ok": True, "doc_id": "doc-import-2"}

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["import_artifact"](
            artifact_path=str(artifact)))

        assert out["ok"] is True
        assert [method for method, _ in calls] == ["part_import_file"]
        assert "vendor_source" not in out

    def test_metadata_failure_warns_but_preserves_import_success(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        artifact.write_text("STEP")
        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(artifact), sku="91290A115"))

        from services.mcp import core_bridge as cb

        def _call(method, params):
            if method == "part_import_file":
                return {"ok": True, "doc_id": "doc-import-3"}
            if method == "doc_set_meta":
                raise RuntimeError("metadata store unavailable")
            raise AssertionError(f"unexpected rpc {method}")

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["import_artifact"](
            artifact_path=str(artifact)))

        assert out["ok"] is True
        assert "metadata store unavailable" in out["metadata_warning"]
        assert "vendor_source" not in out

    def test_vendor_register_parses_simple_scraped_price(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(artifact), site="mcmaster", sku="91290A115", price="$12.34",
            source_url="https://www.mcmaster.com/91290A115/",
            fetched_ts="2026-08-03T12:00:00Z"))
        captured = {}

        from services.mcp import core_bridge as cb

        def _call(method, params):
            assert method == "vendor_part_register"
            captured.update(params)
            return {"ok": True, "part": {"supplier": params["supplier"]}}

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["vendor_part_register"](
            artifact_path=str(artifact)))

        assert out["ok"] is True
        assert captured["unit_price"] == 12.34
        assert captured["supplier"] == "mcmaster"
        assert captured["part_number"] == "91290A115"

    def test_vendor_register_does_not_invent_pack_unit_price(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        raw_price = "$10.00 / pack of 50"
        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(artifact), site="mcmaster", sku="PACK-50", price=raw_price))
        captured = {}

        from services.mcp import core_bridge as cb

        def _call(method, params):
            captured.update(params)
            return {"ok": True, "part": {}}

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["vendor_part_register"](
            artifact_path=str(artifact)))

        assert captured["unit_price"] is None
        assert out["price_unparsed"] == raw_price
        assert "pass unit_price=" in out["hint"]

    def test_vendor_register_passes_scrape_provenance_and_fetch_epoch(
            self, monkeypatch, tmp_path, registered_parts_tools):
        artifact = tmp_path / "part.step"
        from services.browser import queue
        monkeypatch.setattr(queue, "queue_status", lambda: _queue_job(
            str(artifact), site="mcmaster", sku="91290A115", price="$12.34",
            source_url="https://www.mcmaster.com/91290A115/",
            session_profile_used="mcmaster-profile",
            license_note="User obtained under McMaster terms.",
            fetched_ts="2026-08-03T12:00:00Z"))
        captured = {}

        from services.mcp import core_bridge as cb

        def _call(method, params):
            captured.update(params)
            return {"ok": True, "part": {}}

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["vendor_part_register"](
            artifact_path=str(artifact)))

        assert out["ok"] is True
        assert captured["provenance"] == {
            "method": "cef_browser_scrape",
            "scraped": True,
            "source_url": "https://www.mcmaster.com/91290A115/",
            "session_profile_used": "mcmaster-profile",
            "license_note": "User obtained under McMaster terms.",
        }
        assert isinstance(captured["fetched_ts"], float)
        assert captured["fetched_ts"] == 1785758400.0

    def test_vendor_register_missing_core_rpc_degrades_to_error_envelope(
            self, monkeypatch, registered_parts_tools):
        from services.mcp import core_bridge as cb

        def _call(method, params):
            raise RuntimeError("unknown method vendor_part_register")

        monkeypatch.setattr(cb, "call", _call)
        out = json.loads(registered_parts_tools["vendor_part_register"](
            supplier="mcmaster", part_number="91290A115", unit_price=12.34))

        assert out["ok"] is False
        assert "unknown method vendor_part_register" in out["reason"]


# ---------------------------------------------------------------------------
# match_model + reverse_engineer_product (aicad.match_model/1, contracts §2a/§2b)
# ---------------------------------------------------------------------------

# A mixed-tier envelope: exactly one exact (attach:true), one strong, one weak.
_MATCH_ENVELOPE = {
    "ok": True,
    "product": {"title": "Acme Widget", "mpn": "1LNK-4B", "brand": "AcmeCorp"},
    "matches": [
        {"part_id": "catalog:grabcad::https://g/1", "title": "Widget 1LNK-4B",
         "site": "grabcad", "mpn": "1LNK-4B", "confidence": "exact",
         "score": 4.0, "attach": True,
         "artifact_plan": {"how": "scrape_import", "site": "grabcad",
                           "query": "1LNK-4B"}},
        {"part_id": "catalog:traceparts::https://t/2", "title": "Widget 1LNK",
         "site": "traceparts", "mpn": "1LNK", "confidence": "strong",
         "score": 2.0, "attach": False,
         "artifact_plan": {"how": "scrape_import", "site": "traceparts",
                           "query": "1LNK"}},
        {"part_id": "catalog:content3d::https://c/3", "title": "Some Widget",
         "site": "content3d", "mpn": "", "confidence": "weak",
         "score": 1.0, "attach": False,
         "artifact_plan": {"how": "scrape_import", "site": "content3d",
                           "query": "widget"}},
    ],
    "best": None,
    "fallback": None,
    "coverage": {"sites_searched": 9, "raw_hits": 14},
}


class TestMatchModel:
    def _patch(self, monkeypatch, capture=None):
        from services.parts import model_matcher as mm

        def _fake(product, *, limit=8):
            if capture is not None:
                capture["product"] = product
                capture["limit"] = limit
            return _MATCH_ENVELOPE
        monkeypatch.setattr(mm, "match_model", _fake)

    def test_from_identity_args_returns_envelope(self, monkeypatch):
        cap: dict = {}
        self._patch(monkeypatch, cap)
        out = pt.match_model_impl(title="Acme Widget", mpn="1LNK-4B",
                                  brand="AcmeCorp")
        assert out["ok"] is True
        assert cap["product"] == {"title": "Acme Widget", "mpn": "1LNK-4B",
                                  "brand": "AcmeCorp"}
        # the MCP tool wrapper returns a JSON string of the same envelope.
        m = _FakeMCP()
        pt.register_parts_tools(m)
        assert json.loads(m.tools["match_model"](title="Acme Widget",
                                                 mpn="1LNK-4B"))["ok"] is True

    def test_only_exact_carries_attach_true(self, monkeypatch):
        self._patch(monkeypatch)
        out = pt.match_model_impl(mpn="1LNK-4B")
        attaching = [m for m in out["matches"] if m.get("attach")]
        assert len(attaching) == 1
        assert attaching[0]["confidence"] == "exact"
        # strong/weak never auto-attach
        assert all(not m["attach"] for m in out["matches"]
                   if m["confidence"] != "exact")

    def test_part_id_path_fetches_record(self, monkeypatch):
        cap: dict = {}
        self._patch(monkeypatch, cap)
        import services.parts as hub
        rec = {"part_id": "catalog:amazon::https://a/x", "title": "Acme Widget",
               "mpn": "1LNK-4B", "brand": "AcmeCorp"}
        monkeypatch.setattr(hub, "get", lambda pid: rec)
        out = pt.match_model_impl(part_id="catalog:amazon::https://a/x")
        assert out["ok"] is True
        # the WHOLE record is handed to the matcher (it extracts identity itself)
        assert cap["product"]["mpn"] == "1LNK-4B"

    def test_unknown_part_id_is_contract_error(self, monkeypatch):
        import services.parts as hub
        monkeypatch.setattr(hub, "get", lambda pid: None)
        out = pt.match_model_impl(part_id="catalog:x::none")
        assert out["ok"] is False and "not found" in out["reason"]

    def test_no_product_is_contract_error(self):
        out = pt.match_model_impl()
        assert out["ok"] is False and "no product" in out["reason"]

    def test_registered_and_json(self):
        m = _FakeMCP()
        pt.register_parts_tools(m)
        assert "match_model" in m.tools
        assert "reverse_engineer_product" in m.tools


class TestReverseEngineerProduct:
    def _patch(self, monkeypatch, capture=None):
        from services.reveng import worker_rpc as re

        def _fake(params, emit):
            if capture is not None:
                capture["params"] = params
            return {"ok": True, "session_id": "sess_1", "images_added": 3,
                    "documents_ingested": 1,
                    "source_url": "https://a/x", "activeWorkspace": "ReverseEng"}
        monkeypatch.setattr(re, "_reveng_start_from_product", _fake)

    def test_url_seeds_session(self, monkeypatch):
        cap: dict = {}
        self._patch(monkeypatch, cap)
        out = pt.reverse_engineer_product_impl(url="https://a/x", mpn="1LNK-4B")
        assert out["ok"] is True and out["session_id"] == "sess_1"
        assert out["activeWorkspace"] == "ReverseEng"
        assert out["images_added"] == 3 and out["documents_ingested"] == 1
        assert cap["params"]["url"] == "https://a/x"
        assert cap["params"]["mpn"] == "1LNK-4B"

    def test_part_id_seeds_session(self, monkeypatch):
        cap: dict = {}
        self._patch(monkeypatch, cap)
        out = pt.reverse_engineer_product_impl(part_id="catalog:amazon::https://a/x")
        assert out["ok"] is True
        assert cap["params"]["part_id"] == "catalog:amazon::https://a/x"

    def test_no_reference_is_contract_error(self):
        out = pt.reverse_engineer_product_impl(title="just a title")
        assert out["ok"] is False and "reference" in out["reason"]


# ---------------------------------------------------------------------------
# place_parametric_impl — the parametric_* catalog families
# ---------------------------------------------------------------------------

class TestPlaceParametric:
    """384 records (stepper motors, timing pulleys, shaft collars/couplers,
    heatsinks) advertise geometry_ref kinds core/parts/place.cpp::route has never
    known, so part_place answered "Unknown geometry_ref kind
    'parametric_stepper_motor'" for every one of them. These check the ops the
    Python route emits — geometry, not ok:true — with the engine bridge stubbed so
    the test needs no libchudware and no network.
    """

    @staticmethod
    def _record(kind, spec, params=None):
        return {"part_id": f"probe:{kind}", "domain": "probe",
                "params": params or {},
                "geometry_ref": {"kind": kind, "spec": spec}}

    @staticmethod
    def _stub_bridge(monkeypatch, captured):
        def _call(method, params):
            if method == "doc_new":
                return {"doc_id": "doc-probe-1", "revision": 0}
            if method == "doc_apply_ops":
                captured.extend(params["ops"])
                return {"ok": True}
            if method == "eval_mass":
                return {"volume_mm3": 1.0}
            raise AssertionError(f"unexpected rpc {method}")
        from services.mcp import core_bridge as cb
        monkeypatch.setattr(cb, "call", _call)

    def test_non_parametric_kind_is_left_to_the_engine(self):
        # An empty dict is the "not mine" signal part_place keys off; anything else
        # would hijack every core_family placement.
        assert pt.place_parametric_impl(
            "fastener:ISO 4762:M3",
            self._record("core_family", {"family": "shcs", "size": "M3"})) == {}

    def test_stepper_motor_builds_frame_shaft_and_bolt_circle(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        out = pt.place_parametric_impl(
            "motor:stepper:NEMA17-34mm-0p9deg",
            self._record("parametric_stepper_motor",
                         {"nema_size": 17, "body_square_mm": 42.3,
                          "bolt_hole_spacing_mm": 31.0, "shaft_dia_mm": 5.0,
                          "body_length_mm": 34},
                         {"mount_screw": "M3"}))
        assert out["ok"] is True and out["editable"] is True
        assert out["doc_id"] == "doc-probe-1"
        # body + shaft + four mounting holes, and the holes really are cuts.
        names = [o["feature"]["name"] for o in ops]
        assert names == ["Body", "Shaft", "MountHole1", "MountHole2",
                         "MountHole3", "MountHole4"]
        assert all(o["feature"]["params"]["mode"] == "cut"
                   for o in ops if o["feature"]["name"].startswith("MountHole"))
        body = ops[0]["feature"]["params"]
        assert body["length"]["value"] == 42.3 and body["height"]["value"] == 34.0
        # Faceplate seats at z=0 with the body behind it (the documented datum).
        assert body["at"][2] == -34.0
        shaft = ops[1]["feature"]["params"]
        assert shaft["radius"]["value"] == 2.5 and shaft["at"] == [0.0, 0.0, 0.0]
        # M3 faceplate taps on the 31 mm bolt circle.
        holes = [o["feature"]["params"] for o in ops[2:]]
        assert {h["radius"]["value"] for h in holes} == {1.5}
        assert {(h["at"][0], h["at"][1]) for h in holes} == {
            (-15.5, -15.5), (15.5, -15.5), (15.5, 15.5), (-15.5, 15.5)}

    def test_stepper_shaft_length_is_flagged_and_overridable(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        spec = {"nema_size": 17, "body_square_mm": 42.3,
                "bolt_hole_spacing_mm": 31.0, "shaft_dia_mm": 5.0,
                "body_length_mm": 34}
        # Shaft protrusion is a per-SKU option, not a frame-standard dimension, so
        # the nominal used MUST be declared rather than passed off as spec truth.
        assumed = pt.place_parametric_impl(
            "m", self._record("parametric_stepper_motor", spec))
        assert assumed["detail"]["shaft_length_mm"] == 24.0
        assert any("shaft protrusion" in a
                   for a in assumed["detail"]["assumptions"])
        overridden = pt.place_parametric_impl(
            "m", self._record("parametric_stepper_motor", spec),
            {"shaft_length_mm": 40})
        assert overridden["detail"]["shaft_length_mm"] == 40.0
        assert overridden["detail"]["assumptions"] == []

    def test_pulley_body_is_the_exact_pitch_diameter(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        out = pt.place_parametric_impl(
            "motion:pulley:GT2-20T-5mm",
            self._record("parametric_timing_pulley",
                         {"profile": "GT2", "pitch_mm": 2.0, "teeth": 20,
                          "bore_mm": 5.0, "belt_width_mm": 6.0}))
        assert out["ok"] is True
        assert out["detail"]["pitch_dia_mm"] == pytest.approx(12.732, abs=1e-3)
        body, bore = ops[0]["feature"]["params"], ops[1]["feature"]["params"]
        assert body["radius"]["value"] == pytest.approx(40.0 / math.pi / 2)
        assert body["height"]["value"] == 6.0
        assert bore["radius"]["value"] == 2.5 and bore["mode"] == "cut"

    def test_collar_and_coupler_bores_come_from_the_spec(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        pt.place_parametric_impl(
            "motion:collar:COLLAR-setscrew-12mm",
            self._record("parametric_shaft_collar",
                         {"bore_mm": 12.0, "od_mm": 27.6, "width_mm": 9.0}))
        assert [o["feature"]["params"]["radius"]["value"] for o in ops] == [13.8, 6.0]
        ops.clear()
        pt.place_parametric_impl(
            "motion:coupler:COUPLER-setscrew-5mmx8mm",
            self._record("parametric_shaft_coupler",
                         {"bore_a_mm": 5.0, "bore_b_mm": 8.0, "od_mm": 19.0,
                          "length_mm": 25.0}))
        # Each bore enters from its own end and stops halfway.
        a, b = ops[1]["feature"]["params"], ops[2]["feature"]["params"]
        assert (a["radius"]["value"], a["at"][2], a["height"]["value"]) == (2.5, 0.0, 12.5)
        assert (b["radius"]["value"], b["at"][2], b["height"]["value"]) == (4.0, 12.5, 12.5)

    def test_heatsink_maps_the_adapter_spec_onto_the_generator(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        # heatsinks_adapter names services.mcp.heatsink.sink_ops in geometry_ref but
        # ships l_mm/w_mm while sink_ops reads base_l_mm/base_w_mm — calling the
        # advertised generator with the advertised spec used to raise KeyError.
        out = pt.place_parametric_impl(
            "thermal:heatsink:HS-EXTR-40SQ-20",
            self._record("parametric_heatsink",
                         {"l_mm": 40.0, "w_mm": 40.0, "base_t_mm": 3.0,
                          "n_fins": 10, "fin_t_mm": 1.3, "fin_h_mm": 17.0}))
        assert out["ok"] is True
        assert out["detail"]["base_l_mm"] == 40.0
        assert len(ops) == 11  # base + 10 fins
        assert ops[0]["feature"]["params"]["height"]["value"] == 3.0

    def test_belt_is_refused_with_the_layout_numbers(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        out = pt.place_parametric_impl(
            "motion:belt:GT2-160mm-6mm",
            self._record("parametric_timing_belt",
                         {"profile": "GT2", "pitch_mm": 2.0, "teeth": 80,
                          "length_mm": 160.0, "belt_width_mm": 6.0}))
        # A closed loop has no standalone shape; refuse, but hand back everything
        # needed to lay the drive out. Never fabricate a ring.
        assert out["ok"] is False and not ops
        assert "no standalone geometry" in out["reason"]
        assert "160.0 mm" in out["hint"] and "80 teeth" in out["hint"]

    def test_missing_spec_dimension_names_the_parameter(self, monkeypatch):
        ops: list = []
        self._stub_bridge(monkeypatch, ops)
        out = pt.place_parametric_impl(
            "motion:collar:broken",
            self._record("parametric_shaft_collar",
                         {"bore_mm": 12.0, "od_mm": 27.6}))
        assert out["ok"] is False and not ops
        assert "width_mm" in out["reason"] and "width_mm" in out["hint"]
