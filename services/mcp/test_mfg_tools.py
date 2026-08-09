"""Manufacturing MCP surface, risk boundary, and daemon-refusal contracts."""
from __future__ import annotations

import inspect

import pytest

from services.agent import config as agent_config
from services.mcp import mfg_tools
from services.mcp import tool_registry as reg


READ_TOOLS = frozenset({
    "mfg_device_list",
    "mfg_device_describe",
    "mfg_device_capability",
    "mfg_telemetry_snapshot",
    "mfg_gate_report",
    "mfg_run_list",
    "mfg_run_describe",
    "mfg_journal_query",
    "mfg_approval_inspect",
})
WRITE_TOOLS = frozenset({
    "mfg_artifact_register",
    "mfg_preflight_run",
    "mfg_approval_propose",
})
MOTION_TOOLS = frozenset({
    "request_mfg_run_start",
    "request_mfg_hold",
    "request_mfg_abort",
})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS | MOTION_TOOLS


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn
        return decorate


class _FakeClient:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"ok": True}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return self.result


def _required_arguments(fn) -> dict:
    values = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        values[name] = {} if name in {"artifact", "job", "snapshot", "attendance"} else "x"
    return values


def _legacy_risk(name: str) -> str:
    """Classifier behavior before this surface, used over the whole warm catalog."""
    if name in reg._RISK_OVERRIDES:
        return reg._RISK_OVERRIDES[name]
    if name in reg._READ_EXACT - READ_TOOLS:
        return reg.RISK_READ
    if name.startswith(reg._EXTERNAL_PREFIXES) or name.endswith(reg._EXTERNAL_SUFFIXES):
        return reg.RISK_EXTERNAL
    if name.startswith(reg._READ_PREFIXES) or name.endswith(reg._READ_SUFFIXES):
        return reg.RISK_READ
    return reg.RISK_WRITE


@pytest.fixture(autouse=True)
def _policy_cleanup(monkeypatch):
    monkeypatch.delenv("CHUDWARE_TOOLS_ALLOW", raising=False)
    monkeypatch.delenv("CHUDWARE_TOOLS_DENY_RISK", raising=False)
    reg.reset_policy()
    yield
    reg.reset_policy()


def test_surface_is_exact_and_has_no_approval_or_safety_action() -> None:
    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(mcp)
    assert set(mcp.tools) == ALL_TOOLS
    forbidden = {
        "mfg_approval_create", "approve_mfg_run", "mfg_approval_revoke",
        "request_mfg_resume", "request_mfg_jog", "request_mfg_home",
        "request_mfg_unlock", "request_mfg_estop",
    }
    assert forbidden.isdisjoint(mcp.tools)
    assert reg._SAFETY_TOOLS == frozenset()


def test_risk_classes_are_explicit_and_unknown_mfg_defaults_write() -> None:
    assert reg._MOTION_TOOLS == MOTION_TOOLS
    for name in READ_TOOLS:
        assert reg.classify_risk(name) == reg.RISK_READ
    for name in WRITE_TOOLS:
        assert reg.classify_risk(name) == reg.RISK_WRITE
    for name in MOTION_TOOLS:
        assert reg.classify_risk(name) == reg.RISK_MOTION
    assert reg.classify_risk("mfg_new_unlisted_operation") == reg.RISK_WRITE


def test_every_preexisting_catalog_tool_keeps_legacy_risk() -> None:
    reg.warm_catalog()
    for name in sorted(set(reg.catalog()) - ALL_TOOLS):
        assert reg.classify_risk(name) == _legacy_risk(name), name


def test_default_off_family_denies_every_tool_without_calling_daemon(monkeypatch) -> None:
    assert agent_config.get_tool_families({})["manufacture"] is False
    monkeypatch.setattr(agent_config, "is_tool_family_enabled",
                        lambda family, cfg=None: family != "manufacture")
    monkeypatch.setattr(agent_config, "get_tool_options",
                        lambda cfg, name: {"enabled": True})
    fake_client = _FakeClient()
    monkeypatch.setattr(mfg_tools.daemon_bridge, "client", lambda: fake_client)

    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(reg.view(mcp, "manufacture"))
    for name, fn in mcp.tools.items():
        refusal = fn(**_required_arguments(fn))
        assert refusal["ok"] is False
        assert "family 'manufacture' is disabled" in refusal["reason"]
        assert "do not retry it in a loop" in refusal["hint"]
    assert fake_client.calls == []


def test_motion_requires_exact_tool_membership(monkeypatch) -> None:
    monkeypatch.setattr(agent_config, "is_tool_family_enabled", lambda family, cfg=None: True)
    monkeypatch.setattr(agent_config, "get_tool_options",
                        lambda cfg, name: {"enabled": True})
    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(reg.view(mcp, "manufacture"))

    for name in MOTION_TOOLS:
        reg.set_policy(allow=None)
        assert "motion-class" in reg.deny_reason(name)
        reg.set_policy(allow=["family:manufacture"])
        assert "motion-class" in reg.deny_reason(name)
        reg.set_policy(allow=[name])
        assert reg.deny_reason(name) is None


def test_daemon_refusal_reason_and_hint_pass_through_verbatim(monkeypatch) -> None:
    refusal = {
        "ok": False,
        "code": "mfg_gate_refused",
        "reason": "approval binding is not human_or_agent",
        "hint": "Ask a human to inspect and approve the immutable binding.",
    }
    fake_client = _FakeClient(refusal)
    monkeypatch.setattr(mfg_tools.daemon_bridge, "client", lambda: fake_client)
    monkeypatch.setattr(agent_config, "is_tool_family_enabled", lambda family, cfg=None: True)
    monkeypatch.setattr(agent_config, "get_tool_options",
                        lambda cfg, name: {"enabled": True})

    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(reg.view(mcp, "manufacture"))
    result = mcp.tools["mfg_gate_report"](gate_report_ref="gate:7")
    assert result is refusal
    assert fake_client.calls == [("mfg_gate_report", {"gate_report_ref": "gate:7"})]


def test_motion_calls_force_agent_initiator(monkeypatch) -> None:
    fake_client = _FakeClient({"result_seq": 12, "epoch": "epoch:test"})
    monkeypatch.setattr(mfg_tools.daemon_bridge, "client", lambda: fake_client)
    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(mcp)

    mcp.tools["request_mfg_run_start"](
        lease_id="lease:1", run_id="run:1", approval_id="approval:1",
        agent_identity="agent:test", seat="seat:test", agent_run_id="agent-run:1",
    )
    method, params = fake_client.calls[-1]
    assert method == "mfg_run_start"
    assert params == {
        "lease_id": "lease:1",
        "run_id": "run:1",
        "approval_id": "approval:1",
        "initiator": {
            "kind": "agent", "identity": "agent:test", "seat": "seat:test",
            "agent_run_id": "agent-run:1",
        },
    }


def test_write_tools_map_one_to_one_to_daemon(monkeypatch) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(mfg_tools.daemon_bridge, "client", lambda: fake_client)
    mcp = _FakeMCP()
    mfg_tools.register_mfg_tools(mcp)

    mcp.tools["mfg_artifact_register"](
        lease_id="lease:1", device_id="dev:1",
        artifact={"kind": "gcode", "content": "G0 X0"},
    )
    mcp.tools["mfg_preflight_run"](
        lease_id="lease:1", artifact_id="artifact:1",
        job={"job_id": "job:1", "setup_revision": 2, "quantity": 1},
    )
    mcp.tools["mfg_approval_propose"](
        lease_id="lease:1", run_id="run:1", exercisable_by="human_or_agent",
    )
    assert [method for method, _ in fake_client.calls] == [
        "mfg_artifact_register", "mfg_preflight_run", "mfg_approval_propose",
    ]
    assert all(method != "mfg_approval_create" for method, _ in fake_client.calls)
