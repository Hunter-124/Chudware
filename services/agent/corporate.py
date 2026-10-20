"""corporate.py — Corporate Roles org mode for sub-agent orchestration.

Schema: ``aicad.corporate/1``

When ``agent.corporate_mode`` is on and a mission decomposes into ≥2
departments' worth of tasks, complex missions are organized as a company
instead of flat delegation:

  * DIRECTOR — oversight persona wrapping the root orchestrator (depth 0).
  * DEPARTMENTS — derived from the mission's task classes / roles, capped at
    ``agent.corporate.max_departments`` (default 3).
  * DEPARTMENT HEAD — one sub-agent per department (``department_manager``).
  * WORKERS — spawned by heads (any role via role_models) within the hard
    nesting depth cap (``agent.max_subagent_depth``).
  * TEAM LEAD — optional, when a department exceeds ~3 workers and
    ``agent.corporate.team_leads`` is true.

Communication is ENFORCED (not advisory), built on the coordination board:

  * ``dept:<dept_id>`` channel — head + workers (+ team lead).
  * ``heads`` channel — all heads + director.
  * Workers post progress/questions to their dept channel; heads digest.
  * Direct worker → director is REJECTED with a clear error.
  * Heads hold MEETINGS every ``meeting_cadence`` worker completions (or on
    blockage): structured status digest to the heads channel.
  * DIRECTOR (when ``director_review``) reviews digests with template-driven
    checks first; LLM steering notes only when misalignment/blockage is
    detected. Heads relay steering to workers.

This module is complementary to ``org.py`` (aicad.org/1 deliverable
clustering). Corporate mode is the *subagent orchestration* layer for the
copilot; org.py remains the mission-pipeline department runtime.
"""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from services.agent.chudcode_roles import run_role_turn

from services.agent.config import (
    corporate_mode_enabled,
    get_corporate_config,
    get_max_subagent_depth,
)

CORPORATE_SCHEMA = "aicad.corporate/1"

# Stream event kinds (kind="corporate", corp_event=…)
CORP_EVENT_ORG_BUILT = "org_built"
CORP_EVENT_HIRE = "hire"
CORP_EVENT_TEAM_LEAD = "team_lead"
CORP_EVENT_MEETING = "meeting"
CORP_EVENT_STEERING = "steering"
CORP_EVENT_ROUTE_REJECTED = "route_rejected"
CORP_EVENT_WIND_DOWN = "wind_down"

# Effective depth for team_lead→worker when team_leads enabled (local only).
_TEAM_LEAD_SPAWN_FLOOR = 3

# Channel kinds / names
CHANNEL_HEADS = "heads"
CHANNEL_DEPT_PREFIX = "dept:"

# Seat ranks in the org (enforced routing)
RANK_DIRECTOR = "director"
RANK_HEAD = "head"
RANK_TEAM_LEAD = "team_lead"
RANK_WORKER = "worker"

# Meeting / digest
DIGEST_STATUSES = frozenset({"done", "in_progress", "blocked", "risks"})
MEETING_TRIGGER_COMPLETION = "completion"
MEETING_TRIGGER_BLOCKAGE = "blockage"
MEETING_TRIGGER_MANUAL = "manual"

# ~3 workers before a team lead is warranted
_TEAM_LEAD_THRESHOLD = 3

# ---------------------------------------------------------------------------
# Department taxonomy — map roles / task classes / stages → departments
# ---------------------------------------------------------------------------

# Canonical department ids + display names. Order is priority when capping.
DEPARTMENT_CATALOG: tuple[tuple[str, str], ...] = (
    ("geometry", "Geometry"),
    ("simulation", "Simulation"),
    ("manufacturing", "Manufacturing"),
    ("documentation", "Documentation"),
    ("electronics", "Electronics"),
    ("firmware", "Firmware"),
    ("sourcing", "Sourcing"),
    ("intake", "Intake"),
    ("general", "General"),
)

# role id → department id
ROLE_TO_DEPT: dict[str, str] = {
    "model3d": "geometry",
    "sketch2d": "geometry",
    "geometry_designer": "geometry",
    "feature_modeler": "geometry",
    "feature_architect": "geometry",
    "sketcher": "geometry",
    "layout_engineer": "geometry",
    "linkage_planner": "geometry",
    "assembly_fit_engineer": "geometry",
    "re_modeler": "geometry",
    "re_geometry_verifier": "geometry",
    "sim_engineer": "simulation",
    "cam_programmer": "manufacturing",
    "reviewer": "documentation",
    "spec_writer": "documentation",
    "drawing_detailer": "documentation",
    "re_datasheet_reader": "documentation",
    "ecad_engineer": "electronics",
    "pcb_engineer": "electronics",
    "firmware_engineer": "firmware",
    "sourcing_specialist": "sourcing",
    "intake_analyst": "intake",
    "clarifier": "intake",
    "design_thinker": "intake",
    "fixer": "geometry",  # fix work stays with geometry by default
    "department_manager": "general",
    "orchestrator": "general",
    "overseer": "general",
}

# task_class / deliverable kind / stage → department id
TASK_CLASS_TO_DEPT: dict[str, str] = {
    "simple_part": "geometry",
    "simple": "geometry",
    "multi_feature_part": "geometry",
    "assembly": "geometry",
    "enclosure": "geometry",
    "modification": "geometry",
    "cam_program": "manufacturing",
    "analysis": "simulation",
    "fea": "simulation",
    "cfd": "simulation",
    "drawing": "documentation",
    "report": "documentation",
    "ecad": "electronics",
    "pcb": "electronics",
    "schematic": "electronics",
    "firmware": "firmware",
    "fw": "firmware",
    "sourcing": "sourcing",
    "intake": "intake",
    "spec": "documentation",
    "layout": "geometry",
    "sketch": "geometry",
    "feature": "geometry",
    "part_verify": "geometry",
    "sim": "simulation",
    "cam": "manufacturing",
    "deliver": "documentation",
    "fix": "geometry",
    "leaf": "documentation",
    "unknown": "general",
}

# stage vocabulary → department (fallback)
STAGE_TO_DEPT: dict[str, str] = {
    "intake": "intake",
    "spec": "documentation",
    "decompose": "intake",
    "layout": "geometry",
    "sketch": "geometry",
    "feature": "geometry",
    "linkage": "geometry",
    "part_verify": "geometry",
    "assembly": "geometry",
    "sim": "simulation",
    "cam": "manufacturing",
    "deliver": "documentation",
    "fix": "geometry",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fresh_id(prefix: str = "c") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower())
    return s.strip("_") or "x"


def _runs_dir() -> Path:
    """Platform default runs directory (mirrors coordination._runs_dir)."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "Chudware" / "runs"
    return Path.home() / ".local" / "share" / "Chudware" / "runs"


def corporate_event(
    corp_event: str,
    *,
    run_id: str = "",
    dept: Any = None,
    agent: str | None = None,
    payload: dict[str, Any] | None = None,
    chart_revision: int | None = None,
) -> dict[str, Any]:
    """Build a kind=corporate event payload for the agent event stream."""
    body: dict[str, Any] = {
        "kind": "corporate",
        "corp_event": str(corp_event or "").strip(),
        "ts": _now(),
    }
    if run_id:
        body["run_id"] = str(run_id)
    if dept is not None:
        body["dept"] = dept
    if agent:
        body["agent"] = str(agent)
    if chart_revision is not None:
        body["chart_revision"] = int(chart_revision)
    body["payload"] = dict(payload or {})
    return body


def emit_corporate_event(
    corp_event: str,
    *,
    on_event: Callable[..., None] | None = None,
    run_id: str = "",
    dept: Any = None,
    agent: str | None = None,
    payload: dict[str, Any] | None = None,
    chart_revision: int | None = None,
) -> dict[str, Any]:
    """Emit a corporate event onto the live agent event stream (best-effort)."""
    body = corporate_event(
        corp_event,
        run_id=run_id,
        dept=dept,
        agent=agent,
        payload=payload,
        chart_revision=chart_revision,
    )
    if on_event is None:
        return body
    try:
        from services.agent import agent_events as _ae  # noqa: PLC0415

        _ae.emit_event("corporate", body, live=on_event, journal=None)
    except Exception:  # noqa: BLE001 — never block org on telemetry
        try:
            # Fallback: call sink directly with single-dict or dual-arity shape.
            n = 1
            try:
                import inspect  # noqa: PLC0415

                n = sum(
                    1
                    for p in inspect.signature(on_event).parameters.values()
                    if p.default is p.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                )
            except Exception:  # noqa: BLE001
                n = 1
            if n >= 2:
                on_event("corporate", body)
            else:
                on_event(body)
        except Exception:  # noqa: BLE001
            pass
    return body


def effective_corporate_depth_cap(
    max_subagent_depth: int,
    *,
    team_leads: bool = True,
    spawner_rank: str = RANK_HEAD,
) -> int:
    """Local effective depth for corporate spawn (does NOT mutate global config).

    When team_leads are enabled and the spawner is a team_lead, the effective
    cap is ``max(agent.max_subagent_depth, 3)`` so lead→worker seats are allowed
    even when the global nesting default is 2. Head→worker still uses the
    caller's stored cap as-is (workers are depth 2).
    """
    base = max(0, int(max_subagent_depth))
    if team_leads and spawner_rank == RANK_TEAM_LEAD:
        return max(base, _TEAM_LEAD_SPAWN_FLOOR)
    return base


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _kw_match(blob: str, keywords: tuple[str, ...]) -> bool:
    """True if any *keywords* starts a word in *blob* (word-boundary, not a raw
    substring) — e.g. ``"board"`` matches "circuit board" but NOT "onboarding"
    (mid-word), while a stem like ``"machin"`` still matches "machining"."""
    return any(re.search(rf"\b{re.escape(w)}", blob) for w in keywords)


def classify_task_department(task: dict[str, Any] | str | None) -> str:
    """Map one task/role/stage descriptor to a department id."""
    if task is None:
        return "general"
    if isinstance(task, str):
        key = task.strip().lower()
        if key in ROLE_TO_DEPT:
            return ROLE_TO_DEPT[key]
        if key in TASK_CLASS_TO_DEPT:
            return TASK_CLASS_TO_DEPT[key]
        if key in STAGE_TO_DEPT:
            return STAGE_TO_DEPT[key]
        return "general"

    if not isinstance(task, dict):
        return "general"

    for field_name in ("role", "role_id", "assigned_role"):
        role = str(task.get(field_name) or "").strip().lower()
        if role and role in ROLE_TO_DEPT:
            return ROLE_TO_DEPT[role]

    for field_name in ("task_class", "class", "kind", "deliverable_kind", "type"):
        tc = str(task.get(field_name) or "").strip().lower()
        if tc and tc in TASK_CLASS_TO_DEPT:
            return TASK_CLASS_TO_DEPT[tc]

    for field_name in ("stage", "phase", "stage_scope"):
        raw = task.get(field_name)
        if isinstance(raw, (list, tuple)):
            for item in raw:
                st = str(item or "").strip().lower()
                if st in STAGE_TO_DEPT:
                    return STAGE_TO_DEPT[st]
        else:
            st = str(raw or "").strip().lower()
            if st in STAGE_TO_DEPT:
                return STAGE_TO_DEPT[st]

    # Free-text brief / name keyword fallback
    blob = " ".join(
        str(task.get(k) or "")
        for k in ("brief", "name", "title", "description", "goal", "id")
    ).lower()
    if _kw_match(blob, ("cam", "toolpath", "gcode", "machin")):
        return "manufacturing"
    if _kw_match(blob, ("fea", "cfd", "simulat", "thermal", "stress")):
        return "simulation"
    if _kw_match(blob, ("draw", "report", "document", "titleblock")):
        return "documentation"
    if _kw_match(blob, ("schematic", "pcb", "ecad", "board")):
        return "electronics"
    if _kw_match(blob, ("firmware", "elf", "mcu", "fw_")):
        return "firmware"
    if _kw_match(blob, ("sourc", "vendor", "bom", "buy")):
        return "sourcing"
    if _kw_match(blob, ("sketch", "extrude", "revolve", "fillet", "geometry", "part")):
        return "geometry"
    return "general"


def _dept_display_name(dept_id: str) -> str:
    for did, name in DEPARTMENT_CATALOG:
        if did == dept_id:
            return name
    return dept_id.replace("_", " ").title()


def derive_departments(
    tasks: Iterable[Any],
    *,
    max_departments: int = 3,
) -> list[dict[str, Any]]:
    """Cluster *tasks* into department buckets, capped at *max_departments*.

    Returns a list of department dicts::

        {"id": "geometry", "name": "Geometry", "tasks": [...], "roles": [...]}

    When more distinct departments are present than the cap, lower-priority
    departments (later in DEPARTMENT_CATALOG) are merged into ``general`` so
    the chart stays within the cap. ``general`` is only emitted when needed.
    """
    max_d = max(1, int(max_departments))
    buckets: dict[str, list[Any]] = {}
    role_sets: dict[str, set[str]] = {}

    for t in tasks or []:
        dept = classify_task_department(t)
        buckets.setdefault(dept, []).append(t)
        role = ""
        if isinstance(t, dict):
            role = str(t.get("role") or t.get("role_id") or "").strip()
        elif isinstance(t, str) and t in ROLE_TO_DEPT:
            role = t
        if role:
            role_sets.setdefault(dept, set()).add(role)

    if not buckets:
        return []

    # Priority order from catalog; unknown ids last.
    order = [d for d, _ in DEPARTMENT_CATALOG]
    ranked = sorted(
        buckets.keys(),
        key=lambda d: order.index(d) if d in order else len(order),
    )

    if len(ranked) <= max_d:
        keep = ranked
        overflow: list[str] = []
    else:
        # Prefer named depts over general when capping.
        non_general = [d for d in ranked if d != "general"]
        if len(non_general) >= max_d:
            keep = non_general[:max_d]
            overflow = non_general[max_d:] + (
                ["general"] if "general" in ranked else []
            )
        else:
            keep = non_general + (["general"] if "general" in ranked else [])
            keep = keep[:max_d]
            overflow = [d for d in ranked if d not in keep]

    # Merge overflow into last kept dept (or create general).
    if overflow:
        sink = "general" if "general" in keep else keep[-1]
        for od in overflow:
            buckets.setdefault(sink, []).extend(buckets.pop(od, []))
            role_sets.setdefault(sink, set()).update(role_sets.pop(od, set()))
            if od in ranked:
                # already handled
                pass
        if sink not in keep:
            keep.append(sink)

    depts: list[dict[str, Any]] = []
    for did in keep:
        if did not in buckets:
            continue
        depts.append(
            {
                "id": did,
                "name": _dept_display_name(did),
                "tasks": list(buckets[did]),
                "roles": sorted(role_sets.get(did, set())),
            }
        )
    return depts


def should_use_corporate(
    tasks: Iterable[Any] | None = None,
    *,
    cfg: dict | None = None,
    force: bool | None = None,
) -> bool:
    """True when corporate mode should organize this mission.

    Requires ``agent.corporate_mode`` on (unless *force* is set) AND the task
    set decomposes into ≥2 departments.
    """
    if force is not None:
        enabled = bool(force)
    else:
        enabled = corporate_mode_enabled(cfg)
    if not enabled:
        return False
    corp = get_corporate_config(cfg)
    depts = derive_departments(tasks or [], max_departments=corp["max_departments"])
    return len(depts) >= 2


# Public alias used by host/mode plumbing (mode overhaul C3).
should_activate_corporate = should_use_corporate


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Seat:
    """One agent seat on the corporate org chart."""

    agent_id: str
    rank: str  # director | head | team_lead | worker
    dept_id: str | None = None  # None for director
    role: str = ""
    parent_id: str | None = None  # delegation parent seat
    delegation_node: str | None = None
    status: str = "forming"  # forming | active | done | blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "rank": self.rank,
            "dept_id": self.dept_id,
            "role": self.role,
            "parent_id": self.parent_id,
            "delegation_node": self.delegation_node,
            "status": self.status,
        }


@dataclass
class ChannelMessage:
    """One message on a corporate channel."""

    id: str
    channel: str
    from_agent: str
    kind: str  # progress | question | digest | steering | info | blockage
    body: str
    ts: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "from_agent": self.from_agent,
            "kind": self.kind,
            "body": self.body,
            "ts": self.ts,
            "meta": dict(self.meta),
        }


@dataclass
class MeetingDigest:
    """Structured department status digest posted to the heads channel."""

    dept_id: str
    head_id: str
    done: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    trigger: str = MEETING_TRIGGER_COMPLETION
    worker_completions: int = 0
    ts: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"{CORPORATE_SCHEMA}#digest",
            "dept_id": self.dept_id,
            "head_id": self.head_id,
            "done": list(self.done),
            "in_progress": list(self.in_progress),
            "blocked": list(self.blocked),
            "risks": list(self.risks),
            "trigger": self.trigger,
            "worker_completions": self.worker_completions,
            "ts": self.ts,
        }


@dataclass
class DirectorReview:
    """Director's response to a meeting digest (template-first, LLM optional)."""

    digest_dept: str
    aligned: bool
    needs_llm: bool
    notes: list[str] = field(default_factory=list)
    steering: str = ""
    ts: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"{CORPORATE_SCHEMA}#director_review",
            "digest_dept": self.digest_dept,
            "aligned": self.aligned,
            "needs_llm": self.needs_llm,
            "notes": list(self.notes),
            "steering": self.steering,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# OrgChart
# ---------------------------------------------------------------------------


class CorporateError(ValueError):
    """Corporate org rule violation (routing, depth, config)."""


class OrgChart:
    """Per-run corporate organization chart + enforced channel routing.

    Built once per mission when corporate mode engages. Heads spawn workers
    through :meth:`spawn_worker` (respects max_subagent_depth). Communication
    goes through :meth:`post_message` which rejects illegal routes.
    """

    def __init__(
        self,
        run_id: str,
        *,
        mission_goals: list[str] | None = None,
        cfg: dict | None = None,
        board: Any | None = None,
        max_departments: int | None = None,
        team_leads: bool | None = None,
        meeting_cadence: int | None = None,
        director_review: bool | None = None,
        max_subagent_depth: int | None = None,
        on_event: Callable[..., None] | None = None,
        session_id: str = "",
        runs_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id or f"r_{uuid.uuid4().hex[:8]}"
        self.session_id = session_id
        self._cfg = cfg
        corp = get_corporate_config(cfg)
        self.max_departments = (
            max(1, int(max_departments))
            if max_departments is not None
            else corp["max_departments"]
        )
        self.team_leads_enabled = (
            bool(team_leads) if team_leads is not None else corp["team_leads"]
        )
        self.meeting_cadence = (
            max(1, int(meeting_cadence))
            if meeting_cadence is not None
            else corp["meeting_cadence"]
        )
        self.director_review_enabled = (
            bool(director_review)
            if director_review is not None
            else corp["director_review"]
        )
        # Stored global nesting cap (NEVER mutated — effective lead→worker
        # depth is computed locally in spawn_worker via effective_corporate_depth_cap).
        self.max_subagent_depth = (
            max(0, int(max_subagent_depth))
            if max_subagent_depth is not None
            else get_max_subagent_depth(cfg)
        )
        self.mission_goals: list[str] = [
            str(g).strip() for g in (mission_goals or []) if str(g).strip()
        ]
        self.board = board  # optional CoordinationBoard for progress mirror
        self.on_event = on_event  # live agent event sink
        self._runs_dir = runs_dir
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []  # ordered corporate event log

        self.director_id = f"director_{self.run_id[:12]}"
        self.seats: dict[str, Seat] = {
            self.director_id: Seat(
                agent_id=self.director_id,
                rank=RANK_DIRECTOR,
                dept_id=None,
                role="orchestrator",
                status="active",
            ),
        }
        self.departments: list[dict[str, Any]] = []
        # channel_id → membership agent_ids
        self.channels: dict[str, set[str]] = {
            CHANNEL_HEADS: {self.director_id},
        }
        self.messages: list[ChannelMessage] = []
        self.digests: list[MeetingDigest] = []
        self.reviews: list[DirectorReview] = []
        # dept_id → worker completion count since last meeting
        self._completions_since_meeting: dict[str, int] = {}
        self._total_worker_completions: dict[str, int] = {}
        self.chart_revision = 0
        self.active = False
        self._wound_down = False

    def _emit(
        self,
        corp_event: str,
        *,
        dept: Any = None,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record + stream a corporate event (ordered, best-effort)."""
        body = emit_corporate_event(
            corp_event,
            on_event=self.on_event,
            run_id=self.run_id,
            dept=dept,
            agent=agent,
            payload=payload,
            chart_revision=self.chart_revision,
        )
        self._events.append(body)
        return body

    # -- build ----------------------------------------------------------------

    def build_from_tasks(
        self,
        tasks: Iterable[Any],
        *,
        head_role: str = "department_manager",
    ) -> dict[str, Any]:
        """Derive departments from *tasks* and seat a head per department.

        Returns the chart snapshot. Idempotent rebuild bumps chart_revision
        and re-seats heads for new departments (existing seats kept).
        Heads start as ``forming`` until :meth:`seat_heads` hires them live.
        """
        with self._lock:
            depts = derive_departments(tasks, max_departments=self.max_departments)
            if len(depts) < 2:
                self.active = False
                self.departments = depts
                return self.snapshot()

            self.departments = depts
            for d in depts:
                did = d["id"]
                head_id = f"head_{did}_{self.run_id[:8]}"
                if head_id not in self.seats:
                    self.seats[head_id] = Seat(
                        agent_id=head_id,
                        rank=RANK_HEAD,
                        dept_id=did,
                        role=head_role,
                        parent_id=self.director_id,
                        status="forming",
                    )
                d["head_id"] = head_id
                d.setdefault("workers", [])
                d.setdefault("team_lead_id", None)
                # dept channel: head (+ lead/workers added later)
                ch = f"{CHANNEL_DEPT_PREFIX}{did}"
                self.channels.setdefault(ch, set()).add(head_id)
                self.channels[CHANNEL_HEADS].add(head_id)
                self._completions_since_meeting.setdefault(did, 0)
                self._total_worker_completions.setdefault(did, 0)

            self.chart_revision += 1
            self.active = True
            snap = self.snapshot()
            self._emit(
                CORP_EVENT_ORG_BUILT,
                payload={
                    "chart_revision": self.chart_revision,
                    "director_id": self.director_id,
                    "departments": [
                        {
                            "id": d.get("id"),
                            "name": d.get("name"),
                            "head_id": d.get("head_id"),
                            "workers": list(d.get("workers") or []),
                            "team_lead_id": d.get("team_lead_id"),
                        }
                        for d in self.departments
                    ],
                    "mission_goals": list(self.mission_goals),
                },
            )
            return snap

    # -- membership -----------------------------------------------------------

    def seat_for(self, agent_id: str) -> Seat | None:
        return self.seats.get(str(agent_id or "").strip())

    def head_for_dept(self, dept_id: str) -> Seat | None:
        for s in self.seats.values():
            if s.rank == RANK_HEAD and s.dept_id == dept_id:
                return s
        return None

    def workers_in_dept(self, dept_id: str) -> list[Seat]:
        return [
            s
            for s in self.seats.values()
            if s.dept_id == dept_id and s.rank in (RANK_WORKER, RANK_TEAM_LEAD)
        ]

    def dept_channel(self, dept_id: str) -> str:
        return f"{CHANNEL_DEPT_PREFIX}{dept_id}"

    # -- spawn (heads only) ---------------------------------------------------

    def spawn_worker(
        self,
        *,
        head_id: str,
        role: str,
        brief: str = "",
        worker_id: str | None = None,
        as_team_lead: bool = False,
        delegation_node: str | None = None,
    ) -> dict[str, Any]:
        """Register a worker (or team lead) under a department head.

        Enforces:
          * only heads (or existing team leads for workers) may spawn
          * depth = head→worker=2; team_lead→worker=3 must be ≤ *effective*
            cap (local: ``max(global max_subagent_depth, 3)`` for lead→worker
            when team_leads on — global config is NEVER mutated)
          * team lead auto-promotion when workers exceed threshold and
            team_leads is enabled

        Returns the new seat dict or ``{ok: False, error: ...}``.
        """
        head_id = str(head_id or "").strip()
        role = str(role or "").strip()
        if not head_id or not role:
            return {"ok": False, "error": "head_id and role are required"}

        with self._lock:
            head = self.seats.get(head_id)
            if head is None:
                return {"ok": False, "error": f"unknown head_id {head_id!r}"}
            if head.rank not in (RANK_HEAD, RANK_TEAM_LEAD):
                return {
                    "ok": False,
                    "error": (
                        f"only department heads (or team leads) may spawn workers; "
                        f"{head_id!r} is rank={head.rank!r}"
                    ),
                }
            dept_id = head.dept_id
            if not dept_id:
                return {"ok": False, "error": "spawner has no department"}

            # Depth: director=0, head=1, worker/lead=2, team_lead→worker=3
            child_depth = 2 if head.rank == RANK_HEAD else 3
            effective_cap = effective_corporate_depth_cap(
                self.max_subagent_depth,
                team_leads=self.team_leads_enabled,
                spawner_rank=head.rank,
            )
            if child_depth > effective_cap:
                return {
                    "ok": False,
                    "error": (
                        f"max subagent nesting depth ({effective_cap}) "
                        f"reached (attempted corporate worker depth {child_depth}; "
                        f"director=0, head=1, worker=2"
                        + (
                            f"; effective lead→worker cap={effective_cap} "
                            f"from global={self.max_subagent_depth}"
                            if head.rank == RANK_TEAM_LEAD
                            else ""
                        )
                        + ")"
                    ),
                    "max_subagent_depth": self.max_subagent_depth,
                    "effective_depth_cap": effective_cap,
                    "attempted_depth": child_depth,
                }

            rank = RANK_TEAM_LEAD if as_team_lead else RANK_WORKER
            wid = worker_id or f"{rank}_{dept_id}_{_slug(role)}_{uuid.uuid4().hex[:6]}"
            if wid in self.seats:
                return {"ok": False, "error": f"agent_id {wid!r} already seated"}

            seat = Seat(
                agent_id=wid,
                rank=rank,
                dept_id=dept_id,
                role=role,
                parent_id=head_id,
                delegation_node=delegation_node,
                status="active",
            )
            self.seats[wid] = seat
            ch = self.dept_channel(dept_id)
            self.channels.setdefault(ch, set()).add(wid)
            # Update department roster
            for d in self.departments:
                if d.get("id") == dept_id:
                    workers = list(d.get("workers") or [])
                    if wid not in workers:
                        workers.append(wid)
                    d["workers"] = workers
                    if rank == RANK_TEAM_LEAD:
                        d["team_lead_id"] = wid
                    break

            # Auto team-lead when over threshold
            auto_lead: dict[str, Any] | None = None
            if self.team_leads_enabled and not as_team_lead and rank == RANK_WORKER:
                n_workers = len(
                    [
                        s
                        for s in self.seats.values()
                        if s.dept_id == dept_id and s.rank == RANK_WORKER
                    ]
                )
                has_lead = any(
                    s.rank == RANK_TEAM_LEAD and s.dept_id == dept_id
                    for s in self.seats.values()
                )
                if n_workers > _TEAM_LEAD_THRESHOLD and not has_lead:
                    # Promote the first worker to team lead (deterministic: sorted id)
                    workers = sorted(
                        (
                            s
                            for s in self.seats.values()
                            if s.dept_id == dept_id and s.rank == RANK_WORKER
                        ),
                        key=lambda s: s.agent_id,
                    )
                    if workers:
                        promote = workers[0]
                        prev_rank = promote.rank
                        promote.rank = RANK_TEAM_LEAD
                        for d in self.departments:
                            if d.get("id") == dept_id:
                                d["team_lead_id"] = promote.agent_id
                                break
                        auto_lead = promote.to_dict()
                        self._emit(
                            CORP_EVENT_TEAM_LEAD,
                            dept=dept_id,
                            agent=promote.agent_id,
                            payload={
                                "worker_id": promote.agent_id,
                                "dept_id": dept_id,
                                "previous_rank": prev_rank,
                            },
                        )

            self._emit(
                CORP_EVENT_HIRE,
                dept=dept_id,
                agent=wid,
                payload={
                    "role": role,
                    "rank": rank,
                    "parent_id": head_id,
                    "dept_id": dept_id,
                    "brief": brief or "",
                },
            )

            out: dict[str, Any] = {
                "ok": True,
                "seat": seat.to_dict(),
                "dept_id": dept_id,
                "channel": ch,
                "depth": child_depth,
                "effective_depth_cap": effective_cap,
                "global_max_subagent_depth": self.max_subagent_depth,
            }
            if auto_lead is not None:
                out["auto_team_lead"] = auto_lead
            return out

    # -- communication (ENFORCED) ---------------------------------------------

    def _allowed_channels(self, agent_id: str) -> set[str]:
        seat = self.seats.get(agent_id)
        if seat is None:
            return set()
        if seat.rank == RANK_DIRECTOR:
            return {CHANNEL_HEADS}
        if seat.rank == RANK_HEAD:
            chs = {CHANNEL_HEADS}
            if seat.dept_id:
                chs.add(self.dept_channel(seat.dept_id))
            return chs
        # team lead / worker — dept only
        if seat.dept_id:
            return {self.dept_channel(seat.dept_id)}
        return set()

    def _reject_route(
        self,
        *,
        agent: str,
        channel: str,
        reason: str,
        from_rank: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a rejected-route response and emit route_rejected (visible, muted)."""
        out: dict[str, Any] = {
            "ok": False,
            "status": "rejected",
            "error": reason,
            "from_rank": from_rank,
            "attempted_channel": channel,
        }
        if extra:
            out.update(extra)
        self._emit(
            CORP_EVENT_ROUTE_REJECTED,
            dept=None,
            agent=agent,
            payload={
                "from": agent,
                "channel": channel,
                "reason": reason,
                "from_rank": from_rank,
            },
        )
        return out

    def post_message(
        self,
        from_agent: str,
        channel: str,
        body: str,
        *,
        kind: str = "info",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post to a corporate channel with ENFORCED membership + rank rules.

        Rules:
          * Workers/team leads may only post to their dept channel.
          * Heads may post to their dept channel and the heads channel.
          * Director may post only to the heads channel.
          * Direct worker → director (any attempt to address the director or
            post on heads as a worker) is REJECTED.
        """
        agent = str(from_agent or "").strip()
        ch = str(channel or "").strip()
        text = str(body or "").strip()
        kind_s = str(kind or "info").strip().lower() or "info"
        if not agent:
            return {"ok": False, "error": "from_agent must be non-empty"}
        if not ch:
            return {"ok": False, "error": "channel must be non-empty"}
        if not text:
            return {"ok": False, "error": "body must be non-empty"}
        if len(text) > 2000:
            text = text[:1999].rstrip() + "…"

        with self._lock:
            seat = self.seats.get(agent)
            if seat is None:
                return {
                    "ok": False,
                    "error": f"unknown corporate agent {agent!r}; not on org chart",
                }

            # Explicit worker → director rejection (even if they invent a channel)
            if seat.rank in (RANK_WORKER, RANK_TEAM_LEAD):
                if (
                    ch == CHANNEL_HEADS
                    or ch == self.director_id
                    or ch.startswith("director")
                ):
                    return self._reject_route(
                        agent=agent,
                        channel=ch,
                        reason=(
                            "worker→director channel is forbidden under corporate "
                            "mode; post to your department channel "
                            f"({self.dept_channel(seat.dept_id) if seat.dept_id else 'dept:<id>'}) "
                            "and let your department head escalate"
                        ),
                        from_rank=seat.rank,
                        extra={
                            "hint": "workers ↔ their department head only; heads ↔ director",
                        },
                    )
                # Also reject any meta that targets the director
                if meta and str(meta.get("to") or meta.get("to_agent") or "") in (
                    self.director_id,
                    RANK_DIRECTOR,
                    "director",
                ):
                    return self._reject_route(
                        agent=agent,
                        channel=ch,
                        reason=(
                            "direct worker→director messages are rejected; "
                            "route through your department head"
                        ),
                        from_rank=seat.rank,
                    )

            allowed = self._allowed_channels(agent)
            if ch not in allowed:
                return self._reject_route(
                    agent=agent,
                    channel=ch,
                    reason=(
                        f"agent {agent!r} (rank={seat.rank}) may not post to "
                        f"channel {ch!r}; allowed={sorted(allowed)}"
                    ),
                    from_rank=seat.rank,
                    extra={"allowed_channels": sorted(allowed)},
                )

            # Membership check
            members = self.channels.get(ch)
            if members is None:
                return {
                    "ok": False,
                    "error": f"unknown channel {ch!r}",
                }
            if agent not in members:
                # Should not happen if allowed is correct, but belt-and-suspenders
                return {
                    "ok": False,
                    "status": "rejected",
                    "error": f"agent {agent!r} is not a member of channel {ch!r}",
                }

            msg = ChannelMessage(
                id=_fresh_id("msg"),
                channel=ch,
                from_agent=agent,
                kind=kind_s,
                body=text,
                ts=_now(),
                meta=dict(meta or {}),
            )
            self.messages.append(msg)

            # Mirror progress notes onto the coordination board when present
            if self.board is not None and kind_s in (
                "progress",
                "question",
                "blockage",
            ):
                try:
                    self.board.post_progress(agent, f"[{ch}] {text[:400]}")
                except Exception:  # noqa: BLE001
                    pass

            return {"ok": True, "message": msg.to_dict()}

    def read_channel(
        self,
        channel: str,
        *,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read recent messages on *channel* (membership enforced when agent_id set)."""
        ch = str(channel or "").strip()
        with self._lock:
            if agent_id:
                aid = str(agent_id).strip()
                allowed = self._allowed_channels(aid)
                if ch not in allowed:
                    return {
                        "ok": False,
                        "status": "rejected",
                        "error": (
                            f"agent {aid!r} may not read channel {ch!r}; "
                            f"allowed={sorted(allowed)}"
                        ),
                    }
            msgs = [m.to_dict() for m in self.messages if m.channel == ch]
            if limit > 0:
                msgs = msgs[-limit:]
            return {
                "ok": True,
                "channel": ch,
                "messages": msgs,
                "count": len(msgs),
            }

    # -- meetings / digests ---------------------------------------------------

    def record_worker_completion(
        self,
        dept_id: str,
        *,
        worker_id: str | None = None,
        summary: str = "",
        blocked: bool = False,
    ) -> dict[str, Any]:
        """Count a worker task completion; maybe trigger a head meeting.

        Returns ``{ok, completions_since_meeting, meeting, posted, post_error?}``.
        When *summary* + *worker_id* are given, the dept-channel post result is
        captured (not discarded): rejected/unseated posts set ``posted=False``
        and ``post_error`` while the completion still counts.
        """
        did = str(dept_id or "").strip()
        with self._lock:
            if not any(d.get("id") == did for d in self.departments):
                return {"ok": False, "error": f"unknown department {did!r}"}

            self._total_worker_completions[did] = (
                self._total_worker_completions.get(did, 0) + 1
            )
            self._completions_since_meeting[did] = (
                self._completions_since_meeting.get(did, 0) + 1
            )
            count = self._completions_since_meeting[did]

            posted: bool | None = None
            post_error: str | None = None
            post_result: dict[str, Any] | None = None
            if summary and worker_id:
                post_result = self.post_message(
                    worker_id,
                    self.dept_channel(did),
                    summary,
                    kind="progress" if not blocked else "blockage",
                )
                posted = bool(post_result.get("ok"))
                if not posted:
                    post_error = str(
                        post_result.get("error")
                        or post_result.get("message")
                        or "dept channel post rejected"
                    )

            meeting: dict[str, Any] | None = None
            if blocked:
                meeting = self._hold_meeting_unlocked(
                    did,
                    trigger=MEETING_TRIGGER_BLOCKAGE,
                )
            elif count >= self.meeting_cadence:
                meeting = self._hold_meeting_unlocked(
                    did,
                    trigger=MEETING_TRIGGER_COMPLETION,
                )

            out: dict[str, Any] = {
                "ok": True,
                "dept_id": did,
                "completions_since_meeting": (
                    0 if meeting else self._completions_since_meeting.get(did, 0)
                ),
                "total_completions": self._total_worker_completions.get(did, 0),
                "meeting": meeting,
                "posted": posted,
            }
            if post_error is not None:
                out["post_error"] = post_error
            if post_result is not None:
                out["post"] = post_result
            return out

    def hold_meeting(
        self,
        dept_id: str,
        *,
        done: list[str] | None = None,
        in_progress: list[str] | None = None,
        blocked: list[str] | None = None,
        risks: list[str] | None = None,
        trigger: str = MEETING_TRIGGER_MANUAL,
    ) -> dict[str, Any]:
        """Explicitly hold a department-head meeting (posts digest to heads)."""
        with self._lock:
            return self._hold_meeting_unlocked(
                dept_id,
                done=done,
                in_progress=in_progress,
                blocked=blocked,
                risks=risks,
                trigger=trigger,
            ) or {"ok": False, "error": "meeting failed"}

    def _hold_meeting_unlocked(
        self,
        dept_id: str,
        *,
        done: list[str] | None = None,
        in_progress: list[str] | None = None,
        blocked: list[str] | None = None,
        risks: list[str] | None = None,
        trigger: str = MEETING_TRIGGER_COMPLETION,
    ) -> dict[str, Any] | None:
        did = str(dept_id or "").strip()
        head = self.head_for_dept(did)
        if head is None:
            return {"ok": False, "error": f"no head for department {did!r}"}

        # Auto-assemble digest from recent dept channel traffic when not provided
        if done is None or in_progress is None or blocked is None or risks is None:
            auto = self._auto_digest_fields(did)
            done = done if done is not None else auto["done"]
            in_progress = (
                in_progress if in_progress is not None else auto["in_progress"]
            )
            blocked = blocked if blocked is not None else auto["blocked"]
            risks = risks if risks is not None else auto["risks"]

        digest = MeetingDigest(
            dept_id=did,
            head_id=head.agent_id,
            done=list(done or []),
            in_progress=list(in_progress or []),
            blocked=list(blocked or []),
            risks=list(risks or []),
            trigger=trigger,
            worker_completions=self._total_worker_completions.get(did, 0),
            ts=_now(),
        )
        self.digests.append(digest)
        self._completions_since_meeting[did] = 0

        body = (
            f"MEETING DIGEST [{did}] trigger={trigger}\n"
            f"done({len(digest.done)}): {'; '.join(digest.done) or '—'}\n"
            f"in_progress({len(digest.in_progress)}): "
            f"{'; '.join(digest.in_progress) or '—'}\n"
            f"blocked({len(digest.blocked)}): {'; '.join(digest.blocked) or '—'}\n"
            f"risks({len(digest.risks)}): {'; '.join(digest.risks) or '—'}"
        )
        post = self.post_message(
            head.agent_id,
            CHANNEL_HEADS,
            body,
            kind="digest",
            meta={"digest": digest.to_dict()},
        )

        self._emit(
            CORP_EVENT_MEETING,
            dept=did,
            agent=head.agent_id,
            payload={
                **digest.to_dict(),
                "counts": {
                    "done": len(digest.done),
                    "in_progress": len(digest.in_progress),
                    "blocked": len(digest.blocked),
                    "risks": len(digest.risks),
                },
            },
        )

        review: dict[str, Any] | None = None
        if self.director_review_enabled:
            rev = self._director_review_unlocked(digest)
            # LLM refine only on blockage/misalignment (needs_llm).
            if rev.needs_llm and rev.steering:
                refined = self._llm_director_steering(digest, rev)
                if refined:
                    rev.steering = refined
                    rev.notes = list(rev.notes) + ["llm steering refined"]
            self.reviews.append(rev)
            review = rev.to_dict()
            if rev.steering:
                self.post_message(
                    self.director_id,
                    CHANNEL_HEADS,
                    rev.steering,
                    kind="steering",
                    meta={"to_dept": did, "review": review},
                )
                self._emit(
                    CORP_EVENT_STEERING,
                    dept=did,
                    agent=self.director_id,
                    payload={
                        "text": rev.steering,
                        "to_dept": did,
                        "needs_llm": rev.needs_llm,
                        "aligned": rev.aligned,
                        "notes": list(rev.notes),
                        "llm_refined": any("llm steering" in n for n in rev.notes),
                    },
                )
                # Auto-relay blockage steering to dept workers (cheap).
                if digest.trigger == MEETING_TRIGGER_BLOCKAGE or rev.needs_llm:
                    try:
                        self.post_message(
                            head.agent_id,
                            self.dept_channel(did),
                            f"[Director steering] {rev.steering[:800]}",
                            kind="steering",
                            meta={"from_director": True, "to_dept": did},
                        )
                    except Exception:  # noqa: BLE001
                        pass

        return {
            "ok": True,
            "digest": digest.to_dict(),
            "post": post,
            "director_review": review,
        }

    def _auto_digest_fields(self, dept_id: str) -> dict[str, list[str]]:
        ch = self.dept_channel(dept_id)
        recent = [m for m in self.messages if m.channel == ch][-30:]
        done: list[str] = []
        in_progress: list[str] = []
        blocked: list[str] = []
        risks: list[str] = []
        for m in recent:
            snippet = m.body[:120]
            if m.kind == "blockage" or "block" in m.body.lower():
                blocked.append(f"[{m.from_agent}] {snippet}")
            elif m.kind == "progress" and any(
                w in m.body.lower() for w in ("done", "complete", "finished")
            ):
                done.append(f"[{m.from_agent}] {snippet}")
            elif m.kind == "progress":
                in_progress.append(f"[{m.from_agent}] {snippet}")
            elif m.kind == "question":
                risks.append(f"[{m.from_agent}] Q: {snippet}")
        return {
            "done": done[-8:],
            "in_progress": in_progress[-8:],
            "blocked": blocked[-8:],
            "risks": risks[-8:],
        }

    def _director_review_unlocked(self, digest: MeetingDigest) -> DirectorReview:
        """Template-driven alignment check; flag LLM only on misalignment/blockage."""
        notes: list[str] = []
        aligned = True
        needs_llm = False

        if digest.blocked:
            aligned = False
            needs_llm = True
            notes.append(
                f"{len(digest.blocked)} blocked item(s) in {digest.dept_id} — "
                "director steering recommended"
            )
        if digest.risks:
            notes.append(f"{len(digest.risks)} risk/question item(s) noted")
            if len(digest.risks) >= 3:
                needs_llm = True
                aligned = False

        # Objective alignment: do any goals appear abandoned?
        if self.mission_goals:
            blob = " ".join(
                digest.done + digest.in_progress + digest.blocked + digest.risks
            ).lower()
            missing = []
            for g in self.mission_goals:
                # Simple keyword presence: first 3 significant tokens of the goal
                tokens = [
                    t
                    for t in re.findall(r"[a-z0-9]{4,}", g.lower())
                    if t not in ("with", "from", "that", "this", "into", "using")
                ][:3]
                if tokens and not any(t in blob for t in tokens):
                    # Only flag if there is SOME activity (empty digest is early)
                    if blob.strip():
                        missing.append(g)
            if missing:
                notes.append(
                    "possible objective drift — goals not reflected in digest: "
                    + "; ".join(missing[:3])
                )
                # Soft misalignment: note only unless also blocked
                if digest.blocked:
                    aligned = False
                    needs_llm = True

        if (
            not digest.done
            and not digest.in_progress
            and digest.trigger == MEETING_TRIGGER_BLOCKAGE
        ):
            aligned = False
            needs_llm = True
            notes.append("blockage meeting with no active work — investigate")

        steering = ""
        if not aligned:
            parts = [
                f"STEERING for dept={digest.dept_id}:",
            ]
            if digest.blocked:
                parts.append("Prioritize unblocking: " + "; ".join(digest.blocked[:3]))
            if digest.risks:
                parts.append(
                    "Address risks via dept channel: " + "; ".join(digest.risks[:2])
                )
            if self.mission_goals:
                parts.append(
                    "Re-align workers to mission goals: "
                    + "; ".join(self.mission_goals[:3])
                )
            parts.append(
                "Relay this note to workers on the dept channel; "
                "do not let workers contact the director directly."
            )
            steering = " ".join(parts)
            notes.append("template steering issued (LLM optional for refinement)")
        else:
            notes.append("aligned — no steering note required")

        return DirectorReview(
            digest_dept=digest.dept_id,
            aligned=aligned,
            needs_llm=needs_llm,
            notes=notes,
            steering=steering,
            ts=_now(),
        )

    def _llm_director_steering(
        self,
        digest: MeetingDigest,
        review: DirectorReview,
    ) -> str | None:
        """Refine template steering through one bounded Chudcode advisor turn.

        Any configuration, cancellation, provider, or parsing failure returns
        ``None`` so the deterministic director steering remains authoritative.
        """
        try:
            from services.agent import config as agent_config  # noqa: PLC0415
            from services.agent.role_models import (  # noqa: PLC0415
                clamp_subagent_effort,
                resolve_role_model,
                resolve_role_thinking,
            )

            cfg = (
                dict(self._cfg)
                if isinstance(self._cfg, dict)
                else dict(agent_config.load_config() or {})
            )
            assignment = resolve_role_model(
                "advisor",
                cfg,
                task_class="analysis",
                node_depth=0,
            )
            ambient_effort = cfg.get("reasoning_effort") or cfg.get("thinking")
            effort = resolve_role_thinking(
                "advisor",
                cfg,
                task_class="analysis",
                dispatch_effort=clamp_subagent_effort(ambient_effort),
                rank=getattr(assignment, "rank", None),
            )
            settings = {
                "provider": str(getattr(assignment, "provider", "") or ""),
                "model": str(getattr(assignment, "model", "") or ""),
            }
            if effort:
                settings["reasoning_effort"] = effort

            goals = "; ".join(self.mission_goals[:5]) or "(none)"
            digest_text = (
                f"dept={digest.dept_id} trigger={digest.trigger}\n"
                f"done={digest.done[:6]}\n"
                f"in_progress={digest.in_progress[:6]}\n"
                f"blocked={digest.blocked[:6]}\n"
                f"risks={digest.risks[:6]}\n"
                f"notes={list(review.notes)[:6]}"
            )
            request = (
                "COMPANY DIRECTOR COMPLETION CONTRACT\n"
                "Write ONE short steering note (at most 120 words) for a "
                "department head. Be concrete: unblock first, then re-align to "
                "mission goals. No preamble, no markdown, and no tool calls.\n\n"
                f"Mission goals: {goals}\n\n"
                f"Meeting digest:\n{digest_text}\n\n"
                f"Template steering (refine or keep):\n{review.steering}"
            )
            options: dict[str, Any] = {
                "config": cfg,
                "settings": settings,
                "allowed_tools": [],
            }
            if agent_config.offline_enabled(cfg):
                options["offline"] = True
            turn = run_role_turn(
                request,
                "advisor",
                session_id=self.session_id,
                run_id=self.run_id,
                parent_id=(
                    f"{self.director_id}:meeting:{len(self.digests)}:{digest.dept_id}"
                ),
                on_event=self.on_event,
                options=options,
                max_tokens=400,
                max_total_tokens=400,
                close_session=True,
            )
            if (
                not isinstance(turn, dict)
                or not turn.get("success")
                or turn.get("cancelled")
            ):
                return None
            text = turn.get("result")
            if isinstance(text, dict):
                text = next(
                    (
                        text.get(key)
                        for key in ("text", "result", "message", "content")
                        if isinstance(text.get(key), str)
                    ),
                    "",
                )
            out = str(text or "").strip()
            if not out:
                return None
            if len(out) > 1200:
                out = out[:1199].rstrip() + "…"
            return out
        except Exception:  # noqa: BLE001 — deterministic template stays
            return None

    # -- live head hire -------------------------------------------------------

    def seat_heads(
        self,
        *,
        delegate_fn: Callable[..., Any] | None = None,
        head_role: str = "department_manager",
    ) -> dict[str, Any]:
        """Hire department heads as live subagents via *delegate_fn*.

        *delegate_fn* should match MissionDispatch.delegate / tree.delegate
        signature: ``(role=..., brief=..., budget_tokens=..., kind=..., parent_id=...)``.
        When None, heads stay chart-only (status remains forming / active if already).
        """
        hired: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        with self._lock:
            if not self.active:
                return {"ok": False, "error": "org not active", "hired": []}
            for d in self.departments:
                did = d.get("id")
                head_id = d.get("head_id")
                if not head_id:
                    continue
                seat = self.seats.get(head_id)
                if seat is None:
                    continue
                seat.role = seat.role or head_role
                # Resolve model/thinking (best-effort; stored on seat via meta-ish fields)
                model_info: dict[str, Any] | None = None
                try:
                    from services.agent.role_models import (  # noqa: PLC0415
                        resolve_role_model,
                        resolve_role_thinking,
                    )

                    assignment = resolve_role_model(
                        seat.role or head_role,
                        self._cfg,
                        task_class=(d.get("roles") or [None])[0]
                        if isinstance(d.get("roles"), list)
                        else None,
                        node_depth=1,
                    )
                    thinking = resolve_role_thinking(seat.role or head_role, self._cfg)
                    model_info = {
                        **(
                            assignment.to_dict()
                            if hasattr(assignment, "to_dict")
                            else {}
                        ),
                        "thinking": thinking
                        if not isinstance(thinking, dict)
                        else thinking,
                    }
                except Exception:  # noqa: BLE001
                    model_info = None

                tasks = d.get("tasks") or []
                brief_bits = [
                    str(t.get("brief") or t.get("name") or t.get("id") or "")
                    for t in tasks
                    if isinstance(t, dict)
                ]
                brief = (
                    f"Department head for {d.get('name') or did}. "
                    f"Objectives: {'; '.join(brief_bits[:6]) or 'own dept deliverables'}. "
                    f"Spawn workers via corporate_spawn_worker; hold meetings; "
                    f"relay director steering. Mission goals: "
                    f"{'; '.join(self.mission_goals[:3]) or '—'}"
                )
                node_out: dict[str, Any] | None = None
                if delegate_fn is not None:
                    try:
                        node_out = delegate_fn(
                            role=seat.role or head_role,
                            brief=brief,
                            budget_tokens=6000,
                            kind="head",
                            parent_id=None,
                        )
                        if isinstance(node_out, dict) and node_out.get("id"):
                            seat.delegation_node = str(node_out.get("id"))
                            node_out["corporate_agent_id"] = seat.agent_id
                            node_out["corporate_rank"] = seat.rank
                            node_out["corporate_dept"] = seat.dept_id
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"head_id": head_id, "error": str(exc)})
                        continue
                seat.status = "active"
                self._emit(
                    CORP_EVENT_HIRE,
                    dept=did,
                    agent=head_id,
                    payload={
                        "role": seat.role or head_role,
                        "rank": RANK_HEAD,
                        "parent_id": self.director_id,
                        "dept_id": did,
                        "brief": brief[:400],
                        "model": model_info,
                        "live": node_out is not None,
                    },
                )
                hired.append(
                    {
                        "seat": seat.to_dict(),
                        "delegation": node_out,
                        "model": model_info,
                    }
                )
        return {
            "ok": True,
            "hired": hired,
            "errors": errors,
            "count": len(hired),
        }

    # -- wind-down ------------------------------------------------------------

    def wind_down(
        self,
        *,
        success: bool = True,
        summary: str = "",
        drop: bool = True,
    ) -> dict[str, Any]:
        """Tear down the org: final digests, summary artifact, free registry.

        Safe to call multiple times (idempotent after first).
        """
        with self._lock:
            if self._wound_down:
                return {
                    "ok": True,
                    "already": True,
                    "active": self.active,
                }
            final_digests: list[dict[str, Any]] = []
            if self.active:
                for d in list(self.departments):
                    did = d.get("id")
                    if not did:
                        continue
                    try:
                        mtg = self._hold_meeting_unlocked(
                            did,
                            done=[f"dept {did} wind-down"],
                            in_progress=[],
                            blocked=[],
                            risks=[],
                            trigger=MEETING_TRIGGER_MANUAL,
                        )
                        if mtg and mtg.get("digest"):
                            final_digests.append(mtg["digest"])
                    except Exception:  # noqa: BLE001
                        pass
                    head = self.head_for_dept(did)
                    if head is not None:
                        head.status = "done"
                    for w in self.workers_in_dept(did):
                        w.status = "done"

            # Release coordination claims best-effort
            if self.board is not None:
                try:
                    for s in self.seats.values():
                        if hasattr(self.board, "release"):
                            self.board.release(s.agent_id, resources=None)
                except Exception:  # noqa: BLE001
                    pass

            artifact_path: str | None = None
            snap = self.snapshot()
            summary_text = summary or (
                f"Corporate org {'succeeded' if success else 'failed'} "
                f"with {len(self.departments)} department(s), "
                f"{len(self.digests)} meeting(s)."
            )
            # Emit wind_down first so it lands in the ordered event log inside
            # the artifact (artifact_path filled in after write).
            wind_payload = {
                "summary": summary_text,
                "success": bool(success),
                "departments_done": [d.get("id") for d in self.departments],
                "artifact_path": None,
                "digests": final_digests,
            }
            self._emit(CORP_EVENT_WIND_DOWN, payload=wind_payload)

            artifact = {
                "schema": CORPORATE_SCHEMA,
                "event": "corporate_org_finished",
                "run_id": self.run_id,
                "success": bool(success),
                "summary": summary_text,
                "chart": snap,
                "digests": [d.to_dict() for d in self.digests[-20:]],
                "reviews": [r.to_dict() for r in self.reviews[-20:]],
                "events": list(self._events),
                "ts": _now(),
            }
            try:
                rd = self._runs_dir or _runs_dir()
                rd.mkdir(parents=True, exist_ok=True)
                path = rd / f"{self.run_id}.corporate.json"
                tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
                # Patch path into last wind_down event + artifact before write
                artifact_path = str(path)
                wind_payload["artifact_path"] = artifact_path
                if (
                    self._events
                    and self._events[-1].get("corp_event") == CORP_EVENT_WIND_DOWN
                ):
                    self._events[-1].setdefault("payload", {})["artifact_path"] = (
                        artifact_path
                    )
                    artifact["events"] = list(self._events)
                tmp.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
                tmp.replace(path)
            except Exception:  # noqa: BLE001
                artifact_path = None

            self.active = False
            self._wound_down = True
            out = {
                "ok": True,
                "success": bool(success),
                "summary": summary_text,
                "artifact_path": artifact_path,
                "artifact": artifact,
                "final_digests": final_digests,
            }
        if drop:
            drop_org_chart(self.run_id)
        return out

    # -- preambles / tools ----------------------------------------------------

    def preamble_for(self, agent_id: str, *, max_chars: int = 2200) -> str:
        """System-prompt preamble describing the agent's corporate seat + channels."""
        seat = self.seats.get(str(agent_id or "").strip())
        if seat is None:
            return ""
        lines = [
            f"[Corporate org mode — {CORPORATE_SCHEMA}]",
            f"You are {seat.agent_id} rank={seat.rank}"
            + (f" dept={seat.dept_id}" if seat.dept_id else " (company director)"),
            f"role_card={seat.role or '—'}",
        ]
        allowed = sorted(self._allowed_channels(seat.agent_id))
        lines.append(f"Channels you may use: {', '.join(allowed) or '(none)'}")
        if self.mission_goals:
            if seat.rank == RANK_DIRECTOR:
                lines.append("Mission goals: " + "; ".join(self.mission_goals[:6]))
            elif seat.rank == RANK_HEAD:
                lines.append(
                    "Mission goals (dept-relevant): "
                    + "; ".join(self.mission_goals[:4])
                )
            else:
                lines.append(
                    "Mission goals (slice): " + "; ".join(self.mission_goals[:3])
                )
        lines.append(
            "Claim discipline: coordinate_claim before editing geometry/files; "
            "on CONFLICT wait or narrow; coordinate_release on exit."
        )
        lines.append(
            "Escalation ladder: worker → team_lead (if any) → head → director → human."
        )
        if seat.rank in (RANK_WORKER, RANK_TEAM_LEAD):
            lines.append(
                "COMMUNICATION RULE (enforced): post progress/questions only on "
                f"your dept channel. Direct messages to the director are REJECTED. "
                "Escalate via your department head."
            )
            lines.append(
                "Tools: corporate_channel_post(channel, body, kind?), "
                "corporate_channel_read(channel), "
                "corporate_record_completion, "
                "coordinate_claim/release/progress/board."
            )
        elif seat.rank == RANK_HEAD:
            dept = next(
                (d for d in self.departments if d.get("id") == seat.dept_id),
                None,
            )
            task_bits = []
            if dept:
                for t in (dept.get("tasks") or [])[:8]:
                    if isinstance(t, dict):
                        task_bits.append(
                            str(t.get("brief") or t.get("id") or t.get("role") or "")
                        )
                    else:
                        task_bits.append(str(t))
            lines.append(
                "You are a DEPARTMENT HEAD. Objectives + task roster: "
                + ("; ".join(task_bits) if task_bits else "(see mission goals)")
            )
            eff = effective_corporate_depth_cap(
                self.max_subagent_depth,
                team_leads=self.team_leads_enabled,
                spawner_rank=RANK_HEAD,
            )
            lines.append(
                "Spawn policy: corporate_spawn_worker within depth cap "
                f"(global={self.max_subagent_depth}, effective head→worker={eff}). "
                "Prefer the narrowest role card for each slice."
            )
            lines.append(
                f"Meeting duty: every {self.meeting_cadence} worker completions "
                "or on blockage → corporate_hold_meeting. Digest goes to heads; "
                "relay director steering (kind=steering, meta.to_dept=yours) to "
                "workers on the dept channel in the same turn."
            )
            lines.append(
                "Tools: corporate_spawn_worker, corporate_hold_meeting, "
                "corporate_channel_post, corporate_channel_read, "
                "corporate_record_completion, coordinate_* , delegate."
            )
        elif seat.rank == RANK_DIRECTOR:
            lines.append(
                "You are the DIRECTOR (oversight). Heads report via meeting digests "
                "on the heads channel. You do not message workers directly. "
                "Template review runs first; request LLM steering only when "
                "needs_llm is set on a review."
            )
            lines.append(
                "Tools: corporate_channel_read(channel='heads'), "
                "corporate_chart(), coordinate_board."
            )
        if seat.rank in (RANK_HEAD, RANK_DIRECTOR) and self.digests:
            lines.append("Recent digests:")
            for d in self.digests[-3:]:
                lines.append(
                    f"  - {d.dept_id}: done={len(d.done)} ip={len(d.in_progress)} "
                    f"blocked={len(d.blocked)} risks={len(d.risks)} "
                    f"trigger={d.trigger}"
                )
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text

    def tools_for(self, agent_id: str) -> list[str]:
        """Mission-dispatch tool names to expose for this seat."""
        seat = self.seats.get(str(agent_id or "").strip())
        if seat is None:
            return []
        base = [
            "corporate_channel_post",
            "corporate_channel_read",
            "corporate_chart",
        ]
        if seat.rank == RANK_HEAD:
            return base + [
                "corporate_spawn_worker",
                "corporate_hold_meeting",
                "corporate_record_completion",
                "delegate",
            ]
        if seat.rank == RANK_TEAM_LEAD:
            return base + [
                "corporate_spawn_worker",
                "corporate_record_completion",
            ]
        if seat.rank == RANK_WORKER:
            return base + ["corporate_record_completion"]
        # director
        return base

    # -- snapshot -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": CORPORATE_SCHEMA,
                "run_id": self.run_id,
                "active": self.active,
                "chart_revision": self.chart_revision,
                "director_id": self.director_id,
                "max_departments": self.max_departments,
                "team_leads": self.team_leads_enabled,
                "meeting_cadence": self.meeting_cadence,
                "director_review": self.director_review_enabled,
                "max_subagent_depth": self.max_subagent_depth,
                "mission_goals": list(self.mission_goals),
                "departments": [
                    {
                        "id": d.get("id"),
                        "name": d.get("name"),
                        "head_id": d.get("head_id"),
                        "team_lead_id": d.get("team_lead_id"),
                        "workers": list(d.get("workers") or []),
                        "roles": list(d.get("roles") or []),
                        "task_count": len(d.get("tasks") or []),
                        "completions": self._total_worker_completions.get(
                            d.get("id") or "", 0
                        ),
                        "completions_since_meeting": self._completions_since_meeting.get(
                            d.get("id") or "", 0
                        ),
                    }
                    for d in self.departments
                ],
                "seats": [s.to_dict() for s in self.seats.values()],
                "channels": {
                    ch: sorted(members) for ch, members in self.channels.items()
                },
                "digest_count": len(self.digests),
                "review_count": len(self.reviews),
                "message_count": len(self.messages),
            }


# ---------------------------------------------------------------------------
# Process-local registry
# ---------------------------------------------------------------------------

_CHARTS: dict[str, OrgChart] = {}
_CHARTS_LOCK = threading.RLock()


def get_org_chart(
    run_id: str,
    *,
    create: bool = True,
    cfg: dict | None = None,
    board: Any | None = None,
    mission_goals: list[str] | None = None,
    on_event: Callable[..., None] | None = None,
    session_id: str = "",
    runs_dir: Path | None = None,
) -> OrgChart | None:
    """Return the shared OrgChart for *run_id* (create on demand)."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _CHARTS_LOCK:
        chart = _CHARTS.get(rid)
        if chart is not None:
            if board is not None and chart.board is None:
                chart.board = board
            if mission_goals and not chart.mission_goals:
                chart.mission_goals = [
                    str(g).strip() for g in mission_goals if str(g).strip()
                ]
            if on_event is not None and chart.on_event is None:
                chart.on_event = on_event
            if runs_dir is not None and chart._runs_dir is None:
                chart._runs_dir = runs_dir
            if session_id:
                if chart.session_id and chart.session_id != session_id:
                    raise CorporateError("corporate parent session cannot change")
                chart.session_id = session_id
            return chart
        if not create:
            return None
        chart = OrgChart(
            rid,
            cfg=cfg,
            board=board,
            mission_goals=mission_goals,
            on_event=on_event,
            session_id=session_id,
            runs_dir=runs_dir,
        )
        _CHARTS[rid] = chart
        return chart


def drop_org_chart(run_id: str) -> None:
    rid = str(run_id or "").strip()
    with _CHARTS_LOCK:
        _CHARTS.pop(rid, None)


def clear_org_charts() -> None:
    """Test helper — wipe the process-local registry."""
    with _CHARTS_LOCK:
        _CHARTS.clear()


def build_corporate_org(
    run_id: str,
    tasks: Iterable[Any],
    *,
    cfg: dict | None = None,
    board: Any | None = None,
    mission_goals: list[str] | None = None,
    force: bool | None = None,
    on_event: Callable[..., None] | None = None,
    session_id: str = "",
    runs_dir: Path | None = None,
    seat_heads: bool = False,
    delegate_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """High-level entry: maybe build a corporate org for this mission.

    Returns::

        {"ok": True, "active": bool, "reason": str, "chart": snapshot|None}

    When corporate mode is off or <2 departments, ``active`` is False and
    flat delegation should proceed unchanged.
    """
    if not should_use_corporate(tasks, cfg=cfg, force=force):
        reason = "corporate_mode off"
        if force is False or not corporate_mode_enabled(cfg):
            if force is not True:
                reason = "corporate_mode off"
        depts = derive_departments(
            tasks or [],
            max_departments=get_corporate_config(cfg)["max_departments"],
        )
        if corporate_mode_enabled(cfg) or force is True:
            if len(depts) < 2:
                reason = f"need ≥2 departments, found {len(depts)}"
        return {
            "ok": True,
            "active": False,
            "reason": reason,
            "departments_found": len(depts),
            "chart": None,
        }

    chart = get_org_chart(
        run_id,
        create=True,
        cfg=cfg,
        board=board,
        mission_goals=mission_goals,
        on_event=on_event,
        session_id=session_id,
        runs_dir=runs_dir,
    )
    assert chart is not None
    snap = chart.build_from_tasks(tasks)
    heads_out: dict[str, Any] | None = None
    if chart.active and seat_heads:
        heads_out = chart.seat_heads(delegate_fn=delegate_fn)
    return {
        "ok": True,
        "active": bool(chart.active),
        "reason": "corporate org active" if chart.active else "build produced <2 depts",
        "departments_found": len(chart.departments),
        "chart": snap if chart.active else chart.snapshot(),
        "heads": heads_out,
    }


def auto_activate_corporate(
    run_id: str,
    tasks: Iterable[Any],
    *,
    cfg: dict | None = None,
    board: Any | None = None,
    mission_goals: list[str] | None = None,
    on_event: Callable[..., None] | None = None,
    session_id: str = "",
    runs_dir: Path | None = None,
    delegate_fn: Callable[..., Any] | None = None,
    seat_heads: bool = True,
    force: bool | None = None,
) -> dict[str, Any]:
    """Mission-start auto-activate: mode on AND ≥2 depts → build + hire heads.

    When *force* is True (Swarm / options.corporate_force), skips the config
    ``corporate_mode`` gate — still requires ≥2 departments.
    When *force* is None (default), never forces past the dept-count gate;
    single-dept missions stay flat.
    """
    return build_corporate_org(
        run_id,
        tasks,
        cfg=cfg,
        board=board,
        mission_goals=mission_goals,
        force=force,  # None → respect corporate_mode; True → swarm force
        on_event=on_event,
        session_id=session_id,
        runs_dir=runs_dir,
        seat_heads=seat_heads,
        delegate_fn=delegate_fn,
    )


def wind_down_corporate(
    run_id: str,
    *,
    success: bool = True,
    summary: str = "",
) -> dict[str, Any]:
    """Wind down an active org for *run_id* (no-op if none)."""
    chart = get_org_chart(run_id, create=False)
    if chart is None:
        return {"ok": True, "active": False, "reason": "no chart"}
    return chart.wind_down(success=success, summary=summary, drop=True)
