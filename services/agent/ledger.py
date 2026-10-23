"""Design Ledger — persistent mission state with budgeted rendering.

Schema: ``aicad.ledger/1`` (frozen in contracts/advisor.md §2).

The ledger is the anti-context-loss mechanism for Pillar 20: every agent role
cold-starts from ``ledger_render(budget_tokens)`` instead of chat history, making
24k-context local models viable across a whole mission.

Storage: ``<cache_root>/runs/<run_id>/ledger.json`` holds the compact materialized
``state`` snapshot; events append one-per-line to the ``ledger.jsonl`` sidecar next
to it (O(1) append, independent of event count — see contracts/advisor.md §2 for
the on-disk layout note). Events are never mutated; state is re-materialized from
events on every ``append()``. ``load()`` rebuilds state from events alone, so
replay is a full correctness guarantee. Legacy runs whose ``ledger.json`` still
holds the old whole-document ``{"events": [...], "state": {...}}`` shape (no
sidecar) load transparently and migrate on first append.

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

# Design-pipeline event kind → state section (contract design_pipeline.md §4).
# One place both _apply and the section allow-lists read, so the four kinds stay in
# lockstep across the ledger, mission_dispatch, and the phase gates.
_DP_KIND_SECTION: dict[str, str] = {
    "feature": "features",
    "geometry_spec": "geometry_specs",
    "linkage": "linkages",
    "build_node": "build_nodes",
    "mfg_plan": "mfg_plans",
}

_PROVENANCE_REQUIRED_KINDS = frozenset({"dimension", "geometry_spec"})


def _has_numeric_engineering_value(row: dict) -> bool:
    """Whether an audit-native requirement carries a numeric engineering value."""
    ignored = {"critical"}

    def visit(value: Any, key: str = "") -> bool:
        if key in ignored or isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, dict):
            return any(visit(child, str(name)) for name, child in value.items())
        if isinstance(value, (list, tuple)):
            return any(visit(child) for child in value)
        return False

    return visit(row)


def validate_ledger_payload(kind: str, payload: dict) -> None:
    """Enforce provenance invariants shared by every ledger write path."""
    if kind in _PROVENANCE_REQUIRED_KINDS and not payload.get("provenance"):
        raise ValueError(
            f"{kind} events require a 'provenance' field "
            "(tool result / calc / table / user answer)"
        )
    if kind != "mfg_plan":
        return
    for section in ("critical_dimensions", "other_callouts"):
        rows = payload.get(section) or []
        if not isinstance(rows, list):
            raise ValueError(f"mfg_plan {section!r} must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"mfg_plan {section}[{index}] must be an object")
            is_critical = section == "critical_dimensions" or row.get("critical", True)
            if is_critical and _has_numeric_engineering_value(row) and not row.get("provenance"):
                label = row.get("requirement_id") or row.get("id") or index
                raise ValueError(
                    f"mfg_plan numeric critical row {label!r} requires provenance "
                    "(tool result / calc / table / user answer)"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fsync_dir(d: Path) -> None:
    """fsync a directory so a rename into it is durable. No-op where unsupported."""
    try:
        fd = os.open(str(d), getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except (OSError, AttributeError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
        # Per-acceptance-criterion verification results written by the
        # reviewer role (reviewer_exec.run_acceptance_checks). The VERIFY phase
        # gate READS these — it never fabricates PASS. Each entry:
        #   {id, result: PASS|FAIL|NOT-CHECKED, check_tool, pass_rule,
        #    detail, evidence}
        "criterion_results": [],
        # Design-pipeline stage artifacts (contract design_pipeline.md §3/§4,
        # extends aicad.ledger/1). Each is a typed handoff the six-stage refinement
        # writes; the flagged phase gates READ these (never the handoff prose).
        "features": [],          # Stage 1 feature[] (feature_architect)
        "geometry_specs": [],    # Stage 2 geometry_spec[] (geometry_designer)
        "linkages": [],          # Stage 3 linkage[] (linkage_planner)
        "build_nodes": [],       # Stage 4 build_node[] (build_strategist)
        "mfg_plans": [],         # Typed manufacturing-plan scratchpad
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
        # Models often omit id and only send name=…; tolerate missing ids and
        # never KeyError on an earlier id-less decision (mega-2: ledger write
        # failed: 'id' after first decision without id polluted state).
        dec_id = payload.get("id") or payload.get("name") or payload.get("decision_id")
        if dec_id and not payload.get("id"):
            payload = {**payload, "id": dec_id}
        existing = None
        if dec_id is not None:
            existing = next(
                (d for d in state["decisions"]
                 if d.get("id") == dec_id or d.get("name") == dec_id),
                None,
            )
        if existing is None and payload.get("name"):
            existing = next(
                (d for d in state["decisions"] if d.get("name") == payload.get("name")),
                None,
            )
        if existing:
            existing.update(payload)
        else:
            state["decisions"].append(dict(payload))

    elif kind == "dimension":
        # provenance stays in the event; only the frozen state fields go into state
        dim_id = payload.get("id") or payload.get("name") or payload.get("dim_id")
        if dim_id and not payload.get("id"):
            payload = {**payload, "id": dim_id}
        state_entry = {k: v for k, v in payload.items()
                       if k in ("id", "name", "value", "unit", "driver",
                                "tolerance", "status", "confirmed_by", "refs",
                                # §21 additive fields — derivation DAG + linking
                                "derived_from", "derivation", "linked",
                                "cited_by_departments")}
        existing = None
        if dim_id is not None:
            existing = next(
                (d for d in state["dimensions"]
                 if d.get("id") == dim_id or d.get("name") == dim_id),
                None,
            )
        if existing is None and payload.get("name"):
            existing = next(
                (d for d in state["dimensions"] if d.get("name") == payload.get("name")),
                None,
            )
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

    elif kind == "criterion_result":
        # Reviewer's per-criterion verdict. Keyed by ac id; last write wins
        # (a re-review after a CORRECT loop overwrites the earlier verdict).
        ac_id = payload.get("id")
        existing = next((c for c in state["criterion_results"]
                         if c.get("id") == ac_id), None)
        if existing:
            existing.update(dict(payload))
        else:
            state["criterion_results"].append(dict(payload))

    elif kind == "artifact":
        art_id = payload.get("id")
        existing = next((a for a in state["artifacts"] if a["id"] == art_id), None)
        if existing:
            existing.update(payload)
        else:
            state["artifacts"].append(dict(payload))

    elif kind in _DP_KIND_SECTION:
        # Typed design/manufacturing artifacts share stable-id last-write-wins
        # materialization. Keeping this keyed from the map prevents additive kinds
        # from silently drifting out of replay.
        section = _DP_KIND_SECTION[kind]
        item_id = payload.get("id")
        existing = next((x for x in state[section] if x.get("id") == item_id), None)
        if existing:
            existing.update(dict(payload))
        else:
            state[section].append(dict(payload))

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

    Append-only event log + materialized state.  Events are stored as a
    JSONL sidecar (``ledger.jsonl``, one JSON object per line) so ``append``
    is O(1) regardless of how many events the run has accumulated; the
    compact materialized state is snapshotted separately into ``ledger.json``
    (small — bounded by distinct entities, not by event count). State is
    always a pure replay of events — never mutated directly.

    Legacy runs whose ``ledger.json`` still holds the old whole-document
    format (``{"events": [...], "state": {...}}``) load transparently; the
    first append after loading one migrates its events into the JSONL log.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path or run_ledger_path(run_id)
        self._events_path = self.path.with_suffix(".jsonl")
        self.events: list[dict] = []
        self.state: dict = _empty_state(run_id)
        self._needs_migration = False
        if self._events_path.exists():
            self._load_from_jsonl()
        elif self.path.exists():
            self._load_legacy_document()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _read_events_jsonl(path: Path) -> list[dict]:
        events: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # tolerate a torn last line from a mid-write crash
        except OSError:
            return []
        return events

    def _load_from_jsonl(self) -> None:
        self.events = self._read_events_jsonl(self._events_path)
        # Always rematerialize from events (replay guarantee — ignore stored state)
        self.state = _materialize(self.events, self.run_id)

    def _load_legacy_document(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.events = raw.get("events", [])
        # Always rematerialize from events (replay guarantee — ignore stored state)
        self.state = _materialize(self.events, self.run_id)
        self._needs_migration = bool(self.events)

    def _seed_events_jsonl(self, events: list[dict]) -> None:
        """One-time migration: move legacy-loaded events into the JSONL log so
        every append from here on is O(1) instead of rewriting the whole doc."""
        try:
            with self._events_path.open("w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False))
                    f.write("\n")
                f.flush()
        except OSError:
            pass

    def _append_event_jsonl(self, event: dict) -> None:
        """Append one event line — O(1) regardless of ledger size.

        fsync'd: the JSONL log is the source of truth (``_load_from_jsonl`` always
        rematerializes state from it), so an unsynced append is exactly the work a
        hard crash loses. A torn last line is tolerated by the reader.
        """
        try:
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            # Best-effort — a failed save must not break the run
            pass

    def _save(self) -> None:
        """Atomic write of the compact state snapshot: temp file + rename."""
        doc = {"state": self.state}
        tmp = self.path.with_suffix(".tmp")
        try:
            # fsync the temp file BEFORE the rename: on ext4/xfs the rename can be
            # journalled ahead of the data, so a crash right after a save could leave a
            # zero-length ledger.json behind.
            with tmp.open("w", encoding="utf-8") as f:
                f.write(json.dumps(doc, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
            _fsync_dir(self.path.parent)
        except OSError:
            # Best-effort — a failed save must not break the run
            pass

    # ------------------------------------------------------------------
    # Core append
    # ------------------------------------------------------------------

    def append(self, actor: str, kind: str, payload: dict) -> dict:
        """Record one event, rematerialize state, persist.  Returns the event."""
        if self._needs_migration:
            self._seed_events_jsonl(self.events)
            self._needs_migration = False
        event = {
            "seq": len(self.events),
            "ts": _now(),
            "actor": actor,
            "kind": kind,
            "payload": payload,
        }
        self.events.append(event)
        _apply(self.state, event)
        self._append_event_jsonl(event)
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
                         refs: dict | None = None,
                         # §21 additive fields
                         derived_from: list[str] | None = None,
                         derivation: str | None = None,
                         linked: dict | None = None,
                         cited_by_departments: list[str] | None = None) -> dict:
        """Append a dimension event.  ``provenance`` is required (contract rule).

        §21: if *status* is ``'derived'`` or *derivation* is provided and
        *derived_from* is empty, a provenance-violation warning is attached to the
        event payload (overseer audit trigger on repeat) — but the write still
        succeeds (validator warns, does not block).
        """
        if not provenance:
            raise ValueError(
                f"dimension {dim_id!r}: provenance is required "
                "(must be: tool result / calc / table / user answer)"
            )
        payload: dict = {
            "id": dim_id, "name": name, "value": value, "unit": unit,
            "driver": driver, "tolerance": _normalize_tolerance(tolerance),
            "status": status, "confirmed_by": confirmed_by,
            "refs": refs or {},
            # provenance lives in the event payload for audit; not in materialized state
            "provenance": provenance,
        }
        # §21 additive fields — only set when provided to keep events lean
        if derived_from is not None:
            payload["derived_from"] = derived_from
        if derivation is not None:
            payload["derivation"] = derivation
        if linked is not None:
            payload["linked"] = linked
        if cited_by_departments is not None:
            payload["cited_by_departments"] = cited_by_departments

        # §21 provenance-violation warning: derived dim without derived_from refs
        is_derived = (status == "derived") or bool(derivation)
        has_refs = bool(derived_from)
        if is_derived and not has_refs:
            payload["_provenance_warning"] = (
                f"dimension {dim_id!r} is derived but has no derived_from refs — "
                "provenance violation (overseer audit trigger on repeat)"
            )

        return self.append(actor, "dimension", payload)

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
                     path: str, stage: str, note: str = "",
                     **extra: Any) -> dict:
        """Append an artifact event. Extra kwargs (e.g. ``manifest=…``) are
        stored on the payload so ``ledger_get(section="manifest")`` can return
        the full intake deliverables document, not just a note string."""
        body: dict[str, Any] = {
            "kind": kind, "id": artifact_id, "path": path,
            "stage": stage, "note": note,
        }
        for k, v in extra.items():
            if not str(k).startswith("_") and v is not None:
                body[k] = v
        return self.append(actor, "artifact", body)

    def add_criterion_result(self, actor: str, ac_id: str, result: str,
                             *, check_tool: str = "", pass_rule: str = "",
                             detail: str = "",
                             evidence: dict | None = None) -> dict:
        """Record one acceptance-criterion verdict.

        ``result`` is one of ``PASS`` / ``FAIL`` / ``NOT-CHECKED`` (the reviewer
        role's only allowed verdicts). ``evidence`` is the raw check-tool output
        that produced the verdict — the proof the named tool genuinely ran (the
        VERIFY gate reads only the verdict, but the evidence makes a PASS
        auditable and a NOT-CHECKED honest)."""
        return self.append(actor, "criterion_result", {
            "id": ac_id, "result": result,
            "check_tool": check_tool, "pass_rule": pass_rule,
            "detail": detail, "evidence": evidence or {},
        })


    def upsert_mfg_plan(self, actor: str, plan: dict) -> dict:
        """Append a validated typed manufacturing-plan event."""
        body = dict(plan or {})
        if not body.get("id"):
            raise ValueError("mfg_plan requires a stable 'id'")
        validate_ledger_payload("mfg_plan", body)
        return self.append(actor, "mfg_plan", body)
    # ------------------------------------------------------------------
    # Design-pipeline stage-artifact writers
    # (contract design_pipeline.md §3/§4 — extends aicad.ledger/1)
    # ------------------------------------------------------------------

    def add_feature(self, actor: str, feature_id: str, function: str,
                    driving_requirement: str,
                    interfaces_with: list[str] | None = None,
                    criticality: str = "important") -> dict:
        """Append a Stage-1 ``feature`` event (feature_architect).

        Mirrors §22 rung-A features[]. The coverage rule (every feature traces to a
        requirement/assumption; every requirement maps to ≥1 feature) is enforced by
        pipeline/functional_decomposition.py, not this writer — the writer only
        records the typed handoff."""
        return self.append(actor, "feature", {
            "id": feature_id,
            "function": function,
            "driving_requirement": driving_requirement,
            "interfaces_with": interfaces_with or [],
            "criticality": criticality,
        })

    def add_geometry_spec(self, actor: str, spec_id: str,
                          realizes: list[str],
                          shapes: list[dict],
                          provenance: str,
                          nominal_dims: list[dict] | None = None,
                          tolerances: list[dict] | None = None,
                          datum_refs: list[str] | None = None) -> dict:
        """Append a Stage-2 ``geometry_spec`` event (geometry_designer).

        ``provenance`` is REQUIRED (contract §3.2 / §4) — the geometry-side twin of
        the dimension provenance rule: never recall a number. A spec written without
        provenance is rejected here (and at the mission_dispatch back door)."""
        if not provenance:
            raise ValueError(
                f"geometry_spec {spec_id!r}: provenance is required "
                "(must be: tool result / calc / table / user answer)"
            )
        return self.append(actor, "geometry_spec", {
            "id": spec_id,
            "realizes": realizes or [],
            "shapes": shapes or [],
            "nominal_dims": nominal_dims or [],
            "tolerances": tolerances or [],
            "datum_refs": datum_refs or [],
            "provenance": provenance,
        })

    def add_linkage(self, actor: str, linkage_id: str = "LINK_MAIN",
                    datum_tree: list[dict] | None = None,
                    reference_edges: list[dict] | None = None,
                    shared_interfaces: list[dict] | None = None) -> dict:
        """Append a Stage-3 ``linkage`` event (linkage_planner).

        Mirrors §22 rung-C. Reference integrity (no dangling refs / no cycles) and
        interface consistency are checked in pipeline/linkage_integrity.py, not
        this writer."""
        return self.append(actor, "linkage", {
            "id": linkage_id,
            "datum_tree": datum_tree or [],
            "reference_edges": reference_edges or [],
            "shared_interfaces": shared_interfaces or [],
        })

    def add_build_node(self, actor: str, node_id: str, op: str,
                       elements: list[str] | None = None,
                       deps: list[str] | None = None,
                       orientation: str = "",
                       sketch_plane: str = "",
                       why: str = "") -> dict:
        """Append a Stage-4 ``build_node`` event (build_strategist).

        Mirrors §22 rung-D steps[]; lowers into an aicad.buildgraph/1 node. Cycle /
        missing-ref validation is the buildgraph validator's job (advisor.md §13)."""
        return self.append(actor, "build_node", {
            "id": node_id,
            "op": op,
            "elements": elements or [],
            "deps": deps or [],
            "orientation": orientation,
            "sketch_plane": sketch_plane,
            "why": why,
        })

    # ------------------------------------------------------------------
    # Scoped-slice accessors (contract design_pipeline.md §9)
    # Read-only views the mission-dispatch layer hands to
    # fresh-context stages — a stage sees only the prior rung's artifacts.
    # ------------------------------------------------------------------

    def features(self) -> list[dict]:
        """Stage-1 features[] — the input Stage 2 realizes."""
        return list(self.state.get("features", []))

    def geometry_specs(self) -> list[dict]:
        """Stage-2 geometry_spec[] — the input Stage 3 links and Stage 4 orders."""
        return list(self.state.get("geometry_specs", []))

    def linkages(self) -> list[dict]:
        """Stage-3 linkage[] — the input Stage 4 orders."""
        return list(self.state.get("linkages", []))

    def build_nodes(self) -> list[dict]:
        """Stage-4 build_node[] — compiled into the buildgraph at PLAN."""
        return list(self.state.get("build_nodes", []))

    def mfg_plans(self) -> list[dict]:
        """Typed manufacturing plans, upserted by stable plan id."""
        return list(self.state.get("mfg_plans", []))

    def slice_for_stage(self, stage: str) -> dict:
        """The minimal typed inputs a design-pipeline stage needs (§22 fresh-context
        rule: never the full ledger). ``stage`` is a design-pipeline stage name
        (``decompose`` | ``geometry`` | ``linkage`` | ``buildpath``). Unknown stages
        return an empty slice (the caller falls back to a full ledger_render)."""
        s = (stage or "").lower()
        if s == "decompose":
            # Stage 1 reasons from the brief + requirements only (the ledger render
            # already carries those); it needs no prior stage artifact.
            return {}
        if s == "geometry":
            return {"features": self.features()}
        if s == "linkage":
            return {"geometry_specs": self.geometry_specs()}
        if s == "buildpath":
            return {"geometry_specs": self.geometry_specs(),
                    "linkages": self.linkages()}
        return {}

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


def _normalize_tolerance(tol: Any) -> dict:
    """Coerce a dimension tolerance into the dict form the ledger stores.

    The signature says ``dict``, but nothing enforced it and the value comes
    straight from a model-authored tool call. Writing a fit class as a bare
    string — ``tolerance="g6"`` — is the natural way to express a toleranced
    dimension, and it is exactly what a shaft/bore mission produces. That
    string then reached ``_render_dimensions``, which called ``tol.get("fit")``
    and raised ``AttributeError: 'str' object has no attribute 'get'``, taking
    down ``ledger_render`` — the advisory loop's whole state surface — on a
    perfectly reasonable call. Observed live 2026-08-04 on the bearing-mount
    bracket mission (a 20 g6 fit).

    A bare string is a fit designation; anything else unrecognized is dropped
    rather than guessed at.
    """
    if isinstance(tol, dict):
        return tol
    if isinstance(tol, str) and tol.strip():
        return {"fit": tol.strip()}
    return {}


def _render_dimensions(dimensions: list[dict], active_stage: str | None) -> list[str]:
    """Render dimensions, sorted by id for determinism."""
    if not dimensions:
        return []
    sorted_dims = sorted(dimensions, key=lambda d: d.get("id", ""))
    lines = ["## Dimensions"]
    for d in sorted_dims:
        # Defensive as well as normalized at ingest: state materialized from
        # events written before the normalizer existed still carries raw strings.
        tol = _normalize_tolerance(d.get("tolerance"))
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
