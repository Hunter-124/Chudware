#!/usr/bin/env python3
"""issue_reporter.py — AI-guided issue/idea intake GUI for AI-CAD contributors.

Tkinter (stdlib only — no pip installs needed). Draft a bug/idea report, optionally
ask the local `claude` CLI (headless `claude -p`, researching the repo itself) to
elaborate it into a well-scoped report, then file it in one step as a linked GitHub
issue + a work/backlog task so it actually enters the plan instead of getting lost
in chat history. A live graphify preview panel helps you ground the report in real
files before asking the AI.

This is internal dev-team tooling for BUILDING AI-CAD — separate from the product's
own MCP server (services/mcp/) and agent loop. It never touches product code.

Run:  python3 work/tools/issue_reporter.py   (or work/report.sh / work/report.bat)
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

SEVERITIES = ["low", "medium", "high", "critical"]
WORKSTREAMS = ["A", "B", "C", "D", "E", "F", "G", "integration", "backlog"]

ELABORATION_PROMPT = """You are helping a contributor to the AI-CAD repo turn a rough bug/idea report into a well-scoped, actionable one. Research the repo as needed (use `graphify query`/`graphify explain` first per this repo's own CLAUDE.md policy, then targeted grep/read) to ground your answer in real files — do not guess.

Draft report from the contributor:
---
Title: {title}
Description: {description}
Steps to reproduce: {repro}
Contributor's severity guess: {severity}
{extra}
---

Investigate enough to sanity-check the report against the actual code, then respond with ONLY a single fenced ```json code block (nothing before or after it) matching exactly this schema:
{{
  "title": "concise, specific title",
  "root_cause_hypothesis": "1-3 sentences, or null if you can't tell without more info",
  "suspected_files": ["path1", "path2"],
  "refined_repro_steps": "cleaned-up numbered steps",
  "severity": "low|medium|high|critical",
  "workstream_guess": "A|B|C|D|E|F|G|integration|backlog",
  "clarifying_questions": ["question if you need more info to be confident, else empty list"],
  "placement": "github_issue+backlog_task|github_issue_only|needs_more_info",
  "notes": "anything else worth telling a human reviewer"
}}"""


def _next_backlog_id():
    nums = []
    for p in WORK.glob("backlog/T-BL*.md"):
        m = re.match(r"T-BL(\d+)", p.stem)
        if m:
            nums.append(int(m.group(1)))
    return f"T-BL{(max(nums) + 1) if nums else 1:02d}"


def run_claude_elaborate(draft, extra_context="", timeout=180):
    """Blocking — call from a worker thread. Returns (parsed_dict_or_None, raw_text)."""
    if shutil.which("claude") is None:
        return None, ("claude CLI not found on PATH — install Claude Code, or just "
                       "edit the fields above and Submit without AI elaboration.")
    prompt = ELABORATION_PROMPT.format(
        title=draft["title"], description=draft["description"],
        repro=draft["repro"], severity=draft["severity"],
        extra=("Additional context from a follow-up round:\n" + extra_context) if extra_context else "")
    try:
        r = subprocess.run(["claude", "-p", prompt], cwd=str(REPO),
                            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"claude -p timed out after {timeout}s — try again or fill in manually."
    out = r.stdout or ""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", out, re.DOTALL)
    if not blocks:
        return None, out.strip() or (r.stderr or "").strip() or "(no output from claude -p)"
    try:
        return json.loads(blocks[-1]), out
    except json.JSONDecodeError as e:
        return None, f"AI returned invalid JSON ({e}):\n\n{out}"


def submit_report(final):
    """Blocking — call from a worker thread. Files GH issue + backlog task + DAG
    regen + team broadcast. Returns (summary_text, task_id, issue_url_or_None)."""
    lines = []
    body = "\n".join([
        final.get("description_full") or "",
        "",
        f"**Root cause hypothesis:** {final.get('root_cause_hypothesis') or '(none)'}",
        f"**Suspected files:** {', '.join(final.get('suspected_files') or []) or '(none)'}",
        "",
        "**Steps to reproduce:**",
        final.get("refined_repro_steps") or "(not provided)",
        *(["", f"**Notes:** {final['notes']}"] if final.get("notes") else []),
        "",
        "_Filed via work/tools/issue_reporter.py_",
    ])
    labels = [f"severity:{final.get('severity') or 'medium'}"]

    issue_url = None
    if shutil.which("gh") is None:
        lines.append("gh CLI not found — skipped GitHub issue, filing backlog task only.")
    else:
        gh_args = ["gh", "issue", "create", "--title", final["title"], "--body", body]
        for lbl in labels:
            gh_args += ["--label", lbl]
        r = subprocess.run(gh_args, cwd=str(REPO), capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            lines.append(f"gh issue create FAILED: {r.stderr.strip()}")
        else:
            issue_url = r.stdout.strip()
            lines.append(f"GitHub issue filed: {issue_url}")

    tid = _next_backlog_id()
    ws = final.get("workstream_guess") if final.get("workstream_guess") in WORKSTREAMS else "backlog"
    slug = re.sub(r"[^a-z0-9]+", "-", final["title"].lower()).strip("-")[:40] or "issue"
    task_md = f"""---
id: {tid}
title: {final['title']}
workstream: backlog
owner: unassigned
status: backlog
branch: task/{tid}-{slug}
depends_on: []
---

## Context

{final.get('root_cause_hypothesis') or final.get('description_full') or '(see linked issue)'}

Reported via issue_reporter.py{f' — filed as {issue_url}' if issue_url else ''}.
Suggested workstream: {ws} (AI guess — human should confirm before scheduling).

## Definition of done

- [ ] Root cause confirmed
- [ ] Fix implemented and verified
- [ ] Linked GitHub issue closed
"""
    (WORK / "backlog" / f"{tid}.md").write_text(task_md, encoding="utf-8")
    lines.append(f"Backlog task written: work/backlog/{tid}.md")

    dag = subprocess.run([PY, str(TOOLS_DIR / "work.py"), "dag"], cwd=str(REPO),
                          capture_output=True, text=True, timeout=30)
    lines.append("DAG regenerated." if dag.returncode == 0
                 else f"DAG regen warning: {dag.stderr.strip()}")

    msg = (f"New issue filed: {tid} \"{final['title']}\" "
           f"({issue_url or 'no gh'}) — check before starting overlapping work")
    sync = subprocess.run([PY, str(TOOLS_DIR / "teamsync.py"), "send",
                           "--agent", "issue-reporter", "--text", msg],
                          cwd=str(REPO), capture_output=True, text=True, timeout=30)
    lines.append("Team notified." if sync.returncode == 0
                 else f"Team notify warning: {sync.stderr.strip()}")

    return "\n".join(lines), tid, issue_url


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI-CAD — Report an Issue / Idea")
        self.geometry("900x760")
        self.result_queue = queue.Queue()
        self._build_ui()
        self.after(150, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(frm, text="Title").grid(row=0, column=0, sticky="w", **pad)
        self.title_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.title_var, width=90).grid(
            row=0, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(frm, text="Description").grid(row=1, column=0, sticky="nw", **pad)
        self.desc_txt = scrolledtext.ScrolledText(frm, width=90, height=6, wrap="word")
        self.desc_txt.grid(row=1, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(frm, text="Steps to reproduce").grid(row=2, column=0, sticky="nw", **pad)
        self.repro_txt = scrolledtext.ScrolledText(frm, width=90, height=4, wrap="word")
        self.repro_txt.grid(row=2, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(frm, text="Severity").grid(row=3, column=0, sticky="w", **pad)
        self.severity_var = tk.StringVar(value="medium")
        ttk.OptionMenu(frm, self.severity_var, "medium", *SEVERITIES).grid(
            row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Not sure where this lives? Preview graphify:").grid(
            row=4, column=0, sticky="w", **pad)
        self.graphify_q = tk.StringVar()
        ttk.Entry(frm, textvariable=self.graphify_q, width=60).grid(
            row=4, column=1, sticky="we", **pad)
        ttk.Button(frm, text="Preview", command=self._on_graphify_preview).grid(
            row=4, column=2, sticky="w", **pad)
        self.graphify_out = scrolledtext.ScrolledText(
            frm, width=90, height=5, wrap="word", state="disabled", background="#f4f4f4")
        self.graphify_out.grid(row=5, column=0, columnspan=4, sticky="we", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=4, sticky="we", pady=(10, 4))
        self.elaborate_btn = ttk.Button(btns, text="Ask AI to elaborate",
                                        command=self._on_elaborate)
        self.elaborate_btn.pack(side="left", padx=4)
        self.submit_btn = ttk.Button(
            btns, text="Submit  (files GitHub issue + backlog task)",
            command=self._on_submit)
        self.submit_btn.pack(side="left", padx=4)

        ttk.Label(frm, text="AI elaboration — review/edit the JSON before submitting "
                            "(or leave blank to submit from the form fields above):"
                  ).grid(row=7, column=0, columnspan=4, sticky="w", **pad)
        self.elab_txt = scrolledtext.ScrolledText(frm, width=90, height=14, wrap="word")
        self.elab_txt.grid(row=8, column=0, columnspan=4, sticky="nswe", **pad)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status_var, foreground="#555").grid(
            row=9, column=0, columnspan=4, sticky="w", **pad)

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(8, weight=1)

    # ---------------------------------------------------------- graphify preview
    def _on_graphify_preview(self):
        q = self.graphify_q.get().strip()
        if not q:
            return
        self._set_status("Querying graphify…")
        threading.Thread(target=self._graphify_worker, args=(q,), daemon=True).start()

    def _graphify_worker(self, q):
        if shutil.which("graphify") is None:
            self.result_queue.put(("graphify", "graphify CLI not on PATH — see README.md setup."))
            return
        try:
            r = subprocess.run(["graphify", "query", q], cwd=str(REPO),
                               capture_output=True, text=True, timeout=60)
            out = r.stdout.strip() or r.stderr.strip() or "(no output)"
        except Exception as e:
            out = f"error: {e}"
        self.result_queue.put(("graphify", out))

    # ---------------------------------------------------------- AI elaboration
    def _on_elaborate(self):
        title = self.title_var.get().strip()
        desc = self.desc_txt.get("1.0", "end").strip()
        if not title or not desc:
            messagebox.showwarning("Missing info", "Fill in at least a title and description first.")
            return
        draft = {"title": title, "description": desc,
                 "repro": self.repro_txt.get("1.0", "end").strip(),
                 "severity": self.severity_var.get()}
        self.elaborate_btn.config(state="disabled")
        self._set_status("Asking claude -p to research and elaborate this (can take a minute)…")
        threading.Thread(target=self._elaborate_worker, args=(draft,), daemon=True).start()

    def _elaborate_worker(self, draft):
        parsed, raw = run_claude_elaborate(draft)
        self.result_queue.put(("elaborate", (parsed, raw)))

    # ---------------------------------------------------------- submit
    def _on_submit(self):
        text = self.elab_txt.get("1.0", "end").strip()
        final = None
        if text.startswith("{"):
            try:
                final = json.loads(text)
            except json.JSONDecodeError:
                messagebox.showwarning(
                    "Invalid JSON",
                    "The elaboration box looks like JSON but doesn't parse. Fix it, "
                    "or clear the box to submit from the form fields instead.")
                return
        if final is None:
            title = self.title_var.get().strip()
            if not title:
                messagebox.showwarning("Missing title", "Need a title before filing.")
                return
            final = {
                "title": title,
                "description_full": self.desc_txt.get("1.0", "end").strip(),
                "refined_repro_steps": self.repro_txt.get("1.0", "end").strip(),
                "severity": self.severity_var.get(),
                "suspected_files": [],
                "workstream_guess": "backlog",
            }
        if not final.get("title"):
            messagebox.showwarning("Missing title", "Need a title before filing.")
            return
        final.setdefault("description_full", self.desc_txt.get("1.0", "end").strip())
        if not messagebox.askyesno(
                "Confirm", f"File \"{final['title']}\" as a GitHub issue + backlog task now?"):
            return
        self.submit_btn.config(state="disabled")
        self._set_status("Filing GitHub issue + backlog task…")
        threading.Thread(target=self._submit_worker, args=(final,), daemon=True).start()

    def _submit_worker(self, final):
        try:
            result, tid, url = submit_report(final)
        except Exception as e:
            result, tid, url = f"FAILED: {e}", None, None
        self.result_queue.put(("submit", (result, tid, url)))

    # ---------------------------------------------------------- worker->UI marshal
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "graphify":
                    self.graphify_out.config(state="normal")
                    self.graphify_out.delete("1.0", "end")
                    self.graphify_out.insert("1.0", payload)
                    self.graphify_out.config(state="disabled")
                    self._set_status("Ready.")
                elif kind == "elaborate":
                    parsed, raw = payload
                    self.elaborate_btn.config(state="normal")
                    self.elab_txt.delete("1.0", "end")
                    if parsed:
                        self.elab_txt.insert("1.0", json.dumps(parsed, indent=2))
                        cq = parsed.get("clarifying_questions") or []
                        if cq:
                            self._set_status(
                                "AI has clarifying questions in the JSON's "
                                "clarifying_questions field — answer them there (or above) "
                                "and re-run elaborate, or edit directly and Submit.")
                        else:
                            self._set_status("Elaboration ready — review/edit the JSON below, then Submit.")
                    else:
                        self.elab_txt.insert("1.0", raw)
                        self._set_status(
                            "AI elaboration unavailable/unparseable (shown above) — "
                            "clear the box and Submit still works from your form fields.")
                elif kind == "submit":
                    result, tid, url = payload
                    self.submit_btn.config(state="normal")
                    self._set_status(result.splitlines()[0] if result else "Done.")
                    messagebox.showinfo("Filed", result)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _set_status(self, text):
        self.status_var.set(text)


def main():
    try:
        app = App()
    except tk.TclError as e:
        print(f"Could not open a GUI window ({e}). Is a display available? "
              f"Run this on your desktop session, not a headless/SSH shell.", file=sys.stderr)
        sys.exit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
