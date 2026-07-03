#!/usr/bin/env python3
"""work.py — git-tracked task coordination for AI-CAD.

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

ROOT = Path(__file__).resolve().parent.parent  # work/
STATUSES = {"todo", "doing", "review", "done", "blocked", "backlog"}
REQUIRED = ("id", "title", "workstream", "owner", "status", "branch", "depends_on")


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
    tasks, errors = {}, []
    for sub in ("tasks", "backlog"):
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
        if args.owner and t.get("owner", "").lower() != args.owner.lower():
            continue
        deps = t.get("depends_on", [])
        if all(tasks.get(d, {}).get("status") == "done" for d in deps):
            rows.append(t)
    if not rows:
        print("no ready tasks")
        return 0
    rows.sort(key=lambda t: (int(t.get("priority", 5) or 5), str(t.get("day", "99")), t["id"]))
    for t in rows:
        print(f"{t['id']:>8}  P{t.get('priority', 5)}  day {t.get('day','-'):>2}  [{t.get('owner','?')}]  {t['title']}")
    return 0


def cmd_board(tasks, errors, args):
    rows = list(tasks.values())
    if getattr(args, "workstream", None):
        rows = [t for t in rows if t.get("workstream", "").lower() == args.workstream.lower()]
    if getattr(args, "owner", None):
        rows = [t for t in rows if t.get("owner", "").lower() == args.owner.lower()]
    if getattr(args, "status", None):
        rows = [t for t in rows if t.get("status", "").lower() == args.status.lower()]

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
    r = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
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
                     if t["status"] == "todo" and unclaimed(t)
                     and all(tasks.get(d, {}).get("status") == "done"
                             for d in t.get("depends_on", []))]
            # Priority-first auto-claim (Erik, 2026-07-03): lower `priority:` wins
            # (1 = drop-everything, default 5), then day, then id.
            cands.sort(key=lambda t: (int(t.get("priority", 5) or 5),
                                      str(t.get("day", "99")), t["id"]))
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
    p_board = sub.add_parser("board")
    p_board.add_argument("--workstream")
    p_board.add_argument("--owner")
    p_board.add_argument("--status")
    p_board.add_argument("--json", action="store_true")
    sub.add_parser("dag")
    p_prompt = sub.add_parser("prompt")
    p_prompt.add_argument("task_id")
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--owner", required=True)
    p_claim.add_argument("--task")
    p_note = sub.add_parser("note")
    p_note.add_argument("task_id")
    p_note.add_argument("--owner", required=True)
    p_note.add_argument("--text", required=True)
    p_status = sub.add_parser("status")
    p_status.add_argument("task_id")
    p_status.add_argument("--owner", required=True)
    p_status.add_argument("--status", required=True, choices=sorted(STATUSES))
    p_rel = sub.add_parser("release")
    p_rel.add_argument("task_id")
    p_rel.add_argument("--owner", required=True)
    p_relink = sub.add_parser("relink")
    p_relink.add_argument("task_id")
    p_relink.add_argument("--owner", required=True)
    p_relink.add_argument("--depends-on", dest="depends_on", default=None)
    p_relink.add_argument("--workstream", default=None)
    p_checkdod = sub.add_parser("checkdod")
    p_checkdod.add_argument("task_id")
    p_checkdod.add_argument("--owner", required=True)
    p_checkdod.add_argument("--index", type=int, required=True)
    p_checkdod.add_argument("--checked", required=True, choices=["true", "false"])
    args = ap.parse_args()
    tasks, errors = load_tasks()
    fn = {"validate": cmd_validate, "ready": cmd_ready,
          "board": cmd_board, "dag": cmd_dag, "prompt": cmd_prompt,
          "claim": cmd_claim, "note": cmd_note, "status": cmd_status,
          "release": cmd_release, "relink": cmd_relink,
          "checkdod": cmd_checkdod}[args.cmd]
    sys.exit(fn(tasks, errors, args))


if __name__ == "__main__":
    main()
