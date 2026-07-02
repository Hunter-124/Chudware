#!/usr/bin/env python3
"""dashboard.py — read-only team-progress viewer for AI-CAD contributors.

Tkinter (stdlib only). Three tabs, all display-only — nothing here claims,
edits, or writes anything:
  1. Human Roadblocks — everything blocked on a human right now (top priority).
  2. DAG — the master task graph, color-coded, greyed where blocked on unmet
     deps or awaiting human review. Click a node to see its details (read-only).
  3. Graphify Preview — live query/explain with a staleness banner.

To actually change the plan (claim a task, add a note, edit dependencies,
change status), talk to an AI driver in Claude Code — it has the same data
this dashboard shows via the aicad-team MCP tools (work_board, claim, note,
set_status, work_relink, ...), or let autopilot (work/prompts/autopilot.md)
decide on its own. Keeping this GUI to pure display avoids a large surface of
interactive-widget bugs for a window whose only job is "let me see the plan."

Auto-refreshes every 30s; manual "Refresh now" also available.

Run:  python3 work/tools/dashboard.py   (or work/run_version_control.sh/.bat)
"""
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk
except ImportError:
    print("tkinter is not available in this Python install.\n"
          "  Debian/Ubuntu/Fedora: install the 'python3-tk' package, then re-run.\n"
          "  Windows/macOS python.org installers include it by default.",
          file=sys.stderr)
    sys.exit(1)

TOOLS_DIR = Path(__file__).resolve().parent
WORK = TOOLS_DIR.parent
REPO = WORK.parent
PY = sys.executable or "python3"

import _teamlib

STATUS_COLORS = {
    "todo":    ("#e8e8e8", "#999999"),
    "doing":   ("#cfe3ff", "#4a90d9"),
    "review":  ("#ffe9b8", "#d9a441"),
    "done":    ("#cdeec9", "#57a05a"),
    "blocked": ("#f5c6c6", "#c0504d"),
    "backlog": ("#f4f0fa", "#b0a3c9"),
}
LOCKED_COLOR = ("#d9d9d9", "#888888")
AUTO_REFRESH_MS = 30_000


# ==================================================================== data helpers

def load_board():
    r = subprocess.run([PY, str(TOOLS_DIR / "work.py"), "board", "--json"],
                       cwd=str(REPO), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "work.py board --json failed")
    return json.loads(r.stdout)


def load_task_text(task_id):
    for sub in ("tasks", "backlog"):
        p = WORK / sub / f"{task_id}.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def parse_dod(text):
    """Return [(checked, label), ...] for '## Definition of done' checkboxes."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Definition of done")
    except StopIteration:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    items = []
    for i in range(start, end):
        m = re.match(r"^\s*- \[([ xX])\]\s*(.*)", lines[i])
        if m:
            items.append((m.group(1).lower() == "x", m.group(2)))
    return items


def parse_progress_log(text):
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Progress log")
    except StopIteration:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [l for l in lines[start + 1:end] if l.strip().startswith("- ")]


def unanswered_questions():
    """[(stem, open_count), ...] for work/questions/*.md with any '## Qn (open)'."""
    d = WORK / "questions"
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            if p.name == "README.md":
                continue
            text = p.read_text(encoding="utf-8")
            opens = re.findall(r"^## Q\d+ \(open\)", text, re.MULTILINE)
            if opens:
                out.append((p.stem, len(opens)))
    return out


# ==================================================================== App

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI-CAD — Team Progress (read-only)")
        self.geometry("1200x780")
        self.q = queue.Queue()
        self.tasks = []
        self.tasks_by_id = {}
        self.selected_task = None
        self._build_ui()
        self.after(150, self._poll_queue)
        self.refresh_all()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    # ---------------------------------------------------------------- shell

    def _build_ui(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Label(bar, text="Display-only — to change the plan, ask an AI driver in Claude "
                            "Code or let autopilot decide.", foreground="#666").pack(side="left")
        ttk.Button(bar, text="Refresh now", command=self.refresh_all).pack(side="right")
        self.status_var = tk.StringVar(value="Loading…")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555").pack(side="right", padx=10)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)

        self.roadblocks_frame = ttk.Frame(self.nb)
        self.dag_frame = ttk.Frame(self.nb)
        self.graphify_frame = ttk.Frame(self.nb)
        self.nb.add(self.roadblocks_frame, text="Human Roadblocks")
        self.nb.add(self.dag_frame, text="DAG")
        self.nb.add(self.graphify_frame, text="Graphify Preview")

        self._build_roadblocks_tab()
        self._build_dag_tab()
        self._build_graphify_tab()

    # ---------------------------------------------------------------- refresh / threading

    def refresh_all(self):
        self.status_var.set("Loading…")
        threading.Thread(target=self._load_board_worker, daemon=True).start()

    def _auto_refresh(self):
        self.refresh_all()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _load_board_worker(self):
        try:
            self.q.put(("board_loaded", (load_board(), None)))
        except Exception as e:
            self.q.put(("board_loaded", ([], str(e))))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "board_loaded":
                    tasks, err = payload
                    if err:
                        self.status_var.set(f"board load error: {err}")
                    else:
                        self.tasks = tasks
                        self.tasks_by_id = {t["id"]: t for t in tasks}
                        self.render_dag()
                        self.render_roadblocks()
                        self.refresh_graphify_freshness()
                        self.status_var.set(f"loaded {len(tasks)} tasks")
                elif kind == "graphify_result":
                    self._show_graphify_result(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    # ---------------------------------------------------------------- tab 1: Human Roadblocks

    def _build_roadblocks_tab(self):
        f = self.roadblocks_frame
        ttk.Label(f, text="Everything here is blocked on a human right now — check this tab first.",
                  font=("TkDefaultFont", 10, "italic")).pack(anchor="w", padx=8, pady=6)
        self.roadblocks_text = scrolledtext.ScrolledText(f, wrap="word", state="disabled")
        self.roadblocks_text.pack(fill="both", expand=True, padx=8, pady=4)

    def render_roadblocks(self):
        lines = []
        review = [t for t in self.tasks if t.get("status") == "review"]
        main_checkout = [t for t in self.tasks
                         if t.get("owner") == "main-checkout" and t.get("status") != "done"]
        blocked = [t for t in self.tasks if t.get("status") == "blocked"]
        questions = unanswered_questions()

        if review:
            lines.append(f"AWAITING MERGE ({len(review)}) — pushed, ready for human review:")
            lines += [f"  {t['id']:>8}  [{t.get('owner')}]  {t['title']}" for t in review]
            lines.append("")
        if questions:
            lines.append(f"OPEN DRIVER QUESTIONS ({len(questions)}) — need advisor/human answer:")
            lines += [f"  work/questions/{stem}.md  ({n} open question(s))" for stem, n in questions]
            lines.append("")
        if main_checkout:
            lines.append(f"HUMAN-ONLY TASKS ({len(main_checkout)}) — owner: main-checkout:")
            lines += [f"  {t['id']:>8}  {t.get('status')}  {t['title']}" for t in main_checkout]
            lines.append("")
        if blocked:
            lines.append(f"BLOCKED TASKS ({len(blocked)}):")
            lines += [f"  {t['id']:>8}  [{t.get('owner')}]  {t['title']}" for t in blocked]
            lines.append("")
        if not lines:
            lines = ["Nothing is currently blocked on a human."]

        self.roadblocks_text.config(state="normal")
        self.roadblocks_text.delete("1.0", "end")
        self.roadblocks_text.insert("1.0", "\n".join(lines))
        self.roadblocks_text.config(state="disabled")

    # ---------------------------------------------------------------- tab 2: DAG

    def _build_dag_tab(self):
        f = self.dag_frame
        ttk.Label(f, text="Legend: grey = locked (unmet deps, or awaiting human review). "
                          "Click a node for details.").pack(anchor="w", padx=4, pady=2)

        body = ttk.Frame(f)
        body.pack(fill="both", expand=True)
        canvas_frame = ttk.Frame(body)
        canvas_frame.pack(side="left", fill="both", expand=True)
        self.dag_canvas = tk.Canvas(canvas_frame, background="white")
        hbar = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.dag_canvas.xview)
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.dag_canvas.yview)
        self.dag_canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.dag_canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)

        side = ttk.Frame(body, width=320)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        ttk.Label(side, text="Task details", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        self.dag_detail = scrolledtext.ScrolledText(side, wrap="word", state="disabled", height=30)
        self.dag_detail.pack(fill="both", expand=True, padx=8, pady=4)
        self._set_dag_detail("Click a task node to see its details.")

    def _set_dag_detail(self, text):
        self.dag_detail.config(state="normal")
        self.dag_detail.delete("1.0", "end")
        self.dag_detail.insert("1.0", text)
        self.dag_detail.config(state="disabled")

    def _node_color(self, t):
        status = t.get("status", "todo")
        deps = t.get("depends_on", [])
        deps_done = all(self.tasks_by_id.get(d, {}).get("status") == "done" for d in deps)
        if status == "todo" and not deps_done:
            return LOCKED_COLOR
        if status == "review":
            return LOCKED_COLOR
        return STATUS_COLORS.get(status, STATUS_COLORS["todo"])

    def render_dag(self):
        c = self.dag_canvas
        c.delete("all")
        NODE_W, NODE_H = 190, 46
        COL_GAP, ROW_GAP = 60, 16
        by_ws = {}
        for t in self.tasks:
            by_ws.setdefault(t.get("workstream", "?"), []).append(t)
        cols = sorted(by_ws)

        positions, col_headers, max_rows = {}, [], 0
        for ci, ws in enumerate(cols):
            x0 = 20 + ci * (NODE_W + COL_GAP)
            col_headers.append((x0 + NODE_W / 2, ws))
            rows = sorted(by_ws[ws], key=lambda x: (str(x.get("day", "99")), x["id"]))
            max_rows = max(max_rows, len(rows))
            for ri, t in enumerate(rows):
                y0 = 36 + ri * (NODE_H + ROW_GAP)
                positions[t["id"]] = (x0, y0, x0 + NODE_W, y0 + NODE_H, t)

        for t in self.tasks:
            for dep in t.get("depends_on", []):
                if dep in positions and t["id"] in positions:
                    dx0, dy0, dx1, dy1, _ = positions[dep]
                    x0, y0, x1, y1, _ = positions[t["id"]]
                    c.create_line((dx0 + dx1) / 2, dy1, (x0 + x1) / 2, y0,
                                  arrow=tk.LAST, fill="#aaaaaa", width=1)

        for cx, ws in col_headers:
            c.create_text(cx, 14, text=f"workstream {ws}", font=("TkDefaultFont", 10, "bold"))

        for tid, (x0, y0, x1, y1, t) in positions.items():
            fill, outline = self._node_color(t)
            rect = c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=2)
            label = f"{t['id']}  {t.get('status', '?')}\n[{t.get('owner', '?')}]\n{t['title'][:32]}"
            txt = c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                                width=NODE_W - 10, font=("TkDefaultFont", 8))
            c.tag_bind(rect, "<Button-1>", lambda e, tid=tid: self.on_select_node(tid))
            c.tag_bind(txt, "<Button-1>", lambda e, tid=tid: self.on_select_node(tid))

        width = 20 + len(cols) * (NODE_W + COL_GAP) + 40
        height = 36 + max_rows * (NODE_H + ROW_GAP) + 40
        c.configure(scrollregion=(0, 0, max(width, 400), max(height, 300)))

        if self.selected_task and self.selected_task in self.tasks_by_id:
            self.on_select_node(self.selected_task)

    def on_select_node(self, task_id):
        self.selected_task = task_id
        t = self.tasks_by_id.get(task_id)
        if not t:
            return
        deps = t.get("depends_on", [])
        deps_done = all(self.tasks_by_id.get(d, {}).get("status") == "done" for d in deps)
        locked = (t.get("status") == "todo" and not deps_done) or t.get("status") == "review"
        lines = [
            t["id"], t["title"], "",
            f"status:      {t.get('status')}",
            f"owner:       {t.get('owner')}",
            f"workstream:  {t.get('workstream')}",
            f"day:         {t.get('day', '-')}",
            f"branch:      {t.get('branch', '-')}",
            f"depends_on:  {', '.join(deps) or '(none)'}",
        ]
        if locked:
            reason = "awaiting human merge" if t.get("status") == "review" else \
                     "blocked on: " + ", ".join(d for d in deps
                                                if self.tasks_by_id.get(d, {}).get("status") != "done")
            lines += ["", f"LOCKED — {reason}"]
        self._set_dag_detail("\n".join(lines))

    # ---------------------------------------------------------------- tab 3: Graphify Preview

    def _build_graphify_tab(self):
        f = self.graphify_frame
        self.graphify_fresh_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.graphify_fresh_var, foreground="#a55",
                  wraplength=1100).pack(anchor="w", padx=8, pady=(6, 0))

        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Query:").pack(side="left")
        self.graphify_q_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.graphify_q_var, width=70)
        entry.pack(side="left", padx=4, fill="x", expand=True)
        entry.bind("<Return>", lambda e: self._on_graphify_query())
        ttk.Button(row, text="Query", command=self._on_graphify_query).pack(side="left", padx=2)
        ttk.Button(row, text="Explain", command=self._on_graphify_explain).pack(side="left", padx=2)

        self.graphify_out = scrolledtext.ScrolledText(f, wrap="word", state="disabled")
        self.graphify_out.pack(fill="both", expand=True, padx=8, pady=4)

    def refresh_graphify_freshness(self):
        note = _teamlib.graphify_freshness(REPO)
        self.graphify_fresh_var.set(note.strip() if note else "")

    def _on_graphify_query(self):
        self._graphify_run(["query", self.graphify_q_var.get().strip()])

    def _on_graphify_explain(self):
        self._graphify_run(["explain", self.graphify_q_var.get().strip()])

    def _graphify_run(self, args):
        if not args[-1]:
            return
        threading.Thread(target=self._graphify_worker, args=(args,), daemon=True).start()

    def _graphify_worker(self, args):
        if shutil.which("graphify") is None:
            self.q.put(("graphify_result", "graphify CLI not on PATH — see README.md setup."))
            return
        try:
            r = subprocess.run(["graphify", *args], cwd=str(REPO), capture_output=True, text=True, timeout=60)
            out = r.stdout.strip() or r.stderr.strip() or "(no output)"
        except Exception as e:
            out = f"error: {e}"
        self.q.put(("graphify_result", out))

    def _show_graphify_result(self, text):
        self.refresh_graphify_freshness()
        self.graphify_out.config(state="normal")
        self.graphify_out.delete("1.0", "end")
        self.graphify_out.insert("1.0", text)
        self.graphify_out.config(state="disabled")


def main():
    try:
        app = App()
    except tk.TclError as e:
        print(f"Could not open a GUI window ({e}). Run this on your desktop session, "
              f"not a headless/SSH shell.", file=sys.stderr)
        sys.exit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
