"""Register Billet (formerly AI-CAD) as an MCP server in the machine's Claude install(s).

Adds a "billet" entry to the `mcpServers` map of (any legacy "ai-cad" entry is left
in place on register — existing tooling references it — and removed on --unregister):
  * Claude Desktop : %APPDATA%/Claude/claude_desktop_config.json
  * Claude Code     : ~/.claude.json

Idempotent and non-destructive: backs up each file before writing and only writes when the
entry actually changes. Use --dry-run to preview, --unregister to remove.

    python -m installer.register_mcp [--dry-run] [--unregister]
                                     [--python <exe>] [--cwd <dir>] [--target desktop|code]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from services import paths

SERVER_NAME = "billet"
SERVER_NAME_LEGACY = "ai-cad"  # left in place on register; removed on --unregister


def default_python() -> Path:
    return paths.RUNTIME / "venv" / "Scripts" / "python.exe"


def config_paths() -> dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return {
        "desktop": appdata / "Claude" / "claude_desktop_config.json",
        "code": home / ".claude.json",
    }


def server_entry(python_exe: Path, cwd: Path, hostd_bin: Path | None = None) -> dict:
    env: dict[str, str] = {}
    if hostd_bin is not None:
        # Pin the installed engine-host daemon so MCP sessions and the GUI share ONE
        # daemon (same binary path -> same instance key -> same socket/pipe). Without
        # this the MCP venv would resolve a dev-tree hostd and mint a separate daemon.
        env["AICAD_HOSTD_BIN"] = str(hostd_bin)
    return {
        "command": str(python_exe),
        "args": ["-m", "mcp_server.server"],
        "cwd": str(cwd),
        "env": env,
    }


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def apply(python_exe: Path, cwd: Path, targets: list[str], dry_run: bool,
          unregister: bool, hostd_bin: Path | None = None) -> dict:
    entry = server_entry(python_exe, cwd, hostd_bin)
    out: dict = {}
    for name, path in config_paths().items():
        if name not in targets:
            continue
        cfg = _load(path)
        servers = cfg.get("mcpServers") or {}
        if unregister:
            changed = SERVER_NAME in servers or SERVER_NAME_LEGACY in servers
            servers.pop(SERVER_NAME, None)
            servers.pop(SERVER_NAME_LEGACY, None)  # remove legacy id on explicit unregister
        else:
            changed = servers.get(SERVER_NAME) != entry
            servers[SERVER_NAME] = entry
            # Leave any legacy "ai-cad" entry alone — existing tooling references it.
        cfg["mcpServers"] = servers

        wrote = False
        if changed and not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + ".aicad-bak"))
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            wrote = True
        out[name] = {"path": str(path), "exists": path.exists(),
                     "changed": changed, "wrote": wrote}
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Register AI-CAD MCP server with Claude.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--unregister", action="store_true")
    ap.add_argument("--python", type=Path, default=default_python())
    ap.add_argument("--cwd", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--target", choices=["desktop", "code", "both"], default="both")
    ap.add_argument("--hostd-bin", type=Path, default=None,
                    help="installed aicad-hostd binary; written into the server env as "
                         "AICAD_HOSTD_BIN so MCP sessions attach to the installed daemon")
    args = ap.parse_args(argv)

    targets = ["desktop", "code"] if args.target == "both" else [args.target]
    res = apply(args.python, args.cwd, targets, args.dry_run, args.unregister,
                hostd_bin=args.hostd_bin)
    print(json.dumps({
        "action": "unregister" if args.unregister else "register",
        "dry_run": args.dry_run,
        "server": SERVER_NAME,
        "entry": server_entry(args.python, args.cwd, args.hostd_bin),
        "targets": res,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
