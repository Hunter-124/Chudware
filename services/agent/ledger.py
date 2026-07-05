"""Design Ledger — persistent mission state with budgeted rendering.

Schema: ``aicad.ledger/1`` (frozen in contracts/advisor.md §2).

The ledger is the anti-context-loss mechanism for Pillar 20: every agent role
cold-starts from ``ledger_render(budget_tokens)`` instead of chat history, making
24k-context local models viable across a whole mission.

Storage: ``<cache_root>/runs/<run_id>/ledger.json`` — a single JSON document:
``{"events": [...], "state": {...}}``.  Events are never mutated; state is
re-materialized from events on every ``append()``.  ``load()`` rebuilds state from
events alone, so replay is a full correctness guarantee.

Chars-per-token heuristic: ~4 chars/token (same constant used throughout the loop).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

# Same cache_root / run_dir logic as services/agent/interaction.py so both the
# interaction ledger and the design ledger live under <cache>/runs/<run_id>/.
from services.agent.interaction import cache_root

SCHEMA = "aicad.ledger/1"
_CHARS_PER_TOKEN = 4  # repo-wide heuristic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_ledger_path(run_id: str) -> Path:
    """Return the path to the ledger.json for *run_id* (creates parent dirs)."""
    d = cache_root() / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "ledger.json"


# ---------------------------------------------------------------------------
# State materializer — pure function, no I/O
# ---------------------------------------------------------------------------

def _empty_state(run_id: str) -> dict:
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "brief": {"title": "", "summary": "", "success_criteria": []},
        "requirements": [],
        "decisions": [],
        "dimensions": [],
        "questions": [],
        "stage_log": [],
        "artifacts": [],
    }


def _apply(state: dict, event: dict) -> dict:
    """Apply one event to a state dict (mutates in place, returns it)."""
    kind = event["kind"]
    payload = event.get("payload", {})

    if kind == "brief":
        state["brief"].update({k: v for k, v in payload.items()
                                if k in ("title", "summary", "success_criteria")})

    elif kind == "requirement":
        req_id = payload.get("req_id")
        existing = next((r for r in state["requirements"] if r["req_id"] == req_id), None)
        if existing:
            existing.update(payload)
        else:
            state["requirements"].append(dict(payload))

    elif kind == "decision":
        dec_id = payload.get("id")
        existing = next((d for d in state["decisions"] if d["id"] == dec_id), None)
        if existing:
            existing.update(payload)
        else:
            state["decisions"].append(dict(payload))

    elif kind == "dimension":
        # provenance stays in the event; only the frozen state fields go into state
        dim_id = payload.get("id")
        state_entry = {k: v for k, v in payload.items()
                       if k in ("id", "name", "value", "unit", "driver",
                                "tolerance", "status", "confirmed_by", "refs")}
        existing = next((d for d in state["dimensions"] if d["id"] == dim_id), None)
        if existing:
            existing.update(state_entry)
        else:
            state["dimensions"].append(state_entry)

    elif kind == "question":
        q_id = payload.get("id")
        existing = next((q for q in state["questions"] if q["id"] == q_id), None)
        if existing:
            existing.update(payload)
        else:
            state["questions"].append(dict(payload))

    elif kind == "stage":
        # Update the last open stage_log entry or append a new one
        stage = payload.get("stage")
        existing = next((s for s in state["stage_log"]
                         if s["stage"] == stage and s.get("exited") is None), None)
        if existing:
            existing.update(payload)
        else:
            entry = {"stage": stage, "entered": payload.get("entered", _now()),
                     "exited": payload.get("exited"), "exit_gate": payload.get("exit_gate")}
            state["stage_log"].append(entry)

    elif kind == "artifact":
        art_id = payload.get("id")
        existing = next((a for a in state["artifacts"] if a["id"] == art_id), None)
        if existing:
            existing.update(payload)
        else:
            state["artifacts"].append(dict(payload))

    return state


def _materialize(events: list[dict], run_id: str) -> dict:
    """Rebuild state from scratch by replaying all events — the replay guarantee."""
    state = _empty_state(run_id)
    for ev in events:
        _apply(state, ev)
    return state


# ---------------------------------------------------------------------------
# Ledger class
# ---------------------------------------------------------------------------

class Ledger:
    """Persistent design ledger for one run.

    Append-only event log + materialized state, stored in a single
    ``ledger.json``.  State is always a pure replay of events — never mutated
    directly.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path or run_ledger_path(run_id)
        self.events: list[dict] = []
        self.state: dict = _empty_state(run_id)
        if self.path.exists():
            self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.events = raw.get("events", [])
        # Always rematerialize from events (replay guarantee — ignore stored state)
        self.state = _materialize(self.events, self.run_id)

    def _save(self) -> None:
        """Atomic write: temp file + rename to avoid corruption."""
        doc = {"events": self.events, "state": self.state}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            # Best-effort — a failed save must not break the run
            pass

    # ------------------------------------------------------------------
    # Core append
    # ------------------------------------------------------------------

    def append(self, actor: str, kind: str, payload: dict) -> dict:
        """Record one event, rematerialize state, persist.  Returns the event."""
        event = {
            "seq": len(self.events),
            "ts": _now(),
            "actor": actor,
            "kind": kind,
            "payload": payload,
        }
        self.events.append(event)
        _apply(self.state, event)
        self._save()
        return event

    # ------------------------------------------------------------------
    # Typed upsert helpers
    # ------------------------------------------------------------------

    def set_brief(self, actor: str, title: str, summary: str,
                  success_criteria: list[str] | None = None) -> dict:
        return self.append(actor, "brief", {
            "title": title,
            "summary": summary,
            "success_criteria": success_criteria or [],
        })

    def add_requirement(self, actor: str, req_id: str, text: str,
                        source: str = "user",
                        status: str = "open") -> dict:
        return self.append(actor, "requirement", {
            "req_id": req_id, "text": text, "source": source, "status": status,
        })

    def add_decision(self, actor: str, decision_id: str, stage: str,
                     text: str, rationale: str) -> dict:
        return self.append(actor, "decision", {
            "id": decision_id, "stage": stage, "text": text,
            "rationale": rationale, "actor": actor, "ts": _now(),
        })

    def upsert_dimension(self, actor: str, dim_id: str, name: str,
                         value: float, unit: str, driver: str,
                         provenance: str,
                         tolerance: dict | None = None,
                         status: str = "proposed",
                         confirmed_by: str | None = None,
                         refs: dict | None = None) -> dict:
        """Append a dimension event.  ``provenance`` is required (contract rule)."""
        if not provenance:
            raise ValueError(
                f"dimension {dim_id!r}: provenance is required "
                "(must be: tool result / calc / table / user answer)"
            )
        return self.append(actor, "dimension", {
            "id": dim_id, "name": name, "value": value, "unit": unit,
            "driver": driver, "tolerance": tolerance or {},
            "status": status, "confirmed_by": confirmed_by,
            "refs": refs or {},
            # provenance lives in the event payload for audit; not in materialized state
            "provenance": provenance,
        })

    def add_question(self, actor: str, question_id: str, text: str,
                     blocking: bool = False) -> dict:
        return self.append(actor, "question", {
            "id": question_id, "text": text,
            "status": "open", "answer": None, "blocking": blocking,
        })

    def answer_question(self, actor: str, question_id: str, answer: str) -> dict:
        # Do NOT send text=None — that would null the stored question text via _apply's
        # existing.update(payload).  Only send the fields being changed.
        return self.append(actor, "question", {
            "id": question_id,
            "status": "answered", "answer": answer, "blocking": False,
        })

    def log_stage(self, actor: str, stage: str,
                  entered: str | None = None,
                  exited: str | None = None,
                  exit_gate: str | None = None) -> dict:
        return self.append(actor, "stage", {
            "stage": stage,
            "entered": entered or _now(),
            "exited": exited,
            "exit_gate": exit_gate,
        })

    def add_artifact(self, actor: str, kind: str, artifact_id: str,
                     path: str, stage: str, note: str = "") -> dict:
        return self.append(actor, "artifact", {
            "kind": kind, "id": artifact_id, "path": path,
            "stage": stage, "note": note,
        })

    # ------------------------------------------------------------------
    # Class-method loader
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, run_id: str, path: Path | None = None) -> "Ledger":
        """Load (or create) a ledger for *run_id*.  State is rebuilt from events."""
        return cls(run_id, path)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _section_lines(label: str, entries: list[str]) -> list[str]:
    if not entries:
        return []
    return [f"## {label}"] + entries + [""]


def _render_brief(brief: dict) -> list[str]:
    lines = ["## Mission Brief"]
    if brief.get("title"):
        lines.append(f"Title: {brief['title']}")
    if brief.get("summary"):
        lines.append(f"Summary: {brief['summary']}")
    sc = brief.get("success_criteria") or []
    if sc:
        lines.append("Success criteria:")
        for c in sc:
            lines.append(f"  - {c}")
    lines.append("")
    return lines


def _active_stage(state: dict) -> str | None:
    """Last stage_log entry with no exited timestamp, or None."""
    for entry in reversed(state.get("stage_log", [])):
        if not entry.get("exited"):
            return entry.get("stage")
    return None


def _render_dimensions(dimensions: list[dict], active_stage: str | None) -> list[str]:
    """Render dimensions, sorted by id for determinism."""
    if not dimensions:
        return []
    sorted_dims = sorted(dimensions, key=lambda d: d.get("id", ""))
    lines = ["## Dimensions"]
    for d in sorted_dims:
        tol = d.get("tolerance") or {}
        tol_str = ""
        if tol.get("fit"):
            tol_str = f" [{tol['fit']}]"
        elif tol.get("upper") is not None or tol.get("lower") is not None:
            tol_str = f" [+{tol.get('upper', 0)}/{tol.get('lower', 0)}]"
        confirmed = f" (confirmed by {d['confirmed_by']})" if d.get("confirmed_by") else ""
        lines.append(
            f"  {d.get('id','?')} {d.get('name','?')}: "
            f"{d.get('value','')} {d.get('unit','')}{tol_str} "
            f"[{d.get('status','?')}]{confirmed}"
        )
        if d.get("driver"):
            lines.append(f"    driver: {d['driver']}")
    lines.append("")
    return lines


def _render_questions(questions: list[dict]) -> list[str]:
    open_qs = sorted(
        [q for q in questions if q.get("status") == "open"],
        key=lambda q: q.get("id", "")
    )
    if not open_qs:
        return []
    lines = ["## Open Questions"]
    for q in open_qs:
        blocking = " [BLOCKING]" if q.get("blocking") else ""
        lines.append(f"  {q.get('id','?')}{blocking}: {q.get('text','')}")
    lines.append("")
    return lines


def _render_decisions(decisions: list[dict]) -> list[str]:
    sorted_dec = sorted(decisions, key=lambda d: d.get("id", ""))
    entries = []
    for d in sorted_dec:
        entries.append(
            f"  {d.get('id','?')} [{d.get('stage','?')}]: {d.get('text','')} "
            f"— {d.get('rationale','')}"
        )
    return _section_lines("Decisions", entries)


def _render_requirements(requirements: list[dict]) -> list[str]:
    sorted_req = sorted(requirements, key=lambda r: r.get("req_id", ""))
    entries = []
    for r in sorted_req:
        entries.append(
            f"  {r.get('req_id','?')} [{r.get('status','?')}]: {r.get('text','')}"
        )
    return _section_lines("Requirements", entries)


def _render_stage_log(stage_log: list[dict]) -> list[str]:
    if not stage_log:
        return []
    lines = ["## Stage Log"]
    for s in stage_log:
        exited = s.get("exited") or "ongoing"
        gate = f" gate={s['exit_gate']}" if s.get("exit_gate") else ""
        lines.append(
            f"  {s.get('stage','?')}: entered={s.get('entered','')} "
            f"exited={exited}{gate}"
        )
    lines.append("")
    return lines


def _render_artifacts(artifacts: list[dict]) -> list[str]:
    sorted_art = sorted(artifacts, key=lambda a: a.get("id", ""))
    entries = []
    for a in sorted_art:
        entries.append(
            f"  [{a.get('kind','?')}] {a.get('id','?')}: {a.get('path','')} "
            f"(stage={a.get('stage','?')}) {a.get('note','')}"
        )
    return _section_lines("Artifacts", entries)


def _count_entries(state: dict) -> dict[str, int]:
    """Return the data-entry count for each section (used in dropped footer)."""
    open_qs = [q for q in state.get("questions", []) if q.get("status") == "open"]
    return {
        "brief": 1 if (state.get("brief", {}).get("title") or
                       state.get("brief", {}).get("summary")) else 0,
        "dimensions": len(state.get("dimensions", [])),
        "questions": len(open_qs),
        "decisions": len(state.get("decisions", [])),
        "requirements": len(state.get("requirements", [])),
        "stage_log": len(state.get("stage_log", [])),
        "artifacts": len(state.get("artifacts", [])),
    }


def ledger_render(ledger: "Ledger", budget_tokens: int) -> str:
    """Produce a compact, deterministic text rendering of the ledger state.

    Priority order (contract §2): brief → active-stage dimensions → open questions
    → decisions → requirements → stage_log → artifacts.

    Truncation is whole-entry only with strict priority: once a section does not fit,
    all lower-priority sections are also dropped (higher priority always beats lower
    priority — we never skip a large high-priority section to include a smaller
    lower-priority one).  A trailing ``(+N more entries)`` footer counts total
    data entries that were not rendered.  Never truncates mid-entry.
    """
    budget_chars = budget_tokens * _CHARS_PER_TOKEN
    state = ledger.state
    active_stage = _active_stage(state)

    header = f"[LEDGER schema={SCHEMA} run_id={ledger.run_id}]\n"

    # Build sections in priority order — order is the contract, do not reorder.
    sections: list[tuple[str, list[str]]] = [
        ("brief", _render_brief(state.get("brief", {}))),
        ("dimensions", _render_dimensions(state.get("dimensions", []), active_stage)),
        ("questions", _render_questions(state.get("questions", []))),
        ("decisions", _render_decisions(state.get("decisions", []))),
        ("requirements", _render_requirements(state.get("requirements", []))),
        ("stage_log", _render_stage_log(state.get("stage_log", []))),
        ("artifacts", _render_artifacts(state.get("artifacts", []))),
    ]
    entry_counts = _count_entries(state)

    used_chars = len(header)
    output_lines: list[str] = [header]
    dropped_entries = 0
    budget_exhausted = False  # once True, all remaining sections are dropped

    for section_name, lines in sections:
        section_text = "\n".join(lines)
        if not section_text.strip():
            continue  # empty section — skip without penalising the budget
        if budget_exhausted:
            # Strict priority: once budget is gone, every lower-priority section drops.
            dropped_entries += entry_counts.get(section_name, 0)
            continue
        if used_chars + len(section_text) + 1 <= budget_chars:
            output_lines.append(section_text)
            used_chars += len(section_text) + 1
        else:
            # This section doesn't fit — drop it and all lower-priority sections.
            budget_exhausted = True
            dropped_entries += entry_counts.get(section_name, 0)

    if dropped_entries:
        output_lines.append(f"(+{dropped_entries} more entries)")

    output_lines.append("[/LEDGER]")
    return "\n".join(output_lines)
