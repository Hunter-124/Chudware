"""Role adoption policy + catalog."""
from __future__ import annotations

from services.agent.role_adoption import adopt_role, request_role_change
from services.agent.role_catalog import (
    can_adopt_role,
    format_role_catalog_block,
    get_role_adoption_mode,
    harness_context_block,
)
from services.agent.roles import load_role


def test_adoption_mode_default_supervised():
    assert get_role_adoption_mode({}) == "supervised"
    assert get_role_adoption_mode({"agent": {"role_adoption": "free"}}) == "free"


def test_can_adopt_root_always():
    assert can_adopt_role(is_root=True, depth=0, cfg={"agent": {"role_adoption": "supervised"}})
    assert can_adopt_role(is_root=False, depth=1, cfg={"agent": {"role_adoption": "free"}})
    assert not can_adopt_role(is_root=False, depth=1, cfg={"agent": {"role_adoption": "supervised"}})


def test_adopt_role_supervised_subagent_denied():
    r = adopt_role("model3d", is_root=False, depth=1, cfg={"agent": {"role_adoption": "supervised"}})
    assert r["ok"] is False
    assert "supervised" in r["error"]


def test_adopt_role_main_ok():
    r = adopt_role("model3d", is_root=True, depth=0)
    assert r["ok"] is True
    assert r["role"] == "model3d"


def test_request_role_change():
    r = request_role_change("sketch2d", reason="need 2d", from_role="feature_modeler", depth=1)
    assert r["ok"] and r["requested"]
    assert r["role"] == "sketch2d"


def test_catalog_includes_model3d_sketch2d():
    blk = format_role_catalog_block()
    assert "model3d" in blk and "sketch2d" in blk
    assert "delegate" in blk.lower() or "adopt" in blk.lower()


def test_harness_context():
    h = harness_context_block()
    assert "OpenCASCADE" in h or "B-rep" in h
    assert "memory" in h.lower()


def test_memory_role_loads():
    c = load_role("memory")
    assert c.id == "memory"
    low = c.system_prompt.lower()
    assert "memory" in low or "durable" in low or "historian" in low
