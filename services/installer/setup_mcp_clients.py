"""Register the Chudware MCP server into every MCP-capable client on this machine.

The server is registered under the new name ``chudware`` (formerly ``ai-cad``). Existing
``ai-cad`` entries in client configs are left in place on normal registration so that
tooling still referencing the old name continues to work; they are removed only on an
explicit ``--unregister`` run.

One server, multiple clients. This writes/merges the right config file in the right format
for each of:

  * Claude Code  -> <repo>/.mcp.json                         {"mcpServers": {...}}
  * Cursor       -> <repo>/.cursor/mcp.json                  {"mcpServers": {...}}
  * VS Code      -> <repo>/.vscode/mcp.json                  {"servers": {...}}  (type:stdio)
  * opencode     -> <repo>/opencode.json                     {"mcp": {...}}      (type:local)
                    + ~/.config/opencode/opencode.json(c)    (global user config)
  * Grok         -> ~/.grok/config.toml                      [mcp_servers.chudware]
                    + <repo>/.grok/config.toml               (project-scoped)
  * Antigravity  -> ~/.gemini/<antigravity*>/mcp_config.json {"mcpServers": {...}} (global)

Claude Code / Cursor / VS Code / opencode (project) are PROJECT-scoped so the server only
appears when you open this project, and so each machine gets paths that are correct for it.
Grok is written both user-global and project-scoped. Antigravity is global-only.

``--profile lite|full`` selects the MCP instruction/tool surface (env
``CHUDWARE_MCP_PROFILE``). ``lite`` keeps every CAD/sim capability but drops orchestration
scaffolding (DAG/mission/ledger/workspace) — preferred for capable models (Grok, Claude
Code, opencode). Default is ``full`` for backward compatibility with small local models.

The writes are idempotent and non-destructive: existing servers in each file are preserved,
each file is backed up (*.chudware-bak) before the first change, and re-running only rewrites a
file when the "chudware" (or legacy "ai-cad") entry would actually change.

Pure standard library on purpose -- it must run under any Python, including one without the
engine's heavy dependencies installed.

    python services/installer/setup_mcp_clients.py [--python <exe>] [--repo <dir>]
                                          [--profile lite|full]
                                          [--unregister] [--dry-run]
                                          [--detect] [--wizard]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
from pathlib import Path

SERVER_NAME = "chudware"
SERVER_NAME_LEGACY = "ai-cad"


def _default_repo() -> str:
    """Resolve the repository root from the canonical services package."""
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "services" / "mcp").is_dir() and (cand / "requirements.txt").is_file():
            return str(cand)
    return str(here.parents[2])


def _env(repo: str, profile: str) -> dict:
    env = {"PYTHONPATH": repo}
    # Always pin the profile explicitly so clients don't inherit a stale shell default.
    env["CHUDWARE_MCP_PROFILE"] = profile
    # Prefer the staged full engine (OCCT + Netgen) when present so MCP and GUI share one.
    for rel in (
        Path("dist") / "Chudware" / "libchudware.dll",
        Path("build") / "windows-occt" / "bin" / "libchudware.dll",
        Path("dist") / "Chudware" / "libchudware.so",
        Path("core") / "build-occt-linux" / "bin" / "libchudware.so",
    ):
        p = Path(repo) / rel
        if p.is_file():
            env["CHUDWARE_LIBCHUDWARE"] = str(p)
            env["CHUDWARE_LIBCHUDWARE"] = str(p)  # legacy name still honored
            break
    for rel in (
        Path("dist") / "Chudware" / "chudware-hostd.exe",
        Path("build") / "windows-occt" / "bin" / "chudware-hostd.exe",
        Path("dist") / "Chudware" / "chudware-hostd",
        Path("core") / "build-occt-linux" / "bin" / "chudware-hostd",
    ):
        p = Path(repo) / rel
        if p.is_file():
            env["CHUDWARE_HOSTD_BIN"] = str(p)
            break
    return env


# --------------------------------------------------------------------------- entry shapes

def _claude_entry(py: str, repo: str, profile: str) -> dict:
    """Claude Code / Cursor / Antigravity share this shape."""
    return {
        "command": py,
        "args": ["-m", "services.mcp.server"],
        "cwd": repo,
        "env": _env(repo, profile),
    }


def _vscode_entry(py: str, repo: str, profile: str) -> dict:
    return {
        "type": "stdio",
        "command": py,
        "args": ["-m", "services.mcp.server"],
        "cwd": repo,
        "env": _env(repo, profile),
    }


def _opencode_entry(py: str, repo: str, profile: str) -> dict:
    return {
        "type": "local",
        "command": [py, "-m", "services.mcp.server"],
        "enabled": True,
        "environment": _env(repo, profile),
    }


# ------------------------------------------------------------------------------ targets

def _targets(py: str, repo: str, profile: str = "full") -> list[dict]:
    repo_p = Path(repo)
    home = Path.home()
    out = [
        {"client": "Claude Code", "path": repo_p / ".mcp.json",
         "key": "mcpServers", "entry": _claude_entry(py, repo, profile)},
        {"client": "Cursor", "path": repo_p / ".cursor" / "mcp.json",
         "key": "mcpServers", "entry": _claude_entry(py, repo, profile)},
        {"client": "VS Code", "path": repo_p / ".vscode" / "mcp.json",
         "key": "servers", "entry": _vscode_entry(py, repo, profile)},
        {"client": "opencode (project)", "path": repo_p / "opencode.json",
         "key": "mcp", "entry": _opencode_entry(py, repo, profile),
         "extra": {"$schema": "https://opencode.ai/config.json"}},
    ]
    # Global opencode user config (json or jsonc). Prefer existing file name.
    oc_dir = home / ".config" / "opencode"
    oc_jsonc = oc_dir / "opencode.jsonc"
    oc_json = oc_dir / "opencode.json"
    oc_path = oc_jsonc if oc_jsonc.exists() or not oc_json.exists() else oc_json
    out.append({
        "client": "opencode (user)",
        "path": oc_path,
        "key": "mcp",
        "entry": _opencode_entry(py, repo, profile),
        "extra": {"$schema": "https://opencode.ai/config.json"},
        "jsonc": oc_path.suffix == ".jsonc",
    })
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
                    "key": "mcpServers", "entry": _claude_entry(py, repo, profile)})
    # Grok: TOML configs (user + project). Handled separately in _apply_grok.
    out.append({
        "client": "Grok (user)",
        "path": home / ".grok" / "config.toml",
        "format": "grok_toml",
        "entry": {"command": py, "args": ["-m", "services.mcp.server"],
                  "env": _env(repo, profile)},
    })
    out.append({
        "client": "Grok (project)",
        "path": repo_p / ".grok" / "config.toml",
        "format": "grok_toml",
        "entry": {"command": py, "args": ["-m", "services.mcp.server"],
                  "env": _env(repo, profile)},
    })
    return out


# -------------------------------------------------------------------------------- io

def _load(path: Path) -> dict | None:
    if path.exists():
        txt = path.read_text(encoding="utf-8").strip()
        if not txt:
            return {}
        # Strip // line comments for .jsonc (best-effort; does not handle strings with //)
        if path.suffix == ".jsonc" or path.name.endswith(".jsonc"):
            lines = []
            for line in txt.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("//"):
                    continue
                lines.append(line)
            txt = "\n".join(lines).strip() or "{}"
        try:
            return json.loads(txt)
        except Exception:  # noqa: BLE001 -- tolerate hand-edited / non-JSON files
            print(f"  ! {path} is not valid JSON; leaving it alone.", file=sys.stderr)
            return None  # signal: skip, don't clobber
    return {}


def _toml_str(s: str) -> str:
    """TOML string literal safe for Windows paths.

    Basic strings (\"...\") treat \\U as a unicode escape, so ``C:\\Users\\...`` is illegal.
    Literal strings ('...') only forbid unescaped single quotes — perfect for paths.
    """
    if "'" in s:
        # Fall back to basic string with full escaping.
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return f"'{s}'"


def _grok_section(entry: dict) -> str:
    """Render a [mcp_servers.chudware] TOML table for Grok's config.toml."""
    cmd = _toml_str(entry["command"])
    args = ", ".join(_toml_str(a) for a in entry["args"])
    env_pairs = []
    for k, v in (entry.get("env") or {}).items():
        env_pairs.append(f"{k} = {_toml_str(str(v))}")
    env_inline = ", ".join(env_pairs)
    lines = [
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {cmd}",
        f"args = [{args}]",
        f"env = {{ {env_inline} }}",
        "enabled = true",
        "startup_timeout_sec = 60",
        "",
    ]
    return "\n".join(lines)


def _toml_strip_table(text: str, table: str) -> str:
    """Remove a TOML table ``[name]`` and its body (until the next top-level ``[``)."""
    lines = text.splitlines(keepends=True)
    header = f"[{table}]"
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == header or stripped.startswith(header + " "):
            i += 1
            while i < len(lines):
                s = lines[i].lstrip()
                if s.startswith("[") and not s.startswith("[["):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def _toml_upsert_table(text: str, table: str, body: str) -> str:
    """Replace or append a TOML table. ``body`` is the full table including the header line."""
    stripped = _toml_strip_table(text, table).rstrip()
    # Drop orphaned chudware fragments left by older broken rewrites (env/args without a header).
    if table == f"mcp_servers.{SERVER_NAME}":
        cleaned: list[str] = []
        for line in stripped.splitlines(keepends=True):
            s = line.strip()
            if not s:
                cleaned.append(line)
                continue
            # Bare key/value lines that only make sense inside [mcp_servers.chudware]
            if s.startswith("env = {") and "CHUDWARE_MCP_PROFILE" in s:
                continue
            if s.startswith("args = [") and "services.mcp.server" in s:
                continue
            if s.startswith('["-m"') or s.startswith("['-m'"):
                continue
            if s in ("enabled = true", "startup_timeout_sec = 60"):
                # Orphan only if we have no open mcp section context in cleaned so far
                if not any(x.strip().startswith("[mcp_servers.") for x in cleaned):
                    continue
            cleaned.append(line)
        stripped = "".join(cleaned).rstrip()
    body = body if body.endswith("\n") else body + "\n"
    if not stripped:
        return body
    return stripped + "\n\n" + body


def _apply_grok(t: dict, unregister: bool, dry_run: bool) -> dict:
    path: Path = t["path"]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    table = f"mcp_servers.{SERVER_NAME}"
    legacy = f"mcp_servers.{SERVER_NAME_LEGACY}"
    has_server = f"[{table}]" in existing
    has_legacy = f"[{legacy}]" in existing

    if unregister:
        new_txt = _toml_strip_table(existing, table)
        new_txt = _toml_strip_table(new_txt, legacy)
        new_txt = re.sub(r"\n{3,}", "\n\n", new_txt).rstrip() + ("\n" if new_txt.strip() else "")
        changed = new_txt != existing
    else:
        section = _grok_section(t["entry"])
        new_txt = _toml_upsert_table(existing, table, section)
        if not new_txt.endswith("\n"):
            new_txt += "\n"
        changed = new_txt != existing
        # Leave legacy ai-cad section alone on register.

    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".chudware-bak"))
        path.write_text(new_txt, encoding="utf-8")

    status = ("would change" if dry_run else "wrote") if changed else "unchanged"
    if unregister and not (has_server or has_legacy) and not changed:
        status = "unchanged"
    return {"client": t["client"], "path": str(path), "status": status}


def _apply_one(t: dict, unregister: bool, dry_run: bool) -> dict:
    if t.get("format") == "grok_toml":
        return _apply_grok(t, unregister, dry_run)

    path: Path = t["path"]
    cfg = _load(path)
    if cfg is None:
        return {"client": t["client"], "path": str(path), "status": "skipped (unparseable)"}

    servers = cfg.get(t["key"])
    if not isinstance(servers, dict):
        servers = {}

    if unregister:
        changed = SERVER_NAME in servers or SERVER_NAME_LEGACY in servers
        servers.pop(SERVER_NAME, None)
        servers.pop(SERVER_NAME_LEGACY, None)  # remove legacy id on explicit unregister
    else:
        changed = servers.get(SERVER_NAME) != t["entry"]
        servers[SERVER_NAME] = t["entry"]
        # Leave any legacy "ai-cad" entry in place — existing tooling references it.
        # Run with --unregister then re-register to clean up the old entry.

    cfg[t["key"]] = servers
    for k, v in t.get("extra", {}).items():
        if cfg.get(k) != v:
            cfg[k] = v
            changed = True

    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".chudware-bak"))
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    status = ("would change" if dry_run else "wrote") if changed else "unchanged"
    return {"client": t["client"], "path": str(path), "status": status}


# ------------------------------------------------------ first-run wizard groundwork

def _status_of(t: dict) -> dict:
    """Detection record for one client target: is the client present, and are we wired to it?

    ``present`` = the client has left a footprint on this machine (its config file or the
    directory that holds it already exists) -- a good-enough, stdlib-only heuristic for a
    first-run "we found these clients" prompt. ``connected`` = chudware is already registered.
    """
    path: Path = t["path"]
    if t.get("format") == "grok_toml":
        present = path.exists() or path.parent.exists()
        connected = False
        if path.exists():
            txt = path.read_text(encoding="utf-8")
            connected = bool(_GROK_SECTION_RE.search(txt)) or bool(_GROK_LEGACY_RE.search(txt))
        return {"client": t["client"], "path": str(path), "present": present,
                "connected": connected}

    cfg = _load(path) if path.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    connected = SERVER_NAME in (cfg.get(t["key"]) or {}) or SERVER_NAME_LEGACY in (cfg.get(t["key"]) or {})
    present = path.exists() or path.parent.exists()
    return {"client": t["client"], "path": str(path), "present": present,
            "connected": connected}


def detect(py: str, repo: str, profile: str = "full") -> list[dict]:
    """Report every known MCP client target with presence + connection status."""
    return [_status_of(t) for t in _targets(py, repo, profile)]


def _run_wizard(py: str, repo: str, dry_run: bool, profile: str = "full") -> list[dict]:
    """Connect just the clients that are present but not yet wired to chudware."""
    results = []
    for t in _targets(py, repo, profile):
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
    ap.add_argument("--repo", default=_default_repo(),
                    help="Repository root (default: auto-detect from this script)")
    ap.add_argument("--profile", choices=["lite", "full"], default="full",
                    help="MCP profile: lite (capable models) or full (supervised/local). "
                         "Sets CHUDWARE_MCP_PROFILE in every client entry.")
    ap.add_argument("--unregister", action="store_true", help="Remove the chudware/ai-cad entries")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change")
    ap.add_argument("--detect", action="store_true",
                    help="Report which MCP clients are present / already connected, then exit")
    ap.add_argument("--wizard", action="store_true",
                    help="Connect only the clients detected on this machine (first-run flow)")
    args = ap.parse_args(argv)

    py = str(Path(args.python).resolve())
    repo = str(Path(args.repo).resolve())
    profile = args.profile

    if args.detect:
        rows = detect(py, repo, profile)
        width = max(len(r["client"]) for r in rows)
        print("Detected MCP clients (present = footprint on this machine):\n")
        for r in rows:
            mark = "connected" if r["connected"] else ("present" if r["present"] else "-")
            print(f"  {r['client']:<{width}}  {mark:<10}  {r['path']}")
        return 0

    action = "Unregistering" if args.unregister else "Registering"
    if args.wizard:
        action = "Connecting detected"
    print(f"{action} '{SERVER_NAME}' MCP server (legacy: '{SERVER_NAME_LEGACY}')")
    print(f"  python  : {py}")
    print(f"  repo    : {repo}")
    print(f"  profile : {profile}")
    print()

    if args.wizard:
        results = _run_wizard(py, repo, args.dry_run, profile)
    else:
        results = [_apply_one(t, args.unregister, args.dry_run)
                   for t in _targets(py, repo, profile)]
    width = max(len(r["client"]) for r in results)
    for r in results:
        print(f"  {r['client']:<{width}}  {r['status']:<22}  {r['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
