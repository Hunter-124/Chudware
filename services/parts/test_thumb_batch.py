"""Consumer-visible preview resolution and thumbnail RPC behavior, without network or CAD."""
from __future__ import annotations

import base64
import io
import os
import threading
import time

import pytest
from PIL import Image

from services.parts import gui_rpc, thumbs


def _png(color=(80, 120, 160), size=(96, 96)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


PNG = _png()
OTHER_PNG = _png((200, 10, 10))


def _item(item_id, url="https://cdn.example.com/product.png"):
    return {"item_id": item_id, "image_url": url}


def _batch(items):
    return gui_rpc._parts_thumbnails({"items": items}, lambda _: None)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(thumbs, "_network_allowed", lambda: True)
    monkeypatch.setattr(thumbs, "_record_for_part", lambda part_id: None)
    monkeypatch.setattr(thumbs, "_last_trim", 0.0)
    return thumbs


def test_registration_and_empty_or_invalid_batch(isolated):
    assert callable(gui_rpc.GUI_WORKER_METHODS["parts_thumbnail"])
    assert callable(gui_rpc.GUI_WORKER_METHODS["parts_thumbnails"])
    assert _batch([]) == {"ok": True, "images": []}
    assert not _batch("invalid")["ok"]
    assert not _batch([None])["images"][0]["ok"]


def test_ordered_batches_larger_than_32_and_rpc_shape(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(thumbs, "_fetch_image_bytes",
                        lambda url, pid, deadline, **kw: (calls.append(pid) or PNG, 200))
    items = [_item(f"catalog:example::{n}") for n in range(70)]
    result = thumbs.resolve_previews(items)
    assert [r["item_id"] for r in result] == [i["item_id"] for i in items]
    assert len(calls) == 70
    assert all(r == {"item_id": i["item_id"], "ok": True, "data": PNG,
                     "mime": "image/png", "provenance": "product_image", "reason": ""}
               for r, i in zip(result, items))
    rpc = _batch(items)
    assert rpc["ok"] and len(rpc["images"]) == 70
    assert [r["item_id"] for r in rpc["images"]] == [i["item_id"] for i in items]
    assert all(base64.b64decode(r["image_b64"]) == PNG and r["mime"] == "image/png"
               and r["provenance"] == "product_image" for r in rpc["images"])
    assert len(calls) == 70  # cache hits never fetch again
    one = gui_rpc._parts_thumbnail(items[0], lambda _: None)
    assert one == rpc["images"][0]


def test_per_item_failure_does_not_break_batch_or_singular_rpc(isolated, monkeypatch):
    def fetch(url, pid, deadline, **kwargs):
        if pid.endswith("bad"):
            raise RuntimeError("CDN failure")
        return PNG, 200
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", fetch)
    items = [_item("catalog:x::good"), _item("catalog:x::bad"), _item("catalog:x::other")]
    response = _batch(items)
    assert response["ok"]
    assert [row["ok"] for row in response["images"]] == [True, False, True]
    assert response["images"][1]["reason"] == "thumbnail_failed"
    assert gui_rpc._parts_thumbnail(items[1], lambda _: None) == response["images"][1]
    assert gui_rpc._parts_thumbnail({}, lambda _: None)["reason"] == "item_id required"


def test_cache_keys_include_image_url_and_render_version(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(thumbs, "_fetch_image_bytes",
                        lambda url, pid, deadline, **kw:
                        (calls.append(url) or (PNG if url.endswith("a.png") else OTHER_PNG), 200))
    a, b = _item("catalog:x::same", "https://cdn.example.com/a.png"), \
           _item("catalog:x::same", "https://cdn.example.com/b.png")
    assert thumbs.resolve_previews([a])[0]["data"] == PNG
    assert thumbs.resolve_previews([b])[0]["data"] == OTHER_PNG
    assert thumbs.resolve_previews([a])[0]["data"] == PNG
    assert len(calls) == 2
    monkeypatch.setattr(thumbs, "_RENDER_VERSION", "new-render-version")
    assert thumbs.resolve_previews([a])[0]["data"] == PNG
    assert len(calls) == 3


def test_network_failures_have_short_ttl_but_404_is_long(isolated, monkeypatch):
    calls = []
    status = {"value": 503}
    def fetch(url, pid, deadline, **kwargs):
        calls.append(status["value"])
        return None, status["value"]
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", fetch)
    item = _item("catalog:x::part")
    assert thumbs.resolve_previews([item])[0]["reason"] == "transport_failed"
    assert thumbs.resolve_previews([item])[0]["reason"] == "transport_failed"
    assert calls == [503]
    miss = thumbs._cache_paths(item["item_id"], item["image_url"], "product")[1]
    old = time.time() - 61
    os.utime(miss, (old, old))
    status["value"] = 404
    assert thumbs.resolve_previews([item])[0]["reason"] == "no_preview"
    assert calls == [503, 404]
    os.utime(miss, (old, old))
    assert thumbs.resolve_previews([item])[0]["reason"] == "no_preview"
    assert calls == [503, 404]  # 404 remains cached after the transient TTL
    old = time.time() - 86401
    os.utime(miss, (old, old))
    status["value"] = 200
    monkeypatch.setattr(thumbs, "_fetch_image_bytes",
                        lambda url, pid, deadline, **kw: (PNG, 200))
    assert thumbs.resolve_previews([item])[0]["data"] == PNG
    assert not miss.exists()


def test_negative_cache_does_not_hide_a_changed_url(isolated, monkeypatch):
    def fetch(url, pid, deadline, **kwargs):
        return (None, 404) if url.endswith("missing.png") else (PNG, 200)
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", fetch)
    missing = _item("catalog:x::part", "https://cdn.example.com/missing.png")
    replacement = _item("catalog:x::part", "https://cdn.example.com/current.png")
    assert thumbs.resolve_previews([missing])[0]["reason"] == "no_preview"
    assert thumbs.resolve_previews([replacement])[0]["data"] == PNG


def test_logo_is_never_a_part_preview_but_footer_logos_still_work(isolated, monkeypatch):
    from services.net import safe_http
    s2 = "https://www.google.com/s2/favicons?domain=thk.com&sz=64"
    alt = thumbs._gstatic_favicon_url(s2)
    asked = []
    def get(url, **kwargs):
        asked.append(url)
        return safe_http.Response(404 if url == s2 else 200, {}, url,
                                  PNG if url == s2 else OTHER_PNG)
    monkeypatch.setattr(safe_http, "get", get)
    rows = ["vendorlogo:thk", "catalog:x::part"]
    assert all(not r["ok"] and r["reason"] == "no_preview"
               for r in thumbs.resolve_previews([_item(row, s2) for row in rows]))
    assert asked == []
    rpc = _batch([_item(rows[1], s2), _item(rows[0], s2)])
    part, logo = rpc["images"]
    assert not part["ok"] and part["reason"] == "no_preview"
    assert logo["ok"] and logo["provenance"] == ""
    assert base64.b64decode(logo["image_b64"]) == OTHER_PNG
    assert asked == [s2, alt]
    assert gui_rpc._parts_thumbnail(_item(rows[0], s2), lambda _: None)["ok"]
    assert asked == [s2, alt]


def test_footer_can_keep_globe_if_alternate_favicon_is_missing(isolated, monkeypatch):
    from services.net import safe_http
    s2 = "https://www.google.com/s2/favicons?domain=thk.com&sz=64"
    alt = thumbs._gstatic_favicon_url(s2)
    asked = []
    def get(url, **kwargs):
        asked.append(url)
        return safe_http.Response(404, {}, url, PNG if url == s2 else b"")
    monkeypatch.setattr(safe_http, "get", get)
    result = _batch([_item("vendorlogo:thk", s2)])["images"][0]
    assert result["ok"] and base64.b64decode(result["image_b64"]) == PNG
    assert asked == [s2, alt]


@pytest.mark.parametrize("url", ["https://cdn.example.com/logo.png", "https://x/favicon.ico",
                                "https://icon.horse/icon/example.com",
                                "https://cdn.example.com/images/placeholder.jpg"])
def test_obvious_logo_urls_are_not_fetched_for_parts(isolated, monkeypatch, url):
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", lambda *a, **kw: pytest.fail("logo fetched"))
    assert thumbs.resolve_previews([_item("catalog:x::part", url)])[0]["reason"] == "no_preview"
    def unavailable(_):
        raise RuntimeError("hub unavailable")
    monkeypatch.setattr(thumbs, "_record_for_part", unavailable)
    assert thumbs.resolve_previews([_item("catalog:x::another", url)])[0]["reason"] == "no_preview"


@pytest.mark.parametrize("status,body", [(404, PNG), (503, PNG), (200, b"<!DOCTYPE html>oops"),
                                          (200, b"\x89PNG\r\n\x1a\ninvalid"),
                                          (200, _png(size=(1, 1)))])
def test_error_bodies_and_invalid_or_tiny_rasters_are_rejected(isolated, monkeypatch, status, body):
    from services.net import safe_http
    monkeypatch.setattr(safe_http, "get", lambda url, **kw: safe_http.Response(status, {}, url, body))
    result = thumbs.resolve_previews([_item("catalog:x::part")])[0]
    assert not result["ok"] and result["data"] is None and not result["provenance"]


def test_redirect_to_logo_cannot_be_misrepresented(isolated, monkeypatch):
    from services.net import safe_http
    monkeypatch.setattr(safe_http, "get", lambda url, **kw:
                        safe_http.Response(200, {}, "https://cdn.example.com/logo.png", PNG))
    assert thumbs.resolve_previews([_item("catalog:x::part")])[0]["reason"] == "no_preview"


def test_http_request_is_bounded_and_preserves_hotlink_headers(isolated, monkeypatch):
    from services.net import safe_http
    seen = []
    def get(url, **kwargs):
        seen.append(kwargs)
        return safe_http.Response(200, {}, url, PNG)
    monkeypatch.setenv("CHUDWARE_THUMB_FETCH_TIMEOUT_S", "1.5")
    monkeypatch.setattr(safe_http, "get", get)
    item = _item("catalog:mcmaster::https://mcmaster.com/p/")
    assert thumbs.resolve_previews([item])[0]["data"] == PNG
    assert seen[0]["max_bytes"] == 4 * 1024 * 1024
    assert 0 < seen[0]["idle_timeout"] <= 1.5
    assert seen[0]["headers"]["Referer"] == "https://mcmaster.com/p/"
    assert seen[0]["headers"]["User-Agent"].startswith("Mozilla/")


def test_offline_or_denied_network_never_fetches_or_poison_cache(tmp_path, monkeypatch):
    from services.agent import config
    monkeypatch.setattr(thumbs, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "local_only_enabled", lambda: True)
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: None)
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", lambda *a, **kw: pytest.fail("network used"))
    item = _item("catalog:x::offline")
    assert thumbs.resolve_previews([item])[0]["reason"] == "network_disabled"
    assert thumbs.resolve_previews([item], allow_network=False)[0]["reason"] == "network_disabled"
    assert not list(tmp_path.glob("*.miss"))


def test_external_risk_denial_blocks_network(tmp_path, monkeypatch):
    from services.agent import config
    from services.mcp import tool_registry
    monkeypatch.setattr(thumbs, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "local_only_enabled", lambda: False)
    monkeypatch.setattr(tool_registry, "is_risk_denied", lambda risk: risk == tool_registry.RISK_EXTERNAL)
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: None)
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", lambda *a, **kw: pytest.fail("network used"))
    assert thumbs.resolve_previews([_item("catalog:x::part")])[0]["reason"] == "network_disabled"


def test_local_reveng_provider_can_resolve_without_catalog_acquisition(monkeypatch):
    from services import parts
    calls = []
    record = {"part_id": "reveng:local", "geometry_ref": {"kind": "import_file", "path": "part.stl"}}
    class LocalProvider:
        provider_id = "reveng_results"
        def get(self, item_id):
            calls.append(("reveng", item_id))
            return record
    class NetworkProvider:
        provider_id = "browser"
        def get(self, item_id):
            pytest.fail("catalog provider was called")
    class Registry:
        def providers(self):
            return [NetworkProvider(), LocalProvider()]
    class Hub:
        _registry = Registry()
    monkeypatch.setattr(parts, "get", lambda item_id, **kwargs:
                        calls.append(("index", item_id, kwargs)) or None)
    monkeypatch.setattr(parts, "_get_hub", lambda: Hub())
    assert thumbs._record_for_part("reveng:local") == record
    assert calls == [("index", "reveng:local", {"include_live": False, "acquire": False}),
                     ("reveng", "reveng:local")]


def test_bearings_and_fasteners_use_record_geometry_and_close_documents(isolated, monkeypatch):
    from services.mcp import core_bridge
    records = {
        "bearing:ISO 15:623": {"geometry_ref": {"kind": "core_family", "family": "bearing", "size": "623"}},
        "fastener:ISO 4762:M5": {"geometry_ref": {"kind": "core_family", "family": "shcs", "size": "M5"}},
    }
    monkeypatch.setattr(thumbs, "_record_for_part", lambda item_id: records[item_id])
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "parts_make":
            return {"ok": True, "doc_id": "preview-doc"}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
            return {"ok": True}
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    items = [_item(pid, "") for pid in records]
    results = thumbs.resolve_previews(items)
    assert all(r["ok"] and r["data"] == PNG and r["provenance"] == "cad_render" for r in results)
    assert {tuple(sorted(params.items())) for method, params in calls if method == "parts_make"} == {
        (("family", "bearing"), ("size", "623")), (("family", "shcs"), ("size", "M5"))}
    assert sum(method == "doc_close" for method, _ in calls) == 2
    assert all(params["doc_id"] == "preview-doc" for method, params in calls if method == "doc_close")


@pytest.mark.parametrize("extension", [".step", ".stp", ".iges", ".igs", ".stl"])
def test_imported_cad_renders_and_file_changes_invalidate_cache(isolated, tmp_path, monkeypatch, extension):
    from services.mcp import core_bridge
    model = tmp_path / ("part" + extension)
    model.write_bytes(b"local model")
    record = {"geometry_ref": {"kind": "import_file", "path": str(model)}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "part_import_file":
            return {"ok": True, "doc_id": "imported"}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    item = _item("library:mine::part", "")
    assert thumbs.resolve_previews([item])[0]["provenance"] == "cad_render"
    assert thumbs.resolve_previews([item])[0]["ok"]
    assert sum(method == "part_import_file" for method, _ in calls) == 1
    assert next(params for method, params in calls if method == "part_import_file") == {
        "path": str(model), "as_solid": extension == ".stl"}
    model.write_bytes(b"changed local model")
    assert thumbs.resolve_previews([item])[0]["ok"]
    assert sum(method == "part_import_file" for method, _ in calls) == 2
    assert sum(method == "doc_close" for method, _ in calls) == 2


def test_unsupported_or_oversized_import_is_not_rendered(isolated, tmp_path, monkeypatch):
    path = tmp_path / "part.pdf"
    path.write_bytes(b"not cad")
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: {"geometry_ref": {"kind": "import_file", "path": str(path)}})
    monkeypatch.setattr(thumbs, "_render_local_part_png", lambda *a: pytest.fail("not CAD"))
    assert thumbs.resolve_previews([_item("library:x", "")])[0]["reason"] == "no_preview"
    path = tmp_path / "large.stl"
    path.write_bytes(b"stl")
    monkeypatch.setattr(thumbs, "_MAX_CAD_BYTES", 2)
    assert thumbs.resolve_previews([_item("library:x", "")])[0]["reason"] == "no_preview"


def test_unbounded_parametric_recipe_is_not_rendered(isolated, monkeypatch):
    record = {"geometry_ref": {"kind": "parametric_heatsink", "spec": {"n_fins": 10_000_000}}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    monkeypatch.setattr(thumbs, "_render_local_part_png", lambda *a: pytest.fail("unbounded recipe"))
    assert thumbs.resolve_previews([_item("thermal:heatsink", "")])[0]["reason"] == "no_preview"


def test_parametric_recipe_creates_real_native_document(isolated, monkeypatch):
    from services.mcp import core_bridge
    record = {"geometry_ref": {"kind": "parametric_shaft_collar",
                              "spec": {"bore_mm": 5, "od_mm": 10, "width_mm": 4}}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_new":
            return {"ok": True, "doc_id": "parametric", "revision": 0}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    assert thumbs.resolve_previews([_item("motion:collar", "")])[0]["provenance"] == "cad_render"
    assert [method for method, _ in calls] == ["doc_new", "doc_apply_ops", "doc_render_png", "doc_close"]
    assert len(calls[1][1]["ops"]) == 2


def test_new_parametric_placement_builders_are_discovered(isolated, monkeypatch):
    from services.mcp import core_bridge, parts_tools
    record = {"part_id": "custom:one", "geometry_ref": {"kind": "parametric_custom",
                                                           "spec": {"diameter": 8}}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    monkeypatch.setitem(parts_tools._PARAMETRIC_BUILDERS, "parametric_custom",
                        lambda spec: ("Custom", [{"op": "add_feature", "feature":
                                                {"type": "cylinder", "params": spec}}], {}))
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_new":
            return {"ok": True, "doc_id": "new-parametric", "revision": 0}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    result = thumbs.resolve_previews([_item(record["part_id"], "")])[0]
    assert result["ok"] and result["provenance"] == "cad_render"
    assert [method for method, _ in calls] == ["doc_new", "doc_apply_ops", "doc_render_png", "doc_close"]


def test_new_native_placement_kinds_render_only_new_documents(isolated, monkeypatch):
    from services.mcp import core_bridge
    record = {"part_id": "section:example", "geometry_ref": {"kind": "section_profile"},
              "params": {"doc_id": "user-doc", "targetDocumentId": "user-doc", "length_mm": 100}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [{"doc_id": "user-doc"}]}
        if method == "part_place":
            return {"ok": True, "doc_id": "new-part", "entity_id": "new-part", "editable": True}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    result = thumbs.resolve_previews([_item(record["part_id"], "")])[0]
    assert result["ok"] and result["provenance"] == "cad_render"
    assert [method for method, _ in calls] == ["doc_list", "part_place", "doc_render_png", "doc_close"]
    assert calls[1][1]["record"]["params"] == {"length_mm": 100}
    assert calls[-1][1]["doc_id"] == "new-part"


@pytest.mark.parametrize("placement", [
    {"ok": True, "doc_id": "user-doc", "entity_id": "symbol", "editable": True, "doc": {"schema": "aicad.sch/1"}},
    {"ok": True, "doc_id": "user-doc", "entity_id": "user-doc", "editable": True},
])
def test_schema_only_placement_never_renders_or_closes_user_doc(isolated, monkeypatch, placement):
    from services.mcp import core_bridge
    record = {"part_id": "schema:one", "geometry_ref": {"kind": "schema_only"},
              "params": {"doc_id": "user-doc"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append((method, dict(params)))
        if method == "doc_list":
            return {"docs": [{"doc_id": "user-doc"}]}
        return placement
    monkeypatch.setattr(core_bridge, "call", call)
    result = thumbs.resolve_previews([_item(record["part_id"], "")])[0]
    assert not result["ok"] and result["reason"] == "no_preview"
    assert [method for method, _ in calls] == ["doc_list", "part_place"]
    assert calls[1][1]["record"]["params"] == {}


def test_missing_standalone_path_does_not_block_new_native_recipe(isolated, monkeypatch):
    from services.mcp import core_bridge
    record = {"part_id": "section:example", "geometry_ref": {"kind": "section_profile"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    available = False
    def call(method, params):
        if method == "doc_list":
            return {"docs": []}
        if method == "part_place":
            if not available:
                return {"ok": False, "reason": "No structural doc provided."}
            return {"ok": True, "doc_id": "new-part", "entity_id": "new-part", "editable": True}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    item = _item(record["part_id"], "")
    assert thumbs.resolve_previews([item])[0]["reason"] == "no_preview"
    available = True
    assert thumbs.resolve_previews([item])[0]["provenance"] == "cad_render"


@pytest.mark.parametrize("name,should_remove", [("chudware-spec-model-123.step", True),
                                                ("user-model.step", False)])
def test_generated_preview_removes_only_its_temporary_model(isolated, tmp_path, monkeypatch,
                                                            name, should_remove):
    from services.mcp import core_bridge
    record = {"part_id": "generated:part", "geometry_ref": {"kind": "generate_model"}}
    generated = tmp_path / name
    generated.write_bytes(b"geometry")
    monkeypatch.setattr(thumbs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    def call(method, params):
        if method == "doc_list":
            return {"docs": []}
        if method == "part_place":
            return {"ok": True, "doc_id": "new-part", "entity_id": "new-part", "editable": True,
                    "detail": {"step_path": str(generated)}}
        if method == "doc_render_png":
            with open(params["out_path"], "wb") as stream:
                stream.write(PNG)
        if method == "doc_close":
            return {"closed": True}
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    assert thumbs.resolve_previews([_item(record["part_id"], "")])[0]["provenance"] == "cad_render"
    assert generated.exists() is not should_remove


def test_generated_preview_never_deletes_model_of_existing_doc(isolated, tmp_path, monkeypatch):
    from services.mcp import core_bridge
    record = {"part_id": "generated:part", "geometry_ref": {"kind": "generate_model"}}
    generated = tmp_path / "chudware-spec-model-123.step"
    generated.write_bytes(b"geometry")
    monkeypatch.setattr(thumbs.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append(method)
        if method == "doc_list":
            return {"docs": [{"doc_id": "user-doc"}]}
        return {"ok": True, "doc_id": "user-doc", "entity_id": "user-doc", "editable": True,
                "detail": {"step_path": str(generated)}}
    monkeypatch.setattr(core_bridge, "call", call)
    assert thumbs.resolve_previews([_item(record["part_id"], "")])[0]["reason"] == "no_preview"
    assert calls == ["doc_list", "part_place"]
    assert generated.exists()


def test_render_failure_is_retryable_and_document_is_closed(isolated, monkeypatch):
    from services.mcp import core_bridge
    record = {"geometry_ref": {"kind": "core_family", "family": "bearing", "size": "623"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    calls = []
    def call(method, params):
        calls.append(method)
        if method == "parts_make":
            return {"ok": True, "doc_id": "preview"}
        if method == "doc_render_png":
            return {"ok": False}
        return {"ok": True}
    monkeypatch.setattr(core_bridge, "call", call)
    item = _item("bearing:ISO 15:623", "")
    assert thumbs.resolve_previews([item])[0]["reason"] == "render_unavailable"
    assert calls == ["parts_make", "doc_render_png", "doc_close"]
    assert thumbs.resolve_previews([item])[0]["reason"] == "render_unavailable"
    assert calls.count("parts_make") == 1
    identity = thumbs._geometry_identity(record)
    miss = thumbs._cache_paths(item["item_id"], "", "cad", identity)[1]
    old = time.time() - 61
    os.utime(miss, (old, old))
    assert thumbs.resolve_previews([item])[0]["reason"] == "render_unavailable"
    assert calls.count("parts_make") == 2


def test_native_renders_are_serial_but_fetches_can_overlap(isolated, monkeypatch):
    counts = {"render": 0, "render_peak": 0, "fetch": 0, "fetch_peak": 0}
    lock = threading.Lock()
    def hold(kind):
        with lock:
            counts[kind] += 1
            counts[kind + "_peak"] = max(counts[kind + "_peak"], counts[kind])
        time.sleep(0.05)
        with lock:
            counts[kind] -= 1
    record = {"geometry_ref": {"kind": "core_family", "family": "bearing", "size": "623"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    monkeypatch.setattr(thumbs, "_render_local_part_png", lambda *a: (hold("render"), PNG)[1])
    monkeypatch.setattr(thumbs, "_fetch_image_bytes",
                        lambda *a, **kw: (hold("fetch"), PNG, 200)[1:])
    items = ([_item(f"bearing:{i}", "") for i in range(4)] +
             [_item(f"catalog:x::{i}") for i in range(4)])
    result = thumbs.resolve_previews(items)
    assert all(item["ok"] for item in result)
    assert counts["render_peak"] == 1
    assert counts["fetch_peak"] > 1


def test_budget_and_worker_activity_cover_timed_out_work(isolated, monkeypatch):
    monkeypatch.setenv("CHUDWARE_THUMB_BATCH_BUDGET_S", "0.1")
    entered, finish = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        entered.set()
        finish.wait(3)
        return PNG, 200
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", fetch)
    try:
        result = _batch([_item("catalog:x::slow")])["images"][0]
        assert entered.is_set() and result["reason"] == "timeout"
        assert gui_rpc.worker_activity()["by_kind"]["parts_thumbnails"] >= 1
        assert not list(isolated._cache_dir().glob("*.miss"))
    finally:
        finish.set()
        with thumbs._futures_lock:
            futures = list(thumbs._futures)
        for future in futures:
            future.result(timeout=3)


def test_warmer_includes_indexed_native_and_parametric_ids(isolated, tmp_path, monkeypatch):
    from services.parts import gen_standard_thumbs
    model = tmp_path / "local.step"
    model.write_bytes(b"model")
    records = [{"part_id": "bearing:ISO 15:623", "geometry_ref": {"kind": "core_family", "family": "bearing", "size": "623"}},
               {"part_id": "fastener:ISO 4762:M5", "geometry_ref": {"kind": "core_family", "family": "shcs", "size": "M5"}},
               {"part_id": "motion:collar", "geometry_ref": {"kind": "parametric_shaft_collar", "spec": {}}},
               {"part_id": "thermal:heatsink", "geometry_ref": {"kind": "parametric_heatsink", "spec": {"n_fins": 5}}},
               {"part_id": "library:local", "geometry_ref": {"kind": "import_file", "path": str(model)}},
               {"part_id": "section:example", "geometry_ref": {"kind": "section_profile"}},
               {"part_id": "motion:belt", "geometry_ref": {"kind": "parametric_timing_belt"}}]
    class Index:
        record_count = len(records)
        def search(self, query, limit):
            assert query == "" and limit == len(records)
            return records
    class Hub:
        _index = Index()
        def _ensure_index(self):
            pass
    monkeypatch.setattr(gen_standard_thumbs.parts, "_get_hub", lambda: Hub())
    assert [r["part_id"] for r in gen_standard_thumbs._preview_candidates()] == [
        "bearing:ISO 15:623", "fastener:ISO 4762:M5", "motion:collar",
        "thermal:heatsink", "library:local", "section:example"]


def test_warmer_renders_cad_even_if_product_photo_is_cached(isolated, monkeypatch):
    from services.parts import gen_standard_thumbs
    item = _item("bearing:ISO 15:623")
    record = {"part_id": item["item_id"], "params": {"thumbnail": item["image_url"]},
              "geometry_ref": {"kind": "core_family", "family": "bearing", "size": "623"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    monkeypatch.setattr(thumbs, "_fetch_image_bytes", lambda *a, **kw: (PNG, 200))
    monkeypatch.setattr(thumbs, "_render_local_part_png", lambda *a: OTHER_PNG)
    monkeypatch.setattr(gen_standard_thumbs, "_preview_candidates", lambda: [record])
    assert thumbs.resolve_previews([item])[0]["provenance"] == "product_image"
    assert gen_standard_thumbs.main(["--limit", "1"]) == 0
    image, _ = thumbs._cache_paths(item["item_id"], item["image_url"], "cad",
                                   thumbs._geometry_identity(record))
    assert thumbs._cached_image(image)[0] == OTHER_PNG


def test_warmer_skips_schema_only_candidates(isolated, monkeypatch, capsys):
    from services.mcp import core_bridge
    from services.parts import gen_standard_thumbs
    record = {"part_id": "section:example", "geometry_ref": {"kind": "section_profile"}}
    monkeypatch.setattr(thumbs, "_record_for_part", lambda _: record)
    monkeypatch.setattr(gen_standard_thumbs, "_preview_candidates", lambda: [record])
    monkeypatch.setattr(core_bridge, "call", lambda method, params:
                        {"docs": []} if method == "doc_list" else
                        {"ok": False, "reason": "No structural doc provided."})
    assert gen_standard_thumbs.main([]) == 0
    assert "1 without standalone CAD" in capsys.readouterr().out
