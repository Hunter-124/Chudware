#!/usr/bin/env python3
"""_teamlib.py — small shared helpers for team_mcp.py, work.py and dashboard.py.

Two things live here:
  1. Per-machine IDENTITY: who is at this checkout. A human contributor is
     identified by their GitHub username and stored in a gitignored JSON profile
     (work/sync/identity.json). This is what the tooling commits board changes as
     and what it uses to tell you which tasks are waiting on YOU. A legacy
     plaintext file (work/sync/.identity) holding just an agent working-name is
     still read/written for backward compatibility.
  2. A cheap graphify staleness check.

Stdlib only. Not a CLI entry point — imported by the tools in this same directory
(work/tools/), so Python's default "script dir on sys.path[0]" makes
`import _teamlib` work with no path setup.

IDENTITY vs GIT CONFIG (important): we NEVER run `git config user.*` (a hard
project rule — past sessions fabricated identities and poisoned history). To
author a commit as the identified contributor we pass GIT_AUTHOR_*/GIT_COMMITTER_*
environment variables for that one subprocess only — non-persistent, never written
to any git config. If no email is known we don't override anything and the
machine's own git identity is used, exactly as before.
"""
import json
import subprocess
from pathlib import Path

WORK = Path(__file__).resolve().parent.parent   # work/
SYNC = WORK / "sync"                             # gitignored (see repo .gitignore)
IDENTITY_PATH = SYNC / ".identity"               # legacy: plaintext agent name
PROFILE_PATH = SYNC / "identity.json"            # human profile (github-keyed)

# fields we persist in identity.json
_PROFILE_KEYS = ("github_user", "display_name", "git_email", "agent_name")


# ---------------------------------------------------------------- profile I/O

def get_profile():
    """Return the stored identity profile as a dict (possibly empty). Migrates a
    legacy .identity file (agent working-name only) on the fly."""
    if PROFILE_PATH.exists():
        try:
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k in _PROFILE_KEYS and v}
        except Exception:
            pass
    if IDENTITY_PATH.exists():
        name = IDENTITY_PATH.read_text(encoding="utf-8").strip()
        if name:
            return {"agent_name": name}
    return {}


def set_profile(**fields):
    """Merge the given fields into the stored profile and write it. Unknown keys
    are ignored; empty values clear a field. Mirrors the effective owner-name to
    the legacy .identity file so older readers keep working."""
    prof = get_profile()
    for k, v in fields.items():
        if k not in _PROFILE_KEYS:
            continue
        if v:
            prof[k] = str(v).strip()
        else:
            prof.pop(k, None)
    SYNC.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(prof, indent=2) + "\n", encoding="utf-8")
    owner = owner_name(prof)
    if owner:
        IDENTITY_PATH.write_text(owner + "\n", encoding="utf-8")
    return prof


def owner_name(prof=None):
    """The working owner string used on the board: an explicit agent working-name
    if set (autonomous drivers), else the human's GitHub username."""
    prof = prof if prof is not None else get_profile()
    return prof.get("agent_name") or prof.get("github_user") or None


def human_name(prof=None):
    """The human identity (GitHub username) regardless of any agent working-name.
    Used by 'what's waiting for ME' views."""
    prof = prof if prof is not None else get_profile()
    return prof.get("github_user") or prof.get("agent_name") or None


def identity_aliases(prof=None):
    """All owner strings that mean 'me' — github user + agent name + display name —
    lowercased and de-duplicated, so `mine` catches tasks claimed under any of them."""
    prof = prof if prof is not None else get_profile()
    seen = []
    for k in ("github_user", "agent_name", "display_name"):
        v = (prof.get(k) or "").strip().lower()
        if v and v not in seen:
            seen.append(v)
    return seen


# ---- backward-compatible plain helpers (old call sites: team_mcp._need etc.) ----

def get_identity():
    return owner_name()


def set_identity(name):
    """Old whoami(name=...) path: sets the agent working-name only, leaving the
    human github_user profile intact."""
    return set_profile(agent_name=name)


# ---------------------------------------------------------------- detection

def _cap(args, timeout=8):
    try:
        r = subprocess.run(args, cwd=str(WORK.parent), capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def detect_github_user():
    """Best-effort GitHub username: `gh api user`, else the `gh auth status`
    account, else "". Never raises."""
    login = _cap(["gh", "api", "user", "--jq", ".login"])
    if login:
        return login
    # `gh auth status` prints e.g. "Logged in to github.com account NAME (...)"
    out = _cap(["gh", "auth", "status"])
    for tok in out.replace("\n", " ").split():
        if tok and tok not in ("account",) and out.find("account " + tok) != -1:
            return tok
    return ""


def detect_git_email():
    return _cap(["git", "config", "user.email"])


def detect_git_name():
    return _cap(["git", "config", "user.name"])


def resolve_and_store(github_user=None):
    """Fill any missing profile fields from `gh`/`git config` and persist. Returns
    the resolved profile. Used by whoami — safe to call repeatedly (idempotent)."""
    prof = get_profile()
    gh = github_user or prof.get("github_user") or detect_github_user()
    fields = {}
    if gh and gh != prof.get("github_user"):
        fields["github_user"] = gh
    if not prof.get("git_email"):
        em = detect_git_email()
        if em:
            fields["git_email"] = em
    if not prof.get("display_name"):
        fields["display_name"] = prof.get("display_name") or detect_git_name() or gh
    return set_profile(**fields) if fields else prof


# ---------------------------------------------------------------- commit env

def commit_env(base_env=None):
    """Return an env dict for authoring a commit AS the identified contributor,
    or None if we have no email to author with (caller then uses machine identity).
    Never touches git config — this is per-subprocess only."""
    prof = get_profile()
    email = prof.get("git_email")
    if not email:
        return None
    name = prof.get("display_name") or prof.get("github_user")
    if not name:
        return None
    import os
    env = dict(base_env if base_env is not None else os.environ)
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    return env


# ---------------------------------------------------------------- graphify

def graphify_freshness(repo):
    """Return a one-line staleness note if any graphify-indexed file changed since
    the last build, else "". Cheap: mtime comparison against graphify-out/manifest.json
    (no hashing, no rebuild) — files graphify doesn't index are silently skipped."""
    manifest_path = repo / "graphify-out" / "manifest.json"
    if not manifest_path.exists():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    try:
        r = subprocess.run(["git", "ls-files"], cwd=str(repo),
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    if r.returncode != 0:
        return ""
    changed = 0
    for rel in r.stdout.splitlines():
        entry = manifest.get(rel)
        if not isinstance(entry, dict):
            continue
        try:
            mtime = (repo / rel).stat().st_mtime
        except OSError:
            continue
        if mtime > entry.get("mtime", 0) + 1:
            changed += 1
    if changed:
        return (f"[note: {changed} graphify-indexed file(s) changed since the last "
                f"build — this answer may be stale; run `graphify update .`]\n")
    return ""
