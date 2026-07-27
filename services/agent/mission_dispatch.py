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

# Ledger section / kind vocabulary lives in ONE place (services.agent.ledger_vocab)
# so this mission-scoped shadow and services/mcp/ledger_tools.py cannot drift —
# they had already drifted (plan_ruling existed only here), and a model that
# learned a spelling from one path hit "unknown kind" on the other.
from services.agent.ledger_vocab import (  # noqa: E402
    KIND_ALIASES as _LEDGER_NOTE_KIND_ALIASES,
    LEDGER_NOTE_KINDS as _LEDGER_NOTE_KINDS,
    SECTION_ALIASES as _LEDGER_GET_SECTION_ALIASES,
    kind_error as _ledger_kind_error,
    resolve_kind as _resolve_ledger_kind,
    resolve_section as _resolve_ledger_section,
    section_error as _ledger_section_error,
)


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
             "delegate", "adopt_role", "request_role_change", "part_search",
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
                 corporate_agent_id: str | None = None,
                 options: dict | None = None,
                 mode: str | None = None,
                 corporate_force: bool | None = None,
                 plan_only: bool | None = None,
                 advisor: bool | None = None) -> None:
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
        # Copilot mode overhaul: options.mode / corporate_force / plan_only /
        # advisor threaded from host/core so tool + org policy can honor them.
        try:
            from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                mode_options_from_mapping,
            )
            # Merge explicit kwargs into options BEFORE normalize so
            # mode==swarm → corporate_force and mode==plan → plan_only couple.
            _raw = dict(options) if isinstance(options, dict) else {}
            if mode is not None:
                _raw["mode"] = mode
            if corporate_force is not None:
                _raw["corporate_force"] = bool(corporate_force)
            if plan_only is not None:
                _raw["plan_only"] = bool(plan_only)
            if advisor is not None:
                _raw["advisor"] = bool(advisor)
            _opts = mode_options_from_mapping(_raw)
            self.options = _opts
            self.mode = _opts["mode"]
            self.corporate_force = bool(_opts["corporate_force"])
            self.plan_only = bool(_opts["plan_only"])
            self.advisor = bool(_opts.get("advisor", True))
        except Exception:  # noqa: BLE001 — never break dispatch construction
            self.options = {}
            self.mode = (mode or "agent")
            self.corporate_force = bool(corporate_force) if corporate_force is not None else False
            self.plan_only = bool(plan_only) if plan_only is not None else False
            self.advisor = True if advisor is None else bool(advisor)
        self._handlers: dict[str, Callable[..., Any]] = {
            "ledger_get": self.ledger_get,
            "ledger_note": self.ledger_note,
            "dag_plan": self.dag_plan,
            "ask_user": self.ask_user,
            "delegate": self.delegate,
            "adopt_role": self.adopt_role,
            "request_role_change": self.request_role_change,
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
        # Resolve the model's spelling to a canonical section (shared vocabulary):
        # normalises decoration/case/separators, walks path-like values
        # ("/project/mission-123/design/specs/" → "specs" → geometry_specs), then
        # aliases and plural variants.  Only a value nothing resolves is rejected.
        if section is not None and str(section).strip():
            resolved = _resolve_ledger_section(section)
            if resolved is None:
                return _ledger_section_error(section)
            # "all"/"*"/"full ledger" → no section, render the whole thing.
            section = None if resolved == "all" else resolved
        else:
            section = None
        # mega-2: models ask for section="events" (raw event log).
        if section == "events":
            return {
                "ok": True, "run_id": led.run_id, "section": "events",
                "data": list(led.events),
                "event_count": len(led.events),
            }
        if section:
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
        # Resolve the model's spelling to a canonical kind (shared vocabulary):
        # normalisation + aliases + plural variants (mega-2 kind=verdict,
        # measured kind=design_spec).  Only an unresolvable kind is rejected, and
        # the rejection now carries did_you_mean alongside the full valid set.
        raw_kind = (kind or "note").strip().lower() if isinstance(kind, str) else "note"
        resolved_kind = _resolve_ledger_kind(raw_kind)
        if resolved_kind is None:
            return _ledger_kind_error(kind if isinstance(kind, str) else raw_kind)
        kind = resolved_kind
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
            # Best-effort project memory: durable decisions/notes for later turns.
            if kind in ("decision", "note", "requirement", "dimension", "brief"):
                try:
                    from services.agent.config import memory_enabled, workspace_dir  # noqa: PLC0415
                    from services.agent import project_memory as _pm  # noqa: PLC0415
                    if memory_enabled():
                        _root = getattr(self, "project_dir", None) or getattr(self, "workspace_dir", None)
                        _path = _pm.resolve_project_root(_root) or workspace_dir()
                        _text = (
                            str(body.get("text") or body.get("summary") or body.get("value") or "")
                            .strip()
                        )
                        if not _text and body.get("id"):
                            _text = f"{kind}: {body.get('id')}"
                        if _text and _path is not None:
                            _pm.remember(
                                _path,
                                _text,
                                kind=kind,
                                source=f"ledger:{actor}",
                                importance=0.7 if kind == "decision" else 0.5,
                            )
                except Exception:  # noqa: BLE001
                    pass
            return {"ok": True, "seq": event["seq"], "run_id": led.run_id,
                    "kind": kind, "ts": event["ts"],
                    **({"aliased_from": raw_kind} if raw_kind != kind else {})}
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"ledger write failed: {exc}"}

    def _ledger(self, run_id: str | None = None):
        """The bound ledger.  A model-supplied run_id for a DIFFERENT run is
        ignored in favour of the mission's own — a node never writes outside its
        run (scoping is the point of this layer).

        Non-mission runs (the general_agent linear entry) build a dispatch with
        ledger=None while every role card still advertises ledger_get/
        ledger_note, so those calls failed 100% of the time with "no ledger
        bound to this mission run".  Bind the run's own on-disk ledger lazily
        instead of failing an advertised tool; memoized so sub-agents spawned
        later inherit the same object.
        """
        if self.ledger is None and self.run_id:
            try:
                from services.agent.ledger import Ledger  # noqa: PLC0415
                self.ledger = Ledger.load(str(self.run_id))
            except Exception:  # noqa: BLE001 — fall back to structured error
                return None
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
    _BRIEF_MAX = 500  # contract §12 — the RECORDED node-row brief, not the child's task
    _BRIEF_EXEC_MAX = 20000  # sanity bound on what the child actually executes


    def adopt_role(self, role: str = "", **kwargs):
        """Main-session role switch; supervised subagents are refused."""
        from services.agent.role_adoption import adopt_role as _adopt
        depth = int(getattr(self, "depth", 0) or kwargs.get("depth") or 0)
        is_root = bool(getattr(self, "is_root", depth <= 0))
        cfg = None
        try:
            from services.agent import config as agent_config
            cfg = agent_config.load_config()
        except Exception:
            pass
        apply = getattr(self, "_apply_role_adoption", None)
        return _adopt(role, is_root=is_root, depth=depth, cfg=cfg, apply=apply)

    def request_role_change(self, role: str = "", reason: str = "", from_role: str = "", **kwargs):
        from services.agent.role_adoption import request_role_change as _req
        depth = int(getattr(self, "depth", 0) or kwargs.get("depth") or 0)
        return _req(role, reason, from_role=from_role, depth=depth)

    def delegate(self, role: str = "", brief: str = "", budget_tokens: int = 0,
                 kind: str = "worker", parent_id: str | None = None,
                 docs: list | None = None, **_ignored: Any) -> dict:
        """Spawn a sub-agent role with an isolated brief and token budget (§12).

        A mission that reaches this call with no delegation tree but WITH a model
        provider gets one bound on the spot (same tree run_linear_role_turn binds,
        recorded as ``delegation_tree_autobound``) so an advertised tool is not a
        guaranteed failure. With neither tree nor provider nothing could execute,
        so the call returns a structured refusal naming what to do instead — do
        the work directly and record the remainder with ledger_note.

        WS4: spawn-depth policy (max_spawn_depth from capability / config) is
        checked before the tree spawn; deeper spawns require decreasing
        difficulty for auto model assignment.

        mega-5: overlong briefs are soft-truncated for the RECORDED node row
        (≤500 chars, contract §12) instead of burning a turn on a hard reject —
        but the child EXECUTES on the full brief, because the cap describes the
        audit row, not the assignment. Invented role names (part_fixer, …) map
        onto loadable cards so the child is not a bare sub-agent without
        new_part/add_features.
        """
        role_raw = str(role or "").strip()
        role_key = role_raw.lower()
        role_resolved = self._ROLE_ALIASES.get(role_key, role_raw)
        # A model cannot know its own dg_<hex> node id, and nothing injected it,
        # so every nested delegate() arrived with parent_id=None and
        # request_delegation fell back to the tree ROOT — pinning every spawn at
        # depth 1 no matter how many levels of delegation really happened. That
        # made max_subagent_depth / soft_depth_limit unreachable via the only
        # tool a model has to spawn, and kept the depth-aware model-cost
        # throttle from ever seeing a deep leaf. _run_subagent marks the node it
        # is currently executing so the chain is measured for real.
        if not parent_id:
            parent_id = getattr(self, "_current_node_id", None)
        # Models send budget_tokens as "12000" often enough that the bare
        # comparison in DelegationTree.request_delegation blew up with
        # "'<=' not supported between instances of 'str' and 'int'" — an
        # opaque crash for a trivially coercible arg (seen in gym run logs).
        try:
            budget_tokens = int(float(str(budget_tokens).strip() or 0))
        except (TypeError, ValueError):
            budget_tokens = 0
        brief_raw = str(brief or "")
        brief_truncated = False
        if len(brief_raw) > self._BRIEF_MAX:
            brief = brief_raw[: self._BRIEF_MAX - 1].rstrip() + "…"
            brief_truncated = True
        else:
            brief = brief_raw
        # The ≤500 cap is the RECORD shape (contract §12 node row; the
        # agent.spawn event clips to 120) — it was never meant to shrink the
        # child's actual assignment. Executing the truncated text silently
        # deleted the tail of every real spec: a 1464-char part brief lost its
        # slot, handle and material lines, and the child confidently built the
        # wrong part and was then judged for not delivering them.
        exec_brief = brief_raw[: self._BRIEF_EXEC_MAX]

        if self.delegation_tree is None:
            # ADVERTISED BUT IMPOSSIBLE (15 dead calls in the gym ledgers): every
            # role card on the orchestrator/manager path lists `delegate` and the
            # prompt tells the model to use it, so an unbound tree made an
            # advertised tool fail 100% of the time. run_linear_role_turn already
            # binds a tree for exactly this reason — do the same here, lazily, so
            # any mission that reaches delegate() with a provider gets a REAL
            # spawn. The tree self-limits (budget meter, max_subagent_depth), and
            # the binding is recorded, never silent.
            if self.provider is not None:
                try:
                    from services.agent.delegation import (  # noqa: PLC0415
                        DelegationTree as _DelegationTree,
                    )
                    self.delegation_tree = _DelegationTree(
                        run_id=str(getattr(self, "run_id", "") or ""),
                        ledger=self.ledger,
                        on_event=self.on_event,
                    )
                    self._delegation_tree_autobound = True
                    if self.ledger is not None:
                        try:
                            self.ledger.append("agent", "note", {
                                "event": "delegation_tree_autobound",
                                "run_id": str(getattr(self, "run_id", "") or ""),
                                "why": ("delegate called on a mission with no tree; "
                                        "a provider is bound so the spawn really runs"),
                            })
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001 — degrade to the honest refusal
                    self.delegation_tree = None

        if self.delegation_tree is None:
            # No provider ⇒ a spawned node could not execute; saying "spawned"
            # would be a fabricated success. Refuse, and name the alternative.
            if self.ledger is not None:
                try:
                    self.ledger.append("agent", "note", {
                        "event": "delegation_request", "role": role_resolved,
                        "role_requested": role_raw,
                        "brief": brief[:500], "budget_tokens": budget_tokens,
                        "status": "unavailable"})
                except Exception:  # noqa: BLE001
                    pass
            return {
                "ok": False, "status": "unavailable",
                "error": (
                    "delegate is not available in this run: no delegation tree and "
                    "no model provider are bound to it, so no sub-agent can execute. "
                    "Do NOT retry delegate — do the work yourself with the concrete "
                    f"tools this turn already has (the {role_resolved!r} work you "
                    "were about to hand off), and record what is left for another "
                    "run with ledger_note(kind='handoff', ...)."
                ),
                "role_requested": role_raw,
                "role_resolved": role_resolved,
                "do_instead": [
                    "build/inspect directly with the modeling tools in your allowlist",
                    "ledger_note(kind='handoff', payload={...}) for work you cannot finish",
                ],
            }

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

        if isinstance(node, dict) and getattr(self, "_delegation_tree_autobound", False):
            # Observable, not silent: this mission had no tree until this call.
            node["delegation_tree_autobound"] = True

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
            # Record-only truncation; the child still runs on the full brief.
            node["brief_truncated"] = True
            node["brief_len_original"] = len(brief_raw)
            node["brief"] = brief
            node["brief_executed_in_full"] = len(exec_brief) == len(brief_raw)
        node.update(self._run_subagent(node, exec_brief))
        return node

    def _run_subagent(self, node: dict, brief: str) -> dict:
        """Run a sub-agent turn with its OWN current part.

        The parts it creates or opens are current for IT; ours is restored when it
        returns, so a sub-agent can never silently redirect our next edit into a
        document we never asked for."""
        from services.mcp.cad_common import doc_scope  # noqa: PLC0415
        with doc_scope():
            return self._run_subagent_turn(node, brief)

    def _run_subagent_turn(self, node: dict, brief: str) -> dict:
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
            if allowed is not None:
                # T-file-floor: every DAG-node/subagent gets file-handling by
                # default even when the role card narrows the list.
                try:
                    from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                        SUBAGENT_DEFAULT_TOOLS,
                    )
                    allowed = sorted(set(allowed) | SUBAGENT_DEFAULT_TOOLS)
                except Exception:  # noqa: BLE001 — floor union is best-effort
                    pass
                # Settings → AI → Tools "Send bug reports": nodes may file too.
                try:
                    from services.agent.config import bugreport_floor  # noqa: PLC0415
                    allowed = sorted(set(allowed) | bugreport_floor())
                except Exception:  # noqa: BLE001 — floor union is best-effort
                    pass
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
            # Producer roles need room for their whole mandated chain. The role
            # list + floor live in tool_budget so this path and the DAG path
            # (services/dag/node_runner.py) cannot drift apart again — that
            # drift is what starved model3d, and it still starved
            # cam_programmer / sim_engineer / drawing_detailer, whose cards
            # mandate 11-16 calls against an absolute ceiling of 12.
            from services.agent import tool_budget as _tb  # noqa: PLC0415
            _BUILD_MIN_CALLS = _tb.PRODUCER_MIN_CALLS
            _is_build = _tb.is_producer_role(role, allowed, _phase)
            if _is_build:
                max_calls = max(max_calls, _BUILD_MIN_CALLS)
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
                    if _is_build:
                        max_calls = max(int(max_calls or 0), _BUILD_MIN_CALLS)
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
                # Harness role for model resolve:
                #  - leaf workers stay on "task" (cheap) under agent/swarm
                #  - plan mode → planning; edit → task
                #  - node role overrides for orchestrator/advisor seats
                # Top-level turns use role_for_copilot_mode at the entry path;
                # do NOT upgrade every subagent to "default" in agent mode.
                harness_role = "task"
                try:
                    from services.agent.role_models import (  # noqa: PLC0415
                        role_for_copilot_mode,
                    )
                    _m = (
                        getattr(self, "mode", None)
                        or (self.options or {}).get("mode")
                        or "agent"
                    )
                    if _m in ("plan", "edit"):
                        harness_role = role_for_copilot_mode(_m)
                except Exception:  # noqa: BLE001
                    pass
                if role in ("overseer", "orchestrator", "department_manager"):
                    harness_role = "planning"
                elif role == "reviewer":
                    harness_role = "vision"
                elif role == "advisor":
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
                    # Ceiling: a sub-agent NEVER runs above "high". cfg_pick is
                    # a copy of the user's global config, so a Settings-level
                    # max/xhigh rode straight into the child whenever the
                    # resolver above returned None or raised past the rank arms.
                    try:
                        from services.agent.role_models import (  # noqa: PLC0415
                            clamp_subagent_effort,
                        )
                        _raw_eff = cfg_pick.get("reasoning_effort")
                        if _raw_eff:
                            cfg_pick["reasoning_effort"] = clamp_subagent_effort(_raw_eff)
                    except Exception:  # noqa: BLE001 — ceiling is best-effort
                        pass
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
                        if assignment.is_session_default:
                            # Role follows the chat composer: keep the mission's own
                            # provider. There is nothing to build — provider/model are
                            # empty by construction for this mode.
                            provider = self.provider
                        else:
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
            # Mode → tools: intersect role allowlist with copilot mode policy.
            # apply_mode_tools unions SHARED_DEFAULT_TOOLS (advise, ask_user,
            # doc lifecycle, enable_tools / list_tool_categories, …).
            try:
                from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                    apply_mode_tools,
                )
                mode_allowed = apply_mode_tools(
                    getattr(self, "mode", None),
                    allowed,
                    plan_only=bool(getattr(self, "plan_only", False)),
                )
                if mode_allowed is not None:
                    allowed = list(mode_allowed)
                    # Mode narrowing (e.g. ask/plan) can drop the subagent file
                    # floor union'd above — reapply it so DAG nodes keep it.
                    try:
                        from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                            SUBAGENT_DEFAULT_TOOLS,
                        )
                        allowed = sorted(set(allowed) | SUBAGENT_DEFAULT_TOOLS)
                    except Exception:  # noqa: BLE001 — floor union is best-effort
                        pass
                    # Settings → AI → Tools "Send bug reports": keep the tool
                    # through mode narrowing (a report never mutates the doc).
                    try:
                        from services.agent.config import bugreport_floor  # noqa: PLC0415
                        allowed = sorted(set(allowed) | bugreport_floor())
                    except Exception:  # noqa: BLE001 — floor union is best-effort
                        pass
                    # Re-render tools_text against the narrowed set (+ category menu).
                    if allowed:
                        try:
                            from services.agent.tool_schemas import (  # noqa: PLC0415
                                render_tools_block,
                            )
                            tools_text, _meta = render_tools_block(
                                allowed, resolver=_at.resolver_for(self),
                            )
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001 — mode gate is best-effort
                pass
            # Restricted allowlists (mode or role card): ensure meta-tools so the
            # model can expand categories. advise folds into SHARED_DEFAULT_TOOLS
            # for ask/plan/edit; for agent/swarm role cards inject when advisor on.
            try:
                if allowed is not None:
                    for _meta in ("enable_tools", "list_tool_categories"):
                        if _meta not in allowed:
                            allowed = list(allowed) + [_meta]
                    from services.agent.config import advisor_enabled  # noqa: PLC0415
                    adv_on = bool(getattr(self, "advisor", True))
                    if adv_on and advisor_enabled() and "advise" not in allowed:
                        allowed = list(allowed) + ["advise"]
            except Exception:  # noqa: BLE001
                pass
            # Mark the node this turn IS, so a nested delegate() from inside it
            # attaches to this node instead of the tree root. Saved/restored so
            # sibling and deeper spawns nest correctly.
            _prev_node = getattr(self, "_current_node_id", None)
            self._current_node_id = node_id or _prev_node
            try:
                result = _at.run_turn(
                    brief, provider, name=role, actor=role,
                    system_prompt=system_prompt,
                    allowed_tools=allowed,
                    tools_text=tools_text,
                    mission_dispatch=self,
                    ledger=self._ledger(), max_tool_calls=max_calls,
                    on_event=self.on_event, run_id=self.run_id,
                    node_id=node_id, subagent_id=node_id,
                    capability=_cap, tier=self.tier, phase=_phase,
                    task_class=self.task_class)
            finally:
                self._current_node_id = _prev_node
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
                    "hint": "Check that services/parts (the parts hub) is importable."}

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
            from services.parts import get as _get  # noqa: PLC0415
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
            from services.mcp import core_bridge as cb  # noqa: PLC0415 — ported to core (T-RF19)
            out = cb.call("part_import_file", {"path": path})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"import_artifact failed: {exc}",
                    "hint": "Ensure the native core (CHUDWARE_LIBCHUDWARE) is available."}
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
        force: bool | None = None,
        **_ignored: Any,
    ) -> dict:
        """Mission-start hook: auto-activate when mode on and ≥2 departments.

        When ``self.corporate_force`` / mode==swarm, *force* defaults True so
        Swarm mode activates the org even if config.corporate_mode is off.
        Single-dept missions still stay flat (dept-count gate in corporate).
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

            # Swarm / options.corporate_force → force org activation path.
            force_flag = force
            if force_flag is None:
                force_flag = True if getattr(self, "corporate_force", False) else None
            if force_flag is True:
                result = self.corporate_activate(
                    tasks=tasks or [],
                    mission_goals=list(goals or []) or None,
                    force=True,
                    seat_heads=True,
                )
            else:
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
    defaults — a text question whose default answer is empty string.

    Also accepts common aliases:
      type→kind, ask/text→prompt, default→suggested, choices→options,
      multiple→multi, target→targets. Maps design-decision style payloads onto
      ``choice`` and geometry-pick payloads onto ``geometry_select``.
    """
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
        # Field aliases
        if "prompt" not in q:
            q["prompt"] = q.get("text") or q.get("ask") or ""
        if "kind" not in q and q.get("type"):
            q["kind"] = q["type"]
        if q.get("suggested") is None and "default" in q:
            q["suggested"] = q.get("default")
        if "options" not in q and q.get("choices") is not None:
            choices = q.get("choices") or []
            opts = []
            for c in choices:
                if isinstance(c, dict) and "value" in c:
                    opts.append(c)
                elif isinstance(c, str):
                    opts.append({"value": c, "label": c})
            q["options"] = opts
        if "multi" not in q and q.get("multiple") is not None:
            q["multi"] = bool(q.get("multiple"))
        if "targets" not in q and q.get("target") is not None:
            t = q.get("target")
            q["targets"] = t if isinstance(t, list) else [t]

        # Infer kind from payload shape when omitted.
        kind = (q.get("kind") or "").strip().lower()
        if not kind:
            if q.get("options") or q.get("choices"):
                kind = "choice"
            elif q.get("targets") or q.get("target") or q.get("geometry"):
                kind = "geometry_select"
            elif q.get("family") in ("length", "angle", "count") or q.get("unit"):
                kind = "dimension"
            elif "proposal" in q or q.get("approve") is not None:
                kind = "approval"
            else:
                kind = "text"
        # Map common free-form kind aliases.
        kind_aliases = {
            "select": "choice", "single_choice": "choice", "multiple_choice": "choice",
            "mc": "choice", "design": "choice", "design_choice": "choice",
            "decision": "choice", "option": "choice", "options": "choice",
            "number": "dimension", "dim": "dimension", "length": "dimension",
            "string": "text", "free_text": "text", "open": "text", "open_ended": "text",
            "file": "file_request", "image": "file_request", "upload": "file_request",
            "confirm": "approval", "approve": "approval", "yes_no": "approval",
            "geometry": "geometry_select", "pick": "geometry_select",
            "select_geometry": "geometry_select", "point": "geometry_select",
            "feature": "geometry_select", "face": "geometry_select",
        }
        kind = kind_aliases.get(kind, kind)
        q["kind"] = kind

        q.setdefault("id", f"q{i}")
        q.setdefault("required", False)

        # Kind-specific suggested defaults when omitted.
        if q.get("suggested") is None:
            if kind == "choice":
                opts = q.get("options") or []
                if opts and isinstance(opts[0], dict):
                    first = opts[0].get("value", "")
                    q["suggested"] = [first] if q.get("multi") else first
                else:
                    q["suggested"] = [] if q.get("multi") else ""
            elif kind == "dimension":
                unit = q.get("unit") or "mm"
                # No invented engineering number — 0.0 is a placeholder; force required
                # so unattended auto does not ship a degenerate dim unless the model
                # supplied a real suggested value (or required:false explicitly).
                q["suggested"] = {"value": 0.0, "unit": unit}
                q.setdefault("family", "length")
                if isinstance(item, dict) and "required" not in item:
                    q["required"] = True
            elif kind == "approval":
                # Safe deny — never auto-approve a gate the model forgot to default.
                q["suggested"] = False
            elif kind == "file_request":
                q["suggested"] = {"paths": []}
            elif kind == "geometry_select":
                # Honest empty pick — do not fabricate sids (contract §12).
                q["suggested"] = [] if q.get("multi") else {}
                if not q.get("targets"):
                    q["targets"] = ["face", "edge", "feature"]
            else:
                q["suggested"] = ""

        if kind == "choice" and q.get("multi") and not isinstance(q.get("suggested"), list):
            s = q.get("suggested")
            q["suggested"] = [] if s in (None, "") else [s]

        if not q["prompt"]:
            continue
        out.append(q)
    return out


# ---------------------------------------------------------------------------
# Router-driven dispatch (workflow_router structure=="linear")
# ---------------------------------------------------------------------------
#
# workflow_router.route() classifies a raw request into a TaskClass and, via
# structure_for_class(), an execution *structure*: "dag" (the default 8-phase
# intake->spec->build->verify->deliver swarm, compiled by
# services.dag.mission_template.build_mission_graph) or "linear" (a single
# services.agent.agent_turn.run_turn against one role's card — no swarm/dag
# compilation at all). PROJECT_ADMIN (file-org/cleanup/PLM requests) is the
# first "linear" class: routing it into the geometry BUILD swarm hands the
# model geometry-only role tools and it fabricates a dummy part instead of
# doing the file/PLM work the user actually asked for (4 live runs — see
# ~/.local/share/Chudware/chat_history.json).
#
# route_request() is the entry a mission caller should run BEFORE compiling a
# manifest/DAG: "dag" classes get back the router decision so the caller
# proceeds with the EXISTING manifest -> build_mission_graph -> dag_run flow
# unchanged; "linear" classes run to completion right here via
# run_linear_role_turn(), reusing agent_turn.run_turn — the same single-turn
# machinery MissionDispatch._run_subagent uses for sub-agent spawns — instead
# of a new executor.

# A file-org/PLM cleanup pass touches MANY files in ONE turn (fs_list -> N x
# fs_write/fs_edit/run_shell -> N x plm_item_add -> plm_checkpoint). A DAG
# node's per-node budget tops out at 12 (BUILD-role floor 8, see
# MissionDispatch._run_subagent's derived cap 4-12) because a DAG node only
# ever handles ONE deliverable; a linear organizer turn handles the WHOLE
# project in one turn, so it needs more headroom than any single DAG node
# ever gets.
LINEAR_ROLE_MAX_TOOL_CALLS = 30


def run_linear_role_turn(
    request: str,
    role: str,
    provider: Any,
    *,
    on_event: Optional[Callable[..., None]] = None,
    run_id: str | None = None,
    ledger: Any | None = None,
    mission_dispatch: MissionDispatch | None = None,
    capability: Any | None = None,
    max_tool_calls: int | None = None,
    tool_dispatch: Optional[Callable[[str, dict], Any]] = None,
) -> dict[str, Any]:
    """Run ONE bounded ``agent_turn.run_turn`` against *role*'s card.

    The linear-structure executor for ``workflow_router.RouterResult.structure
    == "linear"``: no manifest, no DAG compilation — just the role's card + the
    same file-handling floor every DAG-node/sub-agent gets
    (``copilot_mode_tools.SUBAGENT_DEFAULT_TOOLS``), streamed through *on_event*
    the same way a normal run is so the GUI chat renders it.

    A delegation tree IS bound (see below): the role cards used on this path
    advertise ``delegate`` in their allowlist and their prompts tell the model to
    use it, so leaving the tree unbound made an advertised tool fail every single
    time with "no delegation tree bound to this mission".
    """
    from services.agent import agent_turn as _at
    from services.agent.roles import load_role
    from services.agent.copilot_mode_tools import SUBAGENT_DEFAULT_TOOLS

    card = load_role(role)
    allowed = sorted(set(card.concrete_tools) | SUBAGENT_DEFAULT_TOOLS)
    try:
        from services.agent.config import bugreport_floor  # noqa: PLC0415
        allowed = sorted(set(allowed) | bugreport_floor())
    except Exception:  # noqa: BLE001 — floor union is best-effort
        pass

    # Meta-tool coverage (live admin path passes no dispatcher): ask_user &
    # the other coordination tools resolve only via MissionDispatch.resolve,
    # so a bare linear turn gets a run-scoped dispatcher of its own.
    if mission_dispatch is None and tool_dispatch is None:
        try:
            import uuid as _uuid  # noqa: PLC0415
            _rid = run_id or f"linear-{_uuid.uuid4().hex[:8]}"
            # Bind a real delegation tree so `delegate` — which every role card on
            # this path advertises — actually spawns and RUNS a sub-agent instead of
            # returning a refusal. The provider is already bound below, which is the
            # other half `_run_subagent` needs. The tree self-limits via its budget
            # meter and agent.max_subagent_depth cap, so this stays bounded.
            tree = None
            try:
                from services.agent.delegation import (  # noqa: PLC0415
                    DelegationTree as _DelegationTree,
                )
                tree = _DelegationTree(run_id=_rid, ledger=ledger, on_event=on_event)
            except Exception:  # noqa: BLE001 — degrade to the old no-delegation turn
                tree = None
            mission_dispatch = MissionDispatch(
                run_id=_rid, ledger=ledger, provider=provider, on_event=on_event,
                delegation_tree=tree)
        except Exception:  # noqa: BLE001 — dispatcher is best-effort
            mission_dispatch = None

    system_prompt = card.system_prompt
    try:
        from services.agent.prompt_tiers import (  # noqa: PLC0415
            assemble_system_for_capability,
            letter_from_capability,
        )
        system_prompt = assemble_system_for_capability(
            card.system_prompt, letter_from_capability(capability))
    except Exception:  # noqa: BLE001
        system_prompt = card.system_prompt

    tools_text = None
    try:
        from services.agent.tool_schemas import render_tools_block  # noqa: PLC0415
        tools_text, _meta = render_tools_block(
            allowed, resolver=_at.resolver_for(mission_dispatch))
    except Exception:  # noqa: BLE001 — schema render is best-effort
        tools_text = None

    result = _at.run_turn(
        request, provider, name=role, actor=role,
        system_prompt=system_prompt,
        allowed_tools=allowed,
        tools_text=tools_text,
        max_tool_calls=max_tool_calls or LINEAR_ROLE_MAX_TOOL_CALLS,
        ledger=ledger,
        mission_dispatch=mission_dispatch,
        tool_dispatch=tool_dispatch,
        on_event=on_event,
        run_id=run_id,
        capability=capability,
    )
    # OMP Auto-Learn capture-at-stop (agent.autolearn_*). Main-chat / linear
    # entry only — never raises into the turn path.
    try:
        from services.agent.autolearn import maybe_capture_after_turn  # noqa: PLC0415
        _calls = 0
        _res = result.result if isinstance(getattr(result, "result", None), dict) else {}
        if isinstance(_res, dict):
            _calls = int(_res.get("tool_calls") or 0)
            if not _calls and isinstance(_res.get("calls"), list):
                _calls = len(_res["calls"])
        maybe_capture_after_turn(
            request=request, result=result, tool_calls=_calls,
        )
    except Exception:  # noqa: BLE001
        pass
    # Handoff synthesis — the same rescue _run_subagent applies at its join.
    # This is the UNIVERSAL top-level entry (every online run lands here via
    # worker_rpc.agent_admin_turn), yet it returned run_turn's raw result: on a
    # turn that did real tool work but emitted no formal envelope, the caller
    # got a payload with no "handoff" key at all and had to treat a productive
    # run as a blank failure.
    res_dict = result.result if isinstance(result.result, dict) else {}
    handoff = res_dict.get("handoff")
    if not isinstance(handoff, dict) or not handoff:
        calls = res_dict.get("calls") if isinstance(res_dict.get("calls"), list) else []
        n_tool = len([c for c in calls if isinstance(c, dict)])
        if calls or result.success:
            res_dict = dict(res_dict)
            res_dict["handoff"] = {
                "status": "done" if result.success else "blocked",
                "summary": (
                    f"{role} turn finished stop_reason={result.stop_reason} "
                    f"tool_calls={n_tool}"
                )[:600],
                "outputs": {
                    "tool_calls": n_tool,
                    "calls": [
                        {"tool": c.get("tool"), "ok": c.get("ok")}
                        for c in calls[:20] if isinstance(c, dict)
                    ],
                    "stop_reason": result.stop_reason,
                    "reasoning_summary": (
                        "Synthesised from the turn result (no formal envelope "
                        "emitted); mirrors the sub-agent join rescue."
                    ),
                },
            }
    return {
        "success": result.success,
        "stop_reason": result.stop_reason,
        "result": res_dict or result.result,
        "role": role,
        "allowed_tools": allowed,
    }


def route_request(
    request: str,
    tier: str,
    provider: Any,
    *,
    on_event: Optional[Callable[..., None]] = None,
    run_id: str | None = None,
    ledger: Any | None = None,
    mission_dispatch: MissionDispatch | None = None,
    capability: Any | None = None,
    max_tool_calls: int | None = None,
    tool_dispatch: Optional[Callable[[str, dict], Any]] = None,
    tuning: dict | None = None,
) -> dict[str, Any]:
    """Classify *request* for the advisory Routing card, then run it.

    Model-driven routing (2026-07): there is NO deterministic structure gate.
    EVERY request runs ONE ``run_linear_role_turn`` against ``general_agent`` —
    the universal entry — which decides for itself whether to finish the work
    inline, ``delegate`` a scoped build, or ``mission_start`` the full build
    swarm. ``workflow_router.route`` is still consulted, but ONLY to emit the
    ``agent.classification`` telemetry (contracts/agent_events.md) the GUI
    Routing card renders; its ``structure``/``role`` are advisory, not a gate.
    """
    from services.agent.pipeline import workflow_router

    route = workflow_router.route(request, tier, tuning=tuning, capability=capability)

    if on_event is not None:
        try:
            from services.agent import agent_events as _ae  # noqa: PLC0415
            _kind, _payload = _ae.classification_event(
                run_id or "mission",
                task_class=route.task_class,
                scaffold_level=route.scaffold_level,
                tier=tier,
                reason=route.reason,
                request_snippet=request[:120],
                alternatives=route.alternatives,
            )
            _ae.emit_event(_kind, _payload, live=on_event, journal=None)
        except Exception:  # noqa: BLE001 — telemetry must never block dispatch
            pass

    turn = run_linear_role_turn(
        request, "general_agent", provider,
        on_event=on_event, run_id=run_id, ledger=ledger,
        mission_dispatch=mission_dispatch, capability=capability,
        max_tool_calls=max_tool_calls, tool_dispatch=tool_dispatch,
    )
    return {
        "ok": True,
        "structure": "linear",
        "task_class": route.task_class.value,
        "role": "general_agent",
        "route": route.to_dict(),
        "turn": turn,
    }
