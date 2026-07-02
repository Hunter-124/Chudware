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
    """Return (config_path, base_dir_that_should_already_exist_on_any_real_profile).

    base_dir is the OS profile directory Claude Desktop's own "Claude" folder lives
    under — it exists on any real user account whether or not Desktop has ever been
    launched, so we use it (not the "Claude" folder itself) to decide whether it's
    worth attempting to write. This lets setup run BEFORE Desktop's first launch —
    Desktop will pick up the file the first time it starts.
    """
    sysname = platform.system()
    if sysname == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Claude" / "claude_desktop_config.json", base


def main():
    print(f"Registering {SERVER_NAME} MCP server (repo: {REPO})")
    print(f"Platform: {platform.system()}")

    print("Claude Code (project .mcp.json):")
    merge(REPO / ".mcp.json")

    print("Claude Desktop:")
    dp, base = desktop_config_path()
    if base.exists():
        merge(dp)
    else:
        print(f"  - skipped ({base} not found — unusual profile layout; "
              f"add it manually once Claude Desktop has run at least once)")

    print("\nDone.")
    print("  * Claude Code CLI: next time you run `claude` in this repo, it will ask to")
    print("    approve the new project MCP server (aicad-team) the first time — say yes.")
    print("    This is a normal one-time security prompt per contributor, not a bug.")
    print("  * Claude Desktop: fully quit and reopen (not just close the window) to pick")
    print("    up the config change. If aicad-team still doesn't show in your tool list,")
    print("    your Desktop version may source MCP servers from account-synced")
    print("    Connectors settings instead of this file — add it manually via")
    print("    Settings -> Connectors (or MCP Servers) with:")
    print(f"      Command: {server_entry()['command']}")
    print(f"      Args:    {server_entry()['args']}")
    print("\nNext: start the sync daemon —  work/workd.sh <your-name>   (or work\\workd.bat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
