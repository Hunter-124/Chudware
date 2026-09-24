"""Regression checks for non-destructive Claude MCP registration."""
from __future__ import annotations

import json
from pathlib import Path

from services.installer import register_mcp


def test_unparseable_config_is_preserved_while_other_target_registers(tmp_path, monkeypatch):
    broken = tmp_path / "Claude" / "claude_desktop_config.json"
    broken.parent.mkdir()
    original = b'{"mcpServers":{"another":{"command":"other"}},}'
    broken.write_bytes(original)
    valid = tmp_path / ".claude.json"
    valid.write_text(json.dumps({"setting": "keep", "mcpServers": {"other": {"command": "other"}}}))
    monkeypatch.setattr(register_mcp, "config_paths", lambda: {"desktop": broken, "code": valid})

    result = register_mcp.apply(tmp_path / "bin" / "python", tmp_path,
                                ["desktop", "code"], dry_run=False, unregister=False)

    assert result["desktop"] == {"path": str(broken), "exists": True, "changed": False,
                                 "wrote": False, "reason": "unparseable"}
    assert broken.read_bytes() == original
    assert not broken.with_suffix(broken.suffix + ".chudware-bak").exists()
    registered = json.loads(valid.read_text())
    assert registered["setting"] == "keep"
    assert registered["mcpServers"]["other"] == {"command": "other"}
    assert registered["mcpServers"]["chudware"]["command"] == str(tmp_path / "bin" / "python")
    assert result["code"]["wrote"] is True


def test_default_python_uses_posix_runtime_layout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(register_mcp.paths, "RUNTIME", tmp_path)
    monkeypatch.setattr(register_mcp.sys, "platform", "darwin")
    assert register_mcp.default_python() == tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(register_mcp.sys, "platform", "linux")
    assert register_mcp.default_python() == tmp_path / "venv" / "bin" / "python"
