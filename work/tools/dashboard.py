#!/usr/bin/env python3
"""dashboard.py — team-progress dashboard for AI-CAD contributors.

Tkinter (stdlib only). Four tabs:
  1. Human Roadblocks — everything blocked on a human right now (top priority).
  2. DAG — the master task graph, color-coded, locked/greyed where blocked or
     awaiting human review; click a node for safe actions (claim/note/status/
     release) plus full depends_on/workstream editing (cycle-validated by
     work.py relink — never writes task files directly).
  3. Graphify Preview — live query/explain with a staleness banner.
  4. Verify — a task's Definition-of-done checklist + scripts/gate.sh runner +
     progress log. Purely deterministic, no AI call from this tab.

This is internal dev-team tooling for BUILDING AI-CAD — separate from the
product's own MCP server (services/mcp/) and agent loop. All task-file writes
go through work/tools/work.py's guarded commands (claim/note/status/release/
relink/checkdod), never direct file edits, so this GUI can't corrupt the DAG
or race with other contributors.

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
    from tkinter import messagebox, scrolledtext, simpledialog, ttk
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

# Mirrors work.py's ALLOWED_TRANSITIONS (work.py:322) — kept in sync by hand;
# work.py itself is still the enforcement point, this only drives the dropdown.
ALLOWED_TRANSITIONS = {
    "doing": ["review", "blocked"],
    "blocked": ["doing"],
    "review": ["doing"],
}


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
            return p, p.read_text(encoding="utf-8")
    return None, ""


def parse_dod(text):
    """Return [(index, checked, label), ...] for '## Definition of done' checkboxes."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Definition of done")
    except StopIteration:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    items, idx = [], 0
    for i in range(start, end):
        m = re.match(r"^\s*- \[([ xX])\]\s*(.*)", lines[i])
        if m:
            items.append((idx, m.group(1).lower() == "x", m.group(2)))
            idx += 1
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
        self.title("AI-CAD — Team Progress Dashboard")
        self.geometry("1250x820")
        self.q = queue.Queue()
        self.tasks = []
        self.tasks_by_id = {}
        self.selected_task = None
        self._build_ui()
        self.after(150, self._poll_queue)
        self.refresh_all()

    # ---------------------------------------------------------------- shell

    def _build_ui(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Label(bar, text="Identity:").pack(side="left")
        self.identity_var = tk.StringVar(value=_teamlib.get_identity() or "(not set)")
        ttk.Label(bar, textvariable=self.identity_var, foreground="#0a6").pack(side="left", padx=(2, 10))
        ttk.Button(bar, text="Set identity…", command=self._on_set_identity).pack(side="left")
        ttk.Button(bar, text="Refresh all", command=self.refresh_all).pack(side="right")
        self.status_var = tk.StringVar(value="Loading…")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555").pack(side="right", padx=10)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)

        self.roadblocks_frame = ttk.Frame(self.nb)
        self.dag_frame = ttk.Frame(self.nb)
        self.graphify_frame = ttk.Frame(self.nb)
        self.verify_frame = ttk.Frame(self.nb)
        self.nb.add(self.roadblocks_frame, text="Human Roadblocks")
        self.nb.add(self.dag_frame, text="DAG")
        self.nb.add(self.graphify_frame, text="Graphify Preview")
        self.nb.add(self.verify_frame, text="Verify")

        self._build_roadblocks_tab()
        self._build_dag_tab()
        self._build_graphify_tab()
        self._build_verify_tab()

    def _on_set_identity(self):
        cur = _teamlib.get_identity() or ""
        name = simpledialog.askstring("Set identity", "Your name (used to attribute claims/notes):",
                                      initialvalue=cur, parent=self)
        if name and name.strip():
            _teamlib.set_identity(name.strip())
            self.identity_var.set(name.strip())

    def _require_identity(self):
        ident = _teamlib.get_identity()
        if not ident:
            messagebox.showwarning("No identity", "Set your identity first (top-left) so actions "
                                                   "can be attributed to you.")
            return None
        return ident

    # ---------------------------------------------------------------- refresh / threading

    def refresh_all(self):
        self.identity_var.set(_teamlib.get_identity() or "(not set)")
        self.status_var.set("Loading…")
        threading.Thread(target=self._load_board_worker, daemon=True).start()

    def _load_board_worker(self):
        try:
            self.q.put(("board_loaded", (load_board(), None)))
        except Exception as e:
            self.q.put(("board_loaded", ([], str(e))))

    def _run_threaded(self, args, on_done):
        self.status_var.set("Working…")
        threading.Thread(target=self._threaded_worker, args=(args, on_done), daemon=True).start()

    def _threaded_worker(self, args, on_done):
        try:
            r = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, timeout=60)
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
            ok = r.returncode == 0
        except Exception as e:
            out, ok = str(e), False
        self.q.put(("action_done", (on_done, out.strip(), ok)))

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
                        self._populate_verify_selector()
                        self.refresh_graphify_freshness()
                        self.status_var.set(f"loaded {len(tasks)} tasks")
                elif kind == "action_done":
                    on_done, out, ok = payload
                    on_done(out, ok)
                elif kind == "graphify_result":
                    self._show_graphify_result(payload)
                elif kind == "gate_output":
                    self._append_gate_output(payload)
                elif kind == "gate_done":
                    self._append_gate_output(f"\n--- gate.sh exited {payload} ---\n")
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
                          "Click a node for actions.").pack(anchor="w", padx=4, pady=2)

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

        self.dag_side = ttk.Frame(body, width=340)
        self.dag_side.pack(side="right", fill="y")
        self.dag_side.pack_propagate(False)
        self.node_boxes = {}
        self._dag_side_empty()

    def _dag_side_empty(self):
        for w in self.dag_side.winfo_children():
            w.destroy()
        ttk.Label(self.dag_side, text="Click a task node to see actions.",
                  wraplength=320).pack(padx=8, pady=8)

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

        self.node_boxes = {}
        for tid, (x0, y0, x1, y1, t) in positions.items():
            fill, outline = self._node_color(t)
            rect = c.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=2)
            label = f"{t['id']}  {t.get('status', '?')}\n[{t.get('owner', '?')}]\n{t['title'][:32]}"
            txt = c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                                width=NODE_W - 10, font=("TkDefaultFont", 8))
            self.node_boxes[tid] = (x0, y0, x1, y1)
            c.tag_bind(rect, "<Button-1>", lambda e, tid=tid: self.on_select_node(tid))
            c.tag_bind(txt, "<Button-1>", lambda e, tid=tid: self.on_select_node(tid))

        width = 20 + len(cols) * (NODE_W + COL_GAP) + 40
        height = 36 + max_rows * (NODE_H + ROW_GAP) + 40
        c.configure(scrollregion=(0, 0, max(width, 400), max(height, 300)))

        if self.selected_task and self.selected_task in self.tasks_by_id:
            self._render_dag_side_panel(self.selected_task)
        else:
            self._dag_side_empty()

    def on_select_node(self, task_id):
        self.selected_task = task_id
        self._render_dag_side_panel(task_id)

    def _render_dag_side_panel(self, task_id):
        for w in self.dag_side.winfo_children():
            w.destroy()
        t = self.tasks_by_id.get(task_id)
        if not t:
            return
        pad = {"padx": 6, "pady": 3}
        side = self.dag_side
        ttk.Label(side, text=t["id"], font=("TkDefaultFont", 12, "bold")).pack(anchor="w", **pad)
        ttk.Label(side, text=t["title"], wraplength=320).pack(anchor="w", **pad)
        ttk.Label(side, text=f"status: {t.get('status')}   owner: {t.get('owner')}").pack(anchor="w", **pad)
        ttk.Label(side, text=f"workstream: {t.get('workstream')}   day: {t.get('day', '-')}").pack(anchor="w", **pad)

        deps = t.get("depends_on", [])
        deps_done = all(self.tasks_by_id.get(d, {}).get("status") == "done" for d in deps)
        locked = (t.get("status") == "todo" and not deps_done) or t.get("status") == "review"
        if locked:
            reason = "awaiting human merge" if t.get("status") == "review" else \
                     "blocked on: " + ", ".join(d for d in deps
                                                if self.tasks_by_id.get(d, {}).get("status") != "done")
            ttk.Label(side, text=f"locked — {reason}", foreground="#a55", wraplength=320).pack(anchor="w", **pad)

        ttk.Separator(side).pack(fill="x", pady=6)
        ttk.Label(side, text="Safe actions", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", **pad)

        actions = ttk.Frame(side)
        actions.pack(fill="x", **pad)
        can_claim = (t.get("status") == "todo" and deps_done
                    and t.get("owner", "unassigned") in ("unassigned", "", "none"))
        claim_btn = ttk.Button(actions, text="Claim", command=lambda: self._act_claim(task_id))
        claim_btn.pack(side="left", padx=2)
        if not can_claim:
            claim_btn.state(["disabled"])

        is_mine = t.get("owner") == (_teamlib.get_identity() or "")
        release_btn = ttk.Button(actions, text="Release", command=lambda: self._act_release(task_id))
        release_btn.pack(side="left", padx=2)
        if not is_mine:
            release_btn.state(["disabled"])

        ttk.Label(side, text="Note:").pack(anchor="w", **pad)
        note_entry = tk.Text(side, height=3, width=36)
        note_entry.pack(padx=6)
        ttk.Button(side, text="Add note",
                  command=lambda: self._act_note(task_id, note_entry.get("1.0", "end").strip())
                  ).pack(anchor="w", **pad)

        allowed = ALLOWED_TRANSITIONS.get(t.get("status"), [])
        if allowed:
            ttk.Label(side, text="Set status:").pack(anchor="w", **pad)
            status_var = tk.StringVar(value=allowed[0])
            ttk.OptionMenu(side, status_var, allowed[0], *allowed).pack(anchor="w", padx=6)
            ttk.Button(side, text="Apply status",
                      command=lambda: self._act_status(task_id, status_var.get())).pack(anchor="w", **pad)

        ttk.Separator(side).pack(fill="x", pady=6)
        ttk.Label(side, text="Edit topology (relink)", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", **pad)
        ttk.Label(side, text="depends_on (ctrl/shift-click to multi-select):").pack(anchor="w", **pad)
        lb = tk.Listbox(side, selectmode="extended", height=8, exportselection=False)
        all_ids = sorted(self.tasks_by_id)
        for i, tid2 in enumerate(all_ids):
            lb.insert("end", tid2)
            if tid2 in deps:
                lb.selection_set(i)
        lb.pack(padx=6, fill="x")
        ttk.Label(side, text="workstream:").pack(anchor="w", **pad)
        ws_var = tk.StringVar(value=t.get("workstream", ""))
        ws_options = sorted({tt.get("workstream", "") for tt in self.tasks})
        ttk.Combobox(side, textvariable=ws_var, values=ws_options, width=20).pack(anchor="w", padx=6)
        ttk.Button(side, text="Save relink",
                  command=lambda: self._act_relink(task_id, [all_ids[i] for i in lb.curselection()],
                                                   ws_var.get())).pack(anchor="w", **pad)

    def _act_claim(self, task_id):
        ident = self._require_identity()
        if not ident:
            return
        self._run_threaded([PY, str(TOOLS_DIR / "work.py"), "claim", "--owner", ident, "--task", task_id],
                           self._after_mutation)

    def _act_release(self, task_id):
        ident = self._require_identity()
        if not ident:
            return
        self._run_threaded([PY, str(TOOLS_DIR / "work.py"), "release", task_id, "--owner", ident],
                           self._after_mutation)

    def _act_note(self, task_id, text):
        ident = self._require_identity()
        if not ident or not text:
            return
        self._run_threaded([PY, str(TOOLS_DIR / "work.py"), "note", task_id, "--owner", ident, "--text", text],
                           self._after_mutation)

    def _act_status(self, task_id, status):
        ident = self._require_identity()
        if not ident:
            return
        self._run_threaded([PY, str(TOOLS_DIR / "work.py"), "status", task_id, "--owner", ident,
                            "--status", status], self._after_mutation)

    def _act_relink(self, task_id, deps, workstream):
        ident = self._require_identity()
        if not ident:
            return
        args = [PY, str(TOOLS_DIR / "work.py"), "relink", task_id, "--owner", ident,
               "--depends-on", ",".join(deps)]
        if workstream:
            args += ["--workstream", workstream]
        self._run_threaded(args, self._after_mutation)

    def _after_mutation(self, out, ok):
        if not ok:
            messagebox.showerror("Action failed", out or "(no output)")
        else:
            messagebox.showinfo("Done", out or "(no output)")
        self.refresh_all()

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

    # ---------------------------------------------------------------- tab 4: Verify

    def _build_verify_tab(self):
        f = self.verify_frame
        row = ttk.Frame(f)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Task:").pack(side="left")
        self.verify_task_var = tk.StringVar()
        self.verify_combo = ttk.Combobox(row, textvariable=self.verify_task_var, state="readonly", width=50)
        self.verify_combo.pack(side="left", padx=4)
        self.verify_combo.bind("<<ComboboxSelected>>", lambda e: self._load_verify_task())
        ttk.Button(row, text="Run scripts/gate.sh", command=self._on_run_gate).pack(side="right", padx=4)

        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(body)
        ttk.Label(left, text="Definition of done", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.dod_frame = ttk.Frame(left)
        self.dod_frame.pack(fill="x", pady=4)
        ttk.Label(left, text="Progress log", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(10, 0))
        self.progress_text = scrolledtext.ScrolledText(left, wrap="word", height=10, state="disabled")
        self.progress_text.pack(fill="both", expand=True, pady=4)
        body.add(left, weight=1)

        right = ttk.Frame(body)
        ttk.Label(right, text="gate.sh output", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.gate_out = scrolledtext.ScrolledText(right, wrap="word", state="disabled")
        self.gate_out.pack(fill="both", expand=True, pady=4)
        body.add(right, weight=1)

    def _populate_verify_selector(self):
        ids = sorted(self.tasks_by_id)
        self.verify_combo["values"] = ids
        if ids and (not self.verify_task_var.get() or self.verify_task_var.get() not in self.tasks_by_id):
            self.verify_task_var.set(ids[0])
        if self.verify_task_var.get():
            self._load_verify_task()

    def _load_verify_task(self):
        tid = self.verify_task_var.get()
        if not tid:
            return
        _, text = load_task_text(tid)
        for w in self.dod_frame.winfo_children():
            w.destroy()
        items = parse_dod(text)
        if not items:
            ttk.Label(self.dod_frame, text="(no Definition of done checklist found)").pack(anchor="w")
        for idx, checked, label in items:
            var = tk.BooleanVar(value=checked)
            ttk.Checkbutton(self.dod_frame, text=label, variable=var,
                            command=lambda i=idx, v=var, t=tid: self._on_toggle_dod(t, i, v.get())
                            ).pack(anchor="w")

        log = parse_progress_log(text)
        self.progress_text.config(state="normal")
        self.progress_text.delete("1.0", "end")
        self.progress_text.insert("1.0", "\n".join(log) if log else "(no progress log entries)")
        self.progress_text.config(state="disabled")

    def _on_toggle_dod(self, task_id, index, checked):
        ident = self._require_identity()
        if not ident:
            self._load_verify_task()  # revert the checkbox visual since we're not proceeding
            return
        args = [PY, str(TOOLS_DIR / "work.py"), "checkdod", task_id, "--owner", ident,
               "--index", str(index), "--checked", "true" if checked else "false"]

        def done(out, ok):
            if not ok:
                messagebox.showerror("checkdod failed", out or "(no output)")
            self._load_verify_task()
            self.refresh_all()

        self._run_threaded(args, done)

    def _on_run_gate(self):
        if not messagebox.askyesno(
                "Run scripts/gate.sh?",
                "This runs the full build/test gate (core build, ctest, pytest, ui/imgui "
                "smoke) — can take several minutes and real CPU. Continue?"):
            return
        self.gate_out.config(state="normal")
        self.gate_out.delete("1.0", "end")
        self.gate_out.insert("1.0", "running scripts/gate.sh …\n")
        self.gate_out.config(state="disabled")
        threading.Thread(target=self._gate_worker, daemon=True).start()

    def _gate_worker(self):
        try:
            p = subprocess.Popen(["bash", "scripts/gate.sh"], cwd=str(REPO), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                self.q.put(("gate_output", line))
            p.wait()
            self.q.put(("gate_done", p.returncode))
        except Exception as e:
            self.q.put(("gate_output", f"error: {e}\n"))
            self.q.put(("gate_done", 1))

    def _append_gate_output(self, line):
        self.gate_out.config(state="normal")
        self.gate_out.insert("end", line)
        self.gate_out.see("end")
        self.gate_out.config(state="disabled")


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
