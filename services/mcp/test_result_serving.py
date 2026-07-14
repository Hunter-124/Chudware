"""Unit tests for services/mcp/result_serving.py — compact MCP result serving."""
from __future__ import annotations

import json
import math
import os

import pytest

from mcp_server import result_serving as rs


def test_count_tokens_chars_per_token():
    assert rs.CHARS_PER_TOKEN == 4
    assert rs.count_tokens("") == 0
    assert rs.count_tokens("abcd") == 1
    assert rs.count_tokens("abcde") == 2  # ceil(5/4)
    assert rs.count_tokens({"a": 1}) == rs.count_tokens(rs.dumps({"a": 1}))


def test_dumps_compact_by_default(monkeypatch):
    monkeypatch.delenv("AICAD_MCP_PRETTY", raising=False)
    s = rs.dumps({"ok": True, "x": [1, 2]})
    assert "\n" not in s
    assert ": " not in s  # compact separators
    assert json.loads(s) == {"ok": True, "x": [1, 2]}


def test_dumps_pretty_via_env(monkeypatch):
    monkeypatch.setenv("AICAD_MCP_PRETTY", "1")
    s = rs.dumps({"ok": True})
    assert "\n" in s
    assert json.loads(s)["ok"] is True


def test_dumps_explicit_indent_wins(monkeypatch):
    monkeypatch.delenv("AICAD_MCP_PRETTY", raising=False)
    s = rs.dumps({"ok": True}, indent=2)
    assert "\n" in s


def test_ok_and_err_envelopes():
    ok = rs.ok_envelope({"summary": "hi"}, warnings=["w"], hint="h", next_actions=["n"])
    assert ok["ok"] is True and ok["summary"] == "hi"
    assert ok["warnings"] == ["w"] and ok["hint"] == "h" and ok["next_actions"] == ["n"]
    err = rs.err_envelope("boom", hint="fix it", next_actions=["retry"], code=3)
    assert err["ok"] is False and err["error"] == "boom" and err["code"] == 3
    assert err["hint"] == "fix it" and err["next_actions"] == ["retry"]


def test_paginate_list():
    items = list(range(10))
    p0 = rs.paginate_list(items, page=0, page_size=3)
    assert p0["items"] == [0, 1, 2]
    assert p0["total"] == 10 and p0["has_more"] is True and p0["next_page"] == 1
    p3 = rs.paginate_list(items, page=3, page_size=3)
    assert p3["items"] == [9] and p3["has_more"] is False and p3["next_page"] is None


def test_serve_projector_and_meta():
    payload = {
        "ok": True, "summary": "s", "component_count": 2,
        "components": [
            {"ref": "R1", "value": "1k", "libid": "Device:R", "pins": [1, 2]},
            {"ref": "R2", "value": "2k", "libid": "Device:R", "pins": [1, 2]},
        ],
        "nets": [{"name": "OUT", "size": 2, "members": ["R1.1", "R2.2"]}],
        "hint": "h", "next_actions": ["go"], "warnings": [],
        "unconnected_pins": [],
    }
    raw = rs.serve(payload, rs.ServeOpts(detail="summary", budget_tokens=800),
                   projector=rs.project_ecad_status)
    body = json.loads(raw)
    assert body["ok"] is True and body["summary"] == "s"
    assert body["meta"]["detail"] == "summary"
    assert "tokens" in body["meta"]
    assert body["components"][0]["ref"] == "R1"
    assert "pins" not in body["components"][0]
    assert "members" not in body["nets"][0]
    assert body["nets"][0]["name"] == "OUT"


def test_serve_full_keeps_pins_and_members():
    payload = {
        "ok": True, "summary": "s", "component_count": 1,
        "components": [{"ref": "R1", "value": "1k", "libid": "Device:R", "pins": [{"n": 1}]}],
        "nets": [{"name": "OUT", "size": 1, "members": ["R1.1"]}],
        "hint": "h", "next_actions": ["go"], "warnings": [],
        "unconnected_pins": ["R1.2"],
    }
    body = json.loads(rs.serve(payload, rs.ServeOpts(detail="full"),
                               projector=rs.project_ecad_status))
    assert body["components"][0]["pins"]
    assert body["nets"][0]["members"] == ["R1.1"]
    assert body["unconnected_pins"] == ["R1.2"]


def test_priority_drop_never_drops_teaching_keys():
    big = {
        "ok": True,
        "summary": "keep me",
        "component_count": 1,
        "hint": "teach",
        "next_actions": ["act"],
        "sim_log": ["x" * 200] * 50,
        "nodes": list(range(200)),
        "probes": [f"V(N${i})" for i in range(200)],
        "components": [{"ref": f"R{i}", "value": "1k", "libid": "Device:R",
                        "pins": list(range(20))} for i in range(40)],
    }
    body = json.loads(rs.serve(big, rs.ServeOpts(detail="summary", budget_tokens=200),
                               projector=rs.project_ecad_status))
    assert body["ok"] is True
    assert body["summary"] == "keep me"
    assert body["hint"] == "teach"
    assert body["next_actions"] == ["act"]
    assert body["component_count"] == 1
    assert body["meta"]["truncated"] is True or body["meta"]["omitted_keys"]


def test_select_default_probes_prefers_named_and_caps():
    nodes = ["N$1", "N$2", "OUT", "IN", "MID"] + [f"N${i}" for i in range(3, 40)]
    devices = [{"kind": "V", "name": "V1"}, {"kind": "R", "name": "R1"}]
    probes = rs.select_default_probes(nodes, devices, cap=5)
    assert "V(OUT)" in probes
    assert "V(IN)" in probes
    assert "I(V1)" in probes
    # Cap applies to node voltages; I(V*) is extra.
    vprobes = [p for p in probes if p.startswith("V(")]
    assert len(vprobes) <= 5
    assert probes.index("V(OUT)") < probes.index("V(N$1)") if "V(N$1)" in probes else True


def test_project_part_row_and_search():
    rec = {
        "part_id": "bearing:ISO 15:6205",
        "title": "Deep-Groove Ball Bearing 6205",
        "domain": "bearing",
        "designation": "6205",
        "params": {"bore": 25.0},
        "sourcing": [{"price": 6.5, "vendor": "local", "stock": 3}],
        "geometry_ref": {"kind": "core_family"},
        "provenance": {"src": "x"},
        "license": "MIT",
    }
    row = rs.project_part_row(rec)
    assert set(row.keys()) == {
        "part_id", "title", "domain", "vendor", "price", "designation", "score",
    }
    assert row["part_id"] == "bearing:ISO 15:6205"
    assert row["price"] == 6.5
    assert "params" not in row

    payload = {"ok": True, "count": 1, "results": [rec], "facet_summary": {}}
    summary = rs.project_part_search(payload, rs.ServeOpts(detail="summary"))
    assert "params" not in summary["results"][0]
    full = rs.project_part_search(payload, rs.ServeOpts(detail="full"))
    assert "params" in full["results"][0]


def test_pretty_vs_compact_ratio(monkeypatch):
    payload = {
        "ok": True,
        "summary": "30 component(s)",
        "component_count": 30,
        "components": [
            {"ref": f"R{i}", "value": "1k", "libid": "Device:R", "power_w": {"typ": None, "max": None}}
            for i in range(30)
        ],
        "nets": [{"name": f"N{i}", "size": 2} for i in range(40)],
        "hint": "h", "next_actions": ["n"], "warnings": [],
    }
    monkeypatch.delenv("AICAD_MCP_PRETTY", raising=False)
    compact = rs.dumps(payload)
    monkeypatch.setenv("AICAD_MCP_PRETTY", "1")
    pretty = rs.dumps(payload)
    assert len(pretty) / max(len(compact), 1) >= 1.3
