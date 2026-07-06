#!/usr/bin/env python3
"""dashboard.py — AI-CAD team-progress local web dashboard.

Starts a stdlib HTTP server on port 8787 (--port to override),
serves the single-page app from dashboard_web/, and opens the
browser automatically (--no-open to skip).

Run:  python3 work/tools/dashboard.py
      python3 work/tools/dashboard.py --port 8888
      python3 work/tools/dashboard.py --no-open

The only write path is POST /api/set which shells out to work.py set
with validated arguments (list args, never shell=True, bound to 127.0.0.1).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

# ---------- paths
TOOLS_DIR = Path(__file__).resolve().parent
WEB_DIR   = TOOLS_DIR / "dashboard_web"
WORK_DIR  = TOOLS_DIR.parent
REPO_DIR  = WORK_DIR.parent
PY        = sys.executable or "python3"

# Bring _teamlib onto sys.path
sys.path.insert(0, str(TOOLS_DIR))
import _teamlib

# ---------- valid sets for /api/set
VALID_LANES = {
    "modeling-core", "assemblies-drawings", "cam", "ecad-firmware",
    "simulation", "parts-sourcing", "ai-copilot", "app-gui",
    "plm-projects", "platform-infra", "ml-flywheel",
}
VALID_KINDS = {"fix", "overhaul", "new"}
VALID_DEFERRED = {"yes", "no"}
VALID_ASSIGN_LANES = VALID_LANES | {"(untagged)"}
BRANCH_RE = re.compile(r"^lane/[a-z0-9._-]+$")

# Identifies our own dashboard when probing an already-bound port.
APP_MARKER = "aicad-dashboard"


# ============================================================
# Data helpers (reused from old dashboard.py)
# ============================================================

def _run_work(*args, timeout=30):
    r = subprocess.run(
        [PY, str(TOOLS_DIR / "work.py"), *args],
        cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout,
    )
    return r


def load_board_json():
    r = _run_work("board", "--json")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "work.py board --json failed")
    return json.loads(r.stdout)


def load_roadblocks_json():
    r = _run_work("roadblocks", "--json")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "work.py roadblocks --json failed")
    return json.loads(r.stdout)


def load_lanes_json():
    r = _run_work("lanes", "--json")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "work.py lanes --json failed")
    return json.loads(r.stdout)


def load_assignments_json():
    r = _run_work("assignments", "--json")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "work.py assignments --json failed")
    return json.loads(r.stdout)


def load_issues_json():
    """Hot GitHub issues from teamsync's work/sync/issues.json (open first, bugs on
    top, newest first). Read-only — teamsync owns that file, we never write it."""
    p = WORK_DIR / "sync" / "issues.json"
    if not p.exists():
        return {"fetched_ts": None, "issues": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"fetched_ts": None, "issues": []}
    issues = data.get("issues", [])
    for it in issues:
        names = [l.get("name", "") for l in it.get("labels", [])]
        it["label_names"] = names
        it["is_bug"] = "bug" in names
        it["assignee_logins"] = [a.get("login", a) if isinstance(a, dict) else a
                                 for a in it.get("assignees", [])]
    openi = [i for i in issues if i.get("state", "").upper() == "OPEN"]
    openi.sort(key=lambda i: (not i.get("is_bug"), str(i.get("updatedAt", "")) < ""),
               reverse=False)
    openi.sort(key=lambda i: str(i.get("updatedAt", "")), reverse=True)
    openi.sort(key=lambda i: not i.get("is_bug"))
    return {"fetched_ts": data.get("fetched_ts"),
            "open_count": len(openi), "issues": openi}


# Cache the (expensive) 37MB graph.json → compact positioned graph in memory,
# keyed by file mtime so `graphify update` is picked up without a restart.
_GRAPHVIZ_CACHE = {"mtime": None, "payload": None}


def load_graphviz_json():
    """Whole-repo knowledge graph as a compact, pre-positioned node/edge list for
    the canvas viewer. Layout = deterministic sunflower over nodes grouped by
    community, so communities read as coloured spiral clusters and intra-community
    edges stay short. Pure-stdlib, O(n)."""
    import math
    p = REPO_DIR / "graphify-out" / "graph.json"
    if not p.exists():
        return {"nodes": [], "edges": [], "stats": {},
                "error": "graphify-out/graph.json not found — run: graphify update ."}
    mtime = p.stat().st_mtime
    if _GRAPHVIZ_CACHE["mtime"] == mtime and _GRAPHVIZ_CACHE["payload"] is not None:
        return _GRAPHVIZ_CACHE["payload"]

    raw = json.loads(p.read_text(encoding="utf-8"))
    nodes = raw.get("nodes", [])
    links = raw.get("links", [])

    order = sorted(range(len(nodes)),
                   key=lambda i: (nodes[i].get("community", -1) if nodes[i].get("community") is not None else -1,
                                  str(nodes[i].get("id", ""))))
    GA = math.pi * (3.0 - math.sqrt(5.0))   # golden angle
    SCALE = 42.0
    out_nodes = [None] * len(nodes)
    for rank, i in enumerate(order):
        rr = SCALE * math.sqrt(rank + 0.5)
        aa = (rank + 0.5) * GA
        n = nodes[i]
        try:
            comm = int(n.get("community", -1))
        except (TypeError, ValueError):
            comm = -1
        out_nodes[i] = [round(rr * math.cos(aa)), round(rr * math.sin(aa)),
                        comm, n.get("label") or str(n.get("id", "")),
                        n.get("source_file", "")]

    idx = {n.get("id"): i for i, n in enumerate(nodes)}
    edges = []
    for l in links:
        s = idx.get(l.get("source"))
        t = idx.get(l.get("target"))
        if s is not None and t is not None and s != t:
            edges.append([s, t])

    comms = len({n[2] for n in out_nodes}) if out_nodes else 0
    payload = {
        "nodes": out_nodes,
        "edges": edges,
        "stats": {"nodes": len(out_nodes), "edges": len(edges),
                  "communities": comms, "commit": str(raw.get("built_at_commit", ""))[:10]},
    }
    _GRAPHVIZ_CACHE["mtime"] = mtime
    _GRAPHVIZ_CACHE["payload"] = payload
    return payload


def load_task_text(task_id):
    for sub in ("tasks", "backlog"):
        p = WORK_DIR / sub / f"{task_id}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def parse_dod(text):
    """Return [{"checked": bool, "label": str}, ...] for ## Definition of done."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Definition of done")
    except StopIteration:
        return []
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    items = []
    for i in range(start, end):
        m = re.match(r"^\s*- \[([ xX])\]\s*(.*)", lines[i])
        if m:
            items.append({"checked": m.group(1).lower() == "x", "label": m.group(2)})
    return items


def parse_progress_log(text):
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Progress log")
    except StopIteration:
        return []
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return [l for l in lines[start + 1 : end] if l.strip().startswith("- ")][-5:]


def graphify_freshness_note():
    return _teamlib.graphify_freshness(REPO_DIR) or ""


def run_graphify(mode, query):
    """mode = 'query' or 'explain'. Returns output string."""
    if shutil.which("graphify") is None:
        return "graphify CLI not found on PATH — see README setup."
    try:
        r = subprocess.run(
            ["graphify", mode, query],
            cwd=str(REPO_DIR), capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip() or r.stderr.strip() or "(no output)"
    except Exception as e:
        return f"error: {e}"


# ============================================================
# HTTP handler
# ============================================================

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class DashboardHandler(BaseHTTPRequestHandler):
    """Handle GET (static files + JSON API) and POST (/api/set)."""

    def log_message(self, fmt, *args):  # quieter logs
        pass  # comment out to re-enable access log

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, msg):
        self._send_json({"error": msg, "out": msg}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ---------- root → index.html
        if path == "/" or path == "":
            path = "/index.html"

        # ---------- API routes
        if path.startswith("/api/"):
            self._handle_api_get(parsed)
            return

        # ---------- static files from dashboard_web/
        file_path = WEB_DIR / path.lstrip("/")
        if not file_path.resolve().is_relative_to(WEB_DIR):
            self._send_error_json(403, "Forbidden")
            return
        if not file_path.exists() or not file_path.is_file():
            self._send_error_json(404, f"Not found: {path}")
            return

        suffix = file_path.suffix.lower()
        ctype = CONTENT_TYPES.get(suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The SPA (index.html/app.js/style.css) changes when the dashboard is
        # updated; without this the browser serves a stale cached copy and new
        # tabs/panels appear to be "missing". Force revalidation every load.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _handle_api_get(self, parsed):
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/api/health":
                # Lets a second launch detect an already-running dashboard on this
                # port and just open a browser tab instead of failing to bind.
                self._send_json({"ok": True, "app": APP_MARKER})

            elif path == "/api/board.json":
                self._send_json(load_board_json())

            elif path == "/api/roadblocks.json":
                self._send_json(load_roadblocks_json())

            elif path == "/api/lanes.json":
                self._send_json(load_lanes_json())

            elif path == "/api/assignments.json":
                self._send_json(load_assignments_json())

            elif path == "/api/issues.json":
                self._send_json(load_issues_json())

            elif path == "/api/graph.json":
                self._send_json(load_graphviz_json())

            elif path == "/api/freshness.json":
                note = graphify_freshness_note()
                self._send_json({"note": note})

            elif path == "/api/graphify.json":
                mode  = qs.get("mode", ["query"])[0]
                query = qs.get("q", [""])[0].strip()
                if mode not in ("query", "explain"):
                    self._send_error_json(400, "mode must be query or explain")
                    return
                if not query:
                    self._send_error_json(400, "q is required")
                    return
                result = run_graphify(mode, query)
                self._send_json({"result": result})

            elif path.startswith("/api/task/"):
                # /api/task/<id>.json
                task_id = path[len("/api/task/"):]
                if task_id.endswith(".json"):
                    task_id = task_id[:-5]
                if not SAFE_ID_RE.match(task_id):
                    self._send_error_json(400, "Invalid task id")
                    return
                text = load_task_text(task_id)
                self._send_json({
                    "dod": parse_dod(text),
                    "log": parse_progress_log(text),
                })

            else:
                self._send_error_json(404, f"Unknown API endpoint: {path}")

        except Exception as e:
            self._send_error_json(500, str(e))

    def do_POST(self):
        if self.path not in ("/api/set", "/api/assign"):
            self._send_error_json(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self._send_error_json(400, "Invalid JSON body")
            return

        if self.path == "/api/assign":
            self._handle_api_assign(payload)
        else:
            self._handle_api_set(payload)

    def _handle_api_set(self, payload):
        task_id = payload.get("id", "")
        if not task_id or not SAFE_ID_RE.match(task_id):
            self._send_error_json(400, "Missing or invalid id")
            return

        cmd = [PY, str(TOOLS_DIR / "work.py"), "set", task_id, "--owner", "dashboard"]

        # priority
        priority = payload.get("priority")
        if priority is not None:
            try:
                p = int(priority)
                if not 1 <= p <= 5:
                    raise ValueError
            except (ValueError, TypeError):
                self._send_error_json(400, "priority must be int 1-5")
                return
            cmd += ["--priority", str(p)]

        # lane
        lane = payload.get("lane")
        if lane is not None:
            if lane not in VALID_LANES:
                self._send_error_json(400, f"Invalid lane: {lane}")
                return
            cmd += ["--lane", lane]

        # kind
        kind = payload.get("kind")
        if kind is not None:
            if kind not in VALID_KINDS:
                self._send_error_json(400, f"Invalid kind: {kind}")
                return
            cmd += ["--kind", kind]

        # deferred
        deferred = payload.get("deferred")
        if deferred is not None:
            if deferred not in VALID_DEFERRED:
                self._send_error_json(400, "deferred must be 'yes' or 'no'")
                return
            cmd += ["--deferred", deferred]

        # reason (free text — list arg, never shell=True)
        reason = payload.get("reason", "").strip()
        if reason:
            cmd += ["--reason", reason]

        self._run_work_write(cmd)

    def _handle_api_assign(self, payload):
        """Assign a lane to a human contributor + branch/goal. Same write-path
        discipline as /api/set: strict validation, list args, never shell=True."""
        lane = payload.get("lane", "")
        if lane not in VALID_ASSIGN_LANES:
            self._send_error_json(400, f"Invalid lane: {lane}")
            return

        cmd = [PY, str(TOOLS_DIR / "work.py"), "assign-lane", lane]

        owner = str(payload.get("owner", "")).strip()
        if payload.get("clear"):
            cmd += ["--clear"]
        elif not re.match(r"^[A-Za-z0-9 _.\-]{1,60}$", owner):
            self._send_error_json(400, "owner must be 1-60 chars [A-Za-z0-9 _.-]")
            return
        # --owner is the acting contributor for authorship/commit; for a real
        # assignment it is also the assignee. work.py resolves it to identity if
        # omitted, but the dashboard has no shell identity so we pass it through.
        if owner:
            cmd += ["--owner", owner]

        branch = str(payload.get("branch", "")).strip()
        if branch:
            if not BRANCH_RE.match(branch):
                self._send_error_json(400, "branch must match ^lane/[a-z0-9._-]+$")
                return
            cmd += ["--branch", branch]

        goal = str(payload.get("goal", "")).strip()
        if goal:
            if len(goal) > 200:
                self._send_error_json(400, "goal too long (max 200 chars)")
                return
            cmd += ["--goal", goal]

        self._run_work_write(cmd)

    def _run_work_write(self, cmd):
        """Run a validated work.py mutating command and reply with its result,
        mapping exit-code 3 (lock/diverge BUSY) to HTTP 409."""
        try:
            r = subprocess.run(
                cmd,
                cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30,
            )
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 3:
                # BUSY — work.py uses exit 3 for lock contention
                self._send_json({"busy": True, "out": out}, status=409)
                return
            if r.returncode != 0:
                self._send_json({"error": True, "out": out}, status=500)
                return
            self._send_json({"ok": True, "out": out})
        except Exception as e:
            self._send_error_json(500, str(e))


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads (needed for slow graphify/set calls)."""
    daemon_threads = True


# ============================================================
# Main
# ============================================================

def _probe_port(host, port, timeout=0.75):
    """Classify what's on host:port: 'ours' (this dashboard, via /api/health),
    'other' (some server answered but not our marker — e.g. an OLDER dashboard
    started before this file gained /api/health), or 'none' (nothing listening)."""
    import urllib.request
    import urllib.error
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "ours" if data.get("app") == APP_MARKER else "other"
    except urllib.error.HTTPError:
        return "other"   # a server replied (e.g. 404) — occupied, but not ours
    except Exception:
        return "none"    # connection refused / nothing there


def main():
    parser = argparse.ArgumentParser(description="AI-CAD team-progress dashboard")
    parser.add_argument("--port", type=int, default=8787, help="Port to serve on (default: 8787)")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    host = "127.0.0.1"
    port = args.port
    url = f"http://{host}:{port}/"

    def open_existing():
        print(f"AI-CAD dashboard already running at {url} — opening a new tab.")
        if not args.no_open:
            webbrowser.open(url)

    # Already running? Re-launching should still open the UI, not crash on a
    # port-in-use.
    status = _probe_port(host, port)
    if status == "ours":
        open_existing()
        return
    if status == "other":
        # Most common cause: an older dashboard from before an update is still
        # running on this port. Opening a tab would show the STALE UI, so say so
        # instead and point at the fix.
        print(f"Port {port} is already serving something that isn't this dashboard —\n"
              f"likely an OLDER dashboard started before the latest update.\n"
              f"Stop it and re-run, e.g.:\n"
              f"    pkill -f 'work/tools/dashboard.py'   # then run this script again\n"
              f"or launch on another port:\n"
              f"    python3 work/tools/dashboard.py --port 8788",
              file=sys.stderr)
        sys.exit(1)

    try:
        server = ThreadedHTTPServer((host, port), DashboardHandler)
    except OSError as e:
        # Lost a race with another launch. If it's now ours, just open it.
        if _probe_port(host, port) == "ours":
            open_existing()
            return
        print(f"Could not start dashboard on {host}:{port}: {e}\n"
              f"Another program is using the port. Try: python3 work/tools/dashboard.py --port 8788",
              file=sys.stderr)
        sys.exit(1)

    print(f"AI-CAD dashboard: {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_open:
        # Slight delay so server is up before browser hits it
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
