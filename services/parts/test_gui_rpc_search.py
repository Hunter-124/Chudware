"""Parts Library GUI search routing in a desktop build without CEF."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from services.parts import gui_rpc


def test_servo_routes_to_public_cad_while_cef_vendors_are_skipped(monkeypatch):
    from services import parts

    servo = {
        "part_id": "mech_family:hobby_servo:MG90S", "domain": "mech_family",
        "title": "Micro hobby servo MG90S", "designation": "MG90S",
        "params": {"family": "hobby_servo", "size": "MG90S"},
        "geometry_ref": {"kind": "core_family", "family": "hobby_servo", "size": "MG90S"},
        "source": "local", "sourcing": [],
    }
    searched = []
    entered = threading.Event()
    release = threading.Event()
    owner = ("pytest", f"gui-search-{id(servo):x}")
    caller = threading.current_thread()
    real_owner_key = gui_rpc.owner_key
    monkeypatch.setattr(gui_rpc, "owner_key",
                        lambda: owner if threading.current_thread() is caller else real_owner_key())

    def local_search(query, *, domain=None, limit=20, include_live=True, **kwargs):
        searched.append((query, include_live))
        assert not include_live  # instant layer must not start a blocking browser fan-out
        return [dict(servo)]

    monkeypatch.setattr(parts, "search", local_search)
    monkeypatch.setattr(gui_rpc, "_family_size_options", lambda records: None)
    monkeypatch.setattr(gui_rpc, "_index_status", lambda: {"building": False, "count": 1})
    monkeypatch.setattr(gui_rpc, "_cef_unavailable", lambda: True)
    monkeypatch.setattr(gui_rpc, "_vendor_cache_get", lambda *args: None)
    monkeypatch.setattr(gui_rpc, "_vendor_cache_put", lambda *args: None)
    fetched = []

    def public_models(site, query):
        entered.set()
        release.wait()
        fetched.append((site, query))
        return [{"part_id": "catalog:printables::https://www.printables.com/model/123-servo",
                 "domain": "catalog", "title": "MG90S servo CAD",
                 "params": {"site": "printables"}, "source": "live",
                 "license": "CC BY 4.0"}]

    monkeypatch.setattr(gui_rpc, "_community_records", public_models)
    query = {"query": "MG90S", "limit": 50, "live": False}
    local = gui_rpc._parts_search(query, lambda *_: None)
    assert local["ok"] and local["results"][0]["id"] == servo["part_id"]
    assert local["results"][0]["placeable"] and local["results"][0]["score"] == 100
    assert searched == [("MG90S", False)]

    try:
        started = gui_rpc._parts_search_live_start(query, lambda *_: None)
        assert started["ok"] and started["sites"] == ["printables"]
        assert len(started["notices"]) == 1
        assert started["notices"][0]["reason"] == "cef_unavailable"
        deadline = time.monotonic() + 2.0
        assert entered.wait(max(0.0, deadline - time.monotonic())), \
            "public CAD worker did not start"
        with gui_rpc._live_lock:
            threads = [t for t, stored_owner in gui_rpc._live_threads.items()
                       if stored_owner == owner]
        assert len(threads) == 1
        pending = gui_rpc._parts_search_live_poll({"token": started["token"]}, lambda *_: None)
        assert pending["done"] is False
        release.set()
        threads[0].join(timeout=max(0.0, deadline - time.monotonic()))
        assert not threads[0].is_alive(), "public CAD worker did not finish in time"
        polled = gui_rpc._parts_search_live_poll({"token": started["token"]}, lambda *_: None)
        assert polled["done"] and fetched == [("printables", "MG90S")]
        assert len(polled["results"]) == 1
        assert polled["results"][0]["license"] == "CC BY 4.0"
        assert polled["results"][0]["source"] == "live"
        assert polled["notices"] == started["notices"]
    finally:
        release.set()
        with gui_rpc._live_lock:
            threads = [t for t, stored_owner in gui_rpc._live_threads.items()
                       if stored_owner == owner]
        for thread in threads:
            thread.join()


def test_public_cad_result_prefers_model_identity_and_displays_selected_file(monkeypatch):
    from services.browser import adapters
    from services.browser.adapters import printables

    hits = [
        {"url": f"https://www.printables.com/model/{number}-{slug}", "title": title,
         "license": "CC BY 4.0"}
        for number, slug, title in (
            (10, "micro-servo-mg90s", "Micro Servo MG90S"),
            (20, "mg90s-servo-motor-model", "MG90S Servo Motor Model"),
            (30, "mg90s-servo-pulley", "MG90S Servo Pulley"),
            (40, "mg90s-servo-gears", "MG90S Servo Gears"),
        )
    ]
    monkeypatch.setattr(adapters, "get_adapter",
                        lambda site: SimpleNamespace(key=site, search=lambda query: hits))

    def details(query, variables):
        assert "p0:print(" in query and "p3:print(" in query
        return {"data": {
            "p0": {"stls": [{"name": "MG90S-body.stl"},
                            {"name": "MicroServoMG90S.step"}]},
            "p1": {"stls": [{"name": "MG90S Servo Motor.stl"}]},
            "p2": {"stls": [{"name": "MG90S_Pulley.stl"},
                            {"name": "Servo Finger v1.step"}]},
            "p3": {"stls": [{"name": "MG90S_gears.step"}]},
        }}

    monkeypatch.setattr(printables, "_query", details)
    records = gui_rpc._community_records("printables", "MG90S")
    items = [gui_rpc._record_to_item({**rec, "score": gui_rpc._search_score(rec, "MG90S")})
             for rec in records]
    by_title = {item["name"]: item for item in items}
    assert by_title["Micro Servo MG90S"]["file_name"] == "MicroServoMG90S.step"
    assert by_title["MG90S Servo Pulley"]["file_name"] == "Servo Finger v1.step"
    assert by_title["MG90S Servo Pulley"]["part_number"] == "Servo Finger v1.step"
    assert (by_title["Micro Servo MG90S"]["score"] >
            by_title["MG90S Servo Motor Model"]["score"] >
            by_title["MG90S Servo Gears"]["score"] >
            by_title["MG90S Servo Pulley"]["score"])

    monkeypatch.setattr(gui_rpc, "_cef_unavailable", lambda: True)
    monkeypatch.setattr(gui_rpc, "_index_status", lambda: {"building": False, "count": 0})
    monkeypatch.setattr(gui_rpc, "_family_size_options", lambda records: None)
    monkeypatch.setattr("services.parts.search", lambda *args, **kwargs: [])
    combined = gui_rpc._parts_search({"query": "MG90S", "live": True}, None)
    assert combined["ok"]
    assert [item["name"] for item in combined["results"][:2]] == [
        "Micro Servo MG90S", "MG90S Servo Motor Model"]


def test_manifest_record_download_and_insert_use_shared_acquisition(monkeypatch, tmp_path):
    from services import parts
    from services.mcp import core_bridge
    from services.parts import parts_library

    part_id = "bearing:bundle:6205"
    pending = {"part_id": part_id, "title": "6205 bearing", "domain": "bearing",
               "artifact": {"path": "objects/6205.step"}, "geometry_ref": None}
    item = gui_rpc._record_to_item(pending)
    assert not item["downloaded"] and not item["placeable"] and item["downloadable"]
    path = tmp_path / "6205.step"
    path.write_bytes(b"model")
    acquired = {**pending, "geometry_ref": {"kind": "import_file", "path": str(path)}}
    calls = []
    monkeypatch.setattr(parts_library, "acquire_part", lambda pid, **kw:
                        calls.append((pid, kw)) or acquired)
    monkeypatch.setattr(parts, "get", lambda pid, **kw:
                        calls.append((pid, kw)) or acquired)
    monkeypatch.setattr(core_bridge, "call", lambda method, params:
                        calls.append((method, params)) or {"ok": True, "doc_id": "doc1"})
    result = gui_rpc._parts_download({"item_id": part_id, "include_live": False}, lambda *_: None)
    assert result["ok"] and result["downloaded"]
    assert calls[-1] == (part_id, {"offline": True})
    item = gui_rpc._record_to_item(acquired)
    assert item["downloaded"] and item["placeable"] and not item["downloadable"]
    result = gui_rpc._parts_insert_into_doc({"item_id": part_id, "include_live": False}, lambda *_: None)
    assert result["ok"]
    assert calls[-2] == (part_id, {"include_live": False, "acquire": True})
    assert calls[-1][1]["record"] == acquired


def test_offline_gui_search_never_starts_live_provider(monkeypatch):
    from services import parts
    calls = []
    monkeypatch.setattr(parts, "search", lambda query, **kw: calls.append(kw) or [])
    monkeypatch.setattr(gui_rpc, "_family_size_options", lambda records: None)
    monkeypatch.setattr(gui_rpc, "_index_status", lambda: {})
    result = gui_rpc._parts_search({"query": "bearing", "live": True, "include_live": False},
                                   lambda *_: None)
    assert result["ok"]
    assert len(calls) == 1 and calls[0]["include_live"] is False
    started = gui_rpc._parts_search_live_start({"query": "bearing", "include_live": False},
                                               lambda *_: None)
    assert started["ok"] and started["done"] and started["sites"] == []
