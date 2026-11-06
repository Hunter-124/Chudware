"""Dispatch allow-list + tagging tests for the MCP tool registry.

Lives under `services/agent/` but exercises the canonical
`services.mcp.tool_registry` package — no FastMCP, libchudware, or network.
"""
from __future__ import annotations

import asyncio

import pytest

from services.mcp import tool_registry as reg


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
    assert reg.classify_risk("scad_convert") == reg.RISK_WRITE
    assert reg.classify_risk("scad_inspect") == reg.RISK_READ
    assert reg.classify_risk("scad_status") == reg.RISK_READ
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
    monkeypatch.setenv("CHUDWARE_TOOLS_ALLOW", "describe_part, family:cad")
    assert reg.is_allowed("add_features")  # via family:cad
    monkeypatch.setenv("CHUDWARE_TOOLS_ALLOW", "describe_part")
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


def test_registration_description_reaches_the_callable_docstring():
    """Host-tool loads read the callable's docstring; a tool described only by the
    `description=` kwarg (the check_geometry_health aliases) advertised an empty
    description and Chudcode refused the whole set_host_tools call, killing the turn."""
    import inspect

    stub = _StubMCP()

    def alias(doc_id: str = "") -> dict:
        return {"ok": True}

    reg.view(stub, "cad").tool(name="alias", description="Alias of the health check.")(alias)
    assert inspect.getdoc(stub.tools["alias"]) == "Alias of the health check."


def test_result_contract_survives_registry_guards():
    stub = _StubMCP()
    cad = reg.view(stub, "cad")

    @cad.tool()
    @reg.result_contract("Returns structured data.")
    def probe() -> dict:
        return {"ok": True, "value": 3}

    @cad.tool()
    @reg.result_contract("Returns async data.")
    async def async_probe() -> dict:
        return {"ok": True}

    assert stub.tools["probe"]._cad_result_contract == "Returns structured data."
    assert stub.tools["async_probe"]._cad_result_contract == "Returns async data."
    assert stub.tools["probe"]() == {"ok": True, "value": 3}
    reg.set_policy(allow=["nothing"])
    assert stub.tools["probe"]()["ok"] is False
    assert asyncio.run(stub.tools["async_probe"]())["ok"] is False


@pytest.mark.parametrize("name", [
    "interface_check", "stackup_analyze", "verify_battery", "design_review", "measure",
    "assembly_dof", "fastener_check", "tool_access_check", "disassembly_check",
    "mate_fit_check", "bom_consistency_check",
    "recommend_fit", "cam_audit", "piping_spec_check", "ecad_run_drc", "ecad_run_erc",
    "ecad_run_dfm", "ecad_run_sicheck", "gear_check", "bearing_life",
])
def test_read_only_analysis_tools_classify_as_read(name):
    assert reg.classify_risk(name) == reg.RISK_READ


def test_a_file_writing_inspector_is_a_write():
    assert reg.classify_risk("inspect_native_files") == reg.RISK_WRITE




def test_a_writing_argument_makes_that_call_a_write():
    stub = _StubMCP()
    cad = reg.view(stub, "cad")

    @cad.tool()
    def render_part(doc_id: str | None = None, save_path: str | None = None,
                    display_quality: str | None = None) -> dict:
        return {"ok": True}

    reg.set_policy(deny_risk=["write"])
    assert stub.tools["render_part"]()["ok"] is True, "a plain render stays a read"
    by_keyword = stub.tools["render_part"](save_path="figures/x.png")
    assert by_keyword["ok"] is False and by_keyword["code"] == "risk_denied"
    by_position = stub.tools["render_part"]("doc-1", None, "fine")
    assert by_position["ok"] is False and by_position["code"] == "risk_denied"


def test_battery_only_escalates_checks_that_write():
    stub = _StubMCP()
    cad = reg.view(stub, "cad")

    @cad.tool()
    def verify_battery(checks=None, doc_id=None) -> dict:
        return {"ok": True}

    reg.set_policy(deny_risk=["write"])
    assert stub.tools["verify_battery"](checks=["check_interference"])["ok"] is True
    refused = stub.tools["verify_battery"](["run_fea_static"])
    assert refused["ok"] is False and refused["code"] == "risk_denied"


def test_a_refusal_carries_the_shared_error_shape():
    stub = _register_sample()
    reg.set_policy(allow=["describe_part"])
    out = stub.tools["add_features"]()
    assert out["ok"] is False and out["code"] == "not_allowed"
    assert out["error"] == out["reason"] and out["hint"]


def test_a_refused_dispatch_evaluates_the_policy_once(monkeypatch):
    stub = _register_sample()
    reg.set_policy(allow=["nothing"])
    calls = []
    original = reg._denial
    monkeypatch.setattr(reg, "_denial", lambda *a, **k: calls.append(a) or original(*a, **k))
    assert stub.tools["add_features"]()["ok"] is False
    assert len(calls) == 1


def test_concurrent_runs_keep_their_own_policy():
    import contextvars
    import threading

    _register_sample()
    both_bound = threading.Barrier(2)
    seen: dict[str, tuple[bool, bool]] = {}

    def run(name: str, allowed: list[str]) -> None:
        reg.set_policy(allow=allowed)
        both_bound.wait(timeout=10)  # each run has now bound its policy
        seen[name] = (reg.is_allowed("describe_part"), reg.is_allowed("add_features"))

    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(run, "a", ["describe_part"])),
        threading.Thread(target=contextvars.copy_context().run, args=(run, "b", ["add_features"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert seen == {"a": (True, False), "b": (False, True)}
    # Neither run touched the context that started them.
    assert reg.is_allowed("describe_part") and reg.is_allowed("add_features")


def test_enable_tools_widens_only_the_calling_runs_allow_list():
    import contextvars

    _register_sample()
    mine, other = contextvars.copy_context(), contextvars.copy_context()
    mine.run(reg.set_policy, ["describe_part"])
    other.run(reg.set_policy, ["describe_part"])
    mine.run(reg.extend_allow, ["add_features"])
    assert mine.run(reg.is_allowed, "add_features") is True
    assert other.run(reg.is_allowed, "add_features") is False


def test_extending_an_unrestricted_run_changes_nothing():
    _register_sample()
    reg.extend_allow(["describe_part"])
    assert reg._current_allow() is None
    assert reg.is_allowed("add_features")


def test_a_turns_policy_changes_do_not_outlive_its_scope():
    _register_sample()
    reg.set_policy(allow=["describe_part"])
    with reg.policy_scope():
        reg.extend_allow(["add_features"])
        assert reg.is_allowed("add_features")
    assert not reg.is_allowed("add_features")


def test_explicit_registration_risk_controls_dispatch():
    stub = _StubMCP()
    cad = reg.view(stub, "cad")

    @cad.tool(risk=reg.RISK_READ)
    def observational_probe() -> dict:
        return {"ok": True}

    assert reg.catalog()["observational_probe"]["risk"] == reg.RISK_READ
    reg.set_policy(deny_risk=[reg.RISK_WRITE])
    assert stub.tools["observational_probe"]()["ok"] is True
