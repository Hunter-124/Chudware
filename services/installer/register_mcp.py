"""Register Chudware (formerly Billet / AI-CAD) as an MCP server in the machine's Claude install(s).

Adds a "chudware" entry to the `mcpServers` map of (any legacy "ai-cad" entry is left
in place on register — existing tooling references it — and removed on --unregister):
  * Claude Desktop : %APPDATA%/Claude/claude_desktop_config.json
  * Claude Code     : ~/.claude.json

Idempotent and non-destructive: backs up each file before writing and only writes when the
entry actually changes. Use --dry-run to preview, --unregister to remove.

    python -m services.installer.register_mcp [--dry-run] [--unregister]
        [--python <exe>] [--mcp-bin <exe>] [--cwd <dir>] [--target desktop|code]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from services import paths

SERVER_NAME = "chudware"
SERVER_NAME_LEGACY = "ai-cad"  # left in place on register; removed on --unregister


def default_python() -> Path:
    return paths.RUNTIME / "venv" / "Scripts" / "python.exe"


def default_mcp_bin() -> Path | None:
    value = os.environ.get("CHUDWARE_MCP_BIN")
    return Path(value).expanduser().resolve() if value else None

def adjacent_mcp_bin(cwd: Path) -> Path:
    name = "chudware-mcp.exe" if os.name == "nt" else "chudware-mcp"
    return (cwd.expanduser().resolve() / name).resolve()


def config_paths() -> dict[str, Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return {
        "desktop": appdata / "Claude" / "claude_desktop_config.json",
        "code": home / ".claude.json",
    }


def server_entry(python_exe: Path, cwd: Path, hostd_bin: Path | None = None,
                 mcp_bin: Path | None = None) -> dict:
    cwd = cwd.expanduser().resolve()
    adjacent = adjacent_mcp_bin(cwd)
    supplied = mcp_bin if mcp_bin is not None else default_mcp_bin()
    if supplied is not None and supplied.expanduser().resolve() != adjacent:
        raise ValueError("CHUDWARE_MCP_BIN/--mcp-bin must name the adjacent packaged MCP binary")
    use_compiled = supplied is not None or adjacent.is_file()
    env: dict[str, str] = {}
    if hostd_bin is not None:
        # Pin the installed engine-host daemon so MCP sessions and the GUI share ONE
        # daemon (same binary path -> same instance key -> same socket/pipe).
        env["CHUDWARE_HOSTD_BIN"] = str(hostd_bin)
    if use_compiled:
        command = str(adjacent)
        command_args = ["--module", "mcp"]
    else:
        # Developer-source registration only. A packaged install carries the adjacent binary
        # and therefore never emits a Python fallback.
        command = str(python_exe)
        command_args = ["-m", "services.mcp.server"]
    return {
        "command": command,
        "args": command_args,
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
          unregister: bool, hostd_bin: Path | None = None,
          mcp_bin: Path | None = None) -> dict:
    entry = server_entry(python_exe, cwd, hostd_bin, mcp_bin)
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
                shutil.copy2(path, path.with_suffix(path.suffix + ".chudware-bak"))
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            wrote = True
        out[name] = {"path": str(path), "exists": path.exists(),
                     "changed": changed, "wrote": wrote}
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Register Chudware MCP server with Claude.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--unregister", action="store_true")
    ap.add_argument("--python", type=Path, default=default_python())
    ap.add_argument("--mcp-bin", type=Path, default=default_mcp_bin(),
                    help="compiled MCP executable (must be adjacent to --cwd)")
    ap.add_argument("--cwd", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--target", choices=["desktop", "code", "both"], default="both")
    ap.add_argument("--hostd-bin", type=Path, default=None,
                    help="installed chudware-hostd binary; written into the server env as "
                         "CHUDWARE_HOSTD_BIN so MCP sessions attach to the installed daemon")
    args = ap.parse_args(argv)

    if args.mcp_bin is not None:
        args.mcp_bin = args.mcp_bin.expanduser().resolve()
    try:
        entry = server_entry(args.python, args.cwd, args.hostd_bin, args.mcp_bin)
    except ValueError as exc:
        ap.error(str(exc))
    if not args.unregister and entry["args"] == ["--module", "mcp"] and not Path(
            entry["command"]).is_file():
        ap.error(f"compiled MCP executable not found: {entry['command']}")

    targets = ["desktop", "code"] if args.target == "both" else [args.target]
    res = apply(args.python, args.cwd, targets, args.dry_run, args.unregister,
                hostd_bin=args.hostd_bin, mcp_bin=args.mcp_bin)
    print(json.dumps({
        "action": "unregister" if args.unregister else "register",
        "dry_run": args.dry_run,
        "server": SERVER_NAME,
        "entry": entry,
        "targets": res,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
