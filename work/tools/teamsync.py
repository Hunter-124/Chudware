#!/usr/bin/env python3
"""teamsync.py — multi-contributor sync engine for the work/ task system.

Keeps parallel Claude CLI / Claude Desktop sessions on different machines
coordinated, without ever touching the contributor's working tree:

  * task assignment/progress truth lives on origin/master (work.py claim/note/
    status commit there under lock) — this tool only READS it via `git show`;
  * presence heartbeats + inter-worker messages live on a dedicated
    `work-sync` branch, maintained in a hidden worktree at .worksync/
    (per-agent files, so merges never conflict);
  * GitHub issues are cached locally via `gh` so "who is fixing what" is
    one cheap call away and fixes don't overlap.

Local state (all gitignored): work/sync/ (caches, digest, inbox markers),
.worksync/ (side-branch worktree).

Usage:
  python3 work/tools/teamsync.py sync   [--agent NAME]      # one-shot
  python3 work/tools/teamsync.py daemon [--agent NAME] [--interval 60]
  python3 work/tools/teamsync.py presence --agent NAME [--activity TEXT] [--task ID]
  python3 work/tools/teamsync.py send --agent NAME [--to NAME] --text TEXT
  python3 work/tools/teamsync.py inbox --agent NAME [--peek]
  python3 work/tools/teamsync.py team                        # who's online / holdings
  python3 work/tools/teamsync.py issues [--refresh]

Stdlib only. `gh` (GitHub CLI) is optional — issue features degrade gracefully.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORK = Path(__file__).resolve().parent.parent          # work/
REPO = WORK.parent                                     # repo root
SYNC = WORK / "sync"                                   # local cache (gitignored)
WS = REPO / ".worksync"                                # work-sync worktree (gitignored)
BRANCH = "work-sync"
PRESENCE_TTL = 600          # seconds before a heartbeat counts as offline
HEARTBEAT_MIN = 240         # min seconds between heartbeat commits

sys.path.insert(0, str(WORK / "tools"))
import work as workmod      # reuse front-matter parser


def _now():
    return datetime.now(timezone.utc)


def _ts():
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(iso):
    try:
        then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (_now() - then).total_seconds()
    except Exception:
        return 1e9


def _git(args, cwd=REPO, check=False):
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def _has_remote():
    return bool(_git(["remote"]).stdout.strip())


# ---------------------------------------------------------------- work-sync branch

def ensure_worksync():
    """Make .worksync/ a live worktree of the work-sync branch (create if absent)."""
    if not _has_remote():
        return False
    _git(["fetch", "origin", BRANCH], cwd=REPO)  # ok to fail if branch is new
    have_local = _git(["rev-parse", "--verify", BRANCH]).returncode == 0
    have_remote = _git(["rev-parse", "--verify", f"origin/{BRANCH}"]).returncode == 0
    if not WS.exists():
        if have_remote:
            _git(["branch", "-f", BRANCH, f"origin/{BRANCH}"], check=not have_local)
            _git(["worktree", "add", str(WS), BRANCH], check=True)
        elif have_local:
            _git(["worktree", "add", str(WS), BRANCH], check=True)
        else:
            # create an orphan coordination branch with an empty tree
            empty = _git(["mktree"], check=True).stdout.strip() or \
                    _git(["hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
            commit = _git(["commit-tree", empty, "-m", "work-sync: coordination branch"],
                          check=True).stdout.strip()
            _git(["branch", BRANCH, commit], check=True)
            _git(["worktree", "add", str(WS), BRANCH], check=True)
            (WS / "README.md").write_text(
                "Machine-managed coordination branch (presence heartbeats + messages).\n"
                "Written by work/tools/teamsync.py — do not edit by hand.\n",
                encoding="utf-8")
            _ws_commit_push("work-sync: init")
    _ws_pull()
    return True


def _ws_pull():
    if _git(["rev-parse", "--verify", f"origin/{BRANCH}"]).returncode == 0:
        _git(["pull", "--rebase", "origin", BRANCH], cwd=WS)


def _ws_commit_push(msg, retries=3):
    _git(["add", "-A"], cwd=WS)
    if not _git(["status", "--porcelain"], cwd=WS).stdout.strip():
        return True
    _git(["commit", "-m", msg], cwd=WS, check=True)
    if not _has_remote():
        return True
    for _ in range(retries):
        if _git(["push", "origin", f"{BRANCH}:{BRANCH}"], cwd=WS).returncode == 0:
            return True
        # divergence (another machine pushed, or unrelated orphan) — take theirs,
        # replay our per-agent files (kept in the working tree by rebase/reset).
        _git(["fetch", "origin", BRANCH], cwd=WS)
        if _git(["rebase", f"origin/{BRANCH}"], cwd=WS).returncode != 0:
            _git(["rebase", "--abort"], cwd=WS)
            mine = {p: p.read_bytes() for p in WS.rglob("*")
                    if p.is_file() and ".git" not in p.parts}
            _git(["reset", "--hard", f"origin/{BRANCH}"], cwd=WS, check=True)
            for p, data in mine.items():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            _git(["add", "-A"], cwd=WS)
            if _git(["status", "--porcelain"], cwd=WS).stdout.strip():
                _git(["commit", "-m", msg + " (replay)"], cwd=WS, check=True)
    return False


def heartbeat(agent, activity=None, task=None, force=False):
    if not ensure_worksync():
        return False
    pdir = WS / "presence"
    pdir.mkdir(exist_ok=True)
    path = pdir / f"{agent}.json"
    prev = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    rec = {
        "agent": agent,
        "host": socket.gethostname(),
        "activity": activity if activity is not None else prev.get("activity", ""),
        "task": task if task is not None else prev.get("task", ""),
        "ts": _ts(),
    }
    changed = {k: v for k, v in rec.items() if k != "ts"} != \
              {k: v for k, v in prev.items() if k != "ts"}
    stale = _age_s(prev.get("ts", "")) > HEARTBEAT_MIN
    if not (changed or stale or force):
        return True
    path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return _ws_commit_push(f"presence: {agent} @ {rec['host']}")


def send_message(agent, text, to=None):
    if not ensure_worksync():
        raise RuntimeError("no remote configured — messages need origin")
    mdir = WS / "messages"
    mdir.mkdir(exist_ok=True)
    ts = _now().strftime("%Y%m%dT%H%M%SZ")
    rec = {"from": agent, "to": to or "*", "text": text, "ts": _ts()}
    (mdir / f"{ts}-{agent}.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return _ws_commit_push(f"msg: {agent} -> {to or '*'}")


def _messages():
    mdir = WS / "messages"
    out = []
    if mdir.is_dir():
        for p in sorted(mdir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out


def read_inbox(agent, mark=True):
    ensure_worksync()
    marker = SYNC / f".inbox-{agent}"
    last = marker.read_text().strip() if marker.exists() else ""
    msgs = [m for m in _messages()
            if m.get("ts", "") > last and m.get("from") != agent
            and m.get("to") in ("*", agent)]
    if mark and msgs:
        SYNC.mkdir(exist_ok=True)
        marker.write_text(max(m["ts"] for m in msgs))
    return msgs


def presence_table():
    ensure_worksync()
    pdir = WS / "presence"
    rows = []
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.json")):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            age = _age_s(r.get("ts", ""))
            r["online"] = age < PRESENCE_TTL
            r["age_min"] = int(age // 60)
            rows.append(r)
    return rows


# ---------------------------------------------------------------- remote board

def remote_board():
    """Parse task front matter straight from origin/master (no tree changes)."""
    if not _has_remote():
        return {}
    _git(["fetch", "origin", "master"])
    ls = _git(["ls-tree", "-r", "--name-only", "origin/master",
               "work/tasks", "work/backlog"])
    tasks = {}
    for rel in ls.stdout.split():
        if not rel.endswith(".md"):
            continue
        show = _git(["show", f"origin/master:{rel}"])
        if show.returncode != 0:
            continue
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", show.stdout, re.DOTALL)
        if not m:
            continue
        fm = {}
        for line in m.group(1).splitlines():
            line = line.split("#", 1)[0].rstrip()
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
        if fm.get("id"):
            fm["_rel"] = rel
            tasks[fm["id"]] = fm
    return tasks


def local_board():
    tasks, _ = workmod.load_tasks()
    return {tid: {"id": tid, "owner": t.get("owner", ""), "status": t.get("status", ""),
                  "title": t.get("title", "")} for tid, t in tasks.items()}


# ---------------------------------------------------------------- issues (gh)

def _gh(args):
    try:
        return subprocess.run(["gh"] + args, cwd=str(REPO),
                              capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def refresh_issues():
    r = _gh(["issue", "list", "--state", "all", "--limit", "200", "--json",
             "number,title,state,assignees,labels,updatedAt,url"])
    if r is None or r.returncode != 0:
        return None
    SYNC.mkdir(exist_ok=True)
    data = {"fetched_ts": _ts(), "issues": json.loads(r.stdout or "[]")}
    (SYNC / "issues.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def cached_issues():
    p = SYNC / "issues.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- sync + daemon

def _load_state():
    p = SYNC / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state):
    SYNC.mkdir(exist_ok=True)
    (SYNC / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")


def sync_once(agent=None, quiet=False):
    """One sync cycle. Returns list of human-readable change lines."""
    changes = []
    state = _load_state()

    remote = remote_board()
    prev = state.get("board", {})
    for tid, t in sorted(remote.items()):
        old = prev.get(tid, {})
        cur = f"{t.get('owner','?')}/{t.get('status','?')}"
        was = f"{old.get('owner','?')}/{old.get('status','?')}" if old else None
        if was is None and prev:
            changes.append(f"task {tid}: NEW ({cur})  {t.get('title','')}")
        elif was is not None and was != cur:
            changes.append(f"task {tid}: {was} -> {cur}")
    state["board"] = {tid: {"owner": t.get("owner", ""), "status": t.get("status", "")}
                      for tid, t in remote.items()}

    # local working tree vs origin/master divergence (informational only)
    local = local_board()
    stale = [tid for tid, t in remote.items()
             if tid in local and (local[tid]["owner"], local[tid]["status"]) !=
             (t.get("owner", ""), t.get("status", ""))]
    if stale:
        changes.append(f"local checkout behind on {len(stale)} task(s) "
                       f"({', '.join(sorted(stale)[:6])}) — `git pull --rebase --autostash` when convenient")

    if agent:
        heartbeat(agent)
        msgs = read_inbox(agent, mark=False)
        for m in msgs[-10:]:
            changes.append(f"msg from {m['from']}: {m['text']}")

    issues = refresh_issues()
    if issues is not None:
        prev_nums = set(state.get("issue_nums", []))
        cur_nums = {i["number"] for i in issues["issues"] if i["state"] == "OPEN"}
        for n in sorted(cur_nums - prev_nums):
            i = next(x for x in issues["issues"] if x["number"] == n)
            who = ",".join(a["login"] for a in i.get("assignees", [])) or "unassigned"
            changes.append(f"issue #{n} OPEN [{who}]: {i['title']}")
        state["issue_nums"] = sorted(cur_nums)

    pres = presence_table()
    online = [f"{r['agent']}({r.get('task') or r.get('activity') or 'idle'})"
              for r in pres if r["online"]]
    digest = [f"synced {_ts()}  online: {', '.join(online) or 'nobody else'}"] + changes
    SYNC.mkdir(exist_ok=True)
    (SYNC / "digest.md").write_text("\n".join(digest) + "\n", encoding="utf-8")
    state["last_sync"] = _ts()
    _save_state(state)
    if not quiet:
        print("\n".join(digest))
    return changes


def daemon(agent=None, interval=60):
    print(f"teamsync daemon: interval={interval}s agent={agent or '-'} "
          f"(Ctrl-C to stop; digest -> work/sync/digest.md)")
    while True:
        try:
            sync_once(agent=agent)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[teamsync] cycle error: {e}", file=sys.stderr)
        try:
            time.sleep(max(15, interval))
        except KeyboardInterrupt:
            print("teamsync daemon: stopped")
            return


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync");     p.add_argument("--agent")
    p = sub.add_parser("daemon");   p.add_argument("--agent"); p.add_argument("--interval", type=int, default=60)
    p = sub.add_parser("presence"); p.add_argument("--agent", required=True)
    p.add_argument("--activity", default=None); p.add_argument("--task", default=None)
    p = sub.add_parser("send");     p.add_argument("--agent", required=True)
    p.add_argument("--to", default=None); p.add_argument("--text", required=True)
    p = sub.add_parser("inbox");    p.add_argument("--agent", required=True)
    p.add_argument("--peek", action="store_true")
    sub.add_parser("team")
    p = sub.add_parser("issues");   p.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    if a.cmd == "sync":
        sync_once(agent=a.agent)
    elif a.cmd == "daemon":
        daemon(agent=a.agent, interval=a.interval)
    elif a.cmd == "presence":
        ok = heartbeat(a.agent, a.activity, a.task, force=True)
        print("presence updated" if ok else "presence update FAILED (no remote?)")
    elif a.cmd == "send":
        send_message(a.agent, a.text, to=a.to)
        print("sent")
    elif a.cmd == "inbox":
        msgs = read_inbox(a.agent, mark=not a.peek)
        if not msgs:
            print("inbox empty")
        for m in msgs:
            print(f"[{m['ts']}] {m['from']} -> {m['to']}: {m['text']}")
    elif a.cmd == "team":
        rows = presence_table()
        if not rows:
            print("no presence data (is the daemon running anywhere?)")
        for r in rows:
            mark = "ONLINE " if r["online"] else f"seen {r['age_min']}m ago"
            print(f"{r['agent']:>14} @{r.get('host','?'):<12} {mark:>14}  "
                  f"task={r.get('task') or '-'}  {r.get('activity','')}")
        data = cached_issues()
        if data:
            open_ = [i for i in data["issues"] if i["state"] == "OPEN"]
            print(f"\nopen issues: {len(open_)} (cached {data['fetched_ts']})")
            for i in open_[:15]:
                who = ",".join(x["login"] for x in i.get("assignees", [])) or "unassigned"
                print(f"  #{i['number']:>4} [{who}] {i['title']}")
    elif a.cmd == "issues":
        data = refresh_issues() if a.refresh else (cached_issues() or refresh_issues())
        if data is None:
            print("gh unavailable — install GitHub CLI and `gh auth login`")
            return
        for i in data["issues"]:
            if i["state"] != "OPEN":
                continue
            who = ",".join(x["login"] for x in i.get("assignees", [])) or "unassigned"
            labels = ",".join(l["name"] for l in i.get("labels", []))
            print(f"#{i['number']:>4} [{who}] ({labels}) {i['title']}")


if __name__ == "__main__":
    main()
