#!/usr/bin/env python3
"""setup_team_tools.py — register the aicad-team MCP server with local clients.

Idempotent; merges into existing configs (never clobbers other servers, e.g. the
ai-cad engine MCP registered by installer/setup_mcp_clients.py).

Registers with:
  * Claude Code (CLI)  — project .mcp.json at the repo root (gitignored, per-machine)
  * Claude Desktop     — claude_desktop_config.json (platform path auto-detected)

Run:  python3 work/tools/setup_team_tools.py          (Linux/macOS)
      py -3 work\\tools\\setup_team_tools.py           (Windows)
"""
import json
import os
import platform
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SERVER_NAME = "aicad-team"


def server_entry():
    py = "py" if platform.system() == "Windows" else "python3"
    args = (["-3"] if py == "py" else []) + [str(REPO / "work" / "tools" / "team_mcp.py")]
    return {"command": py, "args": args}


def merge(path: Path, top_key="mcpServers"):
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = path.with_suffix(path.suffix + ".aicad-bak")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  ! {path} was invalid JSON — backed up to {backup.name}")
            cfg = {}
    cfg.setdefault(top_key, {})[SERVER_NAME] = server_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  ✓ {path}")


def desktop_config_path():
    sysname = platform.system()
    if sysname == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
    if sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def main():
    print(f"Registering {SERVER_NAME} MCP server (repo: {REPO})")
    print("Claude Code (project .mcp.json):")
    merge(REPO / ".mcp.json")
    print("Claude Desktop:")
    dp = desktop_config_path()
    if dp.parent.exists():
        merge(dp)
    else:
        print(f"  - skipped ({dp.parent} not found — is Claude Desktop installed?)")
    print("\nDone. Restart Claude Desktop / new Claude Code session to load the tools.")
    print("Next: start the sync daemon —  work/workd.sh <your-name>   (or work\\workd.bat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
