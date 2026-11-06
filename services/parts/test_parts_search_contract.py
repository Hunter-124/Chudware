"""Consumer-visible Parts Library search, pagination and live-job contracts."""
from __future__ import annotations

import threading
from pathlib import Path

from services.parts import gui_rpc
from services.parts.index import PartsIndex


def _record(part_id, domain, title, *, designation="", params=None, family=""):
    return {"part_id": part_id, "domain": domain, "title": title,
            "designation": designation, "params": params or {}, "sourcing": [],
            "geometry_ref": {"kind": "core_family", "family": family} if family else None}


def _use_index(monkeypatch, index):
    from services import parts

    def search(query, *, domain=None, limit=20, **kwargs):
        return index.search(query, domain=domain, limit=limit)

    monkeypatch.setattr(parts, "search", search)
    monkeypatch.setattr(gui_rpc, "_family_size_options", lambda records: None)
    monkeypatch.setattr(gui_rpc, "_index_status", lambda: {"count": index.record_count})


def test_empty_browse_is_stable_and_diverse(monkeypatch):
    records = [_record(f"bearing:{n:03}", "bearing", f"Bearing {n}") for n in range(180)]
    records += [_record(f"{domain}:{n:03}", domain, f"{domain} {n}")
                for domain in ("fastener", "ecad", "section") for n in range(40)]
    first, reversed_index = PartsIndex(), PartsIndex()
    first.rebuild(iter(records))
    reversed_index.rebuild(iter(reversed(records)))
    first_ids = [row["part_id"] for row in first.search("", limit=100)]
    assert first_ids == [row["part_id"] for row in first.search(" ", limit=100)]
    assert first_ids == [row["part_id"] for row in reversed_index.search("", limit=100)]
    assert {pid.split(":")[0] for pid in first_ids} == {"bearing", "fastener", "ecad", "section"}
    assert first.search("", domain="bearing", limit=10) == reversed_index.search(
        "", domain="bearing", limit=10)
    _use_index(monkeypatch, first)
    page = gui_rpc._parts_search({"limit": 100}, None)
    assert page["ok"] and len(page["results"]) == 100
    assert [row["id"] for row in page["results"]] == first_ids
    assert page["total"] == len(records) and page["total_exact"] and page["has_more"]


def test_domain_scoped_unmatched_query_does_not_enumerate():
    index = PartsIndex()
    index.rebuild(iter([_record("bearing:1", "bearing", "unobtainium bearing"),
                        _record("section:1", "section", "steel section")]))
    assert index.search("unobtainium", domain="bearing")
    assert index.search("unobtainium", domain="section") == []


def test_filters_and_sort_precede_pagination(monkeypatch):
    records = []
    for n in range(150):
        records.append(_record(
            f"bearing:{n:03}", "bearing", f"Deep Groove Ball Bearing {n:03}",
            designation=str(n), params={"bore_mm": 12, "material": "steel",
                                        "vendor": "Target" if n >= 130 else "Other",
                                        "price_usd": float(n + 1)}))
    index = PartsIndex()
    index.rebuild(iter(records))
    _use_index(monkeypatch, index)
    request = {"category_id": "bearings_motion.ball_bearings.deep_groove",
               "facets": {"bore_mm": {"min": 10, "max": 12}}, "material": "steel",
               "vendor": "Target", "price_min": 100, "source": "local",
               "sort": "price_desc", "limit": 7}
    pages = [gui_rpc._parts_search({**request, "offset": offset}, None)
             for offset in (0, 7, 14, 21)]
    assert all(page["ok"] and page["total"] == 20 and page["total_exact"] for page in pages)
    assert [page["offset"] for page in pages] == [0, 7, 14, 21]
    assert [page["count"] for page in pages] == [7, 7, 6, 0]
    assert [page["has_more"] for page in pages] == [True, True, False, False]
    assert [item["price_usd"] for page in pages for item in page["results"]] == list(
        map(float, range(150, 130, -1)))


def test_bounded_candidate_count_is_reported_as_inexact(monkeypatch):
    index = PartsIndex()
    index.rebuild(iter(_record(f"bearing:{n}", "bearing", f"Bearing {n}") for n in range(5)))
    _use_index(monkeypatch, index)
    monkeypatch.setattr(gui_rpc, "_SEARCH_CANDIDATE_CAP", 3)
    result = gui_rpc._parts_search({"limit": 2}, None)
    assert result["total"] == 3 and result["total_exact"] is False and result["has_more"]


def test_category_scope_and_unknown_category(monkeypatch):
    index = PartsIndex()
    index.rebuild(iter([_record("bearing:1", "bearing", "Deep-Groove Ball Bearing 623"),
                        _record("bearing:2", "bearing", "Angular Contact Bearing 123")]))
    _use_index(monkeypatch, index)
    result = gui_rpc._parts_search(
        {"category_id": "bearings_motion.ball_bearings.angular_contact"}, None)
    assert [row["id"] for row in result["results"]] == ["bearing:2"]
    result = gui_rpc._parts_search({"category_id": "does.not.exist"}, None)
    assert result["ok"] is False and result["reason"] == "unknown_category"
    result = gui_rpc._parts_search_live_start({"category_id": "does.not.exist"}, None)
    assert result["ok"] is False and result["reason"] == "unknown_category"


def test_bearing_title_and_currency_metadata(tmp_path):
    from services.parts.adapters.bolts_adapter import BoltsAdapter
    from services.parts.adapters.local_library_adapter import _vendor_row_to_record

    bearing = BoltsAdapter().get("bearing:ISO 15:623")
    assert bearing is not None and bearing["title"] == "Deep-Groove Ball Bearing 623"
    item = gui_rpc._record_to_item(bearing)
    assert item["family"] == "Deep-Groove Ball Bearing" and item["designation"] == "623"
    assert item["vendor"] == "" and item["price"] == 0 and item["currency"] == ""
    cad = tmp_path / "part.step"
    cad.write_text("CAD", encoding="utf-8")
    rec = _vendor_row_to_record({"cad_path": str(cad), "supplier": "library",
                                 "part_number": "Part-1", "description": "Part-1",
                                 "unit_price": 12.5, "currency": "EUR", "stock": 7})
    item = gui_rpc._record_to_item(rec)
    assert item["name"] == "Part-1" and item["vendor"] == ""
    assert item["price"] == 12.5 and item["currency"] == "EUR" and item["price_usd"] == 0
    assert item["stock"] == 7 and "price_usd" not in rec["params"]
    assert {"kind", "thumbnail_url", "mpn", "manufacturer", "model_formats"} <= item.keys()


def test_vendor_item_preserves_source_metadata():
    record = _record("catalog:test::https://example.test/p", "catalog", "Valve",
                     params={"kind": "product", "thumbnail": "https://example.test/p.png",
                             "mpn": "P-42", "manufacturer": "Acme", "stock": 0,
                             "model_formats": ["step", "iges"], "material": "Brass"})
    record["source"] = "live"
    record["sourcing"] = [{"price": 19.5, "currency": "EUR", "stock": 0}]
    item = gui_rpc._record_to_item(record)
    assert item["price"] == 19.5 and item["currency"] == "EUR" and item["price_usd"] == 0
    assert item["mpn"] == "P-42" and item["manufacturer"] == "Acme" and item["stock"] == 0
    assert item["kind"] == "product" and item["model_formats"] == ["step", "iges"]
    assert item["thumbnail_url"] == "https://example.test/p.png" and item["material"] == "Brass"


def test_live_jobs_are_superseded_only_within_client(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(gui_rpc, "_cef_unavailable", lambda: False)
    monkeypatch.setattr(gui_rpc, "_routed_sites", lambda query: ["printables"])
    monkeypatch.setattr(gui_rpc, "_vendor_cache_get", lambda *args: None)
    monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *args: None)

    def community(site, query):
        release.wait(5)
        return [_record(f"catalog:{site}::https://example.test/{query}",
                        "catalog", query, params={"site": site})]

    monkeypatch.setattr(gui_rpc, "_community_records", community)
    tokens = []
    try:
        for client, query in (("one", "first"), ("two", "second"), ("one", "third")):
            started = gui_rpc._parts_search_live_start({"client": client, "query": query}, None)
            assert started["ok"]
            tokens.append(started["token"])
        with gui_rpc._live_lock:
            jobs = [gui_rpc._live_jobs[token] for token in tokens]
            assert jobs[0]["cancelled"] and not jobs[1]["cancelled"] and not jobs[2]["cancelled"]
            threads = list(gui_rpc._live_threads)
        release.set()
        for thread in threads:
            thread.join(timeout=5)
        assert all(job["pending"] == 0 and job["done"] for job in jobs)
        assert jobs[1]["results"] and jobs[2]["results"]
    finally:
        release.set()
        with gui_rpc._live_lock:
            for token in tokens:
                gui_rpc._live_jobs.pop(token, None)


def test_live_conversion_exception_completes_and_is_not_cached(monkeypatch):
    from services import parts
    monkeypatch.setattr(gui_rpc, "_vendor_cache_get", lambda *args: None)
    saved = []
    monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *args: saved.append(args))
    monkeypatch.setattr(parts, "search", lambda *args, **kwargs: [
        _record("catalog:test::https://example.test/a", "catalog", "A")])
    monkeypatch.setattr(gui_rpc, "_filter_items", lambda *args: (_ for _ in ()).throw(ValueError("bad record")))
    job = {"results": [], "notices": [], "seen_ids": set(), "seen_notices": set(),
           "pending": 1, "done": False, "cancelled": False,
           "vendors": {"test": {"status": "queued"}}}
    gui_rpc._run_live_site_inner("test", {"vendor_query": "a", "facet_defs": []}, 10, {}, job)
    assert job["done"] and job["pending"] == 0 and job["vendors"]["test"]["status"] == "error"
    assert job["notices"][0]["site"] == "test" and "bad record" in job["notices"][0]["hint"]
    assert saved == []


def test_bolts_reload_when_fingerprint_changes(monkeypatch, tmp_path):
    from services.parts import hub as hub_module
    from services.parts.adapters.bolts_adapter import BoltsAdapter

    table = tmp_path / "fixture.blt.yaml"
    table.write_text('standards:\n- id: ISO 1\n  name: First Bearing\n  kind: bearing\n'
                     '  family: bearing\n  sizes:\n    "123": {bore: 1}\n', encoding="utf-8")
    monkeypatch.setattr(hub_module, "_index_cache_path", lambda: None)
    monkeypatch.setattr(hub_module, "_fingerprint_sources", lambda: [])
    monkeypatch.setattr(hub_module, "_watched_library_root", lambda: None)
    hub = hub_module.PartsHub()
    hub.register(BoltsAdapter(str(tmp_path)))
    assert hub.search("", include_live=False)[0]["title"] == "First Bearing 123"
    table.write_text(table.read_text(encoding="utf-8").replace("First Bearing", "Updated Bearing"),
                     encoding="utf-8")
    assert hub.revalidate()
    assert hub.search("", include_live=False)[0]["title"] == "Updated Bearing 123"


def test_scanner_reconciles_only_its_root(monkeypatch, tmp_path):
    from services.parts import parts_library, vendor_parts
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", tmp_path / "registry")
    monkeypatch.setattr(vendor_parts, "INDEX", tmp_path / "registry" / "index.json")
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "left.step").write_text("CAD", encoding="utf-8")
    (right / "right.step").write_text("CAD", encoding="utf-8")
    parts_library.scan_library_folder(str(left))
    parts_library.scan_library_folder(str(right))
    (left / "left.step").unlink()
    (right / "right.step").unlink()
    parts_library.scan_library_folder(str(left))
    rows = vendor_parts.list_parts()
    assert len(rows) == 1 and Path(rows[0]["cad_path"]).parent == right


def test_cache_separates_profile_auth_state(monkeypatch, tmp_path):
    from services import paths
    from services.browser import profiles

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(gui_rpc, "_vendor_cache_ready", False)
    profile = tmp_path / "profile"
    monkeypatch.setattr(profiles, "_profile_storage", lambda site, create=False: profile)
    records = [{"part_id": "catalog:test::https://example.test/1", "title": "Part"}]
    gui_rpc._vendor_cache_put("test", "part", 10, records)
    assert gui_rpc._vendor_cache_get("test", "part", 10) == records
    Path(str(profile) + ".json").write_text('{"login_verified_ts":123}', encoding="utf-8")
    assert gui_rpc._vendor_cache_get("test", "part", 10) is None
    failure = {"ok": False, "part_id": "catalog:test::failed", "title": "Failed",
               "site": "test", "reason": "blocked"}
    gui_rpc._vendor_cache_put("test", "failure", 10, [failure])
    assert gui_rpc._vendor_cache_get("test", "failure", 10) is None
    clean, notices = gui_rpc._split_notices([failure])
    assert not clean and notices[0]["reason"] == "blocked"


def test_provider_errors_are_exposed_in_index_status(monkeypatch):
    from services.parts import hub as hub_module

    class BrokenProvider:
        domain = "bearing"
        provider_id = "broken"

        def iter_records(self):
            raise ValueError("bad provider data")
            yield

    monkeypatch.setattr(hub_module, "_index_cache_path", lambda: None)
    monkeypatch.setattr(hub_module, "_fingerprint_sources", lambda: [])
    monkeypatch.setattr(hub_module, "_watched_library_root", lambda: None)
    hub = hub_module.PartsHub()
    hub.register(BrokenProvider())
    assert hub.search("", include_live=False) == []
    assert any(d["provider"] == "broken" and d["operation"] == "enumeration"
               and "bad provider data" in d["error"] for d in hub.index_status()["diagnostics"])


def test_explicit_live_search_is_not_suppressed_by_local_matches(monkeypatch):
    from services.parts import hub as hub_module

    class Local:
        provider_id = "local"
        domain = "bearing"

        def iter_records(self):
            yield _record("bearing:123", "bearing", "Bearing 123", designation="123")

    class Live:
        provider_id = "live"
        domain = "catalog"
        site = "test"

        def count(self):
            raise NotImplementedError

        def iter_records(self):
            raise NotImplementedError

        def search(self, query, **kwargs):
            return [_record("catalog:test::https://example.test/a", "catalog", "Bearing 123")]

    monkeypatch.setattr(hub_module, "_index_cache_path", lambda: None)
    monkeypatch.setattr(hub_module, "_fingerprint_sources", lambda: [])
    monkeypatch.setattr(hub_module, "_watched_library_root", lambda: None)
    hub = hub_module.PartsHub()
    hub.register(Local())
    hub.register(Live())
    results = hub.search("123", domain=["bearing", "catalog"], limit=4, force_live=True)
    assert any(row.get("part_id", "").startswith("catalog:test::") for row in results)


def test_worker_explicit_live_requests_force_vendor_discovery(monkeypatch):
    from services import parts
    from services.parts.worker_rpc import _part_search

    calls = []
    monkeypatch.setattr(parts, "search", lambda query, **kwargs: calls.append(kwargs) or [])
    assert _part_search({"query": "part", "include_live": True}, None)["ok"]
    assert calls[-1]["include_live"] and calls[-1]["force_live"]
    assert _part_search({"query": "part", "include_live": False}, None)["ok"]
    assert calls[-1]["include_live"] is False and "force_live" not in calls[-1]


def test_gui_member_choice_delegates_to_owner_scoped_queue(monkeypatch):
    from services.browser import queue

    calls = []
    monkeypatch.setattr(queue, "select_member",
                        lambda job_id, member, fmt: calls.append((job_id, member, fmt))
                        or {"ok": True, "id": "new-job", "status": "pending"}, raising=False)
    monkeypatch.setattr(queue, "ensure_worker_started", lambda: calls.append("started"))
    response = gui_rpc._parts_download_select({"job_id": "old-job", "member": "parts/a.step"}, None)
    assert response == {"ok": True, "id": "new-job", "status": "pending"}
    assert calls == [("old-job", "parts/a.step", None), "started"]


def test_gui_import_validates_real_cad_and_zip_members(monkeypatch, tmp_path):
    import zipfile
    from services.mcp import core_bridge

    imported = []
    def call(method, params):
        imported.append((method, Path(params["path"]).read_bytes()))
        return {"ok": True}

    monkeypatch.setattr(core_bridge, "call", call)
    bad = tmp_path / "fake.step"
    bad.write_text("<html>Not a CAD model</html>", encoding="utf-8")
    assert gui_rpc._parts_import_artifact({"artifact_path": str(bad)}, None)["reason"] == "invalid_cad"
    assert imported == []
    cad = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"
    archive = tmp_path / "models.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.step", "<html>not a model</html>")
        zf.writestr("b.step", cad)
    assert gui_rpc._parts_import_artifact({"artifact_path": str(archive)}, None)["ok"]
    assert imported == [("part_import_file", cad)]
    selected_cad = cad.replace(b"HEADER;", b"HEADER; selected")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.step", cad)
        zf.writestr("parts/b.step", selected_cad)
    ambiguous = gui_rpc._parts_import_artifact({"artifact_path": str(archive)}, None)
    assert ambiguous["reason"] == "invalid_cad" and len(ambiguous["candidates"]) == 2
    assert gui_rpc._parts_import_artifact({"artifact_path": str(archive), "format": "zip",
                                           "member": "parts/b.step"}, None)["ok"]
    assert imported[-1] == ("part_import_file", selected_cad)
    assert gui_rpc._parts_import_artifact({"artifact_path": str(archive), "format": "iges",
                                           "member": "parts/b.step"}, None)["reason"] == "invalid_cad"
    assert gui_rpc._parts_import_artifact({"artifact_path": str(archive), "format": "step",
                                           "member": "missing.step"}, None)["reason"] == "invalid_cad"
    assert len(imported) == 2
    extracted = tmp_path / "selected.step"
    extracted.write_bytes(selected_cad)
    assert gui_rpc._parts_import_artifact({"artifact_path": str(extracted), "format": "zip",
                                           "member": "parts/b.step"}, None)["ok"]
    assert imported[-1] == ("part_import_file", selected_cad)
    freecad = tmp_path / "part.fcstd"
    with zipfile.ZipFile(freecad, "w") as zf:
        zf.writestr("Document.xml", "<Document><Objects/></Document>")
    unsupported = gui_rpc._parts_import_artifact({"artifact_path": str(freecad)}, None)
    assert unsupported["reason"] == "unsupported_import_format"
    assert unsupported["artifact_path"] == str(freecad) and unsupported["format"] == "fcstd"
    assert len(imported) == 3


def test_core_family_title_uses_catalog_label_and_preserves_description():
    from services.parts.adapters.core_families_adapter import CoreFamiliesAdapter

    adapter = CoreFamiliesAdapter()
    adapter._catalog = [{"key": "shcs", "name": "Socket Head Cap Screw",
                         "description": "Cylindrical socket head; the default machine screw.",
                         "sizes": ["M3"]}]
    record = next(adapter.iter_records())
    assert record["title"] == "Socket Head Cap Screw M3"
    assert record["family"] == "Socket Head Cap Screw"
    assert record["description"] == "Cylindrical socket head; the default machine screw."
    assert record["description"] in gui_rpc._record_to_item(record)["description"]
    index = PartsIndex()
    index.rebuild(iter([record]))
    assert index.search("cylindrical")[0]["part_id"] == record["part_id"]


def test_kicad_footprint_has_readable_title_and_preserves_identity():
    from services.parts.adapters.ecad_kicad_adapter import _footprint_record

    fpid = "MountingHole:MountingHole_3.2mm_M3"
    record = _footprint_record(fpid, {"description": "Mounting hole for M3 screw"},
                               license_="test", provenance="test")
    assert record["title"] == "Mounting Hole 3.2 mm M3 (footprint)"
    assert record["family"] == "Mounting Hole"
    assert record["designation"] == fpid
    assert record["geometry_ref"]["footprint"] == fpid
    assert record["params"]["description"] == "Mounting hole for M3 screw"
    assert "Mounting hole for M3 screw" in gui_rpc._record_to_item(record)["description"]


def test_lazy_hub_initializes_only_once_concurrently(monkeypatch):
    from services import parts

    started, release = threading.Event(), threading.Event()
    instances = []
    class FakeHub:
        def __init__(self):
            instances.append(self)

        def bootstrap(self):
            started.set()
            release.wait(5)

    monkeypatch.setattr(parts, "_hub", None)
    monkeypatch.setattr(parts, "PartsHub", FakeHub)
    results = []
    threads = [threading.Thread(target=lambda: results.append(parts._get_hub())) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        assert started.wait(5)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert len(instances) == 1 and results == [instances[0]] * 6


def test_equivalent_native_recipes_collapse_for_browse_and_search(monkeypatch):
    from services.parts.adapters.bolts_adapter import BoltsAdapter
    from services.parts.adapters.core_families_adapter import _make_record
    from services.parts.hub import PartsHub

    bolt = BoltsAdapter().get("bearing:ISO 15:6000")
    core = _make_record("bearing", "6000", "Native bearing envelope",
                        {"name": "Deep-Groove Ball Bearing", "standard": "ISO 15 (deep groove)",
                         "needs_length": False, "lengths_mm": []})
    assert bolt is not None
    index = PartsIndex()
    index.rebuild(iter([bolt, core]))
    hub = PartsHub()
    hub._index = index
    hub._index_built = True
    monkeypatch.setattr(hub, "_ensure_index", lambda: None)
    monkeypatch.setattr(hub, "_maybe_revalidate", lambda: None)
    for query in ("", "6000"):
        result = hub.search(query, limit=20, include_live=False)
        assert len(result) == 1
        assert result[0]["part_id"] == bolt["part_id"]
        assert result[0]["params"]["bore"] == 10
        assert result[0]["geometry_ref"]["lengths_mm"] == []
        assert "mech_family:bearing:6000" in result[0]["related_ids"]
        assert "ISO 15 (deep groove)" in result[0]["aliases"]
    assert len(hub.search("", domain="bearing", include_live=False)) == 1
    assert len(hub.search("", domain="mech_family", include_live=False)) == 1


def test_button_screw_duplicates_do_not_merge_other_families():
    from services.parts.adapters.bolts_adapter import BoltsAdapter
    from services.parts.adapters.core_families_adapter import _make_record
    from services.parts.hub import _merge_native_duplicates

    button = BoltsAdapter().get("fastener:ISO 7380:M10")
    assert button is not None
    core = _make_record("bhcs", "M10", "Button head screw",
                        {"name": "Button Head Cap Screw", "standard": "ISO 7380",
                         "needs_length": True, "lengths_mm": [10, 20]})
    socket = _make_record("shcs", "M10", "Socket head screw",
                          {"name": "Socket Head Cap Screw", "standard": "ISO 4762"})
    result = _merge_native_duplicates([core, socket, button])
    assert len(result) == 2
    merged = next(rec for rec in result if rec["part_id"] == button["part_id"])
    assert merged["geometry_ref"]["lengths_mm"] == [10, 20]
    assert "BHCS" in merged["aliases"]
    assert any(rec["part_id"] == socket["part_id"] for rec in result)


def test_all_parts_browse_defers_native_kicad_scan(monkeypatch):
    from services import parts
    from services.parts import hub as hub_module

    calls = []
    class KiCadProvider:
        domain = "ecad"
        provider_id = "ecad_kicad"

        def iter_records(self):
            yield _record("ecad:starter", "ecad", "Starter symbol")

        def search(self, query, **kwargs):
            calls.append(query)
            return [_record("ecad:native", "ecad", "Native KiCad symbol")]

    monkeypatch.setattr(hub_module, "_index_cache_path", lambda: None)
    monkeypatch.setattr(hub_module, "_fingerprint_sources", lambda: [])
    monkeypatch.setattr(hub_module, "_watched_library_root", lambda: None)
    hub = hub_module.PartsHub()
    hub.register(KiCadProvider())
    monkeypatch.setattr(parts, "_hub", hub)
    monkeypatch.setattr(gui_rpc, "_family_size_options", lambda records: None)
    browse = gui_rpc._parts_search({"query": "", "limit": 100}, None)
    assert browse["ok"] and calls == []
    assert browse["total_exact"] is False
    assert any(n["reason"] == "native_browse_deferred" for n in browse["notices"])
    assert any(d.get("reason") == "native_browse_deferred" for d in browse["diagnostics"])
    assert hub.search("", domain="ecad", include_live=False) and calls == [""]
    assert hub.search("native", include_live=False) and calls == ["", "native"]
