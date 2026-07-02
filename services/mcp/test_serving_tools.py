"""End-to-end tests for the adapter-serving MCP tools (T-H9).

Drives the tools through FastMCP.call_tool with the routing table persisted to a tmp
config file, proving the full status -> canary -> rollback -> gated-promote surface and
config round-trip.

Run from repo root: python -m pytest services/mcp/test_serving_tools.py -q
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mcp.server.fastmcp import FastMCP
from mcp_server.serving_tools import register_serving_tools


@pytest.fixture()
def routed(tmp_path, monkeypatch):
    """A FastMCP with serving tools registered and a tmp-backed routing config."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"provider": "anthropic"}), encoding="utf-8")
    import services.serving.routing as routing
    monkeypatch.setattr(routing, "_config_path", lambda: cfg)

    # Seed a route with an adapter under canary.
    from services.serving.routing import Fallback, RouteEntry, RoutingTable, save_routing_table
    table = RoutingTable([RouteEntry(
        insertion_point="IP-13", provider="vllm", model="qwen3-4b",
        adapter_version="IP-13/v3", canary_pct=0.0,
        fallback=Fallback(mode="frontier", provider="anthropic", model="claude"),
    )])
    save_routing_table(table)

    mcp = FastMCP("t")
    register_serving_tools(mcp)
    return mcp, cfg


def _call(mcp, name, args):
    content, _structured = asyncio.run(mcp.call_tool(name, args))
    # tools return a JSON string; recover it from the text content
    text = content[0].text if hasattr(content[0], "text") else content[0]
    return json.loads(text)


def test_status_reports_route(routed):
    mcp, _ = routed
    out = _call(mcp, "serving_status", {})
    assert out["ok"]
    assert len(out["routes"]) == 1
    r = out["routes"][0]
    assert r["insertion_point"] == "IP-13"
    assert r["adapter_version"] == "IP-13/v3"
    assert r["fallback"]["mode"] == "frontier"


def test_set_canary_persists(routed):
    mcp, cfg = routed
    out = _call(mcp, "serving_set_canary", {"insertion_point": "IP-13", "pct": 10.0})
    assert out["ok"] and out["canary_pct"] == 10.0
    # persisted to disk, other keys preserved
    disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert disk["provider"] == "anthropic"
    assert disk["routing"]["IP-13"]["canary"]["pct"] == 10.0


def test_rollback_flips_to_base_when_no_history(routed):
    mcp, _ = routed
    out = _call(mcp, "serving_rollback", {"insertion_point": "IP-13"})
    assert out["ok"] and out["adapter_version"] is None


def test_promote_refused_without_valid_report(routed, tmp_path):
    mcp, _ = routed
    # build a shadow log where the challenger wins, but the report_ref does not exist
    log = tmp_path / "shadow.jsonl"
    from tools.flywheel.shadow_report import generate_fixture_log
    generate_fixture_log(log, n=120, insertion_point="IP-13",
                         challenger_model="IP-13/v4")
    out = _call(mcp, "serving_promote", {
        "insertion_point": "IP-13", "adapter_version": "IP-13/v4",
        "report_ref": str(tmp_path / "nope.txt"), "approver": "erik",
        "shadow_log": str(log),
    })
    assert out["ok"] is False
    assert out["code"] == "promotion_refused"


def test_evaluate_then_promote_happy_path(routed, tmp_path):
    mcp, cfg = routed
    log = tmp_path / "shadow.jsonl"
    # fixture challenger scores are random ~U(0.3,1.0); construct a deterministic winning log
    import json as _json
    rows = []
    for i in range(120):
        rows.append({
            "artifact_id": f"{i:08x}", "insertion_point": "IP-13", "prompt_hash": "h",
            "incumbent_provider": "anthropic", "incumbent_model": "claude",
            "incumbent_latency_ms": 1000.0, "incumbent_scores": {"verifier": 0.6},
            "incumbent_accepted": (i % 2 == 0),
            "challenger_provider": "vllm", "challenger_model": "IP-13/v4",
            "challenger_latency_ms": 800.0, "challenger_scores": {"verifier": 0.85},
            "challenger_accepted": True, "mode": "replay",
        })
    log.write_text("\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    report = tmp_path / "evidence.txt"
    ev = _call(mcp, "serving_evaluate", {
        "insertion_point": "IP-13", "shadow_log": str(log),
        "report_out": str(report)})
    assert ev["ok"] and ev["evidence"]["eligible"] is True
    assert report.exists()

    out = _call(mcp, "serving_promote", {
        "insertion_point": "IP-13", "adapter_version": "IP-13/v4",
        "report_ref": str(report), "approver": "erik", "shadow_log": str(log)})
    assert out["ok"] is True
    assert out["adapter_version"] == "IP-13/v4"
    assert out["canary_pct"] == 100.0
    # promotion persisted, previous version retained for rollback
    disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert disk["routing"]["IP-13"]["adapter_version"] == "IP-13/v4"
    assert "IP-13/v3" in disk["routing"]["IP-13"]["history"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
