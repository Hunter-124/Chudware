"""Tests for the Parts Library V2 MCP surface (services/mcp/parts_tools.py).

Covers the module-level implementations shared with the copilot's mission
dispatch: envelope shapes, taxonomy routing + facet forwarding, facet_summary,
post-filters, vendors_health degradation/needs_human_login, and the
cam_tool_import mapping with an injected add_tool seam. Mocked — no network,
no libaicad, no CAM job.
"""
from __future__ import annotations

import json
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

    import engine_oracle.parts_hub as hub
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
        import engine_oracle.parts_hub as hub

        def _boom(*a, **k):
            raise RuntimeError("index down")
        monkeypatch.setattr(hub, "search", _boom)
        out = pt.search_parts_v2("bearing")
        assert out["ok"] is False and "hint" in out


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
                     "match_model", "reverse_engineer_product",
                     "scrape_enqueue", "scrape_queue_status", "import_artifact"):
            assert name in m.tools, name
        out = json.loads(m.tools["parts_taxonomy"]())
        assert out["ok"] is True
        out = json.loads(m.tools["part_search"]("bearing", facets='{"bore_mm": 12}'))
        assert out["ok"] is True and out["count"] == 1


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
        from engine_oracle.parts_hub import model_matcher as mm

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
        import engine_oracle.parts_hub as hub
        rec = {"part_id": "catalog:amazon::https://a/x", "title": "Acme Widget",
               "mpn": "1LNK-4B", "brand": "AcmeCorp"}
        monkeypatch.setattr(hub, "get", lambda pid: rec)
        out = pt.match_model_impl(part_id="catalog:amazon::https://a/x")
        assert out["ok"] is True
        # the WHOLE record is handed to the matcher (it extracts identity itself)
        assert cap["product"]["mpn"] == "1LNK-4B"

    def test_unknown_part_id_is_contract_error(self, monkeypatch):
        import engine_oracle.parts_hub as hub
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
        from engine_oracle import reveng_rpc as re

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
