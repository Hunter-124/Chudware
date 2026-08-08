"""agent.tool_families enable/disable."""
from __future__ import annotations

import services.agent.config as cfg
import services.mcp.tool_registry as reg


def test_get_tool_families_defaults_all_on():
    """Every family is on by omission, except the explicit default-off set."""
    fams = cfg.get_tool_families({})
    assert fams
    for fam, on in fams.items():
        if fam in cfg.DEFAULT_OFF_TOOL_FAMILIES:
            assert on is False, f"{fam} must stay off by omission"
        else:
            assert on is True, f"{fam} regressed to default-off"
    assert "cad" in fams and "em" in fams


def test_get_tool_families_respects_config():
    fams = cfg.get_tool_families({"agent": {"tool_families": {"em": False, "cad": True}}})
    assert fams["em"] is False
    assert fams["cad"] is True
    assert fams.get("reveng", True) is True


def test_disabled_family_denies_tool(monkeypatch):
    reg.record("unit_test_em_tool", "em", "read")
    reg.record("unit_test_cad_tool", "cad", "read")
    reg.reset_policy()

    def _fake_enabled(family: str, cfg=None):
        return family != "em"

    monkeypatch.setattr(cfg, "is_tool_family_enabled", _fake_enabled)
    # deny_reason imports lazily from services.agent.config — patch there too
    import services.agent.config as cfgmod
    monkeypatch.setattr(cfgmod, "is_tool_family_enabled", _fake_enabled)

    reason = reg.deny_reason("unit_test_em_tool")
    assert reason is not None and "em" in reason
    assert reg.deny_reason("unit_test_cad_tool") is None or "family" not in (reg.deny_reason("unit_test_cad_tool") or "")


def test_missing_config_defaults_enabled(monkeypatch):
    """Fresh install / empty config: families stay enabled (open by default)."""
    monkeypatch.setattr(cfg, "load_config", lambda: {})
    fams = cfg.get_tool_families()
    assert fams["cad"] is True
