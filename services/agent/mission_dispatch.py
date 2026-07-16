"""mission_dispatch.py — run-scoped tool dispatch for mission agent turns (T-W32).

T-W30's REAL gym cycles diagnosed a *dispatch-coverage gap*: role cards allow
run-scoped tools (``ledger_get``/``ledger_note``, ``dag_plan``, ``ask_user``,
``delegate``, ``part_search``) that the process-wide battery/research resolver
cannot serve — they need mission context (the run's ledger handle, run_id,
autonomy, delegation tree).  Every such call errored ``"could not be resolved"``
in cycle-1-real-r3.

This module provides that context layer: a :class:`MissionDispatch` bound to one
mission run.  ``agent_turn.run_turn`` consults it BEFORE the global registry
fallback (``reviewer_exec._resolve_fn``), so:

  * a mission-scoped tool resolves against the run (mission hit), and
  * everything else falls through to the ONE process-wide registry (global
    fallback) — same singleton discipline as T-W30.

Honesty rules:
  * ``ask_user`` in an autonomous run never fabricates a human: it RECORDS the
    questions (mission ledger + interaction ledger) and returns each question's
    ``suggested`` default with ``source: "default"`` — the same contract-§4 path
    ``interaction.ask_user`` takes for a non-blocking batch.
  * ``delegate`` without a live delegation tree returns a structured refusal
    (never a fabricated sub-agent result) and records the request.
  * ``dag_plan`` inside a running mission does not recursively re-plan: it
    returns the CURRENT mission plan state (graph summary / stage log) and points
    the model at the replan_request seam for changes.
  * Handlers return plain dicts (``{"ok": bool, ...}``) — errors are data fed
    back to the model, mirroring the MCP tool modules they shadow.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

# Mirrors mcp_server/ledger_tools.py VALID_KINDS (the tool this shadows).
# T-DP1 additive: the four design-pipeline stage kinds (contract design_pipeline.md §4).
_LEDGER_NOTE_KINDS = {"brief", "decision", "dimension", "question",
                      "requirement", "stage", "artifact", "note",
                      "feature", "geometry_spec", "linkage", "build_node",
                      # mega-2 / grok45-2: criterion_result is a real ledger kind
                      # (T-W30); accept it on the note path too.
                      "criterion_result"}

# Model-facing aliases → canonical kinds (mega-2 live: kind=verdict from DELIVER;
# grok45-2: review_verdict / audit / deliverables_manifest).
_LEDGER_NOTE_KIND_ALIASES: dict[str, str] = {
    "verdict": "decision",
    "review_verdict": "decision",
    "audit": "decision",
    "ruling": "decision",
    "plan_ruling": "decision",
    "spec": "decision",
    # mega-3 residual: intake/organize models write kind=manifest_confirm
    "manifest_confirm": "decision",
    "confirm": "decision",
    "confirmation": "decision",
    "deliverables_manifest": "artifact",
    "manifest": "artifact",
    "report": "artifact",
    "battery": "artifact",
    "criterion": "criterion_result",
    "ac_result": "criterion_result",
    "acceptance": "criterion_result",
    "assumption": "decision",
    "verification": "artifact",
    "verification_result": "artifact",
}


def _coerce_ledger_payload(payload: Any) -> dict:
    """Normalize ledger_note payload to a dict (mega-24 leaf thrash).

    Live ollama leaves often send JSON-as-string (``payload='{}'`` /
    ``'{"name":"x"}'``). ``dict(str)`` raises
    ``dictionary update sequence element #0 has length 1`` and burns the
    BUILD budget on ledger_note retries.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError, ValueError):
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    if isinstance(payload, (list, tuple)):
        # Try list-of-pairs; else stash under items.
        try:
            return dict(payload)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return {"items": list(payload)}
    return {"value": payload}

# Model-facing section aliases → canonical ledger state keys (mega-3 residual:
# interfaces / results / notes thrash after Fixer2 all/events/manifest).
_LEDGER_GET_SECTION_ALIASES: dict[str, str] = {
    "interfaces": "dimensions",
    "interface": "dimensions",
    "fits": "dimensions",
    "results": "artifacts",
    "result": "artifacts",
    "outcomes": "artifacts",
    "notes": "stage_log",
    "note": "stage_log",
    "log": "stage_log",
    "plan": "build_nodes",
    "acceptance": "artifacts",
    "acceptance_criteria": "artifacts",
    "ac": "artifacts",
    "mission_brief": "brief",
    "mission brief": "brief",
    "criterion_result": "criterion_results",
    "criterion_results": "criterion_results",
    "ac_results": "criterion_results",
    "verification": "criterion_results",
    "verifications": "criterion_results",
    # mega-24 thrash: models capitalise section names (Decisions, Dimensions, Bom)
    "decisions": "decisions",
    "dimensions": "dimensions",
    "requirements": "requirements",
    "artifacts": "artifacts",
    "features": "features",
    "bom": "artifacts",
    "bill_of_materials": "artifacts",
    "airframe components": "artifacts",
    "airframe_components": "artifacts",
    "components": "artifacts",
}

# T-DP1: kinds that require a `provenance` field on write (the "never recall a
# number" rule) — dimension (contract §2) + geometry_spec (contract §3.2/§4).
_PROVENANCE_REQUIRED_KINDS = {"dimension", "geometry_spec"}


class MissionDispatch:
    """Run-scoped tool dispatcher for ONE mission.

    Args:
        run_id: the mission run id (ledger key).
        ledger: the mission's live ``services.agent.ledger.Ledger`` handle.  When
            None, ledger tools resolve to a structured error (never a crash).
        autonomy: the run's autonomy floor ("auto" | "approve" | "manual") — under
            "auto" ask_user accepts suggested defaults (contract §4).
        graph: the compiled aicad.dag/1 mission graph, when the caller has it
            (dag_plan renders its summary).
        delegation_tree: a live DelegationTree, when the mission runs the 1.3 org
            layer; without it ``delegate`` returns a structured refusal.
        doc_id: default doc context for tools that accept one.
    """

    #: The run-scoped tool families this layer serves (T-W32 DoD list + the
    #: Parts Library V2 sourcing surface — workstream 5) + coordination board
    #: + corporate org mode (aicad.corporate/1).
    TOOLS = ("ledger_get", "ledger_note", "dag_plan", "ask_user",
             "delegate", "part_search",
             "parts_taxonomy", "part_get", "part_source", "match_model",
             "reverse_engineer_product", "vendors_health",
             "scrape_enqueue", "scrape_queue_status", "import_artifact",
             "cam_tool_import",
             # Coordination board (shared claims / progress across sub-agents)
             "coordinate_claim", "coordinate_release",
             "coordinate_progress", "coordinate_board",
             # Corporate org mode channels / meetings / head spawn
             "corporate_channel_post", "corporate_channel_read",
             "corporate_chart", "corporate_spawn_worker",
             "corporate_hold_meeting", "corporate_record_completion",
             "corporate_activate")


    def __init__(self, *, run_id: str, ledger=None, autonomy: str = "auto",
                 graph: dict | None = None, delegation_tree=None,
                 doc_id: str | None = None, provider=None, on_event=None,
                 capability=None, tier: str | None = None,
                 phase: str | None = None,
                 task_class: str | None = None,
                 coordination_board=None,
                 org_chart=None,
                 corporate_agent_id: str | None = None) -> None:
        self.run_id = run_id
        self.ledger = ledger
        self.autonomy = autonomy
        self.graph = graph
        self.delegation_tree = delegation_tree
        self.doc_id = doc_id
        # Phase 4 (M11): when a *provider* is bound, ``delegate`` actually RUNS the
        # approved sub-agent (one bounded agent_turn, synchronous) instead of only
        # minting a node — so the registry session gets a real transcript and the
        # GUI's queued steering lands at the sub-agent's turn boundaries.
        # *on_event* is the mission's live event sink (agent.tool_call etc. stream
        # from inside the sub-agent turn through the same drain).
        self.provider = provider
        self.on_event = on_event
        # Capability dials (WI-2/4/…): optional profile or tier for scaffolds.
        self.capability = capability
        self.tier = tier
        self.phase = phase
        self.task_class = task_class
        # Shared per-run coordination board (claims + progress notes).
        if coordination_board is not None:
            self.coordination_board = coordination_board
        else:
            try:
                from services.agent.coordination import get_board  # noqa: PLC0415
                self.coordination_board = get_board(run_id) if run_id else None
            except Exception:  # noqa: BLE001
                self.coordination_board = None
        # Corporate org chart (aicad.corporate/1) — optional; activated when
        # agent.corporate_mode is on and the mission has ≥2 departments.
        self.org_chart = org_chart
        # Identity of the agent currently using this dispatch (for corporate
        # channel posts / spawn authority). Root/director when unset.
        self.corporate_agent_id = corporate_agent_id
        self._handlers: dict[str, Callable[..., Any]] = {
            "ledger_get": self.ledger_get,
            "ledger_note": self.ledger_note,
            "dag_plan": self.dag_plan,
            "ask_user": self.ask_user,
            "delegate": self.delegate,
            "part_search": self.part_search,
            "parts_taxonomy": self.parts_taxonomy,
            "part_get": self.part_get,
            "part_source": self.part_source,
            "match_model": self.match_model,
            "reverse_engineer_product": self.reverse_engineer_product,
            "vendors_health": self.vendors_health,
            "scrape_enqueue": self.scrape_enqueue,
            "scrape_queue_status": self.scrape_queue_status,
            "import_artifact": self.import_artifact,
            "cam_tool_import": self.cam_tool_import,
            "coordinate_claim": self.coordinate_claim,
            "coordinate_release": self.coordinate_release,
            "coordinate_progress": self.coordinate_progress,
            "coordinate_board": self.coordinate_board_snapshot,
            "corporate_channel_post": self.corporate_channel_post,
            "corporate_channel_read": self.corporate_channel_read,
            "corporate_chart": self.corporate_chart_snapshot,
            "corporate_spawn_worker": self.corporate_spawn_worker,
            "corporate_hold_meeting": self.corporate_hold_meeting,
            "corporate_record_completion": self.corporate_record_completion,
            "corporate_activate": self.corporate_activate,
        }


    # ------------------------------------------------------------------ resolve
    def resolve(self, tool: str) -> Optional[Callable[..., Any]]:
        """Return the bound handler for a mission-scoped *tool*, or None so the
        caller falls through to the global registry."""
        return self._handlers.get(tool)

    # ------------------------------------------------------------------ ledger
    def ledger_get(self, section: str | None = None, budget_tokens: int = 800,
                   run_id: str | None = None) -> dict:
        """Return the design ledger for this run, rendered within *budget_tokens*.

        section filters to one of brief | requirements | decisions | dimensions |
        questions | stage_log | artifacts | criterion_results | features |
        geometry_specs | linkages | build_nodes | manifest; omit (or
        section="all"/"*") for the full rendered ledger.
        """
        led = self._ledger(run_id)
        if led is None:
            return {"ok": False, "error": "no ledger bound to this mission run"}
        # Alias full-ledger section names → rendered ledger (omit section).
        if section in ("all", "*", "full", "entire", "everything"):
            section = None
        # mega-2: models ask for section="events" (raw event log).
        if section in ("events", "event_log", "history"):
            return {
                "ok": True, "run_id": led.run_id, "section": "events",
                "data": list(led.events),
                "event_count": len(led.events),
            }
        if section:
            # mega-3: alias model-facing section names before allow-list.
            # mega-24: casefold so Decisions/Dimensions/Bom resolve.
            sec_key = str(section).strip()
            sec_low = sec_key.lower()
            section = (
                _LEDGER_GET_SECTION_ALIASES.get(sec_low)
                or _LEDGER_GET_SECTION_ALIASES.get(sec_key)
                or sec_low
            )
            allowed = {"brief", "requirements", "decisions", "dimensions",
                       "questions", "stage_log", "artifacts", "criterion_results",
                       # T-DP1 design-pipeline sections (contract §4/§9)
                       "features", "geometry_specs", "linkages", "build_nodes",
                       # mega-2: intake manifest surface (also under artifacts)
                       "manifest", "events"}
            if section not in allowed:
                return {"ok": False, "error": f"unknown section {section!r}",
                        "allowed": sorted(allowed) + ["all"],
                        "aliases": dict(_LEDGER_GET_SECTION_ALIASES)}
            if section == "manifest":
                arts = led.state.get("artifacts") or []
                manifests = [a for a in arts
                             if (a.get("kind") == "manifest"
                                 or a.get("id") == "manifest")]
                data = manifests[0] if manifests else None
                # Prefer embedded payload when intake wrote it.
                if isinstance(data, dict) and data.get("manifest"):
                    data = data["manifest"]
                return {"ok": True, "run_id": led.run_id, "section": "manifest",
                        "data": data}
            return {"ok": True, "run_id": led.run_id, "section": section,
                    "data": led.state.get(section)}
        from services.agent.ledger import ledger_render
        rendered = ledger_render(led, budget_tokens)
        return {"ok": True, "run_id": led.run_id, "rendered": rendered,
                "event_count": len(led.events)}

    def ledger_note(self, kind: str = "note", payload: dict | None = None,
                    actor: str = "agent", run_id: str | None = None,
                    **extra: Any) -> dict:
        """Append a structured event to this run's design ledger.

        kind: brief | decision | dimension | question | requirement | stage |
        artifact | note | criterion_result (+ aliases: verdict→decision, …).
        Dimension events MUST carry a provenance field (tool result / calc /
        table / user answer) — contract rule.

        mega-2: models often omit ``id`` and only send ``name``/``value`` for
        decision/dimension. Synthesize a stable id from name so the write does
        not leave id-less rows that later KeyError on upsert. Also alias
        kind=verdict/review_verdict/audit → decision (DELIVER thrash).
        """
        led = self._ledger(run_id)
        if led is None:
            return {"ok": False, "error": "no ledger bound to this mission run"}
        # Normalize model aliases before allow-list (mega-2 kind=verdict, etc.).
        raw_kind = (kind or "note").strip().lower() if isinstance(kind, str) else "note"
        kind = _LEDGER_NOTE_KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in _LEDGER_NOTE_KINDS:
            return {
                "ok": False,
                "error": f"unknown kind {raw_kind!r}",
                "valid_kinds": sorted(
                    _LEDGER_NOTE_KINDS | set(_LEDGER_NOTE_KIND_ALIASES)
                ),
                "aliased_to": dict(_LEDGER_NOTE_KIND_ALIASES),
            }
        body = _coerce_ledger_payload(payload)
        body.update({k: v for k, v in extra.items() if not k.startswith("_")})
        # Stash original alias for audit when we remapped.
        if raw_kind != kind:
            body.setdefault("aliased_kind", raw_kind)
        # Promote common aliases so kind=decision/dimension always has id.
        if kind in ("decision", "dimension", "question", "artifact",
                    "feature", "geometry_spec", "linkage", "build_node",
                    "criterion_result"):
            if not body.get("id"):
                for alt in ("name", "decision_id", "dim_id", "req_id",
                            "artifact_id", "feature_id", "spec_id", "node_id",
                            "deliverable", "criterion_id"):
                    if body.get(alt):
                        body["id"] = str(body[alt])
                        break
            if not body.get("id") and body.get("text"):
                slug = "".join(
                    c if c.isalnum() else "_"
                    for c in str(body["text"])[:48].lower()
                ).strip("_")
                body["id"] = slug or f"{kind}_{len(getattr(led, 'events', []) or [])}"
            if not body.get("id"):
                body["id"] = f"{kind}_{len(getattr(led, 'events', []) or [])}"
        if kind == "artifact" and raw_kind != kind:
            body.setdefault("kind", raw_kind)
        # decision alias from verdict often lacks stage/text/rationale
        if kind == "decision":
            body.setdefault("stage", body.get("phase") or "verify")
            if not body.get("text"):
                body["text"] = (
                    body.get("summary")
                    or body.get("measured")
                    or body.get("value")
                    or str(body.get("id") or "decision")
                )
            body.setdefault("rationale", body.get("evidence") or body.get("driver") or "")
        if kind in _PROVENANCE_REQUIRED_KINDS and not body.get("provenance"):
            return {"ok": False,
                    "error": (f"{kind} events require a 'provenance' field "
                              "(tool result / calc / table / user answer)")}
        try:
            event = led.append(actor, kind, body)
            return {"ok": True, "seq": event["seq"], "run_id": led.run_id,
                    "kind": kind, "ts": event["ts"],
                    **({"aliased_from": raw_kind} if raw_kind != kind else {})}
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"ledger write failed: {exc}"}

    def _ledger(self, run_id: str | None = None):
        """The bound ledger.  A model-supplied run_id for a DIFFERENT run is
        ignored in favour of the mission's own — a node never writes outside its
        run (scoping is the point of this layer)."""
        return self.ledger

    # ------------------------------------------------------------------ plan
    def dag_plan(self, brief: str | None = None, **_ignored: Any) -> dict:
        """Return the CURRENT mission plan (this mission is already compiled).

        Inside a running mission dag_plan does not recursively re-plan; to change
        the plan, emit a replan_request in your handoff.
        """
        out: dict[str, Any] = {"ok": True, "run_id": self.run_id,
                               "note": ("mission plan already compiled; to change it, "
                                        "emit a replan_request in your handoff")}
        if self.graph:
            nodes = [{"id": n.get("id"), "type": n.get("type"), "name": n.get("name"),
                      "role": (n.get("inputs") or {}).get("role")}
                     for n in self.graph.get("nodes", [])]
            out["graph"] = {"id": self.graph.get("id"), "name": self.graph.get("name"),
                            "nodes": nodes, "edges": self.graph.get("edges", [])}
        elif self.ledger is not None:
            out["stage_log"] = self.ledger.state.get("stage_log")
        return out

    # ------------------------------------------------------------------ ask_user
    def ask_user(self, questions: Any = None, question: Any = None,
                 scope: str = "run", **_ignored: Any) -> dict:
        """Ask the human a typed question batch (contract §4).

        In an autonomous run there is no blocking transport: every question is
        RECORDED on the run ledger (a human reviews them later) and answered with
        its suggested default (source: "default") — never a fabricated human reply.
        """
        raw = questions if questions is not None else question
        norm = _normalize_questions(raw)
        if not norm:
            return {"ok": False,
                    "error": ("ask_user needs questions=[{id, prompt, suggested, "
                              "kind?}] (or a plain string)")}
        try:
            from services.agent import interaction
            answers = interaction.ask_user(
                norm, scope="run", run_id=self.run_id, blocking=False,
                autonomy=self.autonomy)
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"ask_user failed: {exc}"}
        # Mirror the Q&A onto the mission design ledger so the run record (and the
        # gym classifier) see the questions asked.
        if self.ledger is not None:
            for q in norm:
                try:
                    self.ledger.add_question(
                        "agent", q["id"], q.get("prompt", ""), blocking=False)
                    ans = answers.get(q["id"]) or {}
                    self.ledger.answer_question(
                        "agent", q["id"], json.dumps(ans.get("value"), default=str))
                except Exception:  # noqa: BLE001 — mirroring is best-effort
                    pass
        return {"ok": True, "answers": answers,
                "note": ("autonomous run: questions recorded for the human; "
                         "suggested defaults applied (source per answer)")}

    # ------------------------------------------------------------------ delegate
    # Model-facing role aliases → loadable role cards (mega-5: part_fixer invented).
    # mega-7: part_designer / edit-part appeared on live agent.spawn (attempt-2);
    # map skill-ish and designer names onto feature_modeler so children get
    # new_part/add_features allowlist instead of a bare unknown role.
    _ROLE_ALIASES = {
        "part_fixer": "feature_modeler",
        "geometry_fixer": "feature_modeler",
        "cad_fixer": "feature_modeler",
        "part_modeler": "feature_modeler",
        "part_designer": "feature_modeler",
        "geometry_designer": "geometry_designer",  # real card if present
        "edit-part": "feature_modeler",
        "edit_part": "feature_modeler",
        "part_builder": "feature_modeler",
        "solid_modeler": "feature_modeler",
        "mech_fixer": "fixer",
        "feature_fixer": "fixer",
    }
    _BRIEF_MAX = 500  # contract §12

    def delegate(self, role: str = "", brief: str = "", budget_tokens: int = 0,
                 kind: str = "worker", parent_id: str | None = None,
                 docs: list | None = None, **_ignored: Any) -> dict:
        """Spawn a sub-agent role with an isolated brief and token budget (§12).

        Requires the mission's live delegation tree; without one this returns a
        structured refusal (and records the request) — do the work directly or
        escalate in your handoff.

        WS4: spawn-depth policy (max_spawn_depth from capability / config) is
        checked before the tree spawn; deeper spawns require decreasing
        difficulty for auto model assignment.

        mega-5: overlong briefs are soft-truncated to ≤500 chars (contract §12)
        with an explicit note instead of burning a turn on a hard reject;
        invented role names (part_fixer, …) map onto loadable cards so the
        child is not a bare sub-agent without new_part/add_features.
        """
        role_raw = str(role or "").strip()
        role_key = role_raw.lower()
        role_resolved = self._ROLE_ALIASES.get(role_key, role_raw)
        brief_raw = str(brief or "")
        brief_truncated = False
        if len(brief_raw) > self._BRIEF_MAX:
            brief = brief_raw[: self._BRIEF_MAX - 1].rstrip() + "…"
            brief_truncated = True
        else:
            brief = brief_raw

        if self.delegation_tree is None:
            if self.ledger is not None:
                try:
                    self.ledger.append("agent", "note", {
                        "event": "delegation_request", "role": role_resolved,
                        "role_requested": role_raw,
                        "brief": brief[:500], "budget_tokens": budget_tokens,
                        "status": "unavailable"})
                except Exception:  # noqa: BLE001
                    pass
            return {"ok": False, "status": "unavailable",
                    "error": ("no delegation tree bound to this mission (org layer "
                              "off); do the work directly or escalate in your handoff")}

        # WS4 spawn-depth policy: max depth from capability letter / config.
        try:
            from services.agent.role_models import (  # noqa: PLC0415
                check_spawn_depth_policy,
            )
            parent = None
            try:
                pid = parent_id or getattr(self.delegation_tree, "root_id", None)
                parent = (self.delegation_tree._nodes.get(pid)  # noqa: SLF001
                          if pid is not None else None)
            except Exception:  # noqa: BLE001
                parent = None
            depth = (int(getattr(parent, "depth", 0)) + 1) if parent is not None else 1
            letter = None
            if self.capability is not None:
                letter = getattr(self.capability, "letter", None)
            policy = check_spawn_depth_policy(
                depth,
                letter=letter,
                task_class=self.task_class,
            )
            if not policy.get("ok"):
                return {
                    "ok": False,
                    "status": "refused",
                    "error": policy.get("reason") or "spawn depth policy refused",
                    "spawn_depth_policy": policy,
                }
            # Stash for _run_subagent auto model pick (decreasing difficulty).
            self._last_spawn_policy = policy
        except Exception:  # noqa: BLE001 — policy is best-effort; never block spawn
            self._last_spawn_policy = None

        try:
            from services.agent.delegation import delegate as _delegate
            node = _delegate(
                role_resolved, brief, budget_tokens,
                tree=self.delegation_tree,
                parent_id=parent_id, kind=kind, docs=docs)
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "status": "failed",
                    "error": f"delegate failed: {exc}",
                    "role_requested": role_raw,
                    "role_resolved": role_resolved,
                    "brief_len": len(brief_raw),
                    "brief_truncated": brief_truncated}

        # Phase 4 (M11): run the approved sub-agent NOW (synchronous, bounded).
        # HONESTY: without a bound provider nothing executes — the node is returned
        # spawned-but-not-run with an explicit note (never a fabricated result).
        if not isinstance(node, dict) or node.get("status") != "approved":
            if isinstance(node, dict):
                if role_raw and role_resolved != role_raw:
                    node["role_requested"] = role_raw
                    node["role_resolved"] = role_resolved
                if brief_truncated:
                    node["brief_truncated"] = True
                    node["brief_len_original"] = len(brief_raw)
            return node
        if self.provider is None:
            node["executed"] = False
            node["note"] = ("sub-agent spawned (registry session created) but NOT "
                            "executed: no provider bound to this mission dispatch")
            return node
        node["executed"] = True
        if role_raw and role_resolved != role_raw:
            node["role_requested"] = role_raw
            node["role_resolved"] = role_resolved
        if brief_truncated:
            node["brief_truncated"] = True
            node["brief_len_original"] = len(brief_raw)
            node["brief"] = brief
        node.update(self._run_subagent(node, brief))
        return node

    def _run_subagent(self, node: dict, brief: str) -> dict:
        """Run one bounded synchronous sub-agent turn for an approved node.

        The turn IS the sub-agent's session: its replies land in the registry
        transcript, and steering queued from the GUI (``subagent_steer``, a
        different process) is consumed at each of its model-call boundaries by
        ``agent_turn.run_turn(subagent_id=...)``.
        """
        node_id = str(node.get("id", ""))
        role = str(node.get("role", "subagent"))
        try:
            from services.agent import agent_turn as _at
            from services.agent import subagent_registry as _sreg
            try:
                self.delegation_tree.start(node_id)  # pending→ mirror handles registry
            except Exception:  # noqa: BLE001 — status mirror is best-effort
                pass
            try:
                _sreg.append_transcript(node_id, "system",
                                        f"Sub-agent {role!r} started. Brief: {brief}"[:800])
            except Exception:  # noqa: BLE001
                pass
            # Workstream 5 fix: sub-agents no longer run BARE. When the role has
            # a card (services/agent/roles/<role>.yaml) it gets the card's
            # system_prompt + tool allowlist, real tool schemas rendered against
            # THIS dispatch layer, this MissionDispatch as its mission-scoped
            # resolver, and a budget-derived tool-call cap. Unknown roles keep
            # the old bare behavior (no card = no constraints to apply).
            card = None
            try:
                from services.agent.roles import load_role as _load_role  # noqa: PLC0415
                card = _load_role(role)
            except Exception:  # noqa: BLE001 — unknown role → bare sub-agent
                card = None
            system_prompt = None
            if card is not None:
                # WI-16: append per-capability SOFT phrasing at assembly time.
                try:
                    from services.agent.prompt_tiers import (  # noqa: PLC0415
                        assemble_system_for_capability,
                        letter_from_capability,
                    )
                    from services.agent.capability import resolve_capability  # noqa: PLC0415
                    _tier = ""
                    try:
                        from services.agent.tiers import detect_tier_from_env  # noqa: PLC0415
                        _tier = detect_tier_from_env() or ""
                    except Exception:  # noqa: BLE001
                        _tier = ""
                    _cap = resolve_capability(_tier) if _tier else None
                    system_prompt = assemble_system_for_capability(
                        card.system_prompt, letter_from_capability(_cap))
                except Exception:  # noqa: BLE001
                    system_prompt = card.system_prompt
            allowed = card.concrete_tools if card is not None else None
            # Always expose coordination board tools to sub-agents so they can
            # claim/release resources and post progress without clobbering peers.
            _COORD_TOOLS = (
                "coordinate_claim", "coordinate_release",
                "coordinate_progress", "coordinate_board",
            )
            if allowed is None:
                allowed = list(_COORD_TOOLS)
            else:
                allowed = list(allowed)
                for t in _COORD_TOOLS:
                    if t not in allowed:
                        allowed.append(t)
            # Corporate org tools + preamble when this run has an active chart.
            org = getattr(self, "org_chart", None)
            corp_agent = (
                str(node.get("corporate_agent_id") or "")
                or str(node.get("id") or "")
                or str(getattr(self, "corporate_agent_id", None) or "")
            )
            if org is not None and getattr(org, "active", False):
                try:
                    # Prefer an already-seated corporate id on the node.
                    seat = None
                    if corp_agent:
                        seat = org.seat_for(corp_agent)
                    if seat is None:
                        # Map role → first matching seat (head/worker)
                        for s in org.seats.values():
                            if s.role == role and s.rank != "director":
                                seat = s
                                corp_agent = s.agent_id
                                break
                    if seat is not None:
                        for t in org.tools_for(seat.agent_id):
                            if t not in allowed:
                                allowed.append(t)
                        corp_pre = org.preamble_for(seat.agent_id)
                        if corp_pre:
                            if system_prompt:
                                system_prompt = (
                                    system_prompt.rstrip() + "\n\n" + corp_pre
                                )
                            else:
                                system_prompt = corp_pre
                        # Stash so corporate_* tools know who is calling.
                        self.corporate_agent_id = seat.agent_id
                        if isinstance(node, dict):
                            node["corporate_agent_id"] = seat.agent_id
                            node["corporate_rank"] = seat.rank
                            node["corporate_dept"] = seat.dept_id
                except Exception:  # noqa: BLE001
                    pass
            # Inject a live board snapshot into the system preamble so the child
            # sees existing claims before it starts editing.
            board = getattr(self, "coordination_board", None)
            if board is not None:
                try:
                    preamble = board.preamble_text()
                    if preamble:
                        if system_prompt:
                            system_prompt = system_prompt.rstrip() + "\n\n" + preamble
                        else:
                            system_prompt = preamble
                except Exception:  # noqa: BLE001
                    pass

            # mega-6: if a geometry role card somehow lost mutators, re-seed them
            # so the first mutating call is always enableable.
            if allowed is not None:
                _need = ("new_part", "add_features")
                if role in (
                    "feature_modeler", "geometry_designer", "sketcher",
                    "assembly_fit_engineer",
                ) or any(t in allowed for t in _need):
                    for t in _need:
                        if t not in allowed:
                            allowed = list(allowed) + [t]
            tools_text = None
            if allowed:
                try:
                    from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
                    tools_text, _meta = render_tools_block(
                        allowed, resolver=_at.resolver_for(self))
                except Exception:  # noqa: BLE001 — schema render is best-effort
                    tools_text = None
            granted = 0
            try:
                granted = int((node.get("budget") or {}).get("tokens_granted", 0))
            except (TypeError, ValueError):
                granted = 0
            derived = (granted // 2000) if granted > 0 else 10
            max_calls = max(4, min(12, derived))
            # WI-4: capability phase caps (A/B HARD) override soft derived budget.
            _cap = self.capability
            if _cap is None and self.tier:
                try:
                    from services.agent.scaffolds import coerce_profile  # noqa: PLC0415
                    _cap = coerce_profile(tier=self.tier)
                except Exception:  # noqa: BLE001
                    _cap = None
            _phase = self.phase or str(node.get("phase") or node.get("stage") or "") or None
            # mega-6 BUILD mortality: geometry workers need enough tool steps to
            # reach new_part/add_features (read ledger → new_part → add_features
            # → measure is ≥4). Raise the floor for feature-class roles.
            _BUILD_ROLES = {
                "feature_modeler", "geometry_designer", "sketcher",
                "assembly_fit_engineer", "layout_engineer",
            }
            if role in _BUILD_ROLES or str(_phase or "").lower() in (
                "build", "feature", "geometry", "layout", "assembly",
            ):
                max_calls = max(max_calls, 8)
            if _cap is not None:
                try:
                    from services.agent import scaffolds as _sc  # noqa: PLC0415
                    max_calls, _ = _sc.apply_phase_cap(
                        _cap, _phase, max_calls,
                        ledger=self.ledger, actor=role,
                    )
                    if max_calls is None:
                        max_calls = max(4, min(12, derived))
                    # Re-assert BUILD floor after phase cap so A/B caps don't
                    # starve geometry mutators below enablement threshold.
                    if role in _BUILD_ROLES or str(_phase or "").lower() in (
                        "build", "feature", "geometry", "layout", "assembly",
                    ):
                        max_calls = max(int(max_calls or 0), 8)
                except Exception:  # noqa: BLE001
                    pass
            # WS2/WS4 + mega-5: always attempt per-spawn model assignment with
            # depth-aware min_rank. resolve_for_node returns None when ai_roles
            # is unset (legacy path) — fall through to resolve_role_model so
            # mega/gym missions still emit assignment audit rows and respect
            # decreasing difficulty on deeper spawns.
            provider = self.provider
            try:
                from services.agent.role_models import (  # noqa: PLC0415
                    cost_tier_for,
                    resolve_for_node,
                    resolve_role_model,
                )
                from services.agent import config as agent_config  # noqa: PLC0415
                from services.agent import agent_events as _ae  # noqa: PLC0415
                depth = 0
                try:
                    depth = int((node.get("depth") if isinstance(node, dict)
                                 else getattr(node, "depth", 0)) or 0)
                except (TypeError, ValueError):
                    depth = 0
                policy = getattr(self, "_last_spawn_policy", None) or {}
                risk = "low" if str(node.get("kind") or "") == "ponder" else None
                ctx = {
                    "task_class": self.task_class,
                    "depth": depth,
                    "risk": risk,
                    "node_inputs": {"task_class": self.task_class, "depth": depth},
                }
                # Map harness roles: subagent worker → task; planner-ish → planning.
                harness_role = "task"
                if role in ("overseer", "orchestrator", "department_manager"):
                    harness_role = "planning"
                elif role in ("advisor", "reviewer"):
                    harness_role = "advisor"
                cfg = dict(agent_config.load_config() or {})
                min_rank = policy.get("min_rank")
                try:
                    min_rank_i = int(min_rank) if min_rank is not None else None
                except (TypeError, ValueError):
                    min_rank_i = None
                assignment = resolve_for_node(harness_role, ctx, cfg=cfg)
                if assignment is None or min_rank_i is not None:
                    # Prefer depth-aware resolve; also covers missing ai_roles.
                    try:
                        assignment = resolve_role_model(
                            harness_role,
                            cfg,
                            task_class=self.task_class,
                            node_depth=depth,
                            risk=risk,
                            min_rank=min_rank_i,
                        )
                    except Exception:  # noqa: BLE001
                        if assignment is None:
                            raise
                if assignment is not None:
                    cfg_pick = dict(cfg)
                    cfg_pick["model"] = assignment.model
                    rank = int(assignment.rank or (min_rank_i or 3))
                    # Per-role thinking: concrete level wins; Auto → dispatch
                    # complexity heuristics (rank/task_class). Dispatcher may
                    # also pass an explicit effort on the node.
                    dispatch_effort = None
                    if isinstance(node, dict):
                        dispatch_effort = (
                            node.get("thinking")
                            or node.get("reasoning_effort")
                            or node.get("effort")
                            or (node.get("role_model") or {}).get("reasoning_effort")
                        )
                    else:
                        dispatch_effort = (
                            getattr(node, "effort", None)
                            or getattr(node, "thinking", None)
                            or getattr(node, "reasoning_effort", None)
                        )
                    try:
                        from services.agent.role_models import (  # noqa: PLC0415
                            resolve_role_thinking,
                        )
                        resolved_effort = resolve_role_thinking(
                            harness_role,
                            cfg,
                            request=str(
                                (node.get("brief") if isinstance(node, dict) else None)
                                or (node.get("request") if isinstance(node, dict) else None)
                                or ""
                            ),
                            task_class=self.task_class,
                            node_depth=depth,
                            risk=risk,
                            min_rank=min_rank_i,
                            dispatch_effort=(
                                str(dispatch_effort) if dispatch_effort else None
                            ),
                            rank=rank,
                        )
                        if resolved_effort:
                            cfg_pick["reasoning_effort"] = resolved_effort
                    except Exception:  # noqa: BLE001 — keep rank heuristic
                        if rank <= 2:
                            cfg_pick["reasoning_effort"] = "low"
                        elif rank <= 3:
                            cfg_pick["reasoning_effort"] = "medium"
                    if assignment.provider == "ollama":
                        ocfg = dict(cfg_pick.get("ollama") or {})
                        ocfg.setdefault(
                            "base_url", "http://127.0.0.1:11434/v1")
                        cfg_pick["ollama"] = ocfg
                        oai = dict(cfg_pick.get("openai") or {})
                        oai["base_url"] = ocfg.get("base_url") or (
                            "http://127.0.0.1:11434/v1")
                        oai.setdefault("api_key", "ollama")
                        cfg_pick["openai"] = oai
                    try:
                        prov_name = assignment.provider
                        if prov_name == "ollama":
                            prov_name = "openai"
                            cfg_pick.setdefault("api_key", "ollama")
                        provider = agent_config.get_provider(
                            name=prov_name, cfg=cfg_pick)
                    except Exception:  # noqa: BLE001 — keep mission provider
                        provider = self.provider
                    if isinstance(node, dict):
                        node["role_model"] = {
                            **assignment.to_dict(),
                            "min_rank": min_rank_i,
                            "cost_tier": cost_tier_for(
                                assignment.provider, assignment.model),
                            "reasoning_effort": cfg_pick.get(
                                "reasoning_effort"),
                        }
                    # Emit assignment audit even when provider rebuild falls back.
                    try:
                        emit_min = min_rank_i if min_rank_i is not None else assignment.rank
                        _k, _p = _ae.model_assignment_event(
                            node_id or role,
                            depth=depth,
                            task_class=self.task_class,
                            min_rank=int(emit_min) if emit_min is not None else None,
                            picked_model=assignment.model,
                            picked_provider=assignment.provider,
                            cost_tier=cost_tier_for(
                                assignment.provider, assignment.model),
                            mode=assignment.mode,
                            rank=assignment.rank,
                            reason=assignment.reason,
                            subagent_id=node_id or None,
                            role=role,
                            span_id=node_id or None,
                            parent_id=(node.get("parent")
                                       if isinstance(node, dict) else None),
                            actor=role,
                        )
                        _ae.emit_event(
                            _k, _p, live=self.on_event, journal=None)
                        if self.ledger is not None:
                            try:
                                self.ledger.append("agent", "note", {
                                    "event": "model_assignment",
                                    **{k: _p.get(k) for k in (
                                        "depth", "task_class", "min_rank",
                                        "picked_model", "picked_provider",
                                        "cost_tier", "mode", "rank", "reason",
                                        "role", "subagent_id",
                                    )},
                                })
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001 — never block on telemetry
                        pass
            except Exception:  # noqa: BLE001 — never block subagent on role_models
                provider = self.provider
            result = _at.run_turn(
                brief, provider, name=role, actor=role,
                system_prompt=system_prompt,
                allowed_tools=allowed,
                tools_text=tools_text,
                mission_dispatch=self,
                ledger=self.ledger, max_tool_calls=max_calls,
                on_event=self.on_event, run_id=self.run_id,
                node_id=node_id, subagent_id=node_id,
                capability=_cap, tier=self.tier, phase=_phase,
                task_class=self.task_class)
            # mega-6: prefer real provider usage tokens over the tool-count estimate.
            # Join must never surface handoff=null when the child produced calls or
            # a partial finish (mega-5: executed=true, spent=0, handoff=null).
            res_dict = result.result if isinstance(result.result, dict) else {}
            n_tool = 0
            try:
                n_tool = int(res_dict.get("tool_calls")
                             or len(res_dict.get("calls") or [])
                             or 0)
            except (TypeError, ValueError):
                n_tool = 0
            usage = res_dict.get("usage") if isinstance(res_dict.get("usage"), dict) else {}
            real_tokens = 0
            try:
                real_tokens = int(
                    res_dict.get("tokens_used")
                    or usage.get("total_tokens")
                    or 0
                )
            except (TypeError, ValueError):
                real_tokens = 0
            tokens_used_estimate = real_tokens <= 0
            if real_tokens > 0:
                tokens = real_tokens
            elif n_tool > 0 or result.success:
                # ~1k turn overhead + ~200/call — floor, never fabricated usage.
                tokens = 1000 + 200 * max(n_tool, 1 if result.success else 0)
            else:
                tokens = 0

            handoff = res_dict.get("handoff")
            if not isinstance(handoff, dict) or not handoff:
                # Synthesise join handoff from calls so parent is not blind.
                calls = res_dict.get("calls") if isinstance(res_dict.get("calls"), list) else []
                if calls or result.success:
                    handoff = {
                        "status": "done" if result.success else "blocked",
                        "summary": (
                            f"subagent {role} finished "
                            f"stop_reason={result.stop_reason} "
                            f"tool_calls={n_tool}"
                        )[:600],
                        "outputs": {
                            "tool_calls": n_tool,
                            "calls": [
                                {"tool": c.get("tool"), "ok": c.get("ok")}
                                for c in calls[:20]
                                if isinstance(c, dict)
                            ],
                            "stop_reason": result.stop_reason,
                            "reasoning_summary": (
                                "Join synthesised handoff from child result "
                                "(child omitted formal envelope; mega-6)."
                            ),
                        },
                    }
                else:
                    handoff = None

            role_model = None
            if isinstance(node, dict):
                role_model = node.get("role_model")
            if result.stop_reason == "terminated_by_user":
                try:
                    self.delegation_tree.fail(node_id, reason="terminated by user")
                except Exception:  # noqa: BLE001
                    pass
                try:  # the tree-fail mirror wrote "failed" — the truthful status is "terminated"
                    _sreg.set_status(node_id, "terminated",
                                     note="terminated by user at turn boundary")
                except Exception:  # noqa: BLE001
                    pass
                out = {"ok": False, "subagent_status": "terminated",
                        "result": result.result, "tokens_used": tokens,
                        "tokens_used_estimate": tokens_used_estimate,
                        "handoff": handoff,
                        "provider_usage_recorded": bool(
                            res_dict.get("provider_usage_recorded"))}
                if role_model is not None:
                    out["role_model"] = role_model
                return out
            if result.success:
                try:
                    self.delegation_tree.complete(node_id, tokens_used=tokens)
                except Exception:  # noqa: BLE001
                    pass
                out = {"ok": True, "subagent_status": "done",
                        "handoff": handoff,
                        "result": result.result,
                        "tokens_used": tokens,
                        "tokens_used_estimate": tokens_used_estimate,
                        "provider_usage_recorded": bool(
                            res_dict.get("provider_usage_recorded"))}
                if role_model is not None:
                    out["role_model"] = role_model
                return out
            try:
                self.delegation_tree.fail(node_id, reason=result.stop_reason)
            except Exception:  # noqa: BLE001
                pass
            out = {"ok": False, "subagent_status": "failed",
                    "stop_reason": result.stop_reason, "result": result.result,
                    "handoff": handoff,
                    "tokens_used": tokens,
                    "tokens_used_estimate": tokens_used_estimate,
                    "provider_usage_recorded": bool(
                        res_dict.get("provider_usage_recorded"))}
            if role_model is not None:
                out["role_model"] = role_model
            return out
        except Exception as exc:  # noqa: BLE001 — execution error is data
            try:
                self.delegation_tree.fail(node_id, reason=str(exc))
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "subagent_status": "failed",
                    "error": f"sub-agent execution failed: {exc}"}

    # ------------------------------------------------------------------ parts
    def part_search(self, query: str = "", domain: str = "", limit: int = 20,
                    category_id: str = "", facets: dict | str | None = None,
                    price_max: float = 0.0, vendors: str = "",
                    in_stock: bool = False, **_ignored: Any) -> dict:
        """Search the standard-parts library with taxonomy routing + facet filters.

        query: free text ("M8 socket head", "bearing 6205"); category_id: a
        parts_taxonomy id ("bearings_motion.ball_bearings.deep_groove") that routes
        domains and vendor sites; facets: dict of facet filters keyed by the
        category's facet keys ({"bore_mm": 12, "alloy": "4130"}); price_max /
        vendors (CSV) / in_stock post-filter the result set. Returns results +
        facet_summary (top param values in THIS result set) + applied filters.
        """
        if not query:
            return {"ok": False, "error": "part_search needs a query string"}
        try:
            from services.mcp.parts_tools import search_parts_v2  # noqa: PLC0415
            return search_parts_v2(query, domain=domain, limit=limit,
                                   category_id=category_id, facets=facets,
                                   price_max=price_max, vendors=vendors,
                                   in_stock=in_stock)
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"part_search failed: {exc}",
                    "hint": "Check that engine_oracle/parts_hub is importable."}

    def parts_taxonomy(self, node_id: str = "", depth: int = 2,
                       include_facets: bool = True, **_ignored: Any) -> dict:
        """Browse the parts category taxonomy; a node's EFFECTIVE facets tell you
        which facet keys part_search(facets=...) accepts there.

        node_id "" lists roots; then parts_taxonomy(node_id) for children +
        breadcrumbs + facets. Workflow: browse -> pick category -> faceted search.
        """
        try:
            from services.mcp.parts_tools import taxonomy_browse  # noqa: PLC0415
            return taxonomy_browse(node_id, depth, include_facets)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"parts_taxonomy failed: {exc}"}

    def part_get(self, part_id: str = "", **_ignored: Any) -> dict:
        """Fetch one full PartRecord by stable part_id (inspect params +
        geometry_ref before placing).

        part_id comes from part_search/part_source, e.g. "fastener:ISO4762:M8".
        """
        if not part_id:
            return {"ok": False, "error": "part_get needs a part_id"}
        try:
            from engine_oracle.parts_hub import get as _get  # noqa: PLC0415
            rec = _get(part_id)
            if rec is None:
                return {"ok": False, "error": f"part_id not found: {part_id!r}",
                        "hint": "Use part_search to discover valid part_ids."}
            return {"ok": True, "part": rec}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"part_get failed: {exc}"}

    def part_source(self, need: str = "", constraints: str = "",
                    limit: int = 5, **_ignored: Any) -> dict:
        """Source candidate parts for a plain-language need — deterministic
        taxonomy match + faceted search + scored candidates with artifact plans.

        need: e.g. "4130 chromoly tube OD 25.4 wall 1.25" or "bearing 12mm bore".
        Each candidate carries artifact_plan.how: part_place | scrape_import |
        make_standard_part | find_model (→ match_model) | reverse_engineer
        (→ reverse_engineer_product) — follow it to obtain real geometry.
        """
        try:
            from services.mcp.parts_tools import part_source_impl  # noqa: PLC0415
            return part_source_impl(need, constraints, limit)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"part_source failed: {exc}"}

    def match_model(self, part_id: str = "", title: str = "", mpn: str = "",
                    brand: str = "", limit: int = 8, **_ignored: Any) -> dict:
        """Match a discrete manufactured product (MPN/brand identity, no 3D model,
        not standard hardware, not raw stock) to a CAD model in a source repo —
        fired when a part_source candidate's artifact_plan.how == "find_model".

        Pass a part_id (read internally) OR a title/mpn/brand. Returns {ok,
        product, matches, best, fallback, coverage} (contracts/sourcing.md §2a).
        AUTO-PLACE only matches with confidence "exact" / attach:true; surface
        strong/weak for the user to confirm. When fallback.how ==
        "reverse_engineer" (no confident match), call reverse_engineer_product.
        """
        try:
            from services.mcp.parts_tools import match_model_impl  # noqa: PLC0415
            return match_model_impl(part_id, title, mpn, brand, limit)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"match_model failed: {exc}"}

    def reverse_engineer_product(self, part_id: str = "", url: str = "",
                                 title: str = "", mpn: str = "", brand: str = "",
                                 **_ignored: Any) -> dict:
        """Reverse-engineer hand-off (contracts/sourcing.md §2b): harvest ALL of a
        product's photos + datasheets off its page and seed a reverse-engineering
        session pre-populated with them (GUI opens the RE tab loaded). Call this
        when match_model's fallback.how == "reverse_engineer".

        Pass a part_id (source_url read from its record) OR the product url.
        Returns {ok, session_id, images_added, documents_ingested, source_url,
        activeWorkspace}. Best-effort: zero media still starts a session.
        """
        try:
            from services.mcp.parts_tools import reverse_engineer_product_impl  # noqa: PLC0415
            return reverse_engineer_product_impl(part_id, url, title, mpn, brand)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"reverse_engineer_product failed: {exc}"}

    def vendors_health(self, sites: str = "", **_ignored: Any) -> dict:
        """Canary health check of the vendor source adapters (CSV sites filter).

        Rows with needs_human_login=true mean a human must run vendor_login for
        that site — record a question for the human; never fabricate results.
        """
        try:
            from services.mcp.parts_tools import vendors_health_impl  # noqa: PLC0415
            return vendors_health_impl(sites)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"vendors_health failed: {exc}"}

    def scrape_enqueue(self, site: str = "", query: str = "",
                       **_ignored: Any) -> dict:
        """Enqueue a background vendor search + CAD-artifact download; returns a
        job id to poll with scrape_queue_status.

        site: adapter key ("mcmaster", "grabcad" — vendors_health lists them);
        query: what to search on the vendor site. The download lands as an
        artifact_path you feed to import_artifact.
        """
        if not site or not query:
            return {"ok": False, "error": "scrape_enqueue needs site and query"}
        try:
            from services.browser.queue import enqueue, ensure_worker_started  # noqa: PLC0415
            ensure_worker_started()
            out = enqueue(site.strip().lower(), query.strip())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"scrape_enqueue failed: {exc}",
                    "hint": "services/browser must be available and the site logged in."}
        # Provenance: record the acquisition on the mission ledger (kind=artifact).
        if self.ledger is not None and isinstance(out, dict) and out.get("ok", True):
            try:
                self.ledger.append("agent", "artifact", {
                    "event": "scrape_enqueued", "site": site, "query": query,
                    "job_id": out.get("id", "")})
            except Exception:  # noqa: BLE001 — provenance is best-effort
                pass
        return out if isinstance(out, dict) else {"ok": True, "result": out}

    def scrape_queue_status(self, **_ignored: Any) -> dict:
        """Poll the scrape/download queue: jobs with status pending|running|done|
        error; a done job's artifact_path feeds import_artifact.
        """
        try:
            from services.browser.queue import queue_status  # noqa: PLC0415
            out = queue_status()
            return out if isinstance(out, dict) else {"ok": True, "jobs": out}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"scrape_queue_status failed: {exc}"}

    def import_artifact(self, artifact_path: str = "", **_ignored: Any) -> dict:
        """Import a downloaded CAD file (STEP/IGES artifact_path from a done
        scrape job) into a new editable part document.

        Returns {ok, doc_id, editable: true, ...} on success.
        """
        path = str(artifact_path or "").strip()
        if not path:
            return {"ok": False,
                    "error": "import_artifact needs the artifact_path of a done job"}
        import os  # noqa: PLC0415
        if not os.path.isfile(path):
            return {"ok": False, "error": f"file not found: {path}",
                    "hint": "Take artifact_path from scrape_queue_status on a done job."}
        try:
            from engine_oracle.parts_hub.place import _place_import_file  # noqa: PLC0415
            out = _place_import_file({"kind": "import_file", "path": path}, None, None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"import_artifact failed: {exc}",
                    "hint": "Ensure the native core (AI_CAD_LIBAICAD) is available."}
        if self.ledger is not None and isinstance(out, dict) and out.get("ok"):
            try:
                self.ledger.append("agent", "artifact", {
                    "event": "artifact_imported", "path": path,
                    "doc_id": out.get("doc_id", "")})
            except Exception:  # noqa: BLE001
                pass
        return out

    def cam_tool_import(self, part_id: str = "", number: int = 0,
                        material: str = "", **_ignored: Any) -> dict:
        """Import a cutting-tool part record (end mill / drill from the library)
        into the current CAM job's tool library; number 0 = next free.

        Returns the mapped tool + vendor machining data; feeds/speeds still come
        from the CuttingDB (feeds_speeds tool), not the record.
        """
        try:
            from services.mcp.parts_tools import cam_tool_import_impl  # noqa: PLC0415
            return cam_tool_import_impl(part_id, number, material)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"cam_tool_import failed: {exc}"}


    # ---------------------------------------------------------- coordination
    def _coord_agent_id(self, agent_id: str | None = None) -> str:
        """Resolve the claiming agent id (explicit > subagent node > run)."""
        if agent_id and str(agent_id).strip():
            return str(agent_id).strip()
        return str(self.run_id or "mission")

    def _board_or_error(self) -> tuple[Any, dict | None]:
        board = getattr(self, "coordination_board", None)
        if board is None:
            return None, {
                "ok": False,
                "error": "no coordination board bound to this mission run",
            }
        return board, None

    def coordinate_claim(
        self,
        resources: list | None = None,
        intent: str = "",
        agent_id: str | None = None,
        **_ignored: Any,
    ) -> dict:
        """Claim exclusive ownership of resources (doc/feature/file ids).

        Returns ok+claims on success, or status=CONFLICT with holder+intent when
        another agent already holds a resource. Never hard-deadlocks — narrow
        scope or wait for the holder to release_claim / coordinate_release.
        """
        board, err = self._board_or_error()
        if err is not None:
            return err
        aid = self._coord_agent_id(agent_id)
        res = resources if isinstance(resources, list) else (
            [resources] if resources else []
        )
        out = board.register_claim(aid, res, intent)
        # Surface conflicts as spawn-side warnings without blocking siblings.
        if isinstance(out, dict) and out.get("status") == "CONFLICT":
            out.setdefault(
                "warning",
                "claim conflict — wait or narrow scope; do not hard-deadlock",
            )
        return out

    def coordinate_release(
        self,
        resources: list | None = None,
        agent_id: str | None = None,
        claim_ids: list | None = None,
        **_ignored: Any,
    ) -> dict:
        """Release claims held by this agent (all, or listed resources/claim_ids)."""
        board, err = self._board_or_error()
        if err is not None:
            return err
        aid = self._coord_agent_id(agent_id)
        res = resources if isinstance(resources, list) else (
            [resources] if resources else None
        )
        return board.release_claim(aid, res, claim_ids=claim_ids)

    def coordinate_progress(
        self,
        note: str = "",
        agent_id: str | None = None,
        **_ignored: Any,
    ) -> dict:
        """Post a short progress note to the shared board for peer awareness."""
        board, err = self._board_or_error()
        if err is not None:
            return err
        aid = self._coord_agent_id(agent_id)
        return board.post_progress(aid, note)

    def coordinate_board_snapshot(self, **_ignored: Any) -> dict:
        """Return active claims + recent progress notes for this run."""
        board, err = self._board_or_error()
        if err is not None:
            return err
        snap = board.board_snapshot()
        return {"ok": True, **snap}

    # ---------------------------------------------------------- corporate org
    def _org_or_error(self) -> tuple[Any, dict | None]:
        org = getattr(self, "org_chart", None)
        if org is None:
            return None, {
                "ok": False,
                "error": "no corporate org chart bound to this mission run",
                "hint": (
                    "Call corporate_activate with the mission task list when "
                    "agent.corporate_mode is on, or bind org_chart on MissionDispatch."
                ),
            }
        return org, None

    def _corporate_caller(self, agent_id: str | None = None) -> str:
        if agent_id and str(agent_id).strip():
            return str(agent_id).strip()
        if self.corporate_agent_id and str(self.corporate_agent_id).strip():
            return str(self.corporate_agent_id).strip()
        org = getattr(self, "org_chart", None)
        if org is not None:
            return str(getattr(org, "director_id", "") or self.run_id or "mission")
        return str(self.run_id or "mission")

    def corporate_activate(
        self,
        tasks: list | None = None,
        mission_goals: list | None = None,
        force: bool | None = None,
        seat_heads: bool = True,
        **_ignored: Any,
    ) -> dict:
        """Build/activate the corporate org chart for this run from *tasks*.

        No-ops (active=False) when corporate_mode is off or tasks span <2
        departments — flat delegation remains unchanged. When active, seats
        department heads as live subagents (if a delegation tree is bound).
        """
        try:
            from services.agent.corporate import build_corporate_org  # noqa: PLC0415
            from services.agent import config as agent_config  # noqa: PLC0415
            cfg = agent_config.load_config()
            goals = mission_goals
            if not goals and isinstance(self.graph, dict):
                brief = self.graph.get("brief") or self.graph.get("goal") or ""
                if brief:
                    goals = [str(brief)]

            def _delegate(**kwargs: Any) -> Any:
                return self.delegate(**kwargs)

            result = build_corporate_org(
                self.run_id,
                tasks or [],
                cfg=cfg,
                board=self.coordination_board,
                mission_goals=list(goals or []) or None,
                force=force,
                on_event=self.on_event,
                provider=self.provider,
                seat_heads=bool(seat_heads),
                delegate_fn=_delegate if self.delegation_tree is not None else None,
            )
            if result.get("active") and result.get("chart"):
                from services.agent.corporate import get_org_chart  # noqa: PLC0415
                self.org_chart = get_org_chart(
                    self.run_id,
                    create=False,
                    on_event=self.on_event,
                    provider=self.provider,
                )
                if self.org_chart is not None:
                    if self.org_chart.on_event is None:
                        self.org_chart.on_event = self.on_event
                    if self.org_chart.provider is None:
                        self.org_chart.provider = self.provider
                    if not self.corporate_agent_id:
                        self.corporate_agent_id = self.org_chart.director_id
            if self.ledger is not None:
                try:
                    self.ledger.append("agent", "artifact", {
                        "event": "corporate_org",
                        "active": bool(result.get("active")),
                        "reason": result.get("reason"),
                        "departments_found": result.get("departments_found"),
                    })
                except Exception:  # noqa: BLE001
                    pass
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"corporate_activate failed: {exc}"}

    def maybe_auto_activate_corporate(
        self,
        tasks: list | None = None,
        mission_goals: list | None = None,
        **_ignored: Any,
    ) -> dict:
        """Mission-start hook: auto-activate when mode on and ≥2 departments.

        Never force-activates. Single-dept missions stay flat.
        """
        try:
            from services.agent.corporate import auto_activate_corporate  # noqa: PLC0415
            from services.agent import config as agent_config  # noqa: PLC0415
            cfg = agent_config.load_config()
            goals = mission_goals
            if not goals and isinstance(self.graph, dict):
                brief = self.graph.get("brief") or self.graph.get("goal") or ""
                if brief:
                    goals = [str(brief)]

            def _delegate(**kwargs: Any) -> Any:
                return self.delegate(**kwargs)

            result = auto_activate_corporate(
                self.run_id,
                tasks or [],
                cfg=cfg,
                board=self.coordination_board,
                mission_goals=list(goals or []) or None,
                on_event=self.on_event,
                provider=self.provider,
                delegate_fn=_delegate if self.delegation_tree is not None else None,
                seat_heads=True,
            )
            if result.get("active"):
                from services.agent.corporate import get_org_chart  # noqa: PLC0415
                self.org_chart = get_org_chart(
                    self.run_id,
                    create=False,
                    on_event=self.on_event,
                    provider=self.provider,
                )
                if self.org_chart is not None and not self.corporate_agent_id:
                    self.corporate_agent_id = self.org_chart.director_id
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"auto_activate failed: {exc}"}

    def corporate_wind_down(
        self,
        success: bool = True,
        summary: str = "",
        **_ignored: Any,
    ) -> dict:
        """Tear down the corporate org at run finish (success or failure)."""
        try:
            from services.agent.corporate import wind_down_corporate  # noqa: PLC0415
            out = wind_down_corporate(
                self.run_id,
                success=bool(success),
                summary=str(summary or ""),
            )
            self.org_chart = None
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"corporate_wind_down failed: {exc}"}

    def corporate_chart_snapshot(self, **_ignored: Any) -> dict:
        """Return the corporate org chart snapshot for this run."""
        org, err = self._org_or_error()
        if err is not None:
            return err
        snap = org.snapshot()
        return {"ok": True, **snap}

    def corporate_channel_post(
        self,
        channel: str = "",
        body: str = "",
        kind: str = "info",
        agent_id: str | None = None,
        meta: dict | None = None,
        **_ignored: Any,
    ) -> dict:
        """Post to a corporate channel (membership + rank rules enforced).

        Workers may only post to their dept channel; worker→director is rejected.
        """
        org, err = self._org_or_error()
        if err is not None:
            return err
        aid = self._corporate_caller(agent_id)
        return org.post_message(aid, channel, body, kind=kind, meta=meta)

    def corporate_channel_read(
        self,
        channel: str = "",
        agent_id: str | None = None,
        limit: int = 50,
        **_ignored: Any,
    ) -> dict:
        """Read recent messages on a corporate channel (membership enforced)."""
        org, err = self._org_or_error()
        if err is not None:
            return err
        aid = self._corporate_caller(agent_id)
        return org.read_channel(channel, agent_id=aid, limit=limit)

    def corporate_spawn_worker(
        self,
        role: str = "",
        brief: str = "",
        head_id: str | None = None,
        budget_tokens: int = 0,
        as_team_lead: bool = False,
        **_ignored: Any,
    ) -> dict:
        """Department head spawns a worker seat (within max_subagent_depth).

        Also requests a delegation node when a tree is bound. Workers get
        coordinate_* + dept channel tools via _run_subagent.
        """
        org, err = self._org_or_error()
        if err is not None:
            return err
        if not getattr(org, "active", False):
            return {
                "ok": False,
                "error": "corporate org is not active on this run",
            }
        caller = self._corporate_caller(head_id)
        seat = org.seat_for(caller)
        if seat is None or seat.rank not in ("head", "team_lead"):
            return {
                "ok": False,
                "status": "rejected",
                "error": (
                    f"only department heads/team leads may spawn workers; "
                    f"caller={caller!r} rank="
                    f"{getattr(seat, 'rank', None)!r}"
                ),
            }
        # Seat on the org chart first (depth check).
        seated = org.spawn_worker(
            head_id=caller,
            role=role,
            brief=brief,
            as_team_lead=bool(as_team_lead),
        )
        if not seated.get("ok"):
            return seated

        # Optional live delegation + execution through the existing path.
        node_out: dict[str, Any] | None = None
        if self.delegation_tree is not None and role:
            parent_node = seat.delegation_node
            try:
                node_out = self.delegate(
                    role=role,
                    brief=brief or f"corporate worker in dept {seat.dept_id}",
                    budget_tokens=budget_tokens or 4000,
                    kind="worker",
                    parent_id=parent_node,
                )
                if isinstance(node_out, dict) and node_out.get("id"):
                    # Bind delegation node id onto the corporate seat.
                    worker_seat = org.seat_for(seated["seat"]["agent_id"])
                    if worker_seat is not None:
                        worker_seat.delegation_node = str(node_out.get("id"))
                        # Tag node so _run_subagent finds the corporate seat.
                        node_out["corporate_agent_id"] = worker_seat.agent_id
                        node_out["corporate_rank"] = worker_seat.rank
                        node_out["corporate_dept"] = worker_seat.dept_id
            except Exception as exc:  # noqa: BLE001
                seated["delegation_error"] = str(exc)

        seated["delegation"] = node_out
        return seated

    def corporate_hold_meeting(
        self,
        dept_id: str = "",
        done: list | None = None,
        in_progress: list | None = None,
        blocked: list | None = None,
        risks: list | None = None,
        trigger: str = "manual",
        **_ignored: Any,
    ) -> dict:
        """Department head posts a structured status digest to the heads channel."""
        org, err = self._org_or_error()
        if err is not None:
            return err
        did = str(dept_id or "").strip()
        if not did:
            caller = self._corporate_caller()
            seat = org.seat_for(caller)
            if seat is not None and seat.dept_id:
                did = seat.dept_id
        if not did:
            return {"ok": False, "error": "dept_id required for hold_meeting"}
        return org.hold_meeting(
            did,
            done=done,
            in_progress=in_progress,
            blocked=blocked,
            risks=risks,
            trigger=trigger or "manual",
        )

    def corporate_record_completion(
        self,
        dept_id: str = "",
        summary: str = "",
        blocked: bool = False,
        agent_id: str | None = None,
        **_ignored: Any,
    ) -> dict:
        """Record a worker task completion; may trigger a cadence meeting."""
        org, err = self._org_or_error()
        if err is not None:
            return err
        aid = self._corporate_caller(agent_id)
        seat = org.seat_for(aid)
        did = str(dept_id or "").strip()
        if not did and seat is not None:
            did = seat.dept_id or ""
        if not did:
            return {"ok": False, "error": "dept_id required (or call as a dept seat)"}
        return org.record_worker_completion(
            did,
            worker_id=aid,
            summary=summary,
            blocked=bool(blocked),
        )


# --------------------------------------------------------------------------- helpers

def _normalize_questions(raw: Any) -> list[dict]:
    """Coerce model-authored question payloads into contract-§2 question dicts.

    Accepts a plain string, a dict, or a list of strings/dicts.  Fills the
    required fields a small model tends to omit (id, kind, suggested) with honest
    defaults — a text question whose default answer is empty string."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for i, item in enumerate(items[:4], 1):  # contract cap: 4 questions per batch
        if isinstance(item, str):
            q: dict[str, Any] = {"prompt": item}
        elif isinstance(item, dict):
            q = dict(item)
        else:
            continue
        q.setdefault("id", f"q{i}")
        q.setdefault("kind", "text")
        q.setdefault("prompt", q.get("text", "") or q.get("ask", ""))
        if q.get("suggested") is None:
            q["suggested"] = q.get("default", "")
        q.setdefault("required", False)
        if not q["prompt"]:
            continue
        out.append(q)
    return out
