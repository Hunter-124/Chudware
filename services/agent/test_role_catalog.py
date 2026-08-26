"""Canonical role catalog and delegated-role loading."""
from __future__ import annotations

from services.agent.role_catalog import format_role_catalog_block, harness_context_block
from services.agent.roles import load_role




def test_catalog_includes_model3d_sketch2d():
    blk = format_role_catalog_block()
    assert "model3d" in blk and "sketch2d" in blk
    assert "delegate" in blk.lower()


def test_harness_context():
    h = harness_context_block()
    assert "OpenCASCADE" in h or "B-rep" in h
    assert "memory" in h.lower()


def test_memory_role_loads():
    c = load_role("memory")
    assert c.id == "memory"
    low = c.system_prompt.lower()
    assert "memory" in low or "durable" in low or "historian" in low


def test_yaml_beats_shipped_md_for_advisor():
    """delegate(load_role) must use YAML handoff contract, not prompts/*.md."""
    import yaml
    from pathlib import Path
    from services.agent.roles import load_role
    # Anchor at the repo root (this file's parents[2]) — never the process CWD:
    # some suite tests leave a stale/deleted CWD behind (openEMS fdtd.Run
    # cleanup chdirs into a temp dir it then removes).
    repo = Path(__file__).resolve().parents[2]
    y = yaml.safe_load((repo / "services/agent/roles/advisor.yaml").read_text())
    card = load_role("advisor")
    assert card.system_prompt.strip() == y["system_prompt"].strip()


