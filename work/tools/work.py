#!/usr/bin/env python3
"""work.py — git-tracked task coordination for Chudware.

Tasks are markdown files in work/tasks/ and work/backlog/ with flat YAML-ish
front matter (key: value, inline lists like [T-A1, T-B2]). Stdlib only.

Usage:
  python3 work/tools/work.py validate
  python3 work/tools/work.py ready [--owner NAME]
  python3 work/tools/work.py board [--workstream X] [--owner Y] [--status Z] [--json]
  python3 work/tools/work.py dag        # regenerates work/DAG.md
  python3 work/tools/work.py relink ID --owner NAME [--depends-on A,B] [--workstream X]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows consoles default to a legacy code page (e.g. cp1252) that cannot encode
# characters like U+2192 (→) that appear in task titles, which crashed `board`
# with UnicodeEncodeError. Force UTF-8 output so the CLI is robust on any console;
# this is a no-op where stdout is already UTF-8. stdout may be a plain pipe with no
# reconfigure(), so guard it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:                       # identity/profile helpers live beside this script
    import _teamlib
except Exception:          # never let a helper import break the core CLI
    _teamlib = None

ROOT = Path(__file__).resolve().parent.parent  # work/


def _identity_owner():
    """The stored contributor identity (GitHub username / agent name), or None."""
    if _teamlib is None:
        return None
    try:
        return _teamlib.owner_name()
    except Exception:
        return None


def _resolve_owner(args):
    """Owner from --owner, else the stored identity. Errors clearly if neither."""
    owner = getattr(args, "owner", None) or _identity_owner()
    if not owner:
        raise RuntimeError("no --owner given and no identity set — run "
                           "`work.py whoami --github <username>` once, or pass --owner")
    return owner


# "dropped" = cancelled outright and never coming back. Distinct from "backlog"
# (shelved, still wanted): a dropped card must not read as future work.
STATUSES = {"todo", "doing", "review", "done", "blocked", "backlog", "dropped"}
REQUIRED = ("id", "title", "workstream", "owner", "status", "branch", "depends_on")

# Workflow v2 (2026-07-03, Hunter directive — see work/WORKFLOW.md):
# lane   = user-facing feature area (dashboard swim-rows; "what are we close to shipping")
# kind   = fix (issue with an existing feature) | overhaul (redesign of existing) | new (unbuilt)
# deferred: yes = parked plan (ML training, SaaS, ...) — hidden from ready/claim by default
LANES = {
    "modeling-core", "assemblies-drawings", "cam", "ecad-firmware", "simulation",
    "parts-sourcing", "ai-copilot", "app-gui", "plm-projects", "platform-infra",
    "ml-flywheel",
}
KINDS = {"fix", "overhaul", "new"}
KIND_RANK = {"fix": 0, "overhaul": 1, "new": 2}  # fix-first tiebreak inside a priority

# --- Lane ownership / branch model (2026-07-06, Hunter directive) -----------
# Each lane is owned by a human contributor and worked on its own branch, so
# agents driven toward that lane's scope land on lane/<lane> and never touch
# master until a human reviews & merges. Uncategorised / untagged bugfixes are
# grouped onto ONE shared branch (lane/misc-fixes) to reduce cross-branch
# divergence. Authored here (git-tracked work/lanes.json) — NOT in work/sync/,
# which teamsync regenerates from remote and would clobber.
LANES_FILE = ROOT / "lanes.json"
UNTAGGED = "(untagged)"
ASSIGNABLE_LANES = LANES | {UNTAGGED}
MISC_BRANCH = "lane/misc-fixes"
BRANCH_RE = re.compile(r"^lane/[a-z0-9._-]+$")


def _default_branch(lane):
    return MISC_BRANCH if lane == UNTAGGED else f"lane/{lane}"


def _load_lane_board():
    """The stored lane->{owner,branch,goal} map, or {} if unset/corrupt."""
    if not LANES_FILE.exists():
        return {}
    try:
        data = json.loads(LANES_FILE.read_text(encoding="utf-8"))
        return data.get("lanes", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _lane_board_view():
    """Every assignable lane with its owner/branch/goal (defaults filled in)."""
    stored = _load_lane_board()
    out = {}
    for lane in sorted(ASSIGNABLE_LANES):
        rec = stored.get(lane, {})
        out[lane] = {
            "lane": lane,
            "owner": rec.get("owner") or "unassigned",
            "branch": rec.get("branch") or _default_branch(lane),
            "goal": rec.get("goal") or "",
        }
    return out


def _save_lane_board(lanes):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    LANES_FILE.write_text(
        json.dumps({"version": 1, "updated": stamp, "lanes": lanes}, indent=1) + "\n",
        encoding="utf-8",
    )


def _commit_lane_board(msg):
    """Commit + push work/lanes.json to master under the claim lock (same path
    discipline as _commit_task_change: add exact file, author as identity, push)."""
    main = _main_worktree()
    rel = LANES_FILE.resolve().relative_to(main.resolve())
    _git(["add", str(rel)], main, check=True)
    _git(["commit", "-m", msg], main, check=True)
    if _git(["remote"], main).stdout.strip():
        if _git(["push"], main).returncode != 0:
            _git(["pull", "--rebase"], main)
            _git(["push"], main, check=True)


def _is_deferred(t):
    return str(t.get("deferred", "no")).lower() in ("yes", "true", "1")


def parse_front_matter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None, f"{path.name}: missing front matter"
    fm = {}
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            return None, f"{path.name}: bad front-matter line: {line!r}"
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            fm[key] = [v.strip() for v in inner.split(",") if v.strip()]
        else:
            fm[key] = val
    fm["_path"] = path
    return fm, None


def load_tasks():
    """Load every task, including archived ones.

    `tasks/` and `backlog/` hold the LIVE board. Completed tasks are moved into a `done/`
    subdirectory of either so the board directories stay readable — 238 of 251 task files
    were `status: done`, which buried the 11 that still needed attention.

    Archived tasks are still loaded, and that is load-bearing rather than tidiness: live
    tasks depend on completed ones (T-BL52 -> T-BL21, T-W11 -> T-W2/W5/W7/W8,
    T-X5 -> T-X1), and the readiness test below reads `tasks.get(dep, {}).get("status")`,
    so an unresolvable dependency is indistinguishable from an unfinished one and would
    silently drop the dependent task out of `ready`. Lane ship-readiness counts done work
    too. Archiving therefore changes directory layout only, never command output.
    """
    tasks, errors = {}, []
    for sub in ("tasks", "backlog", "tasks/done", "backlog/done"):
        d = ROOT / sub
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            fm, err = parse_front_matter(path)
            if err:
                errors.append(err)
                continue
            tid = fm.get("id", "")
            if tid in tasks:
                errors.append(f"{path.name}: duplicate id {tid}")
            else:
                tasks[tid] = fm
    return tasks, errors


def validate(tasks, errors):
    for tid, t in tasks.items():
        name = t["_path"].name
        for key in REQUIRED:
            if key not in t:
                errors.append(f"{name}: missing key '{key}'")
        if t.get("status") not in STATUSES:
            errors.append(f"{name}: bad status {t.get('status')!r}")
        branch = t.get("branch", "")
        if not re.fullmatch(rf"task/{re.escape(tid)}-[A-Za-z0-9-]+", branch):
            errors.append(f"{name}: branch {branch!r} != task/{tid}-<slug>")
        if "lane" in t and t["lane"] not in LANES:
            errors.append(f"{name}: bad lane {t['lane']!r} (allowed: {sorted(LANES)})")
        if "kind" in t and t["kind"] not in KINDS:
            errors.append(f"{name}: bad kind {t['kind']!r} (allowed: {sorted(KINDS)})")
        if "deferred" in t and str(t["deferred"]).lower() not in ("yes", "no", "true", "false"):
            errors.append(f"{name}: deferred must be yes|no, got {t['deferred']!r}")
        if "priority" in t:
            try:
                p = int(t["priority"])
                if not 1 <= p <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{name}: priority must be an int 1..5, got {t['priority']!r}")
        for dep in t.get("depends_on", []):
            if dep not in tasks:
                errors.append(f"{name}: unknown dependency {dep}")
    # cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in tasks}

    def visit(tid, stack):
        color[tid] = GRAY
        for dep in tasks[tid].get("depends_on", []):
            if dep not in tasks:
                continue
            if color[dep] == GRAY:
                errors.append("dependency cycle: " + " -> ".join(stack + [dep]))
            elif color[dep] == WHITE:
                visit(dep, stack + [dep])
        color[tid] = BLACK

    for tid in tasks:
        if color[tid] == WHITE:
            visit(tid, [tid])
    return errors


def cmd_validate(tasks, errors, _args):
    errors = validate(tasks, errors)
    if errors:
        for e in errors:
            print("ERROR:", e)
        return 1
    print(f"OK: {len(tasks)} tasks valid")
    return 0


def cmd_ready(tasks, errors, args):
    if errors:
        print("(warning: parse errors — run validate)", file=sys.stderr)
    rows = []
    for tid, t in sorted(tasks.items()):
        if t.get("status") != "todo":
            continue
        if _is_deferred(t) and not getattr(args, "include_deferred", False):
            continue
        if args.owner and t.get("owner", "").lower() != args.owner.lower():
            continue
        deps = t.get("depends_on", [])
        if all(tasks.get(d, {}).get("status") == "done" for d in deps):
            rows.append(t)
    if not rows:
        print("no ready tasks")
        return 0
    rows.sort(key=lambda t: (int(t.get("priority", 5) or 5), str(t.get("day", "99")),
                             KIND_RANK.get(t.get("kind"), 2), t["id"]))
    for t in rows:
        tags = f"{t.get('lane','-'):<18} {t.get('kind','-'):<8}"
        print(f"{t['id']:>8}  P{t.get('priority', 5)}  {tags}  [{t.get('owner','?')}]  {t['title']}")
    return 0


def cmd_board(tasks, errors, args):
    rows = list(tasks.values())
    if getattr(args, "workstream", None):
        rows = [t for t in rows if t.get("workstream", "").lower() == args.workstream.lower()]
    if getattr(args, "owner", None):
        rows = [t for t in rows if t.get("owner", "").lower() == args.owner.lower()]
    if getattr(args, "status", None):
        rows = [t for t in rows if t.get("status", "").lower() == args.status.lower()]
    if getattr(args, "lane", None):
        rows = [t for t in rows if t.get("lane", "").lower() == args.lane.lower()]
    if getattr(args, "kind", None):
        rows = [t for t in rows if t.get("kind", "").lower() == args.kind.lower()]

    if getattr(args, "json", False):
        out = [{k: v for k, v in t.items() if not k.startswith("_")}
               for t in sorted(rows, key=lambda x: x["id"])]
        print(json.dumps(out))
        return 0

    by_ws = {}
    for t in rows:
        by_ws.setdefault(t.get("workstream", "?"), []).append(t)
    for ws in sorted(by_ws):
        print(f"\n== workstream {ws} ==")
        for t in sorted(by_ws[ws], key=lambda x: (str(x.get("day", "99")), x["id"])):
            print(f"  {t['id']:>8}  {t.get('status','?'):>8}  day {t.get('day','-'):>2}"
                  f"  [{t.get('owner','?'):>10}]  {t['title']}")
    return 0


def mermaid_id(tid):
    return tid.replace("-", "_")


def cmd_dag(tasks, errors, _args):
    lines = [
        "# Task DAG",
        "",
        "Generated by `python3 work/tools/work.py dag` — do not edit by hand.",
        "",
        "```mermaid",
        "graph TD",
        "  classDef todo fill:#e8e8e8,stroke:#999,color:#333",
        "  classDef doing fill:#cfe3ff,stroke:#4a90d9,color:#1a3c6e",
        "  classDef review fill:#ffe9b8,stroke:#d9a441,color:#6e4f1a",
        "  classDef done fill:#cdeec9,stroke:#57a05a,color:#1e4d20",
        "  classDef blocked fill:#f5c6c6,stroke:#c0504d,color:#5e1f1d",
        "  classDef backlog fill:#f4f0fa,stroke:#b0a3c9,color:#4a3d63,stroke-dasharray: 4 3",
    ]
    by_ws = {}
    for t in tasks.values():
        by_ws.setdefault(t.get("workstream", "?"), []).append(t)
    for ws in sorted(by_ws):
        lines.append(f'  subgraph WS_{ws}["workstream {ws}"]')
        for t in sorted(by_ws[ws], key=lambda x: x["id"]):
            title = t["title"].replace('"', "'")
            lines.append(f'    {mermaid_id(t["id"])}["{t["id"]}: {title}"]:::{t.get("status","todo")}')
        lines.append("  end")
    for tid, t in sorted(tasks.items()):
        for dep in t.get("depends_on", []):
            if dep in tasks:
                lines.append(f"  {mermaid_id(dep)} --> {mermaid_id(tid)}")
    lines += ["```", ""]
    out = ROOT / "DAG.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({len(tasks)} tasks)")
    return 0


def _git(args, cwd, check=False):
    # Author board commits AS the identified contributor via per-subprocess env
    # (GIT_AUTHOR_*/GIT_COMMITTER_*). We NEVER run `git config user.*`; if no
    # identity email is known, env is None and the machine identity is used.
    env = None
    if args and args[0] == "commit" and _teamlib is not None:
        try:
            env = _teamlib.commit_env()
        except Exception:
            env = None
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                       text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def _safe_sync(main):
    """Sync the main checkout WITHOUT ever rebasing the foreman's unpushed merges.

    pull --rebase here twice stranded the checkout mid-replay and once clobbered
    refs/heads/master (2026-07-03 incidents). Policy: ahead-only -> nothing to do;
    behind-only -> fast-forward; diverged -> caller must bail (return False).
    """
    _git(["fetch", "--quiet"], main)
    r = _git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], main)
    counts = r.stdout.split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    if ahead and behind:
        return False
    if behind:
        _git(["merge", "--ff-only", "@{upstream}"], main)
    return True


def _main_worktree():
    r = _git(["rev-parse", "--git-common-dir"], ROOT.parent, check=True)
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = (ROOT.parent / common).resolve()
    return common.parent


def _lock():
    """Cross-platform advisory lock via O_EXCL lockfile. Stale after 120s."""
    path = ROOT / ".claim.lock"
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            if path.exists() and time.time() - path.stat().st_mtime > 120:
                path.unlink(missing_ok=True)
                continue
            if time.time() > deadline:
                raise RuntimeError("claim lock held too long; remove work/.claim.lock if stale")
            time.sleep(1)


def _set_front_matter(path, **updates):
    text = path.read_text(encoding="utf-8")
    for key, val in updates.items():
        text, n = re.subn(rf"(?m)^{key}:.*$", f"{key}: {val}", text, count=1)
        if n != 1:
            raise RuntimeError(f"{path.name}: could not set {key}")
    path.write_text(text, encoding="utf-8")


def _upsert_front_matter(path, **updates):
    """Like _set_front_matter but inserts missing keys before the closing ---."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"{path.name}: missing front matter")
    for key, val in updates.items():
        fm_block = m.group(1)
        if re.search(rf"(?m)^{key}:", fm_block):
            text = text[:m.start(1)] + re.sub(rf"(?m)^{key}:.*$", f"{key}: {val}",
                                              fm_block, count=1) + text[m.end(1):]
        else:
            text = text[:m.end(1)] + f"\n{key}: {val}" + text[m.end(1):]
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    path.write_text(text, encoding="utf-8")


def cmd_claim(tasks, errors, args):
    main = _main_worktree()
    if main.resolve() != ROOT.parent.resolve():
        print(f"ERROR: run claim from the main checkout ({main}), not a worktree", file=sys.stderr)
        return 1
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], main).stdout.strip()
    if branch != "master":
        print(f"ERROR: main checkout is on {branch!r}; claims must land on master", file=sys.stderr)
        return 1
    lock = _lock()
    try:
        # Refuse to touch the main checkout while a merge/rebase is in progress or
        # the tree has uncommitted tracked changes — this pull --rebase once began
        # rebasing a foreman merge session mid-resolution (2026-07-03 near-miss;
        # same class as the opus-dawn 211-commit incident).
        gitdir = main / ".git"
        busy = any((gitdir / m).exists() for m in ("MERGE_HEAD", "rebase-merge", "rebase-apply"))
        dirty = bool(_git(["status", "--porcelain", "--untracked-files=no"], main).stdout.strip())
        if busy or dirty:
            print("BUSY: main checkout has a merge/rebase in progress or uncommitted "
                  "changes (likely a foreman merge session) — retry claim in ~60s",
                  file=sys.stderr)
            return 3
        has_remote = bool(_git(["remote"], main).stdout.strip())
        if has_remote:
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, errors = load_tasks()  # reload under lock, post-pull
        unclaimed = lambda t: t.get("owner", "unassigned") in ("unassigned", "", "none")
        if args.task:
            cands = [tasks[args.task]] if args.task in tasks else []
            if cands and (tasks[args.task]["status"] != "todo" or not unclaimed(cands[0])):
                print(f"NOTHING-TO-CLAIM: {args.task} is not an unclaimed todo", file=sys.stderr)
                return 2
        else:
            cands = [t for t in tasks.values()
                     if t["status"] == "todo" and unclaimed(t) and not _is_deferred(t)
                     and all(tasks.get(d, {}).get("status") == "done"
                             for d in t.get("depends_on", []))]
            # Priority-first auto-claim (Erik, 2026-07-03): lower `priority:` wins
            # (1 = drop-everything, default 5), then day, then fix-before-new, then id.
            cands.sort(key=lambda t: (int(t.get("priority", 5) or 5),
                                      str(t.get("day", "99")),
                                      KIND_RANK.get(t.get("kind"), 2), t["id"]))
        if not cands:
            print("NOTHING-TO-CLAIM: no unclaimed ready tasks (see `board` for in-flight work)")
            return 2
        t = cands[0]
        _set_front_matter(t["_path"], owner=args.owner, status="doing")
        rel = t["_path"].relative_to(main)
        _git(["add", str(rel)], main, check=True)
        _git(["commit", "-m", f"claim {t['id']} ({args.owner})"], main, check=True)
        if has_remote:
            if _git(["push"], main).returncode != 0:
                _git(["pull", "--rebase"], main)
                fm, _ = parse_front_matter(t["_path"])
                if fm.get("owner") != args.owner:
                    print(f"LOST-RACE: {t['id']} claimed by {fm.get('owner')}; rerun claim", file=sys.stderr)
                    return 2
                _git(["push"], main)
        print(f"branch: {t['branch']}\ntitle: {t['title']}\nCLAIMED: {t['id']}")
        return 0
    finally:
        lock.unlink(missing_ok=True)


def _commit_task_change(t, msg):
    """Commit + push a single task-file change to master under the claim lock."""
    main = _main_worktree()
    rel = t["_path"].resolve().relative_to(main.resolve())
    _git(["add", str(rel)], main, check=True)
    _git(["commit", "-m", msg], main, check=True)
    if _git(["remote"], main).stdout.strip():
        if _git(["push"], main).returncode != 0:
            _git(["pull", "--rebase"], main)
            _git(["push"], main, check=True)


def _owned_task(tasks, tid, owner):
    if tid not in tasks:
        raise RuntimeError(f"unknown task {tid}")
    t = tasks[tid]
    if t.get("owner", "").lower() != owner.lower():
        raise RuntimeError(f"{tid} is owned by {t.get('owner')!r}, not {owner!r}")
    return t


def cmd_note(tasks, errors, args):
    """Append a timestamped progress note to your claimed task + push to master."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        t = _owned_task(tasks, args.task_id, args.owner)
        stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        text = t["_path"].read_text(encoding="utf-8")
        if "## Progress log" not in text:
            text = text.rstrip() + "\n\n## Progress log\n"
        text = text.rstrip() + f"\n- {stamp} [{args.owner}] {args.text}\n"
        t["_path"].write_text(text, encoding="utf-8")
        _commit_task_change(t, f"note {args.task_id} ({args.owner})")
        print(f"NOTED: {args.task_id}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


ALLOWED_TRANSITIONS = {
    ("doing", "review"), ("doing", "blocked"),
    ("blocked", "doing"), ("review", "doing"),
}


def cmd_status(tasks, errors, args):
    """Set your claimed task's status (guarded transitions; never -> done)."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        t = _owned_task(tasks, args.task_id, args.owner)
        cur, new = t.get("status"), args.status
        if new == "done":
            raise RuntimeError("flipping to done is a HUMAN merge-time action")
        if (cur, new) not in ALLOWED_TRANSITIONS:
            raise RuntimeError(f"transition {cur} -> {new} not allowed "
                               f"(allowed: {sorted(ALLOWED_TRANSITIONS)})")
        _set_front_matter(t["_path"], status=new)
        _commit_task_change(t, f"status {args.task_id}: {cur} -> {new} ({args.owner})")
        print(f"STATUS: {args.task_id} {cur} -> {new}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def cmd_release(tasks, errors, args):
    """Give a claimed task back to the pool (owner -> unassigned, status -> todo)."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        t = _owned_task(tasks, args.task_id, args.owner)
        _set_front_matter(t["_path"], owner="unassigned", status="todo")
        _commit_task_change(t, f"release {args.task_id} ({args.owner})")
        print(f"RELEASED: {args.task_id}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def cmd_relink(tasks, errors, args):
    """Edit a task's depends_on / workstream — cycle-validated before writing. The
    only place task topology gets rewritten; commits like note/status/release."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        if args.task_id not in tasks:
            raise RuntimeError(f"unknown task {args.task_id}")
        t = tasks[args.task_id]

        new_deps = t.get("depends_on", [])
        if args.depends_on is not None:
            new_deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
        new_ws = args.workstream if args.workstream else t.get("workstream")

        baseline = set(validate({tid: dict(tt) for tid, tt in tasks.items()}, []))
        trial = {tid: dict(tt) for tid, tt in tasks.items()}
        trial[args.task_id]["depends_on"] = new_deps
        trial[args.task_id]["workstream"] = new_ws
        new_errors = set(validate(trial, [])) - baseline
        if new_errors:
            raise RuntimeError("relink rejected — would break the DAG:\n  " +
                               "\n  ".join(sorted(new_errors)))

        updates = {"depends_on": "[" + ", ".join(new_deps) + "]"}
        if args.workstream:
            updates["workstream"] = new_ws
        _set_front_matter(t["_path"], **updates)
        _commit_task_change(t, f"relink {args.task_id} ({args.owner})")
        print(f"RELINKED: {args.task_id} depends_on={new_deps} workstream={new_ws}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def cmd_checkdod(tasks, errors, args):
    """Toggle one '## Definition of done' checklist item (0-based index) for a task."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        if args.task_id not in tasks:
            raise RuntimeError(f"unknown task {args.task_id}")
        t = tasks[args.task_id]
        text = t["_path"].read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines) if l.strip() == "## Definition of done"), None)
        if start is None:
            raise RuntimeError(f"{args.task_id} has no '## Definition of done' section")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        idxs = [i for i in range(start, end) if re.match(r"^\s*- \[[ xX]\]", lines[i])]
        if not (0 <= args.index < len(idxs)):
            raise RuntimeError(f"checklist index {args.index} out of range (0..{len(idxs) - 1})")
        li = idxs[args.index]
        checked = args.checked == "true"
        lines[li] = re.sub(r"- \[[ xX]\]", f"- [{'x' if checked else ' '}]", lines[li], count=1)
        t["_path"].write_text("\n".join(lines) + "\n", encoding="utf-8")
        _commit_task_change(t, f"checkdod {args.task_id}[{args.index}]={checked} ({args.owner})")
        print(f"CHECKED: {args.task_id}[{args.index}] = {checked}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def cmd_set(tasks, errors, args):
    """Steer a task's priority/lane/kind/deferred (metadata only — no ownership needed;
    commits like note/status). This is how humans + foreman re-prioritize the board."""
    lock = _lock()
    try:
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        tasks, _ = load_tasks()
        if args.task_id not in tasks:
            raise RuntimeError(f"unknown task {args.task_id}")
        t = tasks[args.task_id]
        updates, parts = {}, []
        if args.priority is not None:
            if not 1 <= args.priority <= 5:
                raise RuntimeError("priority must be 1..5")
            updates["priority"] = args.priority
            parts.append(f"P{args.priority}")
        if args.lane:
            if args.lane not in LANES:
                raise RuntimeError(f"bad lane (allowed: {sorted(LANES)})")
            updates["lane"] = args.lane
            parts.append(f"lane={args.lane}")
        if args.kind:
            if args.kind not in KINDS:
                raise RuntimeError(f"bad kind (allowed: {sorted(KINDS)})")
            updates["kind"] = args.kind
            parts.append(f"kind={args.kind}")
        if args.deferred:
            updates["deferred"] = args.deferred
            parts.append(f"deferred={args.deferred}")
        if not updates:
            raise RuntimeError("nothing to set (use --priority/--lane/--kind/--deferred)")
        _upsert_front_matter(t["_path"], **updates)
        if args.reason:
            stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            text = t["_path"].read_text(encoding="utf-8")
            if "## Progress log" not in text:
                text = text.rstrip() + "\n\n## Progress log\n"
            text = text.rstrip() + f"\n- {stamp} [{args.owner}] set {', '.join(parts)}: {args.reason}\n"
            t["_path"].write_text(text, encoding="utf-8")
        _commit_task_change(t, f"set {args.task_id} {' '.join(parts)} ({args.owner})")
        print(f"SET: {args.task_id} {' '.join(parts)}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def _dependents_index(tasks):
    idx = {}
    for tid, t in tasks.items():
        for dep in t.get("depends_on", []):
            idx.setdefault(dep, []).append(tid)
    return idx


def _downstream_open(tasks, tid, idx):
    """Transitive not-done dependents of tid (the work this task is holding up)."""
    seen, stack, out = set(), [tid], []
    while stack:
        cur = stack.pop()
        for nxt in idx.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            if tasks[nxt].get("status") != "done":
                out.append(nxt)
            stack.append(nxt)
    return out


def _open_questions():
    qs = []
    qdir = ROOT / "questions"
    if qdir.is_dir():
        for path in sorted(qdir.glob("*.md")):
            n = len(re.findall(r"(?m)^## Q\d+ \(open\)", path.read_text(encoding="utf-8")))
            if n:
                qs.append({"file": f"work/questions/{path.name}", "open": n})
    return qs


def _roadblock_impact(tasks, t, idx):
    down = _downstream_open(tasks, t["id"], idx)
    live = [d for d in down if not _is_deferred(tasks[d])]
    min_p = min((int(tasks[d].get("priority", 5) or 5) for d in live), default=None)
    self_p = int(t.get("priority", 5) or 5)
    if min_p is not None and min_p <= 2:
        verdict = "HIGH-IMPACT: blocks P%d work — merge/answer or relink to unblock" % min_p
    elif self_p >= 4 and (min_p is None or min_p >= 4):
        verdict = "low-impact: nothing important waits on this — candidate to defer/skip"
    else:
        verdict = "normal"
    return {"blocks": live, "blocks_min_priority": min_p, "verdict": verdict}


def cmd_roadblocks(tasks, errors, args):
    """Everything waiting on a HUMAN, with impact analysis: review-queue merges,
    open questions, human-owned tasks, blocked tasks. --json for the dashboard."""
    idx = _dependents_index(tasks)
    def row(t):
        r = {k: v for k, v in t.items() if not k.startswith("_")}
        r.update(_roadblock_impact(tasks, t, idx))
        return r
    data = {
        "review_queue": [row(t) for t in tasks.values() if t.get("status") == "review"],
        "blocked": [row(t) for t in tasks.values() if t.get("status") == "blocked"],
        "human_owned": [row(t) for t in tasks.values()
                        if t.get("owner") == "main-checkout"
                        and t.get("status") not in ("done", "backlog", "dropped")],
        "questions": _open_questions(),
    }
    for key in ("review_queue", "blocked", "human_owned"):
        data[key].sort(key=lambda r: (r["blocks_min_priority"] or 9,
                                      int(r.get("priority", 5) or 5), r["id"]))
    if getattr(args, "json", False):
        print(json.dumps(data))
        return 0
    for key, label in (("review_queue", "NEEDS HUMAN MERGE (status: review)"),
                       ("blocked", "BLOCKED"),
                       ("human_owned", "HUMAN-OWNED (main-checkout)")):
        if data[key]:
            print(f"\n== {label} ==")
            for r in data[key]:
                holds = f" — holds up {len(r['blocks'])} task(s)" if r["blocks"] else ""
                print(f"  {r['id']:>8}  P{r.get('priority', 5)}  [{r.get('owner','?')}]  {r['title']}{holds}")
                if r["verdict"] != "normal":
                    print(f"           ^ {r['verdict']}")
    if data["questions"]:
        print("\n== OPEN QUESTIONS ==")
        for q in data["questions"]:
            print(f"  {q['open']} open in {q['file']}")
    if not any(data.values()):
        print("no human roadblocks — nothing is waiting on you")
    return 0


def cmd_lanes(tasks, errors, args):
    """Per-lane rollup sorted by closeness to shipping. --json for the dashboard."""
    lanes = {}
    for t in tasks.values():
        lane = t.get("lane", "(untagged)")
        L = lanes.setdefault(lane, {"lane": lane, "total": 0, "done": 0, "doing": 0,
                                    "review": 0, "todo": 0, "blocked": 0, "backlog": 0,
                                    "deferred": 0, "fix_open": 0, "new_open": 0})
        if _is_deferred(t):
            L["deferred"] += 1
            continue
        L["total"] += 1
        L[t.get("status", "todo")] = L.get(t.get("status", "todo"), 0) + 1
        if t.get("status") not in ("done",):
            if t.get("kind") == "fix":
                L["fix_open"] += 1
            elif t.get("kind") == "new":
                L["new_open"] += 1
    rows = []
    for L in lanes.values():
        L["pct_done"] = round(100.0 * L["done"] / L["total"], 1) if L["total"] else 0.0
        rows.append(L)
    rows.sort(key=lambda L: (-L["pct_done"], L["lane"]))
    if getattr(args, "json", False):
        print(json.dumps(rows))
        return 0
    print(f"{'lane':<20} {'done':>5} {'total':>5} {'%':>6}  {'doing':>5} {'review':>6} "
          f"{'todo':>4} {'blkd':>4}  {'fix-open':>8} {'new-open':>8} {'defer':>5}")
    for L in rows:
        print(f"{L['lane']:<20} {L['done']:>5} {L['total']:>5} {L['pct_done']:>5}%  "
              f"{L['doing']:>5} {L['review']:>6} {L['todo']:>4} {L['blocked']:>4}  "
              f"{L['fix_open']:>8} {L['new_open']:>8} {L['deferred']:>5}")
    return 0


def cmd_assignments(tasks, errors, args):
    """Show who owns each lane, on which branch, toward what goal. --json for the
    dashboard. Lanes not yet assigned show owner=unassigned + the default branch."""
    view = _lane_board_view()
    if getattr(args, "json", False):
        print(json.dumps(list(view.values())))
        return 0
    print(f"{'lane':<20} {'owner':<18} {'branch':<22} goal")
    for rec in view.values():
        print(f"{rec['lane']:<20} {rec['owner']:<18} {rec['branch']:<22} {rec['goal']}")
    return 0


def cmd_assign_lane(tasks, errors, args):
    """Assign a lane (feature area) to a human contributor + its working branch and
    goal. This is how a human claims a slice of the roadmap to drive their own agents
    on lane/<lane> without touching master. Writes git-tracked work/lanes.json and
    pushes, so every contributor's dashboard live-syncs the ownership map."""
    lock = _lock()
    try:
        lane = args.lane
        if lane not in ASSIGNABLE_LANES:
            raise RuntimeError(f"bad lane (allowed: {sorted(ASSIGNABLE_LANES)})")
        branch = args.branch or _default_branch(lane)
        if not BRANCH_RE.match(branch):
            raise RuntimeError("branch must match ^lane/[a-z0-9._-]+$ "
                               "(lane work lives on lane/* only, never master)")
        main = _main_worktree()
        if _git(["remote"], main).stdout.strip():
            if not _safe_sync(main):
                print("BUSY: main checkout diverged from origin (foreman mid-session)"
                      " — retry in ~60s", file=sys.stderr)
                return 3
        lanes = _load_lane_board()
        rec = lanes.get(lane, {})
        if args.clear:
            rec["owner"] = "unassigned"
            verb = "unassigned"
        else:
            rec["owner"] = args.owner
            verb = f"assigned to {args.owner}"
        rec["branch"] = branch
        if args.goal is not None:
            rec["goal"] = args.goal
        rec.setdefault("goal", "")
        lanes[lane] = rec
        _save_lane_board(lanes)
        _commit_lane_board(f"assign lane {lane} -> {rec['owner']} on {branch} ({args.owner})")
        print(f"ASSIGN: {lane} {verb} on {branch}")
        return 0
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def cmd_whoami(tasks, errors, args):
    """Show or set the contributor identity (GitHub username) for this checkout.
    Stored gitignored in work/sync/identity.json; used to author board commits and
    to answer `work.py mine`."""
    if _teamlib is None:
        print("ERROR: _teamlib helper not importable", file=sys.stderr)
        return 1
    if args.github or args.name or args.email:
        fields = {}
        if args.github:
            fields["github_user"] = args.github
        if args.name:
            fields["display_name"] = args.name
        if args.email:
            fields["git_email"] = args.email
        _teamlib.set_profile(**fields)
    prof = _teamlib.resolve_and_store(args.github) if not args.name and not args.email \
        else _teamlib.get_profile()
    if not prof:
        print("no identity set — run: work.py whoami --github <your-username>")
        return 0
    print(f"github:  {prof.get('github_user', '(unknown — pass --github)')}")
    print(f"name:    {prof.get('display_name', '(unset)')}")
    print(f"email:   {prof.get('git_email', '(unset — commits use machine identity)')}")
    if prof.get("agent_name"):
        print(f"agent:   {prof['agent_name']} (autonomous working-name)")
    print(f"commits authored as: "
          f"{'this identity' if prof.get('git_email') else 'the machine git identity'}")
    return 0


def cmd_mine(tasks, errors, args):
    """What's on YOUR plate: tasks you own (in progress), your branches awaiting a
    human merge, and tasks that name you. Matches any of your identity aliases
    (GitHub username / agent working-name)."""
    if args.owner:
        aliases = [args.owner.lower()]
        label = args.owner
    elif _teamlib is not None and _teamlib.identity_aliases():
        aliases = _teamlib.identity_aliases()
        label = _teamlib.human_name() or aliases[0]
    else:
        print("ERROR: no --owner given and no identity set — run "
              "`work.py whoami --github <username>` once", file=sys.stderr)
        return 1
    mine = [t for t in tasks.values() if t.get("owner", "").lower() in aliases]
    doing = [t for t in mine if t.get("status") == "doing"]
    review = [t for t in mine if t.get("status") == "review"]
    blocked = [t for t in mine if t.get("status") == "blocked"]

    def show(label, rows):
        if not rows:
            return
        print(f"\n== {label} ==")
        for t in sorted(rows, key=lambda x: (int(x.get("priority", 5) or 5), x["id"])):
            print(f"  {t['id']:>8}  P{t.get('priority', 5)}  {t.get('lane','-'):<16}  {t['title']}")

    print(f"identity: {label}")
    show("IN PROGRESS (yours to finish)", doing)
    show("AWAITING HUMAN MERGE (your review branches)", review)
    show("BLOCKED (need a decision)", blocked)
    if not (doing or review or blocked):
        print("\nnothing assigned to you right now — `work.py ready` shows claimable work")
    return 0


def cmd_prompt(tasks, errors, args):
    tid = args.task_id
    if tid not in tasks:
        print(f"ERROR: unknown task {tid}", file=sys.stderr)
        return 1
    t = tasks[tid]
    deps = t.get("depends_on", [])
    not_done = [d for d in deps if tasks.get(d, {}).get("status") != "done"]
    if not_done:
        print(f"WARNING: unmet dependencies: {', '.join(not_done)}", file=sys.stderr)
    template = (ROOT / "prompts" / "driver.md").read_text(encoding="utf-8")
    task_text = t["_path"].read_text(encoding="utf-8")
    print(template.replace("{{TASK_ID}}", tid)
                  .replace("{{BRANCH}}", t.get("branch", ""))
                  .replace("{{TASK_FILE}}", str(t["_path"].relative_to(ROOT.parent)))
          + "\n\n---\n\nYOUR TASK FILE (work order):\n\n" + task_text)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p_ready = sub.add_parser("ready")
    p_ready.add_argument("--owner")
    p_ready.add_argument("--include-deferred", dest="include_deferred", action="store_true")
    p_board = sub.add_parser("board")
    p_board.add_argument("--workstream")
    p_board.add_argument("--owner")
    p_board.add_argument("--status")
    p_board.add_argument("--lane")
    p_board.add_argument("--kind")
    p_board.add_argument("--json", action="store_true")
    # --owner is OPTIONAL on mutating commands: it defaults to the stored identity
    # (work.py whoami --github ...). Resolved centrally below before dispatch.
    p_set = sub.add_parser("set")
    p_set.add_argument("task_id")
    p_set.add_argument("--owner")
    p_set.add_argument("--priority", type=int, default=None)
    p_set.add_argument("--lane", default=None)
    p_set.add_argument("--kind", default=None)
    p_set.add_argument("--deferred", default=None, choices=["yes", "no"])
    p_set.add_argument("--reason", default=None)
    p_road = sub.add_parser("roadblocks")
    p_road.add_argument("--json", action="store_true")
    p_lanes = sub.add_parser("lanes")
    p_lanes.add_argument("--json", action="store_true")
    p_assigns = sub.add_parser("assignments")
    p_assigns.add_argument("--json", action="store_true")
    p_assign = sub.add_parser("assign-lane")
    p_assign.add_argument("lane")
    p_assign.add_argument("--owner")
    p_assign.add_argument("--branch", default=None)
    p_assign.add_argument("--goal", default=None)
    p_assign.add_argument("--clear", action="store_true",
                          help="unassign the lane (keeps its branch mapping)")
    p_who = sub.add_parser("whoami")
    p_who.add_argument("--github", help="your GitHub username")
    p_who.add_argument("--name", help="display name for commit authorship")
    p_who.add_argument("--email", help="git email for commit authorship")
    p_mine = sub.add_parser("mine")
    p_mine.add_argument("--owner")
    sub.add_parser("dag")
    p_prompt = sub.add_parser("prompt")
    p_prompt.add_argument("task_id")
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--owner")
    p_claim.add_argument("--task")
    p_note = sub.add_parser("note")
    p_note.add_argument("task_id")
    p_note.add_argument("--owner")
    p_note.add_argument("--text", required=True)
    p_status = sub.add_parser("status")
    p_status.add_argument("task_id")
    p_status.add_argument("--owner")
    p_status.add_argument("--status", required=True, choices=sorted(STATUSES))
    p_rel = sub.add_parser("release")
    p_rel.add_argument("task_id")
    p_rel.add_argument("--owner")
    p_relink = sub.add_parser("relink")
    p_relink.add_argument("task_id")
    p_relink.add_argument("--owner")
    p_relink.add_argument("--depends-on", dest="depends_on", default=None)
    p_relink.add_argument("--workstream", default=None)
    p_checkdod = sub.add_parser("checkdod")
    p_checkdod.add_argument("task_id")
    p_checkdod.add_argument("--owner")
    p_checkdod.add_argument("--index", type=int, required=True)
    p_checkdod.add_argument("--checked", required=True, choices=["true", "false"])
    args = ap.parse_args()

    # Default --owner to the stored identity for commands that write as a person
    # (NOT board/ready, where --owner is a filter, nor whoami).
    MUTATING = {"claim", "note", "status", "release", "relink", "checkdod", "set",
                "assign-lane"}
    if args.cmd in MUTATING:
        try:
            args.owner = _resolve_owner(args)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    tasks, errors = load_tasks()
    fn = {"validate": cmd_validate, "ready": cmd_ready,
          "board": cmd_board, "dag": cmd_dag, "prompt": cmd_prompt,
          "claim": cmd_claim, "note": cmd_note, "status": cmd_status,
          "release": cmd_release, "relink": cmd_relink,
          "checkdod": cmd_checkdod, "set": cmd_set,
          "roadblocks": cmd_roadblocks, "lanes": cmd_lanes,
          "assignments": cmd_assignments, "assign-lane": cmd_assign_lane,
          "whoami": cmd_whoami, "mine": cmd_mine}[args.cmd]
    sys.exit(fn(tasks, errors, args))


if __name__ == "__main__":
    main()
