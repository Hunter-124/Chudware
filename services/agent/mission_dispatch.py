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
                      "feature", "geometry_spec", "linkage", "build_node"}

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
    #: Parts Library V2 sourcing surface — workstream 5).
    TOOLS = ("ledger_get", "ledger_note", "dag_plan", "ask_user",
             "delegate", "part_search",
             "parts_taxonomy", "part_get", "part_source", "match_model",
             "reverse_engineer_product", "vendors_health",
             "scrape_enqueue", "scrape_queue_status", "import_artifact",
             "cam_tool_import")

    def __init__(self, *, run_id: str, ledger=None, autonomy: str = "auto",
                 graph: dict | None = None, delegation_tree=None,
                 doc_id: str | None = None, provider=None, on_event=None,
                 capability=None, tier: str | None = None,
                 phase: str | None = None,
                 task_class: str | None = None) -> None:
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
        questions | stage_log | artifacts; omit for the full rendered ledger.
        """
        led = self._ledger(run_id)
        if led is None:
            return {"ok": False, "error": "no ledger bound to this mission run"}
        if section:
            allowed = {"brief", "requirements", "decisions", "dimensions",
                       "questions", "stage_log", "artifacts",
                       # T-DP1 design-pipeline sections (contract §4/§9)
                       "features", "geometry_specs", "linkages", "build_nodes"}
            if section not in allowed:
                return {"ok": False, "error": f"unknown section {section!r}",
                        "allowed": sorted(allowed)}
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
        artifact | note.  Dimension events MUST carry a provenance field
        (tool result / calc / table / user answer) — contract rule.
        """
        led = self._ledger(run_id)
        if led is None:
            return {"ok": False, "error": "no ledger bound to this mission run"}
        if kind not in _LEDGER_NOTE_KINDS:
            return {"ok": False, "error": f"unknown kind {kind!r}",
                    "valid_kinds": sorted(_LEDGER_NOTE_KINDS)}
        body = dict(payload or {})
        body.update({k: v for k, v in extra.items() if not k.startswith("_")})
        if kind in _PROVENANCE_REQUIRED_KINDS and not body.get("provenance"):
            return {"ok": False,
                    "error": (f"{kind} events require a 'provenance' field "
                              "(tool result / calc / table / user answer)")}
        try:
            event = led.append(actor, kind, body)
            return {"ok": True, "seq": event["seq"], "run_id": led.run_id,
                    "kind": kind, "ts": event["ts"]}
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
        """
        if self.delegation_tree is None:
            if self.ledger is not None:
                try:
                    self.ledger.append("agent", "note", {
                        "event": "delegation_request", "role": role,
                        "brief": str(brief)[:500], "budget_tokens": budget_tokens,
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
            node = _delegate(role, brief, budget_tokens, tree=self.delegation_tree,
                             parent_id=parent_id, kind=kind, docs=docs)
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "status": "failed", "error": f"delegate failed: {exc}"}

        # Phase 4 (M11): run the approved sub-agent NOW (synchronous, bounded).
        # HONESTY: without a bound provider nothing executes — the node is returned
        # spawned-but-not-run with an explicit note (never a fabricated result).
        if not isinstance(node, dict) or node.get("status") != "approved":
            return node
        if self.provider is None:
            node["executed"] = False
            node["note"] = ("sub-agent spawned (registry session created) but NOT "
                            "executed: no provider bound to this mission dispatch")
            return node
        node["executed"] = True
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
            if _cap is not None:
                try:
                    from services.agent import scaffolds as _sc  # noqa: PLC0415
                    max_calls, _ = _sc.apply_phase_cap(
                        _cap, _phase, max_calls,
                        ledger=self.ledger, actor=role,
                    )
                    if max_calls is None:
                        max_calls = max(4, min(12, derived))
                except Exception:  # noqa: BLE001
                    pass
            # WS2: per-node model pick when ai_roles is set / auto.
            provider = self.provider
            try:
                from services.agent.role_models import resolve_for_node  # noqa: PLC0415
                from services.agent import config as agent_config  # noqa: PLC0415
                depth = 0
                try:
                    depth = int((node.get("depth") if isinstance(node, dict)
                                 else getattr(node, "depth", 0)) or 0)
                except (TypeError, ValueError):
                    depth = 0
                policy = getattr(self, "_last_spawn_policy", None) or {}
                ctx = {
                    "task_class": self.task_class,
                    "depth": depth,
                    "risk": "low" if str(node.get("kind") or "") == "ponder" else None,
                    "node_inputs": {"task_class": self.task_class, "depth": depth},
                }
                # Map harness roles: subagent worker → task; planner-ish → planning.
                harness_role = "task"
                if role in ("overseer", "orchestrator", "department_manager"):
                    harness_role = "planning"
                elif role in ("advisor", "reviewer"):
                    harness_role = "advisor"
                assignment = resolve_for_node(harness_role, ctx)
                if assignment is not None and policy.get("min_rank") is not None:
                    # Re-resolve with depth-aware min_rank (decreasing difficulty).
                    from services.agent.role_models import resolve_role_model  # noqa: PLC0415
                    assignment = resolve_role_model(
                        harness_role,
                        agent_config.load_config(),
                        task_class=self.task_class,
                        node_depth=depth,
                        min_rank=int(policy["min_rank"]),
                    )
                if assignment is not None:
                    cfg = dict(agent_config.load_config())
                    cfg["model"] = assignment.model
                    try:
                        provider = agent_config.get_provider(
                            name=assignment.provider, cfg=cfg)
                        if isinstance(node, dict):
                            node["role_model"] = assignment.to_dict()
                    except Exception:  # noqa: BLE001 — keep mission provider
                        provider = self.provider
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
            tokens = 0  # provider seam exposes no usage here; accrual stays honest at 0
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
                return {"ok": False, "subagent_status": "terminated",
                        "result": result.result}
            if result.success:
                try:
                    self.delegation_tree.complete(node_id, tokens_used=tokens)
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": True, "subagent_status": "done",
                        "handoff": result.result.get("handoff"),
                        "result": result.result}
            try:
                self.delegation_tree.fail(node_id, reason=result.stop_reason)
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "subagent_status": "failed",
                    "stop_reason": result.stop_reason, "result": result.result}
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
