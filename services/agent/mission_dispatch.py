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

    #: The run-scoped tool families this layer serves (task T-W32 DoD list).
    TOOLS = ("ledger_get", "ledger_note", "dag_plan", "ask_user",
             "delegate", "part_search")

    def __init__(self, *, run_id: str, ledger=None, autonomy: str = "auto",
                 graph: dict | None = None, delegation_tree=None,
                 doc_id: str | None = None, provider=None, on_event=None) -> None:
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
        self._handlers: dict[str, Callable[..., Any]] = {
            "ledger_get": self.ledger_get,
            "ledger_note": self.ledger_note,
            "dag_plan": self.dag_plan,
            "ask_user": self.ask_user,
            "delegate": self.delegate,
            "part_search": self.part_search,
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
            result = _at.run_turn(
                brief, self.provider, name=role, actor=role,
                ledger=self.ledger, max_tool_calls=4,
                on_event=self.on_event, run_id=self.run_id,
                node_id=node_id, subagent_id=node_id)
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
                    **_ignored: Any) -> dict:
        """Search the unified standard-parts library (all domains federated).

        query: free text, e.g. "M8 socket head", "bearing 6205"; domain optionally
        filters (fastener | bearing | mech_family | section | fitting | hydraulic |
        ecad); limit caps results (default 20).
        """
        if not query:
            return {"ok": False, "error": "part_search needs a query string"}
        try:
            from engine_oracle.parts_hub import search as _search
            results = _search(query, domain=domain or None,
                              limit=max(1, min(int(limit), 200)))
            return {"ok": True, "count": len(results),
                    "has_live": any(r.get("source") == "live" for r in results),
                    "results": results}
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"part_search failed: {exc}",
                    "hint": "Check that engine_oracle/parts_hub is importable."}


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
