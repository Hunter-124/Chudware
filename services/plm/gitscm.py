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
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from services.plm.git_security import git_argv, git_env, valid_name, validate_location

# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

_TIMEOUT = 60  # seconds — network ops (push/pull/gh) can be slow but not forever
_GH_AUTH_START_TIMEOUT_S = 8.0
_gh_auth_lock = threading.Lock()
_gh_auth_job: dict[str, Any] | None = None


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_gh_auth_lock):
        job = _gh_auth_job
        proc = job["process"] if job is not None else None
        running = proc is not None and proc.poll() is None
        reader = job is not None and not job["reader_done"]
    return {"by_kind": {"github_login": int(running or reader)},
            "processes": [proc] if running else []}


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
        env = git_env() if binary == "git" else dict(os.environ)
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout, "err": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": 124, "out": "", "err": "timed out"}
    except Exception as e:  # noqa: BLE001 — never raise across the seam
        return {"ok": False, "code": 1, "out": "", "err": f"{type(e).__name__}: {e}"}


def _git(args: list[str], cwd: str, **kw) -> dict:
    return _run(git_argv(args), cwd, **kw)


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


def _revision(path: str, ref: str) -> dict:
    if (not isinstance(ref, str) or ref.startswith("-")
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*(?:[~^][0-9]*)*", ref)):
        return _fail("invalid_ref", "Use a commit id or a branch/tag revision, not an option.")
    return _git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], path)


def _network_remote(path: str, remote: str = "") -> dict | None:
    if remote and not valid_name(remote):
        return _fail("invalid_remote", "Use a configured remote name, not an option or URL.")
    configured = _git(["config", "--get-regexp", r"^remote\..*\.(url|pushurl|uploadpack|receivepack)$"], path)
    if configured["code"] not in (0, 1):
        return _fail("invalid_remote", "Could not inspect remote configuration.")
    names = set()
    for line in configured["out"].splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key[len("remote."):].rpartition(".")
        if remote and name != remote:
            continue
        names.add(name)
        if field in ("uploadpack", "receivepack"):
            return _fail("invalid_remote", "Custom remote executables are not allowed.")
        try:
            validate_location(value)
        except ValueError as exc:
            return _fail("invalid_remote", str(exc))
    if remote and remote not in names:
        return _fail("invalid_remote", "Use a configured remote name.")
    # URL rewrite rules can silently select a helper transport or destination.
    rewrites = _git(["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"], path)
    if rewrites["code"] != 1:
        return _fail("invalid_remote", "URL rewrite rules are not allowed for PLM Git sync.")
    return None


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
        revision = _revision(path, ref)
        if not revision["ok"]:
            return _fail("invalid_ref", revision.get("hint") or revision["err"])
        args += ["--end-of-options", revision["out"].strip()]
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
    revision = _revision(path, ref or "HEAD")
    if not revision["ok"]:
        if revision.get("reason") == "invalid_ref":
            return revision
        return {"ok": True, "files": []}
    r = _git(["ls-tree", "-r", "--name-only", revision["out"].strip()], path)
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
    revision = _revision(path, ref or "HEAD")
    if not revision["ok"]:
        return _fail("invalid_ref", revision.get("hint") or revision["err"])
    r = _git(["show", "--end-of-options", f"{revision['out'].strip()}:{file}"], path)
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
    args = ["diff", "--no-ext-diff", "--no-textconv"]
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
    if not valid_name(name):
        return _fail("invalid", "a branch needs a valid non-option name")
    args = ["checkout", "-b", name] if checkout else ["branch", name]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("branch_failed", r["err"] or "could not create branch")
    return {"ok": True}


def checkout_branch(path: str, name: str) -> dict:
    if not _have("git"):
        return _git_missing()
    if not valid_name(name):
        return _fail("invalid", "a branch needs a valid non-option name")
    r = _git(["checkout", name, "--"], path)
    if not r["ok"]:
        return _fail("checkout_failed", r["err"] or "could not switch branch")
    return {"ok": True}


def fetch(path: str, remote: str = "") -> dict:
    if not _have("git"):
        return _git_missing()
    refusal = _network_remote(path, remote)
    if refusal:
        return refusal
    args = ["fetch", "--prune", "--", remote] if remote else ["fetch", "--prune", "--all"]
    r = _git(args, path)
    if not r["ok"]:
        return _fail("fetch_failed", r["err"] or "could not fetch")
    return {"ok": True, "output": (r["out"] + r["err"]).strip()}


def pull(path: str) -> dict:
    if not _have("git"):
        return _git_missing()
    refusal = _network_remote(path)
    if refusal:
        return refusal
    r = _git(["pull", "--ff-only"], path)
    if not r["ok"]:
        return _fail("pull_failed", r["err"] or "could not pull")
    return {"ok": True, "output": (r["out"] + r["err"]).strip()}


def push(path: str, set_upstream: bool = False) -> dict:
    if not _have("git"):
        return _git_missing()
    refusal = _network_remote(path)
    if refusal:
        return refusal
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


def _read_gh_auth_output(proc: subprocess.Popen, job: dict[str, Any]) -> None:
    """Drain login output without ever blocking the caller's deadline."""
    stream = proc.stdout
    if stream is None:
        with _gh_auth_lock:
            job["reader_done"] = True
            job["changed"].set()
        return
    try:
        for line in iter(stream.readline, ""):
            code_match = re.search(r"one-time code:\s*([A-Z0-9-]+)", line)
            url_match = re.search(r"(https?://\S+)", line)
            with _gh_auth_lock:
                job["output"].append(line.rstrip())
                if len(job["output"]) > 32:
                    del job["output"][:-32]
                if code_match:
                    job["code"] = code_match.group(1)
                if url_match and "device" in line.lower():
                    job["url"] = url_match.group(1)
                    job["flow_seen"] = True
                job["changed"].set()
    except Exception as e:  # noqa: BLE001 — surfaced through the status path
        with _gh_auth_lock:
            job["reader_error"] = f"{type(e).__name__}: {e}"
    finally:
        with _gh_auth_lock:
            job["reader_done"] = True
            job["changed"].set()


def _gh_auth_snapshot(job: dict[str, Any], *, cancel: bool = False) -> dict:
    proc = job["process"]
    if cancel and proc.poll() is None:
        try:
            proc.terminate()
            job["cancelled"] = True
        except Exception as e:  # noqa: BLE001 — report cancellation failure
            return _fail("cancel_failed", f"{type(e).__name__}: {e}")
    exit_code = proc.poll()
    with _gh_auth_lock:
        code = job["code"]
        url = job["url"]
        usable = bool(code or job["flow_seen"])
        output = "\n".join(job["output"]).strip()
        cancelled = bool(job["cancelled"])
        reader_error = job["reader_error"]
    running = exit_code is None
    common = {"started": running, "running": running, "completed": not running,
              "cancelled": cancelled, "exit_code": exit_code,
              "code": code, "url": url}
    if cancelled:
        return {**_fail("login_cancelled", "GitHub sign-in was cancelled."),
                **common}
    if not running and not usable:
        hint = output or reader_error or f"gh auth login exited with status {exit_code}"
        return {**_fail("login_failed", hint), **common}
    return {"ok": True, **common,
            "hint": "Enter the code in your browser to finish signing in."}


def gh_auth_login_status(*, cancel: bool = False) -> dict:
    """Poll, or cancel, the most recently started ``gh auth login`` process."""
    with _gh_auth_lock:
        job = _gh_auth_job
    if job is None:
        return _fail("no_login", "No GitHub sign-in process has been started.")
    return _gh_auth_snapshot(job, cancel=cancel)


def gh_auth_login(path: str = ".", hostname: str = "") -> dict:
    """Start web OAuth and wait only briefly for its device-code output."""
    global _gh_auth_job
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

    job: dict[str, Any] = {
        "process": proc,
        "code": "",
        "url": "https://github.com/login/device",
        "flow_seen": False,
        "output": [],
        "reader_error": "",
        "reader_done": False,
        "cancelled": False,
        "changed": threading.Event(),
    }
    with _gh_auth_lock:
        _gh_auth_job = job
    from services.worker_activity import start_background_thread

    start_background_thread(_read_gh_auth_output, args=(proc, job), name="gh-auth-login-output")

    deadline = time.monotonic() + _GH_AUTH_START_TIMEOUT_S
    while True:
        snapshot = _gh_auth_snapshot(job)
        with _gh_auth_lock:
            usable = bool(job["code"] or job["flow_seen"])
            reader_done = bool(job["reader_done"])
        if usable or (not snapshot["running"] and reader_done):
            return snapshot
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return snapshot
        job["changed"].wait(min(remaining, 0.05))
        job["changed"].clear()


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
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            raise ValueError("expected a JSON list of objects")
        prs = []
        for x in rows:
            author = x.get("author") or {}
            if not isinstance(author, dict):
                raise ValueError("expected author to be an object")
            prs.append({"number": x.get("number"), "title": x.get("title", ""),
                        "state": (x.get("state") or "").lower(),
                        "author": author.get("login", ""),
                        "branch": x.get("headRefName", "")})
    except Exception:  # noqa: BLE001
        return _fail("parse_failed", "could not read GitHub pull requests")
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
        if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
            raise ValueError("expected a JSON list of objects")
        issues = []
        for x in rows:
            author = x.get("author") or {}
            if not isinstance(author, dict):
                raise ValueError("expected author to be an object")
            issues.append({"number": x.get("number"), "title": x.get("title", ""),
                           "state": (x.get("state") or "").lower(),
                           "author": author.get("login", "")})
    except Exception:  # noqa: BLE001
        return _fail("parse_failed", "could not read GitHub issues")
    return {"ok": True, "issues": issues}
