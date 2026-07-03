#!/usr/bin/env python3
"""team_mcp.py — MCP stdio server for AI-CAD team/work coordination.

Exposes the work/ task system, presence/messaging, section context packs, and
GitHub issues to any MCP client (Claude Code CLI, Claude Desktop, Cursor, ...).
Pure stdlib — implements the MCP stdio transport (newline-delimited JSON-RPC)
directly so contributors need nothing but python3 + git (+ gh for issues).

Register with:  python3 work/tools/setup_team_tools.py     (all clients)
or manually:    claude mcp add aicad-team -- python3 work/tools/team_mcp.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
WORK = TOOLS_DIR.parent
REPO = WORK.parent
PY = sys.executable or "python3"

import _teamlib

PROTOCOL_FALLBACK = "2025-06-18"
SERVER_INFO = {"name": "aicad-team", "version": "1.0.0"}

COORD_RULES = (
    "Coordination rules: call whoami(github='your-username') once per checkout — it "
    "stores a gitignored identity (work/sync/identity.json) that becomes the default "
    "owner + commit author for your board changes, and powers my_work (what's waiting "
    "on you). owner/agent params can then be omitted. Claim work ONLY via claim() (kind='task'|"
    "'issue') — atomic, the only legitimate way in; legacy work_claim/issue_claim "
    "still work and route to the same place. Before fixing any bug, run issue_list "
    "and claim(kind='issue', id=N) it so fixes never overlap. Post note() on "
    "meaningful progress so parallel sessions (and humans) stay current. Never set "
    "a task's status to 'done' — that is a human merge-time action. Presence is "
    "best-effort and updates automatically as you call these tools (no daemon "
    "required, though work/workd.sh keeps it warmer between calls)."
)


def _run(args, timeout=120):
    r = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True,
                       timeout=timeout)
    out = (r.stdout or "") + (("\n[stderr] " + r.stderr.strip()) if r.stderr.strip() else "")
    return out.strip() or "(no output)", r.returncode


def _work(*args):
    return _run([PY, str(TOOLS_DIR / "work.py"), *args])


def _sync(*args):
    return _run([PY, str(TOOLS_DIR / "teamsync.py"), *args])


def _gh(*args):
    try:
        return _run(["gh", *args], timeout=45)
    except FileNotFoundError:
        return "gh (GitHub CLI) not installed — issue tools unavailable", 1


def _sections():
    d = WORK / "context"
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def _section_blurbs():
    d = WORK / "context"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        lines = p.read_text(encoding="utf-8").splitlines()
        title = lines[0].lstrip("#").strip() if lines else p.stem
        scope = ""
        for ln in lines[1:6]:
            if ln.startswith("Scope:"):
                scope = ln[len("Scope:"):].strip()
                break
        out.append(f"{p.stem} — {title}" + (f" ({scope})" if scope else ""))
    return out


_GRAPHIFY_MISSING = (
    "graphify CLI not found on PATH. Install it (see README.md's graphify section) — "
    "it gives scoped subgraph answers instead of raw grep/read loops. Falling back to "
    "section_context / direct file reads for now."
)
_GRAPH_MISSING = (
    "graphify-out/graph.json does not exist in this checkout yet. Run `graphify .` "
    "once (see README.md) to build the local graph, or `graphify update .` after "
    "code changes. Falling back to section_context / direct file reads for now."
)


def _graphify(*args):
    if shutil.which("graphify") is None:
        return _GRAPHIFY_MISSING, 1
    if not (REPO / "graphify-out" / "graph.json").exists():
        return _GRAPH_MISSING, 1
    out, rc = _run(["graphify", *args], timeout=90)
    note = _teamlib.graphify_freshness(REPO)
    return (note + out if note else out), rc


def _need(a, key):
    """Resolve an owner/agent param from the call, falling back to the stored
    whoami identity. Raises with a clear fix if neither is available."""
    val = a.get(key) or _teamlib.get_identity()
    if not val:
        raise ValueError(f"{key!r} not given and no identity set — call "
                         f"whoami(name=...) once, or pass {key!r} explicitly")
    return val


def _heartbeat_async(who, activity):
    """Fire-and-forget presence ping — never blocks or fails the actual tool call."""
    if not who:
        return
    try:
        subprocess.Popen([PY, str(TOOLS_DIR / "teamsync.py"), "presence",
                          "--agent", who, "--activity", activity],
                         cwd=str(REPO), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------- tool table

def T(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object",
                            "properties": props or {},
                            "required": required or []}}


S = {"type": "string"}
KIND = {"type": "string", "enum": ["task", "issue"]}
TOOLS = [
    T("whoami",
      "Get or set your identity for this checkout (stored gitignored in "
      "work/sync/identity.json). A human contributor is identified by their GitHub "
      "username: pass github=... once (auto-detected from `gh`/`git config` if you "
      "call with no args) and it becomes the default owner + commit author for your "
      "board changes, and drives my_work. name=/email= set commit-authorship fields; "
      "name= alone (no github) sets an ephemeral agent working-name for autonomous "
      "drivers. Call with no args to see/auto-detect what's set.",
      {"github": S, "name": S, "email": S}),
    T("my_work",
      "What's on YOUR plate right now, by your stored identity (or pass owner=): "
      "tasks you're mid-way through, your branches awaiting a human merge, and "
      "anything of yours that's blocked. Call this when you sit down to see what's "
      "waiting for you.",
      {"owner": S}),
    T("claim",
      "Atomically claim a task or GitHub issue — the ONLY legitimate way to take "
      "either kind of work; same double-claim protection either way. "
      "kind='task': locks/pulls/commits owner+status=doing to master (omit id to "
      "claim the next ready task; on LOST-RACE just call again). kind='issue': "
      "assigns the issue to you + labels in-progress. Supersedes work_claim/"
      "issue_claim (still work, route here).",
      {"kind": KIND, "id": S, "owner": S}, ["kind"]),
    T("release",
      "Return a claimed task or issue to the pool if you can't finish it. "
      "kind='task': owner->unassigned, status->todo. kind='issue': un-assigns you, "
      "removes in-progress label. Supersedes work_release.",
      {"kind": KIND, "id": S, "owner": S}, ["kind", "id"]),
    T("set_status",
      "Set a task's or issue's status. kind='task': guarded transitions "
      "doing->review (pushed, awaiting human merge)/doing->blocked/blocked->doing/"
      "review->doing — NEVER 'done', that's a human merge-time action. kind='issue': "
      "issues don't have a real state machine like tasks — status='done' closes it "
      "via gh, other values are applied as a best-effort label swap, not enforced. "
      "Supersedes work_status.",
      {"kind": KIND, "id": S, "owner": S, "status": S}, ["kind", "id", "status"]),
    T("note",
      "Append a progress note. kind='task': timestamped note on the task file, "
      "pushed to master. kind='issue': a GitHub issue comment. Use at meaningful "
      "milestones so parallel sessions/humans stay current without asking. "
      "Supersedes work_note/issue_comment.",
      {"kind": KIND, "id": S, "owner": S, "text": S}, ["kind", "id", "text"]),
    T("work_board",
      "Full task board by workstream (id, status, owner, title) plus who is online "
      "and open GitHub issues. Call this FIRST for situational awareness. Optional "
      "workstream/owner/status filters; format='json' returns structured task "
      "objects instead of the formatted-text board (for programmatic callers). "
      + COORD_RULES,
      {"workstream": S, "owner": S, "status": S,
       "format": {"type": "string", "enum": ["text", "json"]}}),
    T("work_ready",
      "List unblocked, unclaimed tasks (todo with all dependencies done)."),
    T("work_relink",
      "Edit a task's depends_on and/or workstream — cycle-validated before writing "
      "(rejected with a clear error if it would break the DAG). The only supported "
      "way to change task topology; never edit task files directly.",
      {"task_id": S, "owner": S, "depends_on": S, "workstream": S}, ["task_id"]),
    T("work_claim",
      "Legacy alias for claim(kind='task', ...) — still works, routes there. "
      "Atomically claim a task: locks, pulls, sets owner/status=doing, commits to "
      "master, pushes. Omit task_id to claim the next ready task. On LOST-RACE just "
      "call again.",
      {"owner": S, "task_id": S}, []),
    T("work_note",
      "Legacy alias for note(kind='task', ...) — still works, routes there. Append "
      "a timestamped progress note to a claimed task and push it.",
      {"task_id": S, "owner": S, "text": S}, ["task_id", "text"]),
    T("work_status",
      "Legacy alias for set_status(kind='task', ...) — still works, routes there.",
      {"task_id": S, "owner": S, "status": S}, ["task_id", "status"]),
    T("work_release",
      "Legacy alias for release(kind='task', ...) — still works, routes there.",
      {"task_id": S, "owner": S}, ["task_id"]),
    T("work_prompt",
      "Emit the self-contained work order (driver rules + task file) for a task.",
      {"task_id": S}, ["task_id"]),
    T("team_status",
      "Who is online (best-effort presence, auto-updated as agents call these "
      "tools), what task each holds, plus cached open issues. Use before starting "
      "anything that might overlap another active worker — but note this is "
      "best-effort, not a substitute for claim()'s atomic guarantee."),
    T("team_send",
      "Send a message to another active worker (or broadcast). Delivered via the "
      "work-sync branch; their daemon/inbox surfaces it within a minute.",
      {"from_agent": S, "text": S, "to_agent": S}, ["text"]),
    T("team_inbox",
      "Read (and mark read) messages addressed to this agent or broadcast.",
      {"agent": S}),
    T("sync_now",
      "Run one sync cycle now: refresh remote board/issues/presence caches and report "
      "what changed (new claims, status flips, new issues, messages).",
      {"agent": S}),
    T("issue_list",
      "List open GitHub issues with assignees/labels — the anti-overlap registry. "
      "If an issue you are about to fix is assigned to someone else, coordinate "
      "via team_send instead of duplicating the fix.",
      {"state": {"type": "string", "enum": ["open", "closed", "all"]},
       "label": S, "assignee": S}),
    T("issue_view", "Full body + comments of one GitHub issue.",
      {"number": {"type": "integer"}}, ["number"]),
    T("issue_create",
      "File a GitHub issue for a discovered bug/gap you are NOT fixing right now, so "
      "nobody re-discovers it. Include repro + suspected files.",
      {"title": S, "body": S, "labels": S}, ["title", "body"]),
    T("issue_claim",
      "Assign a GitHub issue to the current gh user and label it in-progress — do "
      "this BEFORE starting any fix so parallel workers don't collide.",
      {"number": {"type": "integer"}}, ["number"]),
    T("issue_comment",
      "Comment progress/findings on a GitHub issue.",
      {"number": {"type": "integer"}, "body": S}, ["number", "body"]),
    T("section_context",
      "Load the maintained context pack (source map, entry points, debugging "
      "playbook, graphify starters) for a code section — much cheaper than "
      "re-exploring. Call with no args to list available sections.",
      {"section": S}),
    T("graphify_query",
      "Live scoped-subgraph answer to a codebase question from this repo's graphify "
      "knowledge graph (graphify-out/graph.json) — BFS traversal over god nodes, "
      "community structure, and cross-file relationships. Much cheaper than repeated "
      "grep/read tool-call loops. Call this (or graphify_explain/graphify_path) "
      "BEFORE raw grepping/reading source, per this repo's own graphify policy.",
      {"question": S, "dfs": {"type": "boolean"}, "budget": {"type": "integer"}},
      ["question"]),
    T("graphify_explain",
      "Focused graphify explanation of one concept/symbol/file — narrower and "
      "cheaper than graphify_query for a single known target.",
      {"concept": S}, ["concept"]),
    T("graphify_path",
      "Graphify relationship trace between two named entities (files, symbols, "
      "concepts) — how A connects to B through the codebase graph.",
      {"a": S, "b": S}, ["a", "b"]),
    T("work_set",
      "Steer task metadata: lane, kind, priority, deferred flag, and/or reason. "
      "Use to park ML/SaaS work (deferred=yes — skipped by ready/claim), reprioritize, "
      "or reclassify fix|overhaul|new. Owner is required (either pass it or set via "
      "whoami first — co-located agents clobber shared identity, so pass owner "
      "explicitly on mutating calls). Commits like note().",
      {"task_id": S, "owner": S,
       "priority": {"type": "integer", "minimum": 1, "maximum": 5},
       "lane": {"type": "string", "enum": [
           "modeling-core", "assemblies-drawings", "cam", "ecad-firmware",
           "simulation", "parts-sourcing", "ai-copilot", "app-gui",
           "plm-projects", "platform-infra", "ml-flywheel"]},
       "kind": {"type": "string", "enum": ["fix", "overhaul", "new"]},
       "deferred": {"type": "string", "enum": ["yes", "no"]},
       "reason": S},
      ["task_id"]),
    T("work_roadblocks",
      "Human-attention queue: review tasks awaiting merge (HIGH-IMPACT if they block "
      "P1/P2 work), blocked tasks, and human-owned open questions. Check this FIRST "
      "when coming online — it surfaces the fastest way to unblock agents. "
      "format='json' returns structured objects with blocks/verdict fields.",
      {"format": {"type": "string", "enum": ["text", "json"]}}),
    T("work_lanes",
      "Per-lane ship-readiness rollup: done/total/%, doing/review/todo/blocked counts, "
      "open fix and new work counts, deferred count. Lanes are user-facing feature "
      "areas (modeling-core, cam, app-gui, ai-copilot, …). "
      "format='json' returns structured objects for programmatic callers.",
      {"format": {"type": "string", "enum": ["text", "json"]}}),
]


# ---------------------------------------------------------------- unified claim model

def _do_issue_claim(number):
    out1, rc = _gh("issue", "edit", str(number), "--add-assignee", "@me")
    out2, _ = _gh("issue", "edit", str(number), "--add-label", "in-progress")
    note = "" if rc == 0 else "\n(if the in-progress label is missing, create it once in the repo)"
    return f"{out1}\n{out2}{note}\nClaimed — post findings with note(kind='issue', ...)."


def _issue_release(number):
    out1, _ = _gh("issue", "edit", str(number), "--remove-assignee", "@me")
    out2, _ = _gh("issue", "edit", str(number), "--remove-label", "in-progress")
    return f"{out1}\n{out2}"


def _issue_set_status(number, status):
    if status == "done":
        return _gh("issue", "close", str(number))[0]
    if status in ("doing", "review", "blocked", "todo"):
        _gh("issue", "edit", str(number), "--remove-label", "in-progress")
        if status != "todo":
            _gh("issue", "edit", str(number), "--add-label", status)
        return (f"issue #{number} labeled {status!r} — note issues don't have "
                f"tasks' guarded state machine, this is a label, not enforced")
    raise ValueError(f"unsupported status {status!r} for kind='issue' "
                     f"(expected done|doing|review|blocked|todo)")


def _unified_claim(a):
    kind = a.get("kind", "task")
    if kind == "task":
        args = ["claim", "--owner", _need(a, "owner")]
        if a.get("id"):
            args += ["--task", a["id"]]
        return _work(*args)[0]
    if kind == "issue":
        return _do_issue_claim(int(a["id"]))
    raise ValueError(f"kind must be 'task' or 'issue', got {kind!r}")


def _unified_release(a):
    kind = a.get("kind", "task")
    if kind == "task":
        return _work("release", a["id"], "--owner", _need(a, "owner"))[0]
    if kind == "issue":
        return _issue_release(int(a["id"]))
    raise ValueError(f"kind must be 'task' or 'issue', got {kind!r}")


def _unified_set_status(a):
    kind = a.get("kind", "task")
    if kind == "task":
        return _work("status", a["id"], "--owner", _need(a, "owner"),
                     "--status", a["status"])[0]
    if kind == "issue":
        return _issue_set_status(int(a["id"]), a["status"])
    raise ValueError(f"kind must be 'task' or 'issue', got {kind!r}")


def _unified_note(a):
    kind = a.get("kind", "task")
    if kind == "task":
        return _work("note", a["id"], "--owner", _need(a, "owner"), "--text", a["text"])[0]
    if kind == "issue":
        return _gh("issue", "comment", str(a["id"]), "--body", a["text"])[0]
    raise ValueError(f"kind must be 'task' or 'issue', got {kind!r}")


# ---------------------------------------------------------------- dispatch

def call_tool(name, a):
    who = a.get("owner") or a.get("agent") or a.get("from_agent") or _teamlib.get_identity()
    _heartbeat_async(who, name)

    if name == "whoami":
        fields = {}
        if a.get("github"):
            fields["github_user"] = a["github"]
        if a.get("email"):
            fields["git_email"] = a["email"]
        # name= with github/email present is the display name; name= alone is the
        # legacy ephemeral agent working-name.
        if a.get("name"):
            fields["display_name" if (a.get("github") or a.get("email")) else "agent_name"] = a["name"]
        if fields:
            _teamlib.set_profile(**fields)
        prof = _teamlib.resolve_and_store(a.get("github"))
        if not prof:
            return "no identity set — call whoami(github='your-username') once"
        lines = [f"github:  {prof.get('github_user', '(unknown — pass github=)')}",
                 f"name:    {prof.get('display_name', '(unset)')}",
                 f"email:   {prof.get('git_email', '(unset — uses machine git identity)')}"]
        if prof.get("agent_name"):
            lines.append(f"agent:   {prof['agent_name']} (autonomous working-name)")
        lines.append("board changes commit as: "
                     + ("this identity" if prof.get("git_email") else "the machine git identity"))
        return "\n".join(lines)
    if name == "my_work":
        return _work("mine", *(["--owner", a["owner"]] if a.get("owner") else []))[0]
    if name == "claim":
        return _unified_claim(a)
    if name == "release":
        return _unified_release(a)
    if name == "set_status":
        return _unified_set_status(a)
    if name == "note":
        return _unified_note(a)
    if name == "work_board":
        args = ["board"]
        for k in ("workstream", "owner", "status"):
            if a.get(k):
                args += [f"--{k}", a[k]]
        if a.get("format") == "json":
            args.append("--json")
            return _work(*args)[0]
        board, _ = _work(*args)
        team, _ = _sync("team")
        return f"{board}\n\n== team ==\n{team}\n\n{COORD_RULES}"
    if name == "work_ready":
        return _work("ready")[0]
    if name == "work_relink":
        args = ["relink", a["task_id"], "--owner", _need(a, "owner")]
        if a.get("depends_on") is not None:
            args += ["--depends-on", a["depends_on"]]
        if a.get("workstream"):
            args += ["--workstream", a["workstream"]]
        return _work(*args)[0]
    if name == "work_claim":
        return _unified_claim({"kind": "task", "owner": a.get("owner"), "id": a.get("task_id")})
    if name == "work_note":
        return _unified_note({"kind": "task", "owner": a.get("owner"),
                              "id": a["task_id"], "text": a["text"]})
    if name == "work_status":
        return _unified_set_status({"kind": "task", "owner": a.get("owner"),
                                    "id": a["task_id"], "status": a["status"]})
    if name == "work_release":
        return _unified_release({"kind": "task", "owner": a.get("owner"), "id": a["task_id"]})
    if name == "work_prompt":
        return _work("prompt", a["task_id"])[0]
    if name == "team_status":
        return _sync("team")[0]
    if name == "team_send":
        args = ["send", "--agent", _need(a, "from_agent"), "--text", a["text"]]
        if a.get("to_agent"):
            args += ["--to", a["to_agent"]]
        return _sync(*args)[0]
    if name == "team_inbox":
        return _sync("inbox", "--agent", _need(a, "agent"))[0]
    if name == "sync_now":
        args = ["sync"] + (["--agent", who] if who else [])
        return _sync(*args)[0]
    if name == "issue_list":
        args = ["issue", "list", "--state", a.get("state", "open"), "--limit", "100"]
        if a.get("label"):
            args += ["--label", a["label"]]
        if a.get("assignee"):
            args += ["--assignee", a["assignee"]]
        return _gh(*args)[0]
    if name == "issue_view":
        return _gh("issue", "view", str(a["number"]), "--comments")[0]
    if name == "issue_create":
        args = ["issue", "create", "--title", a["title"], "--body", a["body"]]
        if a.get("labels"):
            for lbl in str(a["labels"]).split(","):
                args += ["--label", lbl.strip()]
        return _gh(*args)[0]
    if name == "issue_claim":
        return _do_issue_claim(a["number"])
    if name == "issue_comment":
        return _gh("issue", "comment", str(a["number"]), "--body", a["body"])[0]
    if name == "section_context":
        sec = a.get("section", "")
        if not sec:
            return "Available sections:\n  " + "\n  ".join(_section_blurbs())
        path = WORK / "context" / f"{sec}.md"
        if not path.exists():
            return f"Unknown section {sec!r}. Available: {', '.join(_sections())}"
        return path.read_text(encoding="utf-8")
    if name == "graphify_query":
        args = ["query", a["question"]]
        if a.get("dfs"):
            args.append("--dfs")
        if a.get("budget"):
            args += ["--budget", str(a["budget"])]
        return _graphify(*args)[0]
    if name == "graphify_explain":
        return _graphify("explain", a["concept"])[0]
    if name == "graphify_path":
        return _graphify("path", a["a"], a["b"])[0]
    if name == "work_set":
        args = ["set", a["task_id"], "--owner", _need(a, "owner")]
        if a.get("priority") is not None:
            args += ["--priority", str(a["priority"])]
        if a.get("lane"):
            args += ["--lane", a["lane"]]
        if a.get("kind"):
            args += ["--kind", a["kind"]]
        if a.get("deferred"):
            args += ["--deferred", a["deferred"]]
        if a.get("reason"):
            args += ["--reason", a["reason"]]
        return _work(*args)[0]
    if name == "work_roadblocks":
        args = ["roadblocks"]
        if a.get("format") == "json":
            args.append("--json")
        return _work(*args)[0]
    if name == "work_lanes":
        args = ["lanes"]
        if a.get("format") == "json":
            args.append("--json")
        return _work(*args)[0]
    raise ValueError(f"unknown tool {name}")


# ---------------------------------------------------------------- MCP stdio loop

def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if method == "initialize":
            client_ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_FALLBACK)
            _reply(msg_id, {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "AI-CAD team coordination server. Full policy: work/WORKFLOW.md. "
                    "IDENTITY: call whoami(github='your-username') once per checkout — "
                    "stored gitignored (work/sync/identity.json), it becomes the default "
                    "owner + git commit-author for your board changes and drives my_work. "
                    "WHEN COMING ONLINE: run my_work to see what's waiting on YOU, then "
                    "work_roadblocks — it surfaces the whole human-attention queue "
                    "(review tasks awaiting merge, blocked tasks, open questions) and is "
                    "the fastest way to unblock other agents. "
                    "Then work_board for full situational awareness. "
                    "LANES: tasks belong to a user-facing feature area (lane): "
                    "modeling-core, assemblies-drawings, cam, ecad-firmware, simulation, "
                    "parts-sourcing, ai-copilot, app-gui, plm-projects, platform-infra, "
                    "ml-flywheel. Use work_lanes for per-lane ship-readiness rollup. "
                    "KIND: fix|overhaul|new — ready/claim tiebreak fix-before-new within "
                    "a priority level, so bugs get cleared before new features. "
                    "DEFERRED: tasks with deferred=yes (ML training, SaaS work, parked "
                    "plans) are invisible to ready/claim — they do not compete for "
                    "agent bandwidth. Use work_set to park or unpark tasks. "
                    "STEERING: work_set changes lane/kind/priority/deferred on any task "
                    "(owner required — pass explicitly, do not rely on shared identity). "
                    + COORD_RULES),
            })
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue  # notifications: no response
        elif method == "ping":
            _reply(msg_id, {})
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                text = call_tool(name, args)
                _reply(msg_id, {"content": [{"type": "text", "text": text}],
                                "isError": False})
            except Exception as e:
                _reply(msg_id, {"content": [{"type": "text", "text": f"ERROR: {e}"}],
                                "isError": True})
        elif msg_id is not None:
            _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
