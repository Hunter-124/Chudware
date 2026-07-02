"""Register the AI-CAD MCP server into every MCP-capable client on this machine.

One server, five clients. This writes/merges the right config file in the right format
for each of:

  * Claude Code  -> <repo>/.mcp.json                         {"mcpServers": {...}}
  * Cursor       -> <repo>/.cursor/mcp.json                  {"mcpServers": {...}}
  * VS Code      -> <repo>/.vscode/mcp.json                  {"servers": {...}}  (type:stdio)
  * opencode     -> <repo>/opencode.json                     {"mcp": {...}}      (type:local)
  * Antigravity  -> ~/.gemini/<antigravity*>/mcp_config.json {"mcpServers": {...}} (global)

Claude Code / Cursor / VS Code / opencode are wired PROJECT-scoped (config lives in the repo)
so the server only appears when you open this project, and so each machine gets paths that
are correct for it. Antigravity has no project-scoped config, so it is wired globally.

The writes are idempotent and non-destructive: existing servers in each file are preserved,
each file is backed up (*.aicad-bak) before the first change, and re-running only rewrites a
file when the "ai-cad" entry would actually change.

Pure standard library on purpose -- it must run under any Python, including one without the
engine's heavy dependencies installed.

    python installer/setup_mcp_clients.py [--python <exe>] [--repo <dir>]
                                          [--unregister] [--dry-run]
                                          [--detect] [--wizard]

First-run "connect your client" wizard groundwork: ``--detect`` reports which MCP clients
this machine appears to have (and whether ai-cad is already wired); ``--wizard`` connects
just the detected-but-unconnected ones (reusing the same idempotent writer). This is the
seam a GUI first-run flow drives -- see contracts/providers.md (Pillar 12 MCP-first).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from pathlib import Path

SERVER_NAME = "ai-cad"


# --------------------------------------------------------------------------- entry shapes

def _claude_entry(py: str, repo: str) -> dict:
    """Claude Code / Cursor / Antigravity share this shape."""
    return {
        "command": py,
        "args": ["-m", "mcp_server.server"],
        "cwd": repo,
        "env": {"PYTHONPATH": repo},
    }


def _vscode_entry(py: str, repo: str) -> dict:
    return {
        "type": "stdio",
        "command": py,
        "args": ["-m", "mcp_server.server"],
        "cwd": repo,
        "env": {"PYTHONPATH": repo},
    }


def _opencode_entry(py: str, repo: str) -> dict:
    return {
        "type": "local",
        "command": [py, "-m", "mcp_server.server"],
        "enabled": True,
        "environment": {"PYTHONPATH": repo},
    }


# ------------------------------------------------------------------------------ targets

def _targets(py: str, repo: str) -> list[dict]:
    repo_p = Path(repo)
    home = Path.home()
    out = [
        {"client": "Claude Code", "path": repo_p / ".mcp.json",
         "key": "mcpServers", "entry": _claude_entry(py, repo)},
        {"client": "Cursor", "path": repo_p / ".cursor" / "mcp.json",
         "key": "mcpServers", "entry": _claude_entry(py, repo)},
        {"client": "VS Code", "path": repo_p / ".vscode" / "mcp.json",
         "key": "servers", "entry": _vscode_entry(py, repo)},
        {"client": "opencode", "path": repo_p / "opencode.json",
         "key": "mcp", "entry": _opencode_entry(py, repo),
         "extra": {"$schema": "https://opencode.ai/config.json"}},
    ]
    # Antigravity: global, no project config. Write into each existing gemini profile
    # that already carries an mcp_config.json (antigravity, antigravity-ide, ...), but
    # never the *-backup copies. If none exist yet, seed the canonical one.
    ag = sorted(
        p for p in glob.glob(str(home / ".gemini" / "*" / "mcp_config.json"))
        if "antigravity" in Path(p).parent.name.lower() and "backup" not in Path(p).parent.name.lower()
    )
    if not ag:
        ag = [str(home / ".gemini" / "antigravity" / "mcp_config.json")]
    for p in ag:
        out.append({"client": f"Antigravity ({Path(p).parent.name})", "path": Path(p),
                    "key": "mcpServers", "entry": _claude_entry(py, repo)})
    return out


# -------------------------------------------------------------------------------- io

def _load(path: Path) -> dict:
    if path.exists():
        txt = path.read_text(encoding="utf-8").strip()
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except Exception:  # noqa: BLE001 -- tolerate hand-edited / non-JSON files
            print(f"  ! {path} is not valid JSON; leaving it alone.", file=sys.stderr)
            return None  # signal: skip, don't clobber
    return {}


def _apply_one(t: dict, unregister: bool, dry_run: bool) -> dict:
    path: Path = t["path"]
    cfg = _load(path)
    if cfg is None:
        return {"client": t["client"], "path": str(path), "status": "skipped (unparseable)"}

    servers = cfg.get(t["key"])
    if not isinstance(servers, dict):
        servers = {}

    if unregister:
        changed = SERVER_NAME in servers
        servers.pop(SERVER_NAME, None)
    else:
        changed = servers.get(SERVER_NAME) != t["entry"]
        servers[SERVER_NAME] = t["entry"]

    cfg[t["key"]] = servers
    for k, v in t.get("extra", {}).items():
        if cfg.get(k) != v:
            cfg[k] = v
            changed = True

    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".aicad-bak"))
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    status = ("would change" if dry_run else "wrote") if changed else "unchanged"
    return {"client": t["client"], "path": str(path), "status": status}


# ------------------------------------------------------ first-run wizard groundwork

def _status_of(t: dict) -> dict:
    """Detection record for one client target: is the client present, and are we wired to it?

    ``present`` = the client has left a footprint on this machine (its config file or the
    directory that holds it already exists) -- a good-enough, stdlib-only heuristic for a
    first-run "we found these clients" prompt. ``connected`` = ai-cad is already registered.
    """
    path: Path = t["path"]
    cfg = _load(path) if path.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    connected = SERVER_NAME in (cfg.get(t["key"]) or {})
    present = path.exists() or path.parent.exists()
    return {"client": t["client"], "path": str(path), "present": present,
            "connected": connected}


def detect(py: str, repo: str) -> list[dict]:
    """Report every known MCP client target with presence + connection status."""
    return [_status_of(t) for t in _targets(py, repo)]


def _run_wizard(py: str, repo: str, dry_run: bool) -> list[dict]:
    """Connect just the clients that are present but not yet wired to ai-cad."""
    results = []
    for t in _targets(py, repo):
        st = _status_of(t)
        if st["connected"] or not st["present"]:
            results.append({"client": t["client"], "path": st["path"],
                            "status": "already connected" if st["connected"]
                            else "skipped (client not detected)"})
            continue
        results.append(_apply_one(t, unregister=False, dry_run=dry_run))
    return results


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=sys.executable,
                    help="Python interpreter the server runs under (default: this one)")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent),
                    help="Repository root (default: this script's parent)")
    ap.add_argument("--unregister", action="store_true", help="Remove the ai-cad entries")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change")
    ap.add_argument("--detect", action="store_true",
                    help="Report which MCP clients are present / already connected, then exit")
    ap.add_argument("--wizard", action="store_true",
                    help="Connect only the clients detected on this machine (first-run flow)")
    args = ap.parse_args(argv)

    py = str(Path(args.python).resolve())
    repo = str(Path(args.repo).resolve())

    if args.detect:
        rows = detect(py, repo)
        width = max(len(r["client"]) for r in rows)
        print("Detected MCP clients (present = footprint on this machine):\n")
        for r in rows:
            mark = "connected" if r["connected"] else ("present" if r["present"] else "-")
            print(f"  {r['client']:<{width}}  {mark:<10}  {r['path']}")
        return 0

    action = "Unregistering" if args.unregister else "Registering"
    if args.wizard:
        action = "Connecting detected"
    print(f"{action} '{SERVER_NAME}' MCP server")
    print(f"  python : {py}")
    print(f"  repo   : {repo}")
    print()

    if args.wizard:
        results = _run_wizard(py, repo, args.dry_run)
    else:
        results = [_apply_one(t, args.unregister, args.dry_run) for t in _targets(py, repo)]
    width = max(len(r["client"]) for r in results)
    for r in results:
        print(f"  {r['client']:<{width}}  {r['status']:<22}  {r['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
