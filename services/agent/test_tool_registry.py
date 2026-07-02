"""Dispatch allow-list + tagging tests for the MCP tool registry.

Lives under agent/ (the gate runs `pytest engine/doc agent`) but exercises
`mcp_server.tool_registry` via the top-level `mcp_server` symlink — no FastMCP, no
libaicad, no network. Contract: contracts/providers.md §6.
"""
from __future__ import annotations

import asyncio

import pytest

from mcp_server import tool_registry as reg


class _StubMCP:
    """Minimal FastMCP-shaped stub: records what `@mcp.tool()` registers."""

    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[getattr(fn, "__name__", "?")] = fn
            return fn
        return deco


@pytest.fixture(autouse=True)
def _clean():
    reg.clear_catalog()
    reg.reset_policy()
    yield
    reg.clear_catalog()
    reg.reset_policy()


def _register_sample():
    """Register a few tools across families/risks through the family view."""
    stub = _StubMCP()
    cad = reg.view(stub, "cad")

    @cad.tool()
    def describe_part(name: str = "x") -> dict:  # read
        return {"ok": True, "ran": "describe_part"}

    @cad.tool()
    def add_features(ops: list | None = None) -> dict:  # write
        return {"ok": True, "ran": "add_features"}

    @cad.tool()
    def export_part(fmt: str = "step") -> dict:  # external
        return {"ok": True, "ran": "export_part"}

    return stub


def test_classify_risk():
    assert reg.classify_risk("describe_part") == reg.RISK_READ
    assert reg.classify_risk("query_geometry") == reg.RISK_READ
    assert reg.classify_risk("hover") == reg.RISK_READ
    assert reg.classify_risk("add_features") == reg.RISK_WRITE
    assert reg.classify_risk("set_material") == reg.RISK_WRITE
    assert reg.classify_risk("export_part") == reg.RISK_EXTERNAL
    assert reg.classify_risk("cam_post") == reg.RISK_EXTERNAL
    assert reg.classify_risk("submit_part_photos") == reg.RISK_EXTERNAL
    # unknown verb -> fail-safe write
    assert reg.classify_risk("frobnicate_widget") == reg.RISK_WRITE


def test_catalog_tags_family_and_risk():
    _register_sample()
    cat = reg.catalog()
    assert cat["describe_part"] == {"family": "cad", "risk": "read"}
    assert cat["add_features"] == {"family": "cad", "risk": "write"}
    assert cat["export_part"] == {"family": "cad", "risk": "external"}


def test_signature_preserved_for_fastmcp_introspection():
    import inspect
    stub = _register_sample()
    # FastMCP introspects the wrapped fn's params to build the schema; wraps() must expose them.
    sig = inspect.signature(stub.tools["describe_part"])
    assert list(sig.parameters) == ["name"]


def test_default_policy_allows_everything():
    stub = _register_sample()
    assert reg.is_allowed("describe_part")
    assert reg.is_allowed("export_part")
    # guarded fn runs the real body when allowed
    assert stub.tools["add_features"]()["ran"] == "add_features"


def test_allow_list_denies_unlisted_with_contract_shape():
    stub = _register_sample()
    reg.set_policy(allow=["describe_part"])
    assert reg.is_allowed("describe_part")
    assert not reg.is_allowed("add_features")
    out = stub.tools["add_features"]()
    assert out["ok"] is False
    assert "reason" in out and "hint" in out
    assert "not in the active allow-list" in out["reason"]
    # allowed tool still runs its real body
    assert stub.tools["describe_part"]()["ran"] == "describe_part"


def test_family_token_allows_whole_family():
    stub = _register_sample()
    reg.set_policy(allow=["family:cad"])
    assert reg.is_allowed("add_features")
    assert stub.tools["export_part"]()["ran"] == "export_part"


def test_deny_risk_class():
    stub = _register_sample()
    reg.set_policy(deny_risk=["external"])
    assert reg.is_allowed("describe_part")
    assert not reg.is_allowed("export_part")
    out = stub.tools["export_part"]()
    assert out["ok"] is False and "external" in out["reason"]


def test_env_fallback(monkeypatch):
    _register_sample()
    reg.reset_policy()
    monkeypatch.setenv("AI_CAD_TOOLS_ALLOW", "describe_part, family:cad")
    assert reg.is_allowed("add_features")  # via family:cad
    monkeypatch.setenv("AI_CAD_TOOLS_ALLOW", "describe_part")
    assert not reg.is_allowed("add_features")


def test_async_tool_guard():
    stub = _StubMCP()
    ecad = reg.view(stub, "ecad")

    @ecad.tool()
    async def ecad_autoroute(effort: str = "high") -> dict:
        return {"ok": True, "ran": "ecad_autoroute"}

    import inspect
    assert inspect.iscoroutinefunction(stub.tools["ecad_autoroute"])
    reg.set_policy(allow=["nothing"])
    out = asyncio.run(stub.tools["ecad_autoroute"]())
    assert out["ok"] is False
    reg.set_policy(allow=["ecad_autoroute"])
    out = asyncio.run(stub.tools["ecad_autoroute"]())
    assert out["ran"] == "ecad_autoroute"
