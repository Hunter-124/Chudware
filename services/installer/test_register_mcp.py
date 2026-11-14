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


def test_source_entry_carries_import_path(tmp_path: Path, monkeypatch):
    """Claude clients start stdio servers outside the registered cwd (Claude Code in the
    user's project), so the source module must stay importable through PYTHONPATH."""
    monkeypatch.delenv("CHUDWARE_MCP_BIN", raising=False)
    entry = register_mcp.server_entry(tmp_path / "python", tmp_path)
    assert entry["args"] == ["-m", "services.mcp.server"]
    assert entry["env"]["PYTHONPATH"] == str(tmp_path.resolve())


def test_default_python_uses_posix_runtime_layout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(register_mcp.paths, "RUNTIME", tmp_path)
    monkeypatch.setattr(register_mcp.sys, "platform", "darwin")
    assert register_mcp.default_python() == tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(register_mcp.sys, "platform", "linux")
    assert register_mcp.default_python() == tmp_path / "venv" / "bin" / "python"


def test_store_build_configs_are_targeted(tmp_path: Path, monkeypatch):
    local = tmp_path / "Local"
    relocated = local / "Claude-Data" / "claude_abc"
    relocated.mkdir(parents=True)
    moved = local / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    moved.mkdir(parents=True)
    (moved / ".user-data-left.claude_abc").write_text("")
    legacy = local / "Packages" / "Claude_old" / "LocalCache" / "Roaming" / "Claude"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(register_mcp.Path, "home", lambda: tmp_path)

    found = register_mcp.config_paths()

    assert found == {
        "desktop": tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json",
        "desktop:claude_abc": relocated / "claude_desktop_config.json",
        "desktop:Claude": legacy / "claude_desktop_config.json",
        "code": tmp_path / ".claude.json",
    }
    result = register_mcp.apply(tmp_path / "python", tmp_path, ["desktop"],
                                dry_run=False, unregister=False)
    assert set(result) == {"desktop", "desktop:claude_abc", "desktop:Claude"}
    cfg = json.loads((relocated / "claude_desktop_config.json").read_text())
    assert cfg["mcpServers"]["chudware"]["command"] == str(tmp_path / "python")
