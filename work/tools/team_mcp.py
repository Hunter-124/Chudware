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

PROTOCOL_FALLBACK = "2025-06-18"
SERVER_INFO = {"name": "aicad-team", "version": "1.0.0"}

COORD_RULES = (
    "Coordination rules: claim tasks ONLY via work_claim (atomic). Before fixing any "
    "bug, run issue_list and issue_claim it so fixes never overlap. Post work_note "
    "on meaningful progress so parallel sessions (and humans) stay current. Never "
    "set a task to done — that is a human merge-time action."
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
    return _run(["graphify", *args], timeout=90)


# ---------------------------------------------------------------- tool table

def T(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object",
                            "properties": props or {},
                            "required": required or []}}


S = {"type": "string"}
TOOLS = [
    T("work_board",
      "Full task board by workstream (id, status, owner, title) plus who is online "
      "and open GitHub issues. Call this FIRST for situational awareness. " + COORD_RULES),
    T("work_ready",
      "List unblocked, unclaimed tasks (todo with all dependencies done)."),
    T("work_claim",
      "Atomically claim a task: locks, pulls, sets owner/status=doing, commits to "
      "master, pushes. The ONLY legitimate way to take work. Omit task_id to claim "
      "the next ready task. On LOST-RACE just call again.",
      {"owner": S, "task_id": S}, ["owner"]),
    T("work_note",
      "Append a timestamped progress note to YOUR claimed task and push it, so "
      "other sessions/humans see progress without asking. Use at meaningful "
      "milestones and before ending a session.",
      {"task_id": S, "owner": S, "text": S}, ["task_id", "owner", "text"]),
    T("work_status",
      "Set YOUR claimed task's status. Allowed: doing->review (work pushed, awaiting "
      "human merge), doing->blocked, blocked->doing, review->doing. NEVER done.",
      {"task_id": S, "owner": S, "status": S}, ["task_id", "owner", "status"]),
    T("work_release",
      "Return YOUR claimed task to the pool (unassigned/todo) if you cannot finish it.",
      {"task_id": S, "owner": S}, ["task_id", "owner"]),
    T("work_prompt",
      "Emit the self-contained work order (driver rules + task file) for a task.",
      {"task_id": S}, ["task_id"]),
    T("team_status",
      "Who is online (presence heartbeats from every contributor's sync daemon), what "
      "task each holds, plus cached open issues. Use before starting anything that "
      "might overlap another active worker."),
    T("team_send",
      "Send a message to another active worker (or broadcast). Delivered via the "
      "work-sync branch; their daemon/inbox surfaces it within a minute.",
      {"from_agent": S, "text": S, "to_agent": S}, ["from_agent", "text"]),
    T("team_inbox",
      "Read (and mark read) messages addressed to this agent or broadcast.",
      {"agent": S}, ["agent"]),
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
]


# ---------------------------------------------------------------- dispatch

def call_tool(name, a):
    if name == "work_board":
        board, _ = _work("board")
        team, _ = _sync("team")
        return f"{board}\n\n== team ==\n{team}\n\n{COORD_RULES}"
    if name == "work_ready":
        return _work("ready")[0]
    if name == "work_claim":
        args = ["claim", "--owner", a["owner"]]
        if a.get("task_id"):
            args += ["--task", a["task_id"]]
        return _work(*args)[0]
    if name == "work_note":
        return _work("note", a["task_id"], "--owner", a["owner"], "--text", a["text"])[0]
    if name == "work_status":
        return _work("status", a["task_id"], "--owner", a["owner"],
                     "--status", a["status"])[0]
    if name == "work_release":
        return _work("release", a["task_id"], "--owner", a["owner"])[0]
    if name == "work_prompt":
        return _work("prompt", a["task_id"])[0]
    if name == "team_status":
        return _sync("team")[0]
    if name == "team_send":
        args = ["send", "--agent", a["from_agent"], "--text", a["text"]]
        if a.get("to_agent"):
            args += ["--to", a["to_agent"]]
        return _sync(*args)[0]
    if name == "team_inbox":
        return _sync("inbox", "--agent", a["agent"])[0]
    if name == "sync_now":
        args = ["sync"] + (["--agent", a["agent"]] if a.get("agent") else [])
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
        out1, rc = _gh("issue", "edit", str(a["number"]), "--add-assignee", "@me")
        out2, _ = _gh("issue", "edit", str(a["number"]), "--add-label", "in-progress")
        note = "" if rc == 0 else "\n(if the in-progress label is missing, create it once in the repo)"
        return f"{out1}\n{out2}{note}\nClaimed — post findings with issue_comment."
    if name == "issue_comment":
        return _gh("issue", "comment", str(a["number"]), "--body", a["body"])[0]
    if name == "section_context":
        secs = _sections()
        sec = a.get("section", "")
        if not sec:
            return "Available sections: " + ", ".join(secs)
        path = WORK / "context" / f"{sec}.md"
        if not path.exists():
            return f"Unknown section {sec!r}. Available: {', '.join(secs)}"
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
                    "AI-CAD team coordination server. Start with work_board. " + COORD_RULES),
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
