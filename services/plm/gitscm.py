"""Real-git / GitHub client backend for the PLM **Git** tab.

Unlike the rest of ``services/plm/`` (the hidden dulwich *vault* — snapshot
history with no git vocabulary), this module is an **explicit** git client: it
drives the system ``git`` and GitHub ``gh`` binaries against the project's real
working repo (the ``.git`` at the project folder, NOT ``.aicadvault/``). The
PLM "Git" GUI tab is a deliberate power-user surface, so git terminology is
expected here.

Every function returns a plain dict enveloped like the worker seam
(``{ok: True, …}`` / ``{ok: False, reason, hint}``) and never raises to the
caller. When ``git`` (or ``gh`` for the GitHub features) is not installed the
call degrades to ``{ok: False, reason: "git_missing"/"gh_missing"}`` so the tab
can show an honest empty state instead of crashing — mirrors the dulwich import
guard in ``worker_rpc.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

_TIMEOUT = 60  # seconds — network ops (push/pull/gh) can be slow but not forever


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(args: list[str], cwd: str, *, timeout: int = _TIMEOUT,
         check_repo: bool = True) -> dict:
    """Run ``args`` in *cwd*. Returns ``{ok, code, out, err}`` (never raises).

    ``ok`` is True only on exit code 0. Callers translate non-zero into the
    ``{ok:False, reason, hint}`` envelope with a domain-appropriate reason.
    """
    binary = args[0]
    if not _have(binary):
        return {"ok": False, "code": 127, "out": "", "err": f"{binary} not found",
                "missing": binary}
    if check_repo and not os.path.isdir(cwd):
        return {"ok": False, "code": 2, "out": "", "err": "no such folder"}
    try:
        # Keep git non-interactive: never prompt for credentials on the GL thread.
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout, "err": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "out": "", "err": "timed out"}
    except Exception as e:  # noqa: BLE001 — never raise across the seam
        return {"ok": False, "code": 1, "out": "", "err": f"{type(e).__name__}: {e}"}


def _git(args: list[str], cwd: str, **kw) -> dict:
    return _run(["git", *args], cwd, **kw)


def _gh(args: list[str], cwd: str, **kw) -> dict:
    return _run(["gh", *args], cwd, **kw)


def _fail(reason: str, hint: str) -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _git_missing() -> dict:
    return _fail("git_missing", "Git is not installed on this machine.")


def _gh_missing() -> dict:
    return _fail("gh_missing",
                 "The GitHub CLI (gh) is not installed — GitHub features are "
                 "unavailable, but local git still works.")


# ---------------------------------------------------------------------------
# repo detection / init
# ---------------------------------------------------------------------------

def is_repo(path: str) -> dict:
    """Whether *path* is inside a git work tree."""
    if not _have("git"):
        return {"ok": True, "is_repo": False, "git_missing": True}
    r = _git(["rev-parse", "--is-inside-work-tree"], path)
    return {"ok": True, "is_repo": bool(r["ok"] and r["out"].strip() == "true")}


def init(path: str) -> dict:
    """``git init`` a project folder that isn't a repo yet."""
    if not _have("git"):
        return _git_missing()
    r = _git(["init"], path, check_repo=True)
    if not r["ok"]:
        return _fail("init_failed", r["err"] or "could not initialize a repo")
    return {"ok": True, "initialized": True}


# ---------------------------------------------------------------------------
# read: status / log / branches / remotes / tree / diff
# ---------------------------------------------------------------------------

def status(path: str) -> dict:
    """Branch, upstream, ahead/behind and per-file change rows.

    Uses ``--porcelain=v2 --branch`` for a stable machine format.
    """
    if not _have("git"):
        return _git_missing()
    r = _git(["status", "--porcelain=v2", "--branch"], path)
    if not r["ok"]:
        return _fail("not_a_repo", "This folder is not a git repository yet.")

    branch = ""
    upstream = ""
    ahead = behind = 0
    files: list[dict[str, Any]] = []
    for line in r["out"].splitlines():
        if line.startswith("# branch.head"):
            branch = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.upstream"):
            upstream = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.ab"):
            # "# branch.ab +A -B"
            parts = line.split()
            for tok in parts:
                if tok.startswith("+"):
                    ahead = int(tok[1:] or 0)
                elif tok.startswith("-"):
                    behind = int(tok[1:] or 0)
        elif line.startswith(("1 ", "2 ")):
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            cols = line.split(" ", 8)
            xy = cols[1]
            fpath = cols[8]
            staged = xy[0] != "."
            unstaged = xy[1] != "."
            files.append({"path": fpath, "xy": xy, "staged": staged,
                          "unstaged": unstaged, "untracked": False})
        elif line.startswith("? "):
            files.append({"path": line[2:], "xy": "??", "staged": False,
                          "unstaged": True, "untracked": True})

    return {"ok": True, "branch": branch, "upstream": upstream,
            "ahead": ahead, "behind": behind, "clean": not files,
            "files": files}


def log(path: str, limit: int = 50, ref: str = "") -> dict:
    """Recent commits: hash (short), author, ISO date, subject."""
    if not _have("git"):
        return _git_missing()
    sep = "\x1f"  # unit separator — safe against commit text
    fmt = sep.join(["%h", "%an", "%aI", "%s"])
    args = ["log", f"--pretty=format:{fmt}", f"-n{max(1, int(limit))}"]
    if ref:
        args.append(ref)
    r = _git(args, path)
    if not r["ok"]:
        # empty repo (no commits) is not an error — return an empty list
        if "does not have any commits" in r["err"] or "unknown revision" in r["err"]:
            return {"ok": True, "commits": []}
        return _fail("log_failed", r["err"] or "could not read history")
    commits: list[dict[str, str]] = []
    for line in r["out"].splitlines():
        if not line:
            continue
        parts = line.split(sep)
        if len(parts) < 4:
            continue
        commits.append({"hash": parts[0], "author": parts[1],
                        "date": parts[2], "subject": parts[3]})

    total = 0
    rc = _git(["rev-list", "--count", "HEAD"], path)
    if rc["ok"]:
        total = int((rc["out"] or "0").strip() or 0)
    last24 = 0
    r24 = _git(["rev-list", "--count", "--since=24.hours.ago", "HEAD"], path)
    if r24["ok"]:
        last24 = int((r24["out"] or "0").strip() or 0)

    return {"ok": True, "commits": commits, "total_commits": total,
            "commits_24h": last24}


def branches(path: str) -> dict:
    """Local + remote branches with the current one flagged."""
    if not _have("git"):
        return _git_missing()
    r = _git(["branch", "-a", "--format=%(refname:short)%09%(HEAD)"], path)
    if not r["ok"]:
        return _fail("branch_failed", r["err"] or "could not list branches")
    out: list[dict[str, Any]] = []
    for line in r["out"].splitlines():
        if not line.strip():
            continue
        name, _, head = line.partition("\t")
        remote = name.startswith("remotes/")
        short = name.replace("remotes/", "", 1) if remote else name
        # Drop the bare remote name and its symbolic HEAD pointer (e.g.
        # "origin", "origin/HEAD") — they aren't real branches.
        if remote and ("/" not in short or short.endswith("/HEAD")):
            continue
        out.append({"name": short, "current": head.strip() == "*",
                    "remote": remote})
    return {"ok": True, "branches": out}


def remotes(path: str) -> dict:
    """Configured remotes: name + fetch url (dedup fetch/push pairs)."""
    if not _have("git"):
        return _git_missing()
    r = _git(["remote", "-v"], path)
    if not r["ok"]:
        return _fail("remote_failed", r["err"] or "could not list remotes")
    seen: dict[str, str] = {}
    for line in r["out"].splitlines():
        parts = line.split()
        if len(parts) >= 2:
            seen.setdefault(parts[0], parts[1])
    return {"ok": True, "remotes": [{"name": n, "url": u} for n, u in seen.items()]}


def tree(path: str, ref: str = "HEAD") -> dict:
    """Tracked files at *ref* (flat list of repo-relative paths)."""
    if not _have("git"):
        return _git_missing()
    r = _git(["ls-tree", "-r", "--name-only", ref or "HEAD"], path)
    if not r["ok"]:
        if "Not a valid object name" in r["err"] or "unknown revision" in r["err"]:
            return {"ok": True, "files": []}  # no commits yet
        return _fail("tree_failed", r["err"] or "could not list files")
    files = [ln for ln in r["out"].splitlines() if ln]
    return {"ok": True, "files": files}


def file_at(path: str, file: str, ref: str = "HEAD",
            max_bytes: int = 200_000) -> dict:
    """Blob content of *file* at *ref* (truncated to *max_bytes*)."""
    if not _have("git"):
        return _git_missing()
    if not file:
        return _fail("invalid", "no file given")
    r = _git(["show", f"{ref or 'HEAD'}:{file}"], path)
    if not r["ok"]:
        return _fail("show_failed", r["err"] or "could not read file")
    text = r["out"]
    truncated = len(text) > max_bytes
    return {"ok": True, "path": file, "content": text[:max_bytes],
            "truncated": truncated}


def diff(path: str, file: str = "", staged: bool = False) -> dict:
    """Unified diff for a file (or the whole tree when *file* is empty)."""
    if not _have("git"):
        return _git_missing()
    args = ["diff"]
    if staged:
        args.append("--cached")
    if file:
        args += ["--", file]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("diff_failed", r["err"] or "could not diff")
    return {"ok": True, "diff": r["out"]}


# ---------------------------------------------------------------------------
# mutations: stage / commit / branch / sync
# ---------------------------------------------------------------------------

def stage(path: str, files: list[str] | None = None) -> dict:
    if not _have("git"):
        return _git_missing()
    args = ["add", "--"] + list(files) if files else ["add", "-A"]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("stage_failed", r["err"] or "could not stage")
    return {"ok": True}


def unstage(path: str, files: list[str] | None = None) -> dict:
    if not _have("git"):
        return _git_missing()
    args = ["restore", "--staged", "--"] + list(files) if files else \
        ["reset", "-q", "HEAD"]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("unstage_failed", r["err"] or "could not unstage")
    return {"ok": True}


def commit(path: str, message: str, stage_all: bool = False) -> dict:
    if not _have("git"):
        return _git_missing()
    if not (message or "").strip():
        return _fail("empty_message", "a commit needs a message")
    if stage_all:
        st = stage(path)
        if not st["ok"]:
            return st
    r = _git(["commit", "-m", message], path)
    if not r["ok"]:
        if "nothing to commit" in (r["out"] + r["err"]):
            return _fail("nothing_to_commit", "there are no staged changes")
        return _fail("commit_failed", r["err"] or "could not commit")
    return {"ok": True, "output": r["out"].strip()}


def create_branch(path: str, name: str, checkout: bool = True) -> dict:
    if not _have("git"):
        return _git_missing()
    if not (name or "").strip():
        return _fail("invalid", "a branch needs a name")
    args = ["checkout", "-b", name] if checkout else ["branch", name]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("branch_failed", r["err"] or "could not create branch")
    return {"ok": True}


def checkout_branch(path: str, name: str) -> dict:
    if not _have("git"):
        return _git_missing()
    r = _git(["checkout", name], path)
    if not r["ok"]:
        return _fail("checkout_failed", r["err"] or "could not switch branch")
    return {"ok": True}


def fetch(path: str, remote: str = "") -> dict:
    if not _have("git"):
        return _git_missing()
    args = ["fetch", "--prune"] + ([remote] if remote else ["--all"])
    r = _git(args, path)
    if not r["ok"]:
        return _fail("fetch_failed", r["err"] or "could not fetch")
    return {"ok": True, "output": (r["out"] + r["err"]).strip()}


def pull(path: str) -> dict:
    if not _have("git"):
        return _git_missing()
    r = _git(["pull", "--ff-only"], path)
    if not r["ok"]:
        return _fail("pull_failed", r["err"] or "could not pull")
    return {"ok": True, "output": (r["out"] + r["err"]).strip()}


def push(path: str, set_upstream: bool = False) -> dict:
    if not _have("git"):
        return _git_missing()
    args = ["push"]
    if set_upstream:
        # push the current branch and set its upstream in one shot
        args = ["push", "-u", "origin", "HEAD"]
    r = _git(args, path)
    if not r["ok"]:
        # a missing upstream is a common, recoverable case — flag it distinctly
        if "has no upstream branch" in r["err"]:
            return _fail("no_upstream",
                         "This branch has no upstream yet — publish it first.")
        return _fail("push_failed", r["err"] or "could not push")
    return {"ok": True, "output": (r["out"] + r["err"]).strip()}


# ---------------------------------------------------------------------------
# GitHub via the gh CLI (auth + repo/PR/issue)
# ---------------------------------------------------------------------------

def gh_auth_status(path: str = ".") -> dict:
    """Whether the user is signed in to GitHub, and as whom."""
    if not _have("gh"):
        return _gh_missing()
    # gh api user is the most reliable "am I authed + who" probe.
    r = _gh(["api", "user", "--jq", ".login"], path, check_repo=False)
    if r["ok"] and r["out"].strip():
        return {"ok": True, "authed": True, "user": r["out"].strip()}
    return {"ok": True, "authed": False, "user": ""}


def gh_auth_login(path: str = ".", hostname: str = "") -> dict:
    """Kick off web-based OAuth sign-in.

    ``gh auth login --web`` prints a one-time device code and a verification
    URL, then opens the default browser. We spawn it non-blocking and parse the
    code/URL from its early output so the GUI can display (and re-open) them.
    Self-hosted GitHub Enterprise: pass *hostname*.
    """
    if not _have("gh"):
        return _gh_missing()
    args = ["gh", "auth", "login", "--web", "--git-protocol", "https"]
    if hostname:
        args += ["--hostname", hostname]
    try:
        env = dict(os.environ)
        proc = subprocess.Popen(args, cwd=path if os.path.isdir(path) else None,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, env=env)
    except Exception as e:  # noqa: BLE001
        return _fail("login_failed", f"{type(e).__name__}: {e}")

    # Read the first lines to capture the one-time code + URL, then detach.
    code = ""
    url = "https://github.com/login/device"
    import re
    import time
    deadline = time.time() + 8
    assert proc.stdout is not None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = re.search(r"one-time code:\s*([A-Z0-9-]+)", line)
        if m:
            code = m.group(1)
        m = re.search(r"(https?://\S+)", line)
        if m and "device" in line.lower():
            url = m.group(1)
        if code:
            break
    return {"ok": True, "started": True, "code": code, "url": url,
            "hint": "Enter the code in your browser to finish signing in."}


def repo_info(path: str) -> dict:
    """GitHub metadata for the repo at *path* (name, owner, visibility, url)."""
    if not _have("gh"):
        return _gh_missing()
    r = _gh(["repo", "view", "--json",
             "name,owner,visibility,url,defaultBranchRef,description"], path)
    if not r["ok"]:
        return _fail("no_github_repo",
                     "This repository isn't connected to GitHub.")
    import json
    try:
        data = json.loads(r["out"])
    except Exception:  # noqa: BLE001
        return _fail("parse_failed", "could not read repo info")
    owner = data.get("owner") or {}
    dbr = data.get("defaultBranchRef") or {}
    return {"ok": True, "name": data.get("name", ""),
            "owner": owner.get("login", ""),
            "visibility": (data.get("visibility") or "").lower(),
            "url": data.get("url", ""),
            "default_branch": dbr.get("name", ""),
            "description": data.get("description") or ""}


def pr_list(path: str, limit: int = 20) -> dict:
    """Open pull requests for the repo."""
    if not _have("gh"):
        return _gh_missing()
    r = _gh(["pr", "list", "--limit", str(int(limit)), "--json",
             "number,title,state,author,headRefName"], path)
    if not r["ok"]:
        return _fail("no_github_repo", "No GitHub pull requests available.")
    import json
    try:
        rows = json.loads(r["out"])
    except Exception:  # noqa: BLE001
        return {"ok": True, "prs": []}
    prs = [{"number": x.get("number"), "title": x.get("title", ""),
            "state": (x.get("state") or "").lower(),
            "author": (x.get("author") or {}).get("login", ""),
            "branch": x.get("headRefName", "")} for x in rows]
    return {"ok": True, "prs": prs}


def issue_list(path: str, limit: int = 20) -> dict:
    """Open GitHub issues for the repo."""
    if not _have("gh"):
        return _gh_missing()
    r = _gh(["issue", "list", "--limit", str(int(limit)), "--json",
             "number,title,state,author"], path)
    if not r["ok"]:
        return _fail("no_github_repo", "No GitHub issues available.")
    import json
    try:
        rows = json.loads(r["out"])
    except Exception:  # noqa: BLE001
        return {"ok": True, "issues": []}
    issues = [{"number": x.get("number"), "title": x.get("title", ""),
               "state": (x.get("state") or "").lower(),
               "author": (x.get("author") or {}).get("login", "")} for x in rows]
    return {"ok": True, "issues": issues}
