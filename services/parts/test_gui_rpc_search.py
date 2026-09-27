"""Parts Library GUI search routing in a desktop build without CEF."""
from __future__ import annotations

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

    started = gui_rpc._parts_search_live_start(query, lambda *_: None)
    assert started["ok"] and started["sites"] == ["printables"]
    assert len(started["notices"]) == 1
    assert started["notices"][0]["reason"] == "cef_unavailable"
    deadline = time.monotonic() + 2
    while True:
        polled = gui_rpc._parts_search_live_poll({"token": started["token"]}, lambda *_: None)
        if polled["done"] or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    assert polled["done"] and fetched == [("printables", "MG90S")]
    assert len(polled["results"]) == 1
    assert polled["results"][0]["license"] == "CC BY 4.0"
    assert polled["results"][0]["source"] == "live"
    assert polled["notices"] == started["notices"]


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
                        lambda site: SimpleNamespace(search=lambda query: hits))

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
