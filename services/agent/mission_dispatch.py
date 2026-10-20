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
import logging
import threading
from typing import Any, Callable, Optional

from services.mcp.tool_registry import model_selectors

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

# ask_user's question normalization / bad-arguments teaching lives in ONE
# place (services.agent.interaction, the interaction core) so this
# mission-scoped shadow and agent_turn._ask_user_sync cannot drift — they had
# already drifted (a claude-sonnet-5 run hit a slightly different corrected
# example from each call site).
from services.agent.interaction import (  # noqa: E402
    PROMPT_ALIASES as _PROMPT_ALIASES,
    TOP_LEVEL_QUESTION_ALIASES as _TOP_LEVEL_QUESTION_ALIASES,
    ask_user_bad_arguments_error as _ask_user_bad_arguments_error,
    normalize_questions as _normalize_questions,
)

logger = logging.getLogger(__name__)


class _RecordOnlyLedger:
    """Persist a live batch's questions and answers without id-keyed replay."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def answered(self, qid: str) -> dict | None:
        return None

    def record_question(self, q: dict) -> None:
        self._inner.record_question(q)

    def record_answer(self, qid: str, answer: dict) -> None:
        self._inner.record_answer(qid, answer)


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


# Provenance validation lives in services.agent.ledger.validate_ledger_payload so
# this mission shadow and the standalone MCP writer cannot drift.


def _delegation_execution_verdict(node: dict) -> dict:
    """Downgrade a delegate node that claims execution it cannot evidence.

    Returns the keys to merge. `executed` is set optimistically before the child runs, so this
    is the only place that can tell the truth about whether anything HAPPENED. Evidence, any one
    of which is enough: tokens actually spent, an attempt actually made, a handoff/result the
    child produced, tool calls it made, or documents it left behind.

    A node with none of those and an EMPTY model descriptor never had a model bound, so it could
    not have run — that is reported as `executed: False` with `ok: False` and a reason, so the
    caller's own honesty gates (agent_turn._did_real_work / _unearned_finish_block) see a failed
    call instead of silent success. Measured live 2026-07-29: firmware pass 2c closed a turn as
    `confirmed` on one such node, claiming a simulation that never happened.
    """
    if not isinstance(node, dict) or node.get("executed") is not True:
        return {}
    budget = node.get("budget") if isinstance(node.get("budget"), dict) else {}
    model = node.get("model") if isinstance(node.get("model"), dict) else {}
    handoff = node.get("handoff")
    spent = int(budget.get("tokens_spent") or 0)
    attempts = int(budget.get("attempts") or 0)
    calls = node.get("calls") or node.get("tool_calls") or []
    ran = bool(
        spent > 0
        or attempts > 0
        or (isinstance(handoff, dict) and handoff)
        or calls
        or node.get("docs")
        or node.get("subagent_status") in ("done", "failed", "error")
        or node.get("result") is not None
    )
    if ran:
        return {}
    bound = (
        str(model.get("provider") or "").strip() or str(model.get("id") or "").strip()
    )
    why = (
        "no model was bound to the sub-agent role"
        if not bound
        else "the sub-agent produced no attempt, no tokens, no handoff and no documents"
    )
    return {
        "executed": False,
        "ok": False,
        "status": node.get("status") or "approved",
        "subagent_status": "not_run",
        "note": (
            f"sub-agent '{node.get('role') or '?'}' was SPAWNED but did NOT run: {why}. "
            f"Nothing was produced, so do not report this brief as done — either do the "
            f"work directly with the concrete tools, or say plainly that delegation is "
            f"unavailable in this run."
        ),
    }


def _actionable_delegate_failure(node: dict) -> dict:
    """Put the sub-agent failure first so the model and clipped ledger see why.

    The live 2026-08-05 holster replay spawned and ran a ``feature_modeler`` whose
    provider then returned no completion. ``_run_subagent_turn`` correctly set
    ``ok=False``, but the merged node started with its id and 500-character brief
    and carried the provider error only inside ``result``. The 1,000-character
    ledger clip and the GUI's generic error fallback therefore showed the request
    echoed back instead of the cause.
    """
    if not isinstance(node, dict) or node.get("ok") is not False:
        return node

    nested = node.get("result") if isinstance(node.get("result"), dict) else {}
    stop_reason = str(
        node.get("stop_reason") or nested.get("stop_reason") or ""
    ).strip()
    subagent_status = str(node.get("subagent_status") or "").strip()
    reason = str(node.get("reason") or "").strip()
    if not reason:
        if stop_reason:
            reason = f"subagent_{stop_reason}"
        elif subagent_status:
            reason = f"subagent_{subagent_status}"
        else:
            reason = "subagent_failed"

    error = str(node.get("error") or "").strip()
    detail = str(
        nested.get("error_detail") or nested.get("error") or node.get("note") or ""
    ).strip()
    runtime_unavailable = stop_reason == "provider_error" and detail.startswith(
        (
            "Agent service RPC support is unavailable",
            "Agent service executable not found",
        )
    )
    if runtime_unavailable:
        role = str(node.get("role") or "sub-agent")
        rest = {
            key: value
            for key, value in node.items()
            if key
            not in {
                "ok",
                "reason",
                "error",
                "status",
                "executed",
                "subagent_status",
                "do_instead",
            }
        }
        return {
            "ok": False,
            "reason": "delegation_unavailable",
            "error": (
                f"delegate cannot run {role!r}: {detail}. No child model attempt "
                "was made. Do NOT retry delegate — do the work yourself with the "
                "concrete tools this turn already has, and record unfinished work "
                "with ledger_note(kind='handoff', ...)."
            ),
            "status": "unavailable",
            "executed": False,
            "subagent_status": "not_run",
            "do_instead": [
                "build/inspect directly with the modeling tools in your allowlist",
                "ledger_note(kind='handoff', payload={...}) for work you cannot finish",
            ],
            **rest,
        }
    if not error:
        role = str(node.get("role") or "sub-agent")
        if stop_reason:
            error = (
                f"delegate spawned {role!r}, but the sub-agent failed with "
                f"stop_reason={stop_reason!r}"
            )
        elif subagent_status == "not_run":
            error = f"delegate spawned {role!r}, but the sub-agent did not run"
        else:
            error = f"delegate spawned {role!r}, but the sub-agent failed"
        if detail:
            error += f": {detail}"
        error += (
            ". The delegated work was not completed; retry only if the cause is "
            "transient, otherwise continue with the concrete tools available here."
        )
    if node.get("provider_retry"):
        # The clip is ~999 chars and starts at `error`, so the retry has to be
        # named HERE or the parent model reads a one-shot terminal failure and
        # cannot tell a dead route from a provider that was already given its
        # second chance.
        error += (
            " A transient-fault retry was already spent on this spawn "
            "(see provider_retry)."
        )

    # Keep these keys first: both agent_events._error_text and the persisted
    # tool-result clip must expose the cause before the spawn record / brief.
    rest = {
        key: value
        for key, value in node.items()
        if key not in {"ok", "reason", "error"}
    }
    return {"ok": False, "reason": reason, "error": error, **rest}


#: Total ``run_turn`` attempts a `delegate` child gets when the FIRST failure was
#: classified transient by ``agent_turn.classify_provider_error``: 1 initial +
#: 1 retry == ``services.dag.context_pack.DEFAULT_RETRY_BUDGET`` (1). The two
#: dispatch lanes must recover the same single 429 / 5xx / truncated reply /
#: mid-stream disconnect and no more; a larger budget here is exactly the
#: cross-path drift this subsystem keeps regressing into.
SUBAGENT_RETRYABLE_ATTEMPTS = 2


def retryable_child_failure(result: Any) -> bool:
    """True when a child turn failed on a fault the provider classifier calls transient.

    The single predicate BOTH dispatch lanes gate on — ``_run_subagent_turn``
    (the ``delegate`` tool) and ``services.dag.node_runner._map_verdict``
    (compiled-DAG nodes). ``agent_turn.run_turn`` stamps ``result['retryable']``
    from ``classify_provider_error``; a 404 dead route and a real auth failure
    are stamped ``False`` there, and that is load-bearing — an absent or False
    flag must still fail fast, because re-running those burns a spawn to fail
    identically. Both the legacy ``failed`` and the current ``provider_error``
    stop_reason shapes carry the flag.
    """
    if bool(getattr(result, "success", False)):
        return False
    if str(getattr(result, "stop_reason", "") or "") not in (
        "failed",
        "provider_error",
    ):
        return False
    payload = getattr(result, "result", None)
    return isinstance(payload, dict) and payload.get("retryable") is True


def configured_subagent_tool_budget(base: int | None) -> int | None:
    """Apply the Settings → AI ``agent.subagent_tool_call_budget`` slider.

    Called LAST on both dispatch lanes, so a user's explicit threshold is never
    silently raised by token-grant derivation or a producer floor. The value
    stays a SOFT convergence threshold — the hard runaway ceiling belongs to
    ``agent_turn.SUBAGENT_HARD_TOOL_CALL_CEILING`` alone.

    Zero (the default) means "derive automatically" and returns *base* untouched.
    This lives in one function because the slider used to be read only by
    ``_run_subagent_turn``: ``services/dag/node_runner`` never read the key at
    all, so moving the slider governed the ``delegate`` lane and had ZERO effect
    on every compiled-DAG (``mission_start``) role, while the GUI presented it
    as one uniform setting.
    """
    try:
        from services.agent.config import (  # noqa: PLC0415
            get_subagent_tool_call_budget,
        )

        configured = get_subagent_tool_call_budget()
    except Exception:  # noqa: BLE001 — automatic derivation is the fallback
        return base
    return configured if configured > 0 else base


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
    TOOLS = (
        "ledger_get",
        "ledger_note",
        "dag_plan",
        "ask_user",
        "delegate",
        "part_search",
        "parts_taxonomy",
        "part_get",
        "part_source",
        "match_model",
        "reverse_engineer_product",
        "vendors_health",
        "scrape_enqueue",
        "scrape_queue_status",
        "import_artifact",
        "cam_tool_import",
        # Coordination board (shared claims / progress across sub-agents)
        "coordinate_claim",
        "coordinate_release",
        "coordinate_progress",
        "coordinate_board",
        # Corporate org mode channels / meetings / head spawn
        "corporate_channel_post",
        "corporate_channel_read",
        "corporate_chart",
        "corporate_spawn_worker",
        "corporate_hold_meeting",
        "corporate_record_completion",
        "corporate_activate",
    )

    def __init__(
        self,
        *,
        run_id: str,
        ledger=None,
        autonomy: str = "auto",
        graph: dict | None = None,
        delegation_tree=None,
        doc_id: str | None = None,
        provider=None,
        on_event=None,
        capability=None,
        tier: str | None = None,
        phase: str | None = None,
        task_class: str | None = None,
        coordination_board=None,
        org_chart=None,
        corporate_agent_id: str | None = None,
        options: dict | None = None,
        mode: str | None = None,
        corporate_force: bool | None = None,
        plan_only: bool | None = None,
        advisor: bool | None = None,
        chudware_actor: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self.ledger = ledger
        self.autonomy = autonomy
        self.graph = graph
        self.delegation_tree = delegation_tree
        self.doc_id = doc_id
        # Online delegated turns execute through Chudcode and never consume this
        # object.  ``provider`` survives only as an injected deterministic local
        # provider for an explicit ``options={"offline": True}`` run.
        self.provider = provider
        self.on_event = on_event
        self.chudware_actor = chudware_actor
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
        # Preserve non-mode execution context (parent Chudcode session,
        # explicit offline provider, settings, workspace and attachments).
        # mode_options_from_mapping intentionally returns only UI mode fields.
        self.execution_options = dict(options) if isinstance(options, dict) else {}
        # Copilot mode overhaul: options.mode / corporate_force / plan_only /
        # advisor threaded from host/core so tool + org policy can honor them.
        try:
            from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                mode_options_from_mapping,
            )

            # Merge explicit kwargs into options BEFORE normalize so
            # mode==swarm → corporate_force and mode==plan → plan_only couple.
            _raw = dict(self.execution_options)
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
            self.mode = mode or "agent"
            self.corporate_force = (
                bool(corporate_force) if corporate_force is not None else False
            )
            self.plan_only = bool(plan_only) if plan_only is not None else False
            self.advisor = True if advisor is None else bool(advisor)
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
    def ledger_get(
        self,
        section: str | None = None,
        budget_tokens: int = 800,
        run_id: str | None = None,
    ) -> dict:
        """Return the design ledger for this run, rendered within *budget_tokens*.

        section filters to one of brief | requirements | decisions | dimensions |
        questions | stage_log | artifacts | criterion_results | features |
        geometry_specs | linkages | build_nodes | mfg_plans | manifest; omit
        (or section="all"/"*") for the full rendered ledger.
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
                "ok": True,
                "run_id": led.run_id,
                "section": "events",
                "data": list(led.events),
                "event_count": len(led.events),
            }
        if section:
            if section == "manifest":
                arts = led.state.get("artifacts") or []
                manifests = [
                    a
                    for a in arts
                    if (a.get("kind") == "manifest" or a.get("id") == "manifest")
                ]
                data = manifests[0] if manifests else None
                # Prefer embedded payload when intake wrote it.
                if isinstance(data, dict) and data.get("manifest"):
                    data = data["manifest"]
                return {
                    "ok": True,
                    "run_id": led.run_id,
                    "section": "manifest",
                    "data": data,
                }
            return {
                "ok": True,
                "run_id": led.run_id,
                "section": section,
                "data": led.state.get(section),
            }
        from services.agent.ledger import ledger_render

        rendered = ledger_render(led, budget_tokens)
        return {
            "ok": True,
            "run_id": led.run_id,
            "rendered": rendered,
            "event_count": len(led.events),
        }

    def ledger_note(
        self,
        kind: str = "note",
        payload: dict | None = None,
        actor: str = "agent",
        run_id: str | None = None,
        **extra: Any,
    ) -> dict:
        """Append a structured event to this run's design ledger.

        kind: brief | decision | dimension | question | requirement | stage |
        artifact | note | criterion_result | feature | geometry_spec | linkage |
        build_node | mfg_plan (+ aliases: verdict→decision, …).
        Numeric dimensions, geometry specs, and numeric manufacturing-plan
        critical rows MUST carry provenance.

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
        if kind in (
            "decision",
            "dimension",
            "question",
            "artifact",
            "feature",
            "geometry_spec",
            "linkage",
            "build_node",
            "mfg_plan",
            "criterion_result",
        ):
            if not body.get("id"):
                for alt in (
                    "name",
                    "decision_id",
                    "dim_id",
                    "req_id",
                    "artifact_id",
                    "feature_id",
                    "spec_id",
                    "node_id",
                    "plan_id",
                    "deliverable",
                    "criterion_id",
                ):
                    if body.get(alt):
                        body["id"] = str(body[alt])
                        break
            if not body.get("id") and body.get("text"):
                slug = "".join(
                    c if c.isalnum() else "_" for c in str(body["text"])[:48].lower()
                ).strip("_")
                body["id"] = slug or f"{kind}_{len(getattr(led, 'events', []) or [])}"
            if not body.get("id"):
                body["id"] = (
                    "main"
                    if kind == "mfg_plan"
                    else f"{kind}_{len(getattr(led, 'events', []) or [])}"
                )
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
            body.setdefault(
                "rationale", body.get("evidence") or body.get("driver") or ""
            )
        try:
            from services.agent.ledger import validate_ledger_payload

            validate_ledger_payload(kind, body)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            event = led.append(actor, kind, body)
            # Best-effort project memory: durable decisions/notes for later turns.
            if kind in ("decision", "note", "requirement", "dimension", "brief"):
                try:
                    from services.agent.config import (  # noqa: PLC0415
                        memory_enabled,
                        memory_learn_enabled,
                        workspace_dir,
                    )
                    from services.agent import project_memory as _pm  # noqa: PLC0415

                    if memory_enabled() and memory_learn_enabled():
                        _root = getattr(self, "project_dir", None) or getattr(
                            self, "workspace_dir", None
                        )
                        _path = _pm.resolve_project_root(_root) or workspace_dir()
                        _text = str(
                            body.get("text")
                            or body.get("summary")
                            or body.get("value")
                            or ""
                        ).strip()
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
            return {
                "ok": True,
                "seq": event["seq"],
                "run_id": led.run_id,
                "kind": kind,
                "ts": event["ts"],
                **({"aliased_from": raw_kind} if raw_kind != kind else {}),
            }
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

        The reply carries the plan in BOTH shapes: ``graph`` is the cheap
        role-per-node summary to read, ``graph_json`` is the executable
        aicad.dag/1 document dag_run wants.  It used to carry only the summary —
        which strips inputs/end_condition/on_fail/autonomy, so it is not a graph
        the executor would accept — while still answering ``ok: True`` with
        "mission plan already compiled".  dag_run, reading its own module-private
        session slot, simultaneously said no graph had been planned.  Live
        2026-08-06 (run-92188923731943) the model believed both and burned the
        rest of the turn alternating between them.  A plan bound to this mission
        is registered in the shared session slot here, so a bare ``dag_run()``
        executes THIS graph.

        With nothing bound, the reply says so rather than claiming a compiled
        plan.  A graph planned elsewhere in this process is reported under
        ``planned_in_session`` with its provenance — never relabelled as this
        mission's, since one hostd serves many sessions.
        """
        graph = self.graph
        out: dict[str, Any] = {"ok": True, "run_id": self.run_id}
        if graph:
            nodes = [
                {
                    "id": n.get("id"),
                    "type": n.get("type"),
                    "name": n.get("name"),
                    "role": (n.get("inputs") or {}).get("role"),
                }
                for n in graph.get("nodes", [])
            ]
            source = str(self.run_id or "")
            out["note"] = (
                "mission plan already compiled; to change it, "
                "emit a replan_request in your handoff"
            )
            out["graph"] = {
                "id": graph.get("id"),
                "name": graph.get("name"),
                "nodes": nodes,
                "edges": graph.get("edges", []),
            }
            out["graph_json"] = graph
            try:
                from services.dag import session as _dag_session  # noqa: PLC0415

                _dag_session.remember_graph(graph, source=source)
                out["next_actions"] = (
                    [f"dag_run(run_id={source!r})"] if source else ["dag_run()"]
                )
                out["hint"] = (
                    "dag_run() with no arguments executes this graph — you do "
                    "not need to copy graph_json back."
                )
            except Exception:  # noqa: BLE001 — plan reporting must not break dispatch
                out["next_actions"] = ["dag_run(graph_json=<result.graph_json>)"]
            return out
        # Nothing bound to THIS mission.  Say so instead of claiming a compiled
        # plan, and never re-label another mission's graph as this one's: one
        # long-lived hostd serves many sessions, so the session slot is not
        # evidence about this run.  It IS worth reporting that a graph exists and
        # what would execute it, with its provenance attached.
        out["note"] = (
            "this mission has no compiled DAG graph bound — mission_start "
            "(with an aicad.deliverables/1 manifest) is what compiles one; "
            "to change an existing plan, emit a replan_request in your handoff"
        )
        out["next_actions"] = [
            "mission_start(request='<the ask>', manifest=<aicad.deliverables/1>)"
        ]
        try:
            from services.dag import session as _dag_session  # noqa: PLC0415

            planned = _dag_session.planned_graph()
            if planned:
                origin = _dag_session.planned_from() or str(planned.get("id") or "")
                out["planned_in_session"] = {
                    "source": origin,
                    "graph_id": planned.get("id"),
                    "graph_name": planned.get("name"),
                    "node_count": len(planned.get("nodes") or []),
                    "note": (
                        "planned elsewhere in this process (not bound to this "
                        "mission run) — dag_run() would execute THIS graph"
                    ),
                }
                out["next_actions"] = [
                    f"dag_run(run_id={origin!r})" if origin else "dag_run()",
                    *out["next_actions"],
                ]
        except Exception:  # noqa: BLE001 — plan reporting must not break dispatch
            pass
        if self.ledger is not None:
            out["stage_log"] = self.ledger.state.get("stage_log")
        return out

    # ------------------------------------------------------------------ ask_user
    def ask_user(
        self,
        questions: Any = None,
        question: Any = None,
        scope: str = "run",
        ask_human: Any = None,
        **extra: Any,
    ) -> dict:
        """Ask the human a typed question batch (contract §4).

        In the desktop chat (``ask_human`` injected by an interactive turn) the batch
        is shown to the user as a form and this call waits for their answers, a typed
        reply, a dismissal, or Stop. Without a human transport (autonomous runs) every
        question is RECORDED on the run ledger (a human reviews them later) and
        answered with its suggested default (source: "default") — never a fabricated
        human reply.
        """
        raw = questions if questions is not None else question
        if raw is None:
            # Models put the question under whatever key they feel like; those
            # used to land in **_ignored and the call looked argument-less.
            for alias in _TOP_LEVEL_QUESTION_ALIASES:
                value = extra.get(alias)
                if value:
                    raw = value
                    break
        norm = _normalize_questions(raw)
        norm = [q for q in norm if str(q.get("prompt") or "").strip()]
        if not norm:
            # An empty/unparseable call repeated verbatim is the live haiku loop
            # (2026-07-27: ask_user {} ×4 until the loop guard killed the run).
            # A schema recital did not help it; a copy-pasteable example and an
            # explicit way OUT of needing the tool at all does. Shared with
            # every other ask_user entry point (services.agent.interaction).
            return _ask_user_bad_arguments_error()
        if callable(ask_human):
            return self._ask_human(norm, ask_human)
        try:
            from services.agent import interaction

            answers = interaction.ask_user(
                norm,
                scope="run",
                run_id=self.run_id,
                blocking=False,
                autonomy=self.autonomy,
            )
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"ask_user failed: {exc}"}
        # Mirror the Q&A onto the mission design ledger so the run record (and the
        # gym classifier) see the questions asked.
        if self.ledger is not None:
            for q in norm:
                try:
                    self.ledger.add_question(
                        "agent", q["id"], q.get("prompt", ""), blocking=False
                    )
                    ans = answers.get(q["id"]) or {}
                    self.ledger.answer_question(
                        "agent", q["id"], json.dumps(ans.get("value"), default=str)
                    )
                except Exception:  # noqa: BLE001 — mirroring is best-effort
                    pass
        return {
            "ok": True,
            "answers": answers,
            "note": (
                "autonomous run: questions recorded for the human; "
                "suggested defaults applied (source per answer)"
            ),
        }

    def _ask_human(self, norm: list[dict], ask_human: Callable[[dict], Any]) -> dict:
        """Show *norm* to the user and wait (the interactive-turn ask_user path).

        Answers go through the interaction core (typed finalization, dimension
        canonicalization, run ledger records). The ledger never REPLAYS here: models
        number every batch q1, q2, … so an id-keyed replay would hand a later batch an
        earlier batch's answers. A typed chat reply, a dismissal or Stop end the wait
        without form answers and are reported as such — never as defaults.
        """
        from services.agent import interaction
        from services.agent.chudcode_questions import QuestionUnavailable

        outcome: dict[str, Any] = {}

        class _Unanswered(Exception):
            pass

        class _HumanTransport:
            def ask(self, batch: Any) -> dict:
                resolution = ask_human(batch.to_dict())
                outcome["resolution"] = resolution
                if resolution.source != "user":
                    raise _Unanswered()
                return dict(resolution.answers)

        ledger: Any = interaction.MemoryLedger()
        if self.run_id:
            try:
                ledger = _RecordOnlyLedger(interaction.FileLedger(self.run_id))
            except OSError:
                pass
        try:
            answers = interaction.ask_user(
                norm,
                scope="run",
                run_id=self.run_id or "interactive",
                blocking=True,
                autonomy="approve",
                transport=_HumanTransport(),
                ledger=ledger,
            )
        except _Unanswered:
            pass
        except QuestionUnavailable as exc:
            return {"ok": False, "error": f"ask_user could not reach the user: {exc}"}
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {"ok": False, "error": f"ask_user failed: {exc}"}
        else:
            return {"ok": True, "answers": answers, "answered_by": "user"}
        resolution = outcome.get("resolution")
        source = getattr(resolution, "source", "cancelled")
        if source == "reply":
            return {
                "ok": True,
                "answers": {},
                "user_reply": resolution.reply,
                "note": (
                    "The user replied in chat instead of filling in the form. Take "
                    "your answers from user_reply; ask again only for what it leaves open."
                ),
            }
        if source == "declined":
            return {
                "ok": True,
                "answers": {},
                "declined": True,
                "note": (
                    "The user dismissed these questions. Continue on stated "
                    "assumptions where that is safe, say which you used, and do not "
                    "re-ask the same questions this turn."
                ),
            }
        return {
            "ok": False,
            "status": "cancelled",
            "cancelled": True,
            "error": "The run was stopped while waiting for the user's answers.",
        }

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

    def delegate(
        self,
        role: str = "",
        brief: str = "",
        budget_tokens: int = 0,
        kind: str = "worker",
        parent_id: str | None = None,
        docs: list | None = None,
        **_ignored: Any,
    ) -> dict:
        """Spawn a sub-agent role with an isolated brief and token budget (§12).

        A mission that reaches this call with no delegation tree gets one bound
        on the spot.  Online execution needs no Python provider: Chudcode owns
        provider construction and the persistent child session.  An explicit
        offline mission may use only the injected offline-safe local provider.

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
        try:
            from services.agent.roles import load_all_roles  # noqa: PLC0415

            known_roles = load_all_roles()
        except Exception:  # noqa: BLE001
            known_roles = {}
        if not role_resolved or role_resolved not in known_roles:
            from difflib import get_close_matches  # noqa: PLC0415

            suggestions = get_close_matches(
                role_resolved, sorted(known_roles), n=3, cutoff=0.55
            )
            return {
                "ok": False,
                "executed": False,
                "reason": "invalid_role",
                "error": (
                    f"Unknown delegate role {role_raw!r}. Use an exact role id "
                    "from the advertised role catalog."
                ),
                "suggested_roles": suggestions,
            }
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
            # Every advertised delegate must have a real, budgeted tree.  Online
            # Chudcode execution does not require a Python provider, so bind the
            # tree unconditionally; explicit offline safety is checked later by
            # chudcode_roles.run_role_turn before the local loop can run.
            try:
                from services.agent.delegation import (  # noqa: PLC0415
                    DelegationTree as _DelegationTree,
                )

                self.delegation_tree = _DelegationTree(
                    run_id=str(getattr(self, "run_id", "") or ""),
                    ledger=self.ledger,
                    tier=self.tier,
                    on_event=self.on_event,
                    max_subagent_depth=self.execution_options.get("max_subagent_depth"),
                )
                self._delegation_tree_autobound = True
                if self.ledger is not None:
                    try:
                        self.ledger.append(
                            "agent",
                            "note",
                            {
                                "event": "delegation_tree_autobound",
                                "run_id": str(getattr(self, "run_id", "") or ""),
                                "why": (
                                    "delegate called on a mission with no tree; "
                                    "the agent service will run the approved child"
                                ),
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 - degrade to the honest refusal
                self.delegation_tree = None

        if self.delegation_tree is None:
            # Tree construction failed; saying "spawned" would be fabricated.
            if self.ledger is not None:
                try:
                    self.ledger.append(
                        "agent",
                        "note",
                        {
                            "event": "delegation_request",
                            "role": role_resolved,
                            "role_requested": role_raw,
                            "brief": brief[:500],
                            "budget_tokens": budget_tokens,
                            "status": "unavailable",
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {
                "ok": False,
                "reason": "delegation_unavailable",
                "status": "unavailable",
                "error": (
                    "delegate is not available in this run: its governed "
                    "delegation tree could not be created, so no sub-agent can "
                    "execute. Do NOT retry delegate — do the work yourself with "
                    "the concrete tools this turn already has, and record what "
                    f"is left from the {role_resolved!r} assignment with "
                    "ledger_note(kind='handoff', ...)."
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
                parent = (
                    self.delegation_tree._nodes.get(pid)  # noqa: SLF001
                    if pid is not None
                    else None
                )
            except Exception:  # noqa: BLE001
                parent = None
            depth = (int(getattr(parent, "depth", 0)) + 1) if parent is not None else 1
            preference_limit = self.execution_options.get("max_subagent_depth")
            if preference_limit is not None and depth > int(preference_limit):
                return {"ok": False, "status": "refused", "reason": "spawn_depth_policy",
                        "error": f"This turn allows at most {preference_limit} levels of delegation."}
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
                    "reason": "spawn_depth_policy",
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
                role_resolved,
                brief,
                budget_tokens,
                tree=self.delegation_tree,
                parent_id=parent_id,
                kind=kind,
                docs=docs,
                project_dir=(
                    self.execution_options.get("project_dir")
                    or self.execution_options.get("project_path")
                    or self.execution_options.get("workspace_dir")
                ),
            )
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {
                "ok": False,
                "reason": "delegation_exception",
                "status": "failed",
                "error": f"delegate failed: {exc}",
                "role_requested": role_raw,
                "role_resolved": role_resolved,
                "brief_len": len(brief_raw),
                "brief_truncated": brief_truncated,
            }

        if isinstance(node, dict) and getattr(
            self, "_delegation_tree_autobound", False
        ):
            # Observable, not silent: this mission had no tree until this call.
            node["delegation_tree_autobound"] = True

        # Approved children run synchronously.  Online Chudcode needs no bound
        # Python provider; explicit offline safety is enforced by run_role_turn.
        if not isinstance(node, dict) or node.get("status") != "approved":
            if isinstance(node, dict):
                if role_raw and role_resolved != role_raw:
                    node["role_requested"] = role_raw
                    node["role_resolved"] = role_resolved
                if brief_truncated:
                    node["brief_truncated"] = True
                    node["brief_len_original"] = len(brief_raw)
            return _actionable_delegate_failure(node)
        node["executed"] = True
        # The tree may apply its own grant policy, but it must never enlarge the
        # caller's requested credit ceiling.
        node["budget_requested"] = budget_tokens
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
        # `executed` was set OPTIMISTICALLY above, before the child ran, and nothing used to
        # take it back. Live 2026-07-29 (firmware pass 2c) a delegate returned
        # executed=true with tokens_spent=0, attempts=0, docs=[] and model={provider:"",id:""}
        # — nothing ran at all — and because `delegate` counts as real work, the parent's
        # unearned-finish gate let the turn close as `confirmed` on the fabricated summary
        # "STM32F4 heartbeat firmware simulated for 1 second". A dispatcher that claims
        # execution it cannot evidence is the worst kind of lie in this harness, because every
        # honesty gate downstream trusts it.
        node.update(_delegation_execution_verdict(node))
        return _actionable_delegate_failure(node)

    def _run_subagent(self, node: dict, brief: str) -> dict:
        """Run a scoped child and guarantee one best-effort terminal transition."""
        node_id = str(node.get("id", ""))
        outcome: dict | None = None
        try:
            try:
                self.delegation_tree.start(node_id)
            except Exception:  # noqa: BLE001 — terminal write below remains authoritative
                pass
            from services.mcp.cad_common import doc_scope  # noqa: PLC0415

            with doc_scope():
                outcome = self._run_subagent_turn(node, brief)
            return outcome
        except Exception as exc:  # noqa: BLE001 — execution error is data
            outcome = {
                "ok": False,
                "subagent_status": "failed",
                "error": f"sub-agent execution failed: {exc}",
            }
            return outcome
        finally:
            self._record_subagent_terminal(node_id, outcome)

    def _record_subagent_terminal(
        self,
        node_id: str,
        outcome: dict | None,
    ) -> None:
        """Best-effort terminal write shared by every delegate exit path."""
        result = outcome if isinstance(outcome, dict) else {}
        succeeded = result.get("ok") is True
        child_status = str(result.get("subagent_status") or "")
        try:
            tokens = int(result.get("tokens_used") or 0)
        except (TypeError, ValueError):
            tokens = 0

        if not succeeded and tokens > 0:
            try:
                self.delegation_tree.accrue(node_id, tokens)
            except Exception:  # noqa: BLE001 — terminal transition must still run
                logger.exception(
                    "Failed to accrue terminal token usage for delegation node %s",
                    node_id,
                )

        target_status = "done" if succeeded else "failed"
        try:
            if succeeded:
                self.delegation_tree.complete(node_id, tokens_used=tokens)
            else:
                reason = str(
                    result.get("stop_reason")
                    or result.get("error")
                    or child_status
                    or "sub-agent execution aborted"
                )
                self.delegation_tree.fail(node_id, reason=reason)
        except Exception as exc:  # noqa: BLE001 — observability must survive write failure
            try:
                self.delegation_tree.record_terminal_transition_error(
                    node_id,
                    target_status=target_status,
                    error=exc,
                )
            except Exception:  # noqa: BLE001 — do not mask the delegate's real outcome
                logger.exception(
                    "Failed to report terminal transition error for delegation node %s",
                    node_id,
                )

        if child_status in ("terminated", "cancelled"):
            try:
                from services.agent import subagent_registry as _sreg  # noqa: PLC0415

                _sreg.set_status(
                    node_id,
                    child_status,
                    note=f"{child_status} by user at turn boundary",
                )
            except Exception:  # noqa: BLE001
                pass

    def _run_subagent_turn(self, node: dict, brief: str) -> dict:
        """Run one persistent delegated role turn through Chudcode.

        The delegation tree remains Chudware's authority for depth, credits,
        approvals, lineage and terminal state.  Chudcode owns the online model
        loop, provider construction, retry and persistent child transcript.
        Only an explicit ``options.offline`` request reaches the deterministic
        local loop, through ``chudcode_roles.run_role_turn``'s offline-safe gate.
        """
        node_id = str(node.get("id") or "")
        role = str(node.get("role") or "subagent")
        try:
            from services.agent import subagent_registry as _sreg
            from services.agent.chudcode_roles import run_role_turn

            try:
                _sreg.append_transcript(
                    node_id,
                    "system",
                    f"Sub-agent {role!r} started. Brief: {brief}"[:800],
                )
            except Exception:  # noqa: BLE001 - registry mirroring is best-effort
                pass

            card = None
            try:
                from services.agent.roles import load_role as _load_role  # noqa: PLC0415

                card = _load_role(role)
            except Exception:  # noqa: BLE001 - run_role_turn returns invalid_role
                card = None

            # Finalize one effective tool scope before Chudcode renders schemas
            # and registers host dispatch.  None is never used here: delegated
            # children are deliberately restricted to their role + shared floor.
            allowed = list(getattr(card, "concrete_tools", None) or [])
            try:
                from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                    SUBAGENT_DEFAULT_TOOLS,
                )

                allowed = sorted(set(allowed) | set(SUBAGENT_DEFAULT_TOOLS))
            except Exception:  # noqa: BLE001
                pass
            try:
                from services.agent.config import bugreport_floor  # noqa: PLC0415

                allowed = sorted(set(allowed) | set(bugreport_floor()))
            except Exception:  # noqa: BLE001
                pass
            for tool in (
                "coordinate_claim",
                "coordinate_release",
                "coordinate_progress",
                "coordinate_board",
            ):
                if tool not in allowed:
                    allowed.append(tool)

            # Corporate seats retain their scoped channel/meeting tools and
            # identity.  The mission-scoped dispatcher remains the governance
            # authority when Chudcode calls those host tools.
            org = getattr(self, "org_chart", None)
            corporate_agent = (
                str(node.get("corporate_agent_id") or "")
                or node_id
                or str(getattr(self, "corporate_agent_id", None) or "")
            )
            if org is not None and getattr(org, "active", False):
                try:
                    seat = org.seat_for(corporate_agent) if corporate_agent else None
                    if seat is None:
                        for candidate in org.seats.values():
                            if candidate.role == role and candidate.rank != "director":
                                seat = candidate
                                corporate_agent = candidate.agent_id
                                break
                    if seat is not None:
                        for tool in org.tools_for(seat.agent_id):
                            if tool not in allowed:
                                allowed.append(tool)
                        self.corporate_agent_id = seat.agent_id
                        node["corporate_agent_id"] = seat.agent_id
                        node["corporate_rank"] = seat.rank
                        node["corporate_dept"] = seat.dept_id
                except Exception:  # noqa: BLE001 - inactive org must not block work
                    pass

            if role in (
                "feature_modeler",
                "geometry_designer",
                "sketcher",
                "assembly_fit_engineer",
            ) or any(tool in allowed for tool in ("new_part", "add_features")):
                for tool in ("new_part", "add_features"):
                    if tool not in allowed:
                        allowed.append(tool)
            try:
                from services.agent.copilot_mode_tools import (  # noqa: PLC0415
                    apply_meta_tool_floor,
                    apply_mode_tools,
                )

                mode_allowed = apply_mode_tools(
                    getattr(self, "mode", None),
                    allowed,
                    plan_only=bool(getattr(self, "plan_only", False)),
                    subagent=True,
                )
                if mode_allowed is not None:
                    allowed = list(mode_allowed)
                    try:
                        from services.agent.config import bugreport_floor  # noqa: PLC0415

                        allowed = sorted(set(allowed) | set(bugreport_floor()))
                    except Exception:  # noqa: BLE001
                        pass
                floored = apply_meta_tool_floor(
                    allowed, advisor=bool(getattr(self, "advisor", True))
                )
                if floored is not None:
                    allowed = list(floored)
            except Exception:  # noqa: BLE001
                pass
            allowed = list(dict.fromkeys(allowed))

            granted = 0
            try:
                granted = int((node.get("budget") or {}).get("tokens_granted") or 0)
            except (TypeError, ValueError):
                granted = 0
            try:
                requested_grant = int(node.get("budget_requested") or 0)
            except (TypeError, ValueError):
                requested_grant = 0
            if requested_grant > 0 and (granted <= 0 or requested_grant < granted):
                granted = requested_grant
            derived_calls = max(4, min(12, granted // 2000 if granted > 0 else 10))
            phase = (
                self.phase or str(node.get("phase") or node.get("stage") or "") or None
            )
            try:
                from services.agent import tool_budget as _tb  # noqa: PLC0415

                derived_calls = _tb.floor_for(role, allowed, phase, base=derived_calls)
            except Exception:  # noqa: BLE001
                pass
            max_calls = configured_subagent_tool_budget(derived_calls)

            options = dict(getattr(self, "execution_options", None) or {})
            options.update(dict(getattr(self, "options", None) or {}))
            settings_value = options.get("settings")
            settings = dict(settings_value) if isinstance(settings_value, dict) else {}
            depth = 0
            try:
                depth = int(node.get("depth") or 0)
            except (TypeError, ValueError):
                depth = 0
            policy = getattr(self, "_last_spawn_policy", None) or {}
            min_rank = policy.get("min_rank")
            try:
                min_rank_i = int(min_rank) if min_rank is not None else None
            except (TypeError, ValueError):
                min_rank_i = None
            risk = "low" if str(node.get("kind") or "") == "ponder" else None

            # CAD still chooses the governed provider/model/effort assignment,
            # but online code passes identifiers to Chudcode rather than
            # constructing a Python provider.
            assignment = None
            cfg: dict[str, Any] = {}
            harness_role = "task"
            try:
                from services.agent import config as agent_config  # noqa: PLC0415
                from services.agent.role_models import (  # noqa: PLC0415
                    clamp_subagent_effort,
                    cost_tier_for,
                    harness_role_for_mission_role,
                    resolve_for_node,
                    resolve_role_model,
                    resolve_role_thinking,
                    role_for_copilot_mode,
                )

                mode = getattr(self, "mode", None) or options.get("mode") or "agent"
                harness_role = (
                    role_for_copilot_mode(mode)
                    if mode in ("plan", "edit")
                    else harness_role_for_mission_role(role)
                )
                cfg = dict(agent_config.load_config() or {})
                from services.agent.preferences import resolve_turn_preferences

                child_preferences = resolve_turn_preferences(brief, cfg=cfg)
                # Existing Advanced rows remain authoritative during Phase 1.
                # Unpinned leaves inherit the worker seat rather than a task alias.
                if harness_role not in (cfg.get("ai_roles") or {}):
                    harness_role = "worker"
                context = {
                    "task_class": self.task_class,
                    "depth": depth,
                    "risk": risk,
                    "node_inputs": {
                        "task_class": self.task_class,
                        "depth": depth,
                    },
                }
                assignment = (None if harness_role == "worker"
                              else resolve_for_node(harness_role, context, cfg=cfg))
                if assignment is None or min_rank_i is not None:
                    depth_assignment = resolve_role_model(
                        harness_role,
                        cfg,
                        task_class=self.task_class,
                        node_depth=depth,
                        risk=risk,
                        min_rank=min_rank_i,
                        request=brief,
                        turn_preferences=child_preferences,
                    )
                    if depth_assignment is not None:
                        assignment = depth_assignment
                if assignment is not None:
                    rank = int(assignment.rank or (min_rank_i or 3))
                    dispatch_effort = (
                        node.get("thinking")
                        or node.get("reasoning_effort")
                        or node.get("effort")
                        or (node.get("role_model") or {}).get("reasoning_effort")
                    )
                    try:
                        if harness_role == "worker":
                            resolved_effort = child_preferences.thinking
                        else:
                            resolved_effort = resolve_role_thinking(
                                harness_role,
                                cfg,
                                request=brief,
                                task_class=self.task_class,
                                node_depth=depth,
                                risk=risk,
                                min_rank=min_rank_i,
                                dispatch_effort=(
                                    str(dispatch_effort) if dispatch_effort else None
                                ),
                                rank=rank,
                            )
                    except Exception:  # noqa: BLE001 - assignment itself remains valid
                        resolved_effort = (
                            "low" if rank <= 2 else "medium" if rank <= 3 else None
                        )
                    if resolved_effort:
                        settings["reasoning_effort"] = clamp_subagent_effort(
                            resolved_effort
                        )
                    if not assignment.is_session_default:
                        if assignment.provider:
                            settings["provider"] = assignment.provider
                        if assignment.model:
                            settings["model"] = assignment.model
                    node["role_model"] = {
                        **assignment.to_dict(),
                        "min_rank": min_rank_i,
                        "cost_tier": cost_tier_for(
                            assignment.provider, assignment.model
                        ),
                        "reasoning_effort": settings.get("reasoning_effort"),
                    }
            except Exception:  # noqa: BLE001 - assignment fallback stays in Chudcode
                assignment = None

            if assignment is not None:
                try:
                    from services.agent import agent_events as _ae  # noqa: PLC0415
                    from services.agent.role_models import cost_tier_for  # noqa: PLC0415

                    emit_min = min_rank_i if min_rank_i is not None else assignment.rank
                    event_kind, event_payload = _ae.model_assignment_event(
                        node_id or role,
                        depth=depth,
                        task_class=self.task_class,
                        min_rank=(int(emit_min) if emit_min is not None else None),
                        picked_model=assignment.model,
                        picked_provider=assignment.provider,
                        cost_tier=cost_tier_for(assignment.provider, assignment.model),
                        mode=assignment.mode,
                        rank=assignment.rank,
                        reason=assignment.reason,
                        subagent_id=node_id or None,
                        role=role,
                        span_id=node_id or None,
                        parent_id=node.get("parent"),
                        actor=role,
                    )
                    _ae.emit_event(
                        event_kind,
                        event_payload,
                        live=self.on_event,
                        journal=None,
                    )
                    turn_ledger = self._ledger()
                    if turn_ledger is not None:
                        turn_ledger.append(
                            "agent",
                            "note",
                            {
                                "event": "model_assignment",
                                **{
                                    key: event_payload.get(key)
                                    for key in (
                                        "depth",
                                        "task_class",
                                        "min_rank",
                                        "picked_model",
                                        "picked_provider",
                                        "cost_tier",
                                        "mode",
                                        "rank",
                                        "reason",
                                        "role",
                                        "subagent_id",
                                    )
                                },
                            },
                        )
                except Exception:  # noqa: BLE001 - telemetry never blocks dispatch
                    pass
            if not settings.get("reasoning_effort"):
                raw_effort = cfg.get("reasoning_effort") or cfg.get("thinking")
                if raw_effort:
                    try:
                        from services.agent.role_models import (  # noqa: PLC0415
                            clamp_subagent_effort,
                        )

                        settings["reasoning_effort"] = clamp_subagent_effort(raw_effort)
                    except Exception:  # noqa: BLE001
                        pass

            options.update(
                {
                    "mission_dispatch": self,
                    "ledger": self._ledger(),
                    "autonomy": self.autonomy,
                    "doc_id": self.doc_id,
                    "node_id": node_id,
                    "subagent_id": node_id,
                    "phase": phase,
                    "task_class": self.task_class,
                    "mode": getattr(self, "mode", None),
                    "plan_only": bool(getattr(self, "plan_only", False)),
                    "advisor": bool(getattr(self, "advisor", True)),
                    "allowed_tools": allowed,
                    "max_tool_calls": max_calls,
                    "settings": settings,
                }
            )
            if options.get("offline") is True:
                options["offline_provider"] = (
                    options.get("offline_provider") or self.provider
                )
            else:
                # A caller-supplied provider belongs only to the explicit local
                # fallback.  Never leak it into the online Chudcode service.
                options.pop("offline_provider", None)

            from services.agent.online_turn import current_tool_session_id

            # Shared governance outlives individual role turns. A nested host
            # call belongs to its live child session, not the root dispatcher's
            # original conversation (nor the shared run id).
            parent_session = str(current_tool_session_id() or options.get("session_id") or "")
            previous_node = getattr(self, "_current_node_id", None)
            self._current_node_id = node_id or previous_node
            try:
                turn = run_role_turn(
                    brief,
                    role,
                    session_id=parent_session,
                    run_id=self.run_id or None,
                    parent_id=node_id or previous_node or parent_session,
                    on_event=self.on_event,
                    options=options,
                    tier=self.tier,
                    capability=self.capability,
                    max_tokens=LINEAR_ROLE_MAX_OUTPUT_TOKENS,
                    max_total_tokens=granted or None,
                    actor=self.chudware_actor,
                    close_session=False,
                )
            finally:
                self._current_node_id = previous_node

            if not isinstance(turn, dict):
                turn = {
                    "success": False,
                    "stop_reason": "provider_error",
                    "result": {
                        "error": "Agent service delegated role returned a non-object result"
                    },
                    "tokens_used": 0,
                    "provider_usage_recorded": False,
                }
            success = turn.get("success") is True
            stop_reason = str(turn.get("stop_reason") or "")
            cancelled = bool(turn.get("cancelled")) or stop_reason in (
                "cancelled",
                "terminated_by_user",
            )
            payload = turn.get("result")
            payload_dict = payload if isinstance(payload, dict) else {}
            handoff = (
                payload_dict.get("handoff")
                if isinstance(payload_dict.get("handoff"), dict)
                else None
            )
            if handoff is None and isinstance(payload, str) and payload.strip():
                handoff = {
                    "status": "done" if success else "blocked",
                    "summary": payload.strip()[:600],
                    "outputs": {
                        "session_id": turn.get("session_id"),
                        "stop_reason": stop_reason,
                    },
                }
            if handoff is None and payload_dict:
                calls = (
                    payload_dict.get("calls")
                    if isinstance(payload_dict.get("calls"), list)
                    else []
                )
                if calls or success:
                    handoff = {
                        "status": "done" if success else "blocked",
                        "summary": (
                            f"subagent {role} finished stop_reason={stop_reason} "
                            f"tool_calls={len(calls)}"
                        )[:600],
                        "outputs": {
                            "tool_calls": len(calls),
                            "calls": [
                                {
                                    "tool": call.get("tool"),
                                    "ok": call.get("ok"),
                                }
                                for call in calls[:20]
                                if isinstance(call, dict)
                            ],
                            "stop_reason": stop_reason,
                        },
                    }

            try:
                tokens = int(turn.get("tokens_used") or 0)
            except (TypeError, ValueError):
                tokens = 0
            tokens_used_estimate = tokens <= 0
            if tokens <= 0 and success:
                tokens = 1000

            transcript_text = ""
            if isinstance(payload, str):
                transcript_text = payload
            elif handoff is not None:
                transcript_text = str(handoff.get("summary") or "")
            if transcript_text:
                try:
                    _sreg.append_transcript(node_id, "subagent", transcript_text[:4000])
                except Exception:  # noqa: BLE001
                    pass

            common = {
                "stop_reason": stop_reason,
                "result": payload,
                "handoff": handoff,
                "tokens_used": tokens,
                "tokens_used_estimate": tokens_used_estimate,
                "provider_usage_recorded": bool(turn.get("provider_usage_recorded")),
                "session_id": turn.get("session_id"),
                "chudcode_session_id": turn.get("chudcode_session_id"),
                "chudcode_session_file": turn.get("chudcode_session_file"),
                "allowed_tools": turn.get("allowed_tools") or allowed,
            }
            if node.get("role_model") is not None:
                common["role_model"] = node["role_model"]

            if cancelled:
                return {
                    "ok": False,
                    "subagent_status": "cancelled",
                    **common,
                }
            if success:
                return {
                    "ok": True,
                    "subagent_status": "done",
                    **common,
                }
            nested_error = (
                str(payload_dict.get("error") or payload_dict.get("error_detail") or "")
                if payload_dict
                else ""
            )
            child_status = (
                stop_reason if stop_reason in ("blocked", "escalated") else "failed"
            )
            return {
                "ok": False,
                "subagent_status": child_status,
                "error": nested_error or "Agent service delegated role turn failed",
                **common,
            }
        except Exception as exc:  # noqa: BLE001 - execution error is result data
            return {
                "ok": False,
                "subagent_status": "failed",
                "error": f"sub-agent execution failed: {exc}",
                "tokens_used": 0,
                "tokens_used_estimate": True,
            }

    # ------------------------------------------------------------------ parts
    def part_search(
        self,
        query: str = "",
        domain: str = "",
        limit: int = 20,
        category_id: str = "",
        facets: dict | str | None = None,
        price_max: float = 0.0,
        vendors: str = "",
        in_stock: bool = False,
        **_ignored: Any,
    ) -> dict:
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

            return search_parts_v2(
                query,
                domain=domain,
                limit=limit,
                category_id=category_id,
                facets=facets,
                price_max=price_max,
                vendors=vendors,
                in_stock=in_stock,
            )
        except Exception as exc:  # noqa: BLE001 — error is data for the model
            return {
                "ok": False,
                "error": f"part_search failed: {exc}",
                "hint": "Check that services/parts (the parts hub) is importable.",
            }

    @model_selectors("node_id")
    def parts_taxonomy(
        self,
        node_id: str = "",
        depth: int = 2,
        include_facets: bool = True,
        **_ignored: Any,
    ) -> dict:
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
                return {
                    "ok": False,
                    "error": f"part_id not found: {part_id!r}",
                    "hint": "Use part_search to discover valid part_ids.",
                }
            return {"ok": True, "part": rec}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"part_get failed: {exc}"}

    def part_source(
        self, need: str = "", constraints: str = "", limit: int = 5, **_ignored: Any
    ) -> dict:
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

    def match_model(
        self,
        part_id: str = "",
        title: str = "",
        mpn: str = "",
        brand: str = "",
        limit: int = 8,
        **_ignored: Any,
    ) -> dict:
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

    def reverse_engineer_product(
        self,
        part_id: str = "",
        url: str = "",
        title: str = "",
        mpn: str = "",
        brand: str = "",
        **_ignored: Any,
    ) -> dict:
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

    def scrape_enqueue(self, site: str = "", query: str = "", **_ignored: Any) -> dict:
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
            return {
                "ok": False,
                "error": f"scrape_enqueue failed: {exc}",
                "hint": "services/browser must be available and the site logged in.",
            }
        # Provenance: record the acquisition on the mission ledger (kind=artifact).
        if self.ledger is not None and isinstance(out, dict) and out.get("ok", True):
            try:
                self.ledger.append(
                    "agent",
                    "artifact",
                    {
                        "event": "scrape_enqueued",
                        "site": site,
                        "query": query,
                        "job_id": out.get("id", ""),
                    },
                )
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
            return {
                "ok": False,
                "error": "import_artifact needs the artifact_path of a done job",
            }
        import os  # noqa: PLC0415

        if not os.path.isfile(path):
            return {
                "ok": False,
                "error": f"file not found: {path}",
                "hint": "Take artifact_path from scrape_queue_status on a done job.",
            }
        try:
            from services.mcp import core_bridge as cb  # noqa: PLC0415 — ported to core (T-RF19)

            out = cb.call("part_import_file", {"path": path})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"import_artifact failed: {exc}",
                "hint": "Ensure the native core (CHUDWARE_LIBCHUDWARE) is available.",
            }
        if self.ledger is not None and isinstance(out, dict) and out.get("ok"):
            try:
                self.ledger.append(
                    "agent",
                    "artifact",
                    {
                        "event": "artifact_imported",
                        "path": path,
                        "doc_id": out.get("doc_id", ""),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return out

    def cam_tool_import(
        self, part_id: str = "", number: int = 0, material: str = "", **_ignored: Any
    ) -> dict:
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
        scope or wait for the holder to call coordinate_release.
        """
        board, err = self._board_or_error()
        if err is not None:
            return err
        aid = self._coord_agent_id(agent_id)
        res = (
            resources
            if isinstance(resources, list)
            else ([resources] if resources else [])
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
        res = (
            resources
            if isinstance(resources, list)
            else ([resources] if resources else None)
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
                session_id=str(self.execution_options.get("session_id") or ""),
                seat_heads=bool(seat_heads),
                delegate_fn=_delegate if self.delegation_tree is not None else None,
            )
            if result.get("active") and result.get("chart"):
                from services.agent.corporate import get_org_chart  # noqa: PLC0415

                self.org_chart = get_org_chart(
                    self.run_id,
                    create=False,
                    on_event=self.on_event,
                    session_id=str(self.execution_options.get("session_id") or ""),
                )
                if self.org_chart is not None:
                    if self.org_chart.on_event is None:
                        self.org_chart.on_event = self.on_event
                    if not self.corporate_agent_id:
                        self.corporate_agent_id = self.org_chart.director_id
            if self.ledger is not None:
                try:
                    self.ledger.append(
                        "agent",
                        "artifact",
                        {
                            "event": "corporate_org",
                            "active": bool(result.get("active")),
                            "reason": result.get("reason"),
                            "departments_found": result.get("departments_found"),
                        },
                    )
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
                    session_id=str(self.execution_options.get("session_id") or ""),
                    delegate_fn=_delegate if self.delegation_tree is not None else None,
                    seat_heads=True,
                )
            if result.get("active"):
                from services.agent.corporate import get_org_chart  # noqa: PLC0415

                self.org_chart = get_org_chart(
                    self.run_id,
                    create=False,
                    on_event=self.on_event,
                    session_id=str(self.execution_options.get("session_id") or ""),
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


# Explicit offline role turns still need enough output room for a complete
# terminal directive; the local legacy loop's default 2048-token completion cap
# can truncate one mid-envelope. Online turns use Chudcode and do not enter this
# loop.
LINEAR_ROLE_MAX_OUTPUT_TOKENS = 8192


def linear_role_allowed_tools(card: Any) -> list[str]:
    """Return the universal main-chat loadout used by live turns and previews.

    The interactive lane is not a capped DAG-node role: it gets the broad
    :data:`tool_categories.MAIN_CHAT_CORE_TOOLS` core with schemas up front, plus
    the card's own seats, the contract fs floor and the bug-report floor. Every
    other registered tool is listed by name in the prompt menu appended by
    :func:`_apply_session_preferences` and loads on demand — nothing is hidden,
    and ``run_shell`` deliberately stays off the automatic floor.
    """
    from services.agent.copilot_mode_tools import (  # noqa: PLC0415
        MAIN_CHAT_DEFAULT_TOOLS,
    )
    from services.agent.tool_categories import (  # noqa: PLC0415
        MAIN_CHAT_CORE_TOOLS,
    )

    allowed = set(MAIN_CHAT_CORE_TOOLS) | set(card.concrete_tools) | MAIN_CHAT_DEFAULT_TOOLS
    try:
        from services.agent.config import bugreport_floor  # noqa: PLC0415

        allowed |= set(bugreport_floor())
    except Exception:  # noqa: BLE001 — floor union is best-effort
        pass
    return sorted(allowed)


def effective_role_system_prompt(card: Any) -> str:
    """Apply the Settings global system-prompt override to a role card.

    Empty means the role's resolved built-in/mission override. A non-empty value
    intentionally replaces role prose, matching the Settings contract; harness
    base, capability, tools, skills, and output enforcement are still assembled
    by the runtime around it.
    """
    try:
        from services.agent.config import load_config  # noqa: PLC0415

        override = str((load_config() or {}).get("system_prompt") or "").strip()
        if override:
            return override
    except Exception:  # noqa: BLE001 — prompt config must never break a turn
        pass
    return str(getattr(card, "system_prompt", "") or "")


def _apply_session_preferences(
    role: str,
    allowed: list[str],
    system_prompt: str,
    preferences: Any | None = None,
) -> tuple[list[str], str]:
    """Compose the same derived preference policy for live turns and previews."""
    if role != "general_agent":
        return allowed, system_prompt
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.copilot_mode_tools import apply_mode_tools
    from services.agent.tool_categories import render_tool_menu

    preferences = preferences or resolve_turn_preferences("", task_kind="modelling")
    if preferences.task_list:
        allowed = sorted(set(allowed) | {"task_list"})
    allowed = apply_mode_tools(preferences.mode, allowed,
                               preferences=preferences) or []
    system_prompt = system_prompt.rstrip() + "\n\n" + preferences.prompt_guidance()
    menu = render_tool_menu(allowed)
    if menu:
        system_prompt += "\n\n" + menu
    return allowed, system_prompt


# ------------------------------------------------- offline-lane static prompt
#: Warning prefixes ``prompt_cache.compose`` emits when a step of THIS lane's
#: composition degraded to a fallback (role policy, capability assembly, session
#: preferences). Such an entry is not this lane's bytes, so its tool text is not
#: trusted and the slot is rendered live instead of copied from a degraded mirror.
_PROMPT_CACHE_COMPOSE_WARNINGS = frozenset(
    {"effective_role_policy", "prompt_tiers", "session_preferences"}
)

#: In-process memo for the offline lane's rendered TOOLS SLOT, keyed by everything
#: ``render_tools_block`` reads: the prompt-cache fingerprint (config.json, the
#: role cards, packs, skills and the composing source — so a settings write or an
#: edited docstring drops the entry), the tier's ``(budget, trim)`` allocation,
#: the allowlist, and the identity of the callable each tool resolves to.
#:
#: The disk cache normally cannot serve this text: it renders with
#: ``resolver_for(None)`` and no tier, while an offline turn resolves the
#: mission-scoped meta tools (``ask_user``, ``delegate``) against its
#: ``MissionDispatch``, whose signatures and docstrings are different bytes, and
#: allocates its own tier slot. The slot is budget-coupled (pass 2 spends the
#: tier's surplus across the whole allowlist), so splicing cached entries around
#: live ones would change the rendered text — those entries therefore stay live
#: and the whole render is reused from here instead.
_TOOLS_SLOT_MEMO: dict[tuple, str] = {}
_TOOLS_SLOT_LOCK = threading.RLock()


def _tool_render_ident(fn: Any) -> tuple[str, str] | None:
    """Identity of what a schema render would describe for one tool.

    ``tool_schemas.describe_callable`` reads only ``inspect.signature`` and the
    docstring, both carried by the underlying function — so two resolutions with
    the same module + qualname render byte-identical entries, and a bound
    ``MissionDispatch`` method compares equal across dispatcher instances (which
    is what makes the memo reusable from one turn to the next).
    """
    if fn is None:
        return None
    target = getattr(fn, "__func__", fn)
    return (
        str(getattr(target, "__module__", "")),
        str(getattr(target, "__qualname__", None) or repr(target)),
    )


def _tools_render_idents(
    allowed: list[str],
    resolver: Callable[[str], Any],
) -> tuple[tuple[str, str] | None, ...]:
    """Per-tool render identity under *resolver*, in allowlist order."""
    out: list[tuple[str, str] | None] = []
    for tool in allowed:
        try:
            fn = resolver(tool)
        except Exception:  # noqa: BLE001 — a broken entry renders name-only
            fn = None
        out.append(_tool_render_ident(fn))
    return tuple(out)


def _compose_role_prompt(
    role: str,
    *,
    card: Any | None,
    capability: Any | None,
    preferences: Any | None,
) -> tuple[str, list[str], str]:
    """``(system_prompt, allowed, cache_witness)`` — the one role composition.

    Role prose (Settings override applied) → capability assembly → session
    preferences (mode/scope tool policy, preference guidance, MORE TOOLS menu) →
    skills catalog. ``cache_witness`` is the prompt before the skills catalog,
    which is exactly what ``prompt_cache.compose`` produces for the offline lane.
    """
    if card is None:
        from services.agent.roles import load_role  # noqa: PLC0415

        card = load_role(role)
    # Main chat gets both halves of the derived fs/shell floor. Rare CAD
    # families remain behind enable_tools; previews call the same helper.
    allowed = linear_role_allowed_tools(card)
    base_system_prompt = effective_role_system_prompt(card)
    system_prompt = base_system_prompt
    try:
        from services.agent.prompt_tiers import (  # noqa: PLC0415
            assemble_system_for_capability,
            letter_from_capability,
        )

        system_prompt = assemble_system_for_capability(
            base_system_prompt, letter_from_capability(capability)
        )
    except Exception:  # noqa: BLE001
        system_prompt = base_system_prompt
    # Preferences BEFORE any tool surface is derived from ``allowed``: a dropped
    # tool must also vanish from the schemas, or the model is shown a tool every
    # call to which will be refused.
    allowed, system_prompt = _apply_session_preferences(role, allowed, system_prompt, preferences)

    # The skills catalog. Role-card prose used to say "full procedure:
    # skill://assembly-merge" while the only skill:// resolver in the online
    # model's reach is Chudcode's own, which answers "Unknown skill" for every CAD
    # pack (live, mission-18d24905b774203f). One line per pack plus the cad_skill
    # fetch instruction, capped at `skills.SKILL_CATALOG_CHAR_LIMIT` — metadata
    # only, bodies load on demand.
    cache_witness = system_prompt
    try:
        from services.agent import skills as _skills  # noqa: PLC0415

        _catalog = _skills.catalog_block()
        if _catalog:
            system_prompt = system_prompt.rstrip() + "\n\n" + _catalog
    except Exception:  # noqa: BLE001 — a broken skills dir never fails a turn
        pass
    return system_prompt, allowed, cache_witness


def role_prompt(
    role: str,
    *,
    card: Any | None = None,
    capability: Any | None = None,
    preferences: Any | None = None,
) -> tuple[str, list[str]]:
    """``(system_prompt, allowed)`` for one ONLINE (Chudcode) role turn.

    The main chat (``worker_rpc._agent_admin_turn``), delegated roles
    (``chudcode_roles._assemble_role_runtime``) and the online prompt
    preview/baseline (``prompt_cache.online_prompt``) all compose here. The
    system prompt is appended to Chudcode's own; ``allowed`` feeds
    ``chudcode_tools.build_host_tools``, which hands the tools to Chudcode as
    native host-tool definitions — so no text tools slot is rendered for this
    lane.
    """
    system_prompt, allowed, _witness = _compose_role_prompt(
        role, card=card, capability=capability, preferences=preferences
    )
    return system_prompt, allowed


def linear_role_prompt(
    role: str,
    *,
    resolver: Callable[[str], Any],
    card: Any | None = None,
    capability: Any | None = None,
    tier: str | None = None,
    preferences: Any | None = None,
) -> tuple[str, list[str], str | None]:
    """``(system_prompt, allowed, tools_text)`` for one OFFLINE linear role turn.

    The explicit-offline loop (:func:`run_linear_role_turn` → ``agent_turn.run_turn``)
    shows its tools as a text slot in the system prompt; online turns use
    :func:`role_prompt`, which composes the same ``system_prompt`` / ``allowed``
    without that slot. The tool-schema render is the expensive part (~11 ms for
    the main-chat floor: ``inspect.signature`` + docstring reads per tool), so
    the fingerprinted ``prompt_cache`` entry is reused when it provably renders
    the same bytes:

    * ``system_prompt`` / ``allowed`` are composed HERE, every turn. They cost
      ~4 ms and they read the preference accessors directly, so the cached
      entry's copies are used only as a witness: it must agree with this
      composition before its tool text is trusted. A knob whose value changed
      without a config.json write (Settings writes call
      ``prompt_cache.invalidate``; the harness tests monkeypatch the accessors)
      would otherwise be silently overridden by a fingerprint-valid entry.
    * ``tools_text`` is taken from the entry only when it provably renders the
      same bytes: same role prose and allowlist, the tier must allocate the
      same ``(budget, trim)`` slot the entry was rendered with, and every
      allowed tool must resolve to the same callable under *resolver* as under
      the entry's ``resolver_for(None)``.
    * Otherwise the slot is rendered live with *resolver* — the mission-scoped
      meta tools (``ask_user``, ``delegate``) resolve to ``MissionDispatch``
      methods whose signatures and docstrings are different bytes, and the slot
      is budget-coupled, so splicing cached entries around live ones would
      change the rendered text — and memoized against the cache fingerprint, so
      a repeat turn reuses the text without recomposing it while a fingerprint
      change (settings write, edited docstring, new role card) recomposes.

    Never raises past the composition itself: a cache fault degrades to the
    live render, and a failed schema render yields ``None``.
    """
    from services.agent import agent_turn as _at  # noqa: PLC0415
    from services.agent import prompt_cache as _pcache  # noqa: PLC0415
    from services.agent import tool_schemas as _tool_schemas  # noqa: PLC0415

    system_prompt, allowed, cache_witness = _compose_role_prompt(
        role, card=card, capability=capability, preferences=preferences
    )
    static = None
    try:
        # No project_root: nothing this lane takes from the entry (role prose,
        # allowlist, tool schemas) depends on the project folder, while keying on
        # it would fold that folder's mtimes into the fingerprint and recompose
        # the slot every time a file there changes.
        static = _pcache.get(role=role, capability=capability)
        if any(
            w.split(":", 1)[0] in _PROMPT_CACHE_COMPOSE_WARNINGS
            for w in static.warnings
        ):
            static = None
    except Exception:  # noqa: BLE001 — the cache is an optimisation, never fatal
        static = None

    slot = _tool_schemas.tier_tools_slot(tier)
    idents = _tools_render_idents(allowed, resolver)

    if (
        static is not None
        and static.tools_text
        and static.role_system == cache_witness
        and list(static.allowed) == allowed
        and slot == _tool_schemas.tier_tools_slot(None)
        and idents == _tools_render_idents(allowed, _at.resolver_for(None))
    ):
        return system_prompt, allowed, static.tools_text

    if static is not None:
        # The fingerprint the entry was just validated against — no second stat
        # sweep, and the memo shares the cache's invalidation epoch.
        fingerprint = static.fingerprint
    else:
        try:
            fingerprint = _pcache.fingerprint()
        except Exception:  # noqa: BLE001 — no fingerprint means no memo
            fingerprint = ""

    key = (fingerprint, slot, tuple(allowed), idents)
    if fingerprint:
        with _TOOLS_SLOT_LOCK:
            hit = _TOOLS_SLOT_MEMO.get(key)
        if hit is not None:
            return system_prompt, allowed, hit

    try:
        tools_text, _meta = _tool_schemas.render_tools_block(
            allowed, resolver=resolver, tier=tier
        )
    except Exception:  # noqa: BLE001 — schema render is best-effort
        return system_prompt, allowed, None

    if fingerprint:
        with _TOOLS_SLOT_LOCK:
            for stale in [k for k in _TOOLS_SLOT_MEMO if k[0] != fingerprint]:
                _TOOLS_SLOT_MEMO.pop(stale, None)
            _TOOLS_SLOT_MEMO[key] = tools_text
    return system_prompt, allowed, tools_text


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
    tier: str | None = None,
    max_tokens: int | None = None,
    max_total_tokens: int | None = None,
    tool_dispatch: Optional[Callable[[str, dict], Any]] = None,
    options: dict | None = None,
    history: list[dict] | None = None,
    attachments: list[dict] | None = None,
    chudware_actor: Any | None = None,
) -> dict[str, Any]:
    """Run one bounded explicit-offline legacy role turn.

    Online role execution goes through
    :func:`services.agent.chudcode_roles.run_role_turn`. This local loop remains
    only for deterministic/loopback providers after that helper has enforced
    offline safety. It preserves the CAD role card, governed tool scope, ledger,
    events and delegation tree for offline modeling.
    """
    from services.agent import agent_turn as _at
    from services.agent.roles import load_role

    # Loaded here (not lazily): an unknown role must fail the turn exactly as it
    # always did, and the card is the fallback when the prompt cache cannot
    # serve this lane's composition.
    card = load_role(role)

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

                tree = _DelegationTree(
                    run_id=_rid, ledger=ledger, tier=tier, on_event=on_event
                )
            except Exception:  # noqa: BLE001 — degrade to the old no-delegation turn
                tree = None
            mission_dispatch = MissionDispatch(
                run_id=_rid,
                ledger=ledger,
                provider=provider,
                on_event=on_event,
                delegation_tree=tree,
                chudware_actor=chudware_actor,
                capability=capability,
                tier=tier,
            )
        except Exception:  # noqa: BLE001 — dispatcher is best-effort
            mission_dispatch = None

    # The dispatch lazily binds the run's on-disk ledger. Use that SAME object
    # for the main agent loop so its tool calls and terminal events reach
    # ledger.jsonl too; previously only delegated sub-agent events were visible.
    if ledger is None and mission_dispatch is not None:
        try:
            ledger = mission_dispatch._ledger()
        except Exception:  # noqa: BLE001 — ledger observability never blocks a turn
            ledger = None

    # Role prose, allowlist and the tool-schema slot: served from the
    # fingerprinted prompt cache when it renders these exact bytes, composed
    # live (and memoized) when the mission-scoped meta tools make it not.
    from services.agent.preferences import resolve_turn_preferences

    system_prompt, allowed, tools_text = linear_role_prompt(
        role,
        resolver=_at.resolver_for(mission_dispatch),
        card=card,
        capability=capability,
        tier=tier or getattr(mission_dispatch, "tier", None),
        preferences=resolve_turn_preferences(request, task_kind="modelling", headless=True),
    )
    explicit_allowed = isinstance(options, dict) and "allowed_tools" in options
    if explicit_allowed:
        raw_allowed = options.get("allowed_tools")
        allowed = (
            None
            if raw_allowed is None
            else [str(tool).strip() for tool in raw_allowed if str(tool).strip()]
        )
        if allowed is None:
            tools_text = None
        else:
            try:
                from services.agent.tool_schemas import (  # noqa: PLC0415
                    render_tools_block,
                )

                tools_text, _ = render_tools_block(
                    allowed,
                    resolver=_at.resolver_for(mission_dispatch),
                    tier=tier or getattr(mission_dispatch, "tier", None),
                )
            except Exception:  # noqa: BLE001
                tools_text = None

    # The process-global org MCP closures need the same run-scoped provider and
    # ledger that agent_turn receives. Bind only for the duration of this turn;
    # outside it council_convene must remain an honest no_mission_context refusal.
    from types import SimpleNamespace as _SimpleNamespace  # noqa: PLC0415
    from services.mcp import org_tools as _org_tools  # noqa: PLC0415

    _org_tools.bind_mission_context(
        _SimpleNamespace(
            provider=provider,
            ledger=ledger,
            dissents_registry=getattr(mission_dispatch, "dissents_registry", None),
            stage=getattr(mission_dispatch, "phase", None),
            tier=tier or getattr(mission_dispatch, "tier", None),
            providers=getattr(mission_dispatch, "providers", None),
            org_state=getattr(mission_dispatch, "org_state", None),
        )
    )
    try:
        result = _at.run_turn(
            request,
            provider,
            name=role,
            actor=role,
            credit_actor=chudware_actor,
            system_prompt=system_prompt,
            allowed_tools=allowed,
            tools_text=tools_text,
            max_tokens=(
                max_tokens if max_tokens is not None else LINEAR_ROLE_MAX_OUTPUT_TOKENS
            ),
            max_total_tokens=max_total_tokens,
            max_tool_calls=(
                options.get("max_tool_calls") if isinstance(options, dict) else None
            ),
            ledger=ledger,
            mission_dispatch=mission_dispatch,
            tool_dispatch=tool_dispatch,
            on_event=on_event,
            run_id=run_id,
            node_id=(options.get("node_id") if isinstance(options, dict) else None),
            subagent_id=(
                options.get("subagent_id") if isinstance(options, dict) else None
            ),
            phase=(options.get("phase") if isinstance(options, dict) else None),
            task_class=(
                options.get("task_class") if isinstance(options, dict) else None
            ),
            capability=capability,
            tier=tier or getattr(mission_dispatch, "tier", None),
            options=options,
            history=history,
            attachments=attachments,
            # The interactive lane's allow-set is an on-ramp, not a scope: the model
            # is expected to pull packs in with enable_tools as it works. Let the
            # harness do it on first reach instead of spending two billed
            # completions per pack. Curated roles (sourcing, DAG nodes, subagents)
            # leave this off, so their allow-list stays a hard boundary.
            auto_enable_packs=(role == "general_agent" and not explicit_allowed),
        )
    finally:
        _org_tools.bind_mission_context(None)
    # TERMINAL SAVE SWEEP. Nothing closes the documents a run builds (T-RF5 deleted
    # loop._terminal_autosave_close_docs; the C++ mission path only doc_saves the doc a
    # DAG node is on), so an agent doc with no real backing file sat abandoned until the
    # 30-min agent GC grace, was retired to hostd's crash-recovery ORPHAN tier, and came
    # back as a "part potentially lost" row in the GUI's "Recover unsaved parts?" prompt
    # — eight of them in one second on the reporting user's machine (2026-08-05).
    # Saving, not closing: a close would fan out doc.closed and destroy the GUI mirror
    # tab the user browses after the run, which is the very reason agent-origin docs get
    # the long GC floor. Saved + clean means hostd just closes them, leaving nothing to
    # recover. Scoped to docs this worker built (never origin_kind=gui). With no
    # project save dir it is a benign skip only when no authored docs are pending;
    # otherwise save_touched_docs returns persistence_failure and delivery is partial.
    _swept: dict[str, Any] = {"saved": [], "errors": []}
    try:
        from services.mcp.cad_common import save_touched_docs  # noqa: PLC0415

        _swept = save_touched_docs()
        if (
            _swept.get("saved")
            or _swept.get("errors")
            or _swept.get("persistence_failure")
        ) and ledger is not None:
            try:
                ledger.append(
                    role,
                    "terminal_autosave",
                    {
                        "run_id": run_id,
                        "saved": _swept.get("saved") or [],
                        "errors": _swept.get("errors") or [],
                        "persistence_failure": _swept.get("persistence_failure"),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        _swept = {"saved": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    # Auto-Learn capture-at-stop (agent.autolearn_*). Main-chat / linear entry
    # only — never raises into the turn path.
    try:
        from services.agent.autolearn import maybe_capture_after_turn  # noqa: PLC0415

        _calls = 0
        _res = (
            result.result if isinstance(getattr(result, "result", None), dict) else {}
        )
        if isinstance(_res, dict):
            _calls = int(_res.get("tool_calls") or 0)
            if not _calls and isinstance(_res.get("calls"), list):
                _calls = len(_res["calls"])
        maybe_capture_after_turn(
            request=request,
            result=result,
            tool_calls=_calls,
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
                        for c in calls[:20]
                        if isinstance(c, dict)
                    ],
                    "stop_reason": result.stop_reason,
                    "reasoning_summary": (
                        "Synthesised from the turn result (no formal envelope "
                        "emitted); mirrors the sub-agent join rescue."
                    ),
                },
            }
    persistence_errors = list(_swept.get("errors") or [])
    persistence_failure = _swept.get("persistence_failure")
    if isinstance(persistence_failure, dict):
        failure_reason = str(
            persistence_failure.get("reason") or "document_persistence_failed"
        )
        if failure_reason not in persistence_errors:
            persistence_errors.append(failure_reason)
    if persistence_errors:
        # Saving is part of delivery, not best-effort decoration. Preserve the
        # completed work and handoff, but never tell the user it is safely done.
        res_dict = dict(res_dict)
        current_handoff = res_dict.get("handoff")
        partial_handoff = (
            dict(current_handoff) if isinstance(current_handoff, dict) else {}
        )
        partial_handoff["status"] = "partial"
        outputs = partial_handoff.get("outputs")
        outputs = dict(outputs) if isinstance(outputs, dict) else {}
        outputs["persistence"] = {
            "saved": list(_swept.get("saved") or []),
            "errors": persistence_errors,
        }
        partial_handoff["outputs"] = outputs
        if not partial_handoff.get("summary"):
            partial_handoff["summary"] = (
                "Work completed, but touched documents were not saved."
            )
        res_dict["handoff"] = partial_handoff
        res_dict["error_detail"] = (
            "Document persistence failed: " + "; ".join(persistence_errors)
        )[:300]
        result_success = False
        result_stop_reason = "persistence_failed"
    else:
        result_success = result.success
        result_stop_reason = result.stop_reason
    return {
        "success": result_success,
        "stop_reason": result_stop_reason,
        "result": res_dict or result.result,
        "role": role,
        "allowed_tools": allowed,
    }
