"""Tests for the real-git backend (services/plm/gitscm.py).

Local-git paths run against a throwaway repo. GitHub (gh) paths are skipped when
gh is absent or unauthenticated, so CI stays green without network/credentials.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import threading
import time

import pytest

from services.plm import gitscm

HAVE_GIT = shutil.which("git") is not None
HAVE_GH = shutil.which("gh") is not None

pytestmark = pytest.mark.skipif(not HAVE_GIT, reason="git not installed")


def _init_repo(path: str) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path,
                   check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    _init_repo(str(d))
    return str(d)


def test_is_repo_detects(tmp_path, repo):
    assert gitscm.is_repo(repo)["is_repo"] is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitscm.is_repo(str(plain))["is_repo"] is False


def test_init_on_plain_folder(tmp_path):
    d = tmp_path / "fresh"
    d.mkdir()
    assert gitscm.init(str(d))["ok"] is True
    assert gitscm.is_repo(str(d))["is_repo"] is True


def test_status_untracked_then_staged(repo):
    (__import__("pathlib").Path(repo) / "a.txt").write_text("hello\n")
    st = gitscm.status(repo)
    assert st["ok"] and not st["clean"]
    assert any(f["path"] == "a.txt" and f["untracked"] for f in st["files"])

    assert gitscm.stage(repo, ["a.txt"])["ok"]
    st2 = gitscm.status(repo)
    assert any(f["path"] == "a.txt" and f["staged"] for f in st2["files"])


def test_commit_and_log(repo):
    p = __import__("pathlib").Path(repo)
    (p / "a.txt").write_text("hello\n")
    gitscm.stage(repo, ["a.txt"])
    c = gitscm.commit(repo, "first commit")
    assert c["ok"], c
    lg = gitscm.log(repo, limit=5)
    assert lg["ok"] and len(lg["commits"]) == 1
    assert lg["commits"][0]["subject"] == "first commit"


def test_commit_empty_message_rejected(repo):
    r = gitscm.commit(repo, "   ")
    assert not r["ok"] and r["reason"] == "empty_message"


def test_log_empty_repo_is_ok(repo):
    lg = gitscm.log(repo)
    assert lg["ok"] and lg["commits"] == []


def test_branches_and_create(repo):
    p = __import__("pathlib").Path(repo)
    (p / "a.txt").write_text("x\n")
    gitscm.stage(repo)
    gitscm.commit(repo, "init")
    assert gitscm.create_branch(repo, "feature/x")["ok"]
    br = gitscm.branches(repo)
    names = {b["name"] for b in br["branches"]}
    assert "feature/x" in names
    assert any(b["current"] and b["name"] == "feature/x" for b in br["branches"])


def test_tree_and_file_at(repo):
    p = __import__("pathlib").Path(repo)
    (p / "a.txt").write_text("content-here\n")
    gitscm.stage(repo)
    gitscm.commit(repo, "init")
    t = gitscm.tree(repo)
    assert "a.txt" in t["files"]
    f = gitscm.file_at(repo, "a.txt")
    assert f["ok"] and "content-here" in f["content"]


def test_diff_staged(repo):
    p = __import__("pathlib").Path(repo)
    (p / "a.txt").write_text("one\n")
    gitscm.stage(repo)
    gitscm.commit(repo, "init")
    (p / "a.txt").write_text("one\ntwo\n")
    gitscm.stage(repo)
    d = gitscm.diff(repo, "a.txt", staged=True)
    assert d["ok"] and "+two" in d["diff"]


@pytest.mark.skipif(not HAVE_GH, reason="gh not installed")
def test_gh_auth_status_shape():
    r = gitscm.gh_auth_status(".")
    assert r["ok"] and "authed" in r and "user" in r


class _FakeLoginProcess:
    def __init__(self, stdout, returncode=None):
        self.stdout = stdout
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def test_gh_auth_login_deadline_is_not_blocked_by_readline(monkeypatch, tmp_path):
    blocked = threading.Event()

    class _BlockingStdout:
        def readline(self):
            blocked.wait()
            return ""

    proc = _FakeLoginProcess(_BlockingStdout())
    monkeypatch.setattr(gitscm, "_have", lambda binary: True)
    monkeypatch.setattr(gitscm, "_GH_AUTH_START_TIMEOUT_S", 0.03)
    monkeypatch.setattr(gitscm.subprocess, "Popen", lambda *a, **kw: proc)

    started = time.monotonic()
    out = gitscm.gh_auth_login(str(tmp_path))
    elapsed = time.monotonic() - started

    assert out["ok"] is True and out["started"] is True
    assert elapsed < 0.2
    assert gitscm.gh_auth_login_status()["running"] is True
    cancelled = gitscm.gh_auth_login_status(cancel=True)
    assert cancelled["cancelled"] is True and proc.terminated is True
    blocked.set()


def test_gh_auth_login_immediate_nonzero_exit_is_failure(monkeypatch, tmp_path):
    proc = _FakeLoginProcess(io.StringIO("fatal: browser unavailable\n"), returncode=9)
    monkeypatch.setattr(gitscm, "_have", lambda binary: True)
    monkeypatch.setattr(gitscm, "_GH_AUTH_START_TIMEOUT_S", 0.1)
    monkeypatch.setattr(gitscm.subprocess, "Popen", lambda *a, **kw: proc)

    out = gitscm.gh_auth_login(str(tmp_path))

    assert out["ok"] is False
    assert out["reason"] == "login_failed"
    assert "browser unavailable" in out["hint"]


@pytest.mark.parametrize(("call", "label"), [
    (gitscm.pr_list, "pull requests"),
    (gitscm.issue_list, "issues"),
])
@pytest.mark.parametrize("payload", ["not json", '{"number": 1}'])
def test_gh_lists_reject_malformed_json(monkeypatch, call, label, payload):
    monkeypatch.setattr(gitscm, "_have", lambda binary: True)
    monkeypatch.setattr(
        gitscm, "_gh",
        lambda *a, **kw: {"ok": True, "code": 0, "out": payload, "err": ""},
    )

    out = call(".")

    assert out == {
        "ok": False,
        "reason": "parse_failed",
        "hint": f"could not read GitHub {label}",
    }
