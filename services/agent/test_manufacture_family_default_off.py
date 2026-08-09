"""MFG-FAM-1: the Manufacture tool family is never enabled by omission.

The family reaches real machines (motion, spindle power, heaters). Every other
family in ``DEFAULT_TOOL_FAMILIES`` is enabled when nothing mentions it; this one
must resolve OFF in all three shapes a real config takes, so no wave can open a
default-on Manufacture surface. Enabling it is config-only and explicit.
"""

from __future__ import annotations

from services.agent import config as cfg


FAMILY = "manufacture"


def test_family_is_registered() -> None:
    assert FAMILY in cfg.DEFAULT_TOOL_FAMILIES
    assert FAMILY in cfg.DEFAULT_OFF_TOOL_FAMILIES


def test_off_with_empty_config() -> None:
    fams = cfg.get_tool_families({})
    assert fams[FAMILY] is False
    assert cfg.is_tool_family_enabled(FAMILY, {}) is False


def test_off_when_role_tools_present_and_silent() -> None:
    conf = {"agent": {"role_tools": {"default": ["cad", "files"]}}}
    fams = cfg.get_tool_families(conf)
    assert fams[FAMILY] is False
    # A sibling family listed by a role is still enabled: the OFF rule is
    # specific to this family, not a blanket change to family resolution.
    assert fams["cad"] is True


def test_off_when_tool_families_map_is_empty() -> None:
    assert cfg.get_tool_families({"agent": {"tool_families": {}}})[FAMILY] is False


def test_off_even_when_a_role_lists_it() -> None:
    """A role listing the family is not consent to command a machine.

    Role unions are edited casually; the family gate is the commissioning
    boundary, so only an explicit ``tool_families`` entry may open it.
    """
    conf = {"agent": {"role_tools": {"default": ["cad", FAMILY]}}}
    assert cfg.get_tool_families(conf)[FAMILY] is False


def test_explicit_true_enables_it() -> None:
    conf = {"agent": {"tool_families": {FAMILY: True}}}
    assert cfg.get_tool_families(conf)[FAMILY] is True
    assert cfg.is_tool_family_enabled(FAMILY, conf) is True


def test_explicit_false_still_wins() -> None:
    conf = {"agent": {"tool_families": {FAMILY: False}, "role_tools": {"default": [FAMILY]}}}
    assert cfg.get_tool_families(conf)[FAMILY] is False


def test_no_other_family_became_default_off() -> None:
    assert cfg.DEFAULT_OFF_TOOL_FAMILIES == (FAMILY,)
    fams = cfg.get_tool_families({})
    for fam in cfg.DEFAULT_TOOL_FAMILIES:
        if fam in cfg.DEFAULT_OFF_TOOL_FAMILIES:
            continue
        assert fams[fam] is True, f"{fam} regressed to default-off"
