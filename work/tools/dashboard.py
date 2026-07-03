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
        self.end_headers()
        self.wfile.write(body)

    def _handle_api_get(self, parsed):
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/api/board.json":
                self._send_json(load_board_json())

            elif path == "/api/roadblocks.json":
                self._send_json(load_roadblocks_json())

            elif path == "/api/lanes.json":
                self._send_json(load_lanes_json())

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
        if self.path != "/api/set":
            self._send_error_json(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except Exception:
            self._send_error_json(400, "Invalid JSON body")
            return

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

def main():
    parser = argparse.ArgumentParser(description="AI-CAD team-progress dashboard")
    parser.add_argument("--port", type=int, default=8787, help="Port to serve on (default: 8787)")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    host = "127.0.0.1"
    port = args.port

    server = ThreadedHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
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
