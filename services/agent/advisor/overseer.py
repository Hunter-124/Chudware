"""Overseer plan-audit engine (`aicad.audit/1`) — the judgment layer of Pillar 20.

Contract: ``contracts/advisor.md`` §11 (audit record, FROZEN) and §17 (replan
routing).  Pillar 20: docs/ADVISOR_ARCHITECTURE.md §2.11, docs/COPILOT_MISSION_
PIPELINE.md §2 ("the overseer's watch") + Phase 6.  Role prompt (already authored
by the lead): ``services/agent/roles/overseer.yaml``.

What this module is
-------------------
The **trigger/dispatch engine** around the authored overseer role card.  It does
NOT re-author the prompt — it:

  * subscribes to trigger points (plan ratify, stage exit, escalation, preflight
    override, change request, manual) and debounces one audit per trigger;
  * assembles **budgeted evidence** — ledger_render + active buildgraph slice +
    last-N handoffs + advisory/audit history fingerprints.  Never raw worker chat:
    handoffs (with their reasoning summaries, per the T-W7 handoff schema) and the
    ledger are the audit surface — that is what makes CoT auditing feasible;
  * calls the strongest configured provider (routing seam, §14) with the role
    card's system prompt + the evidence;
  * parses + validates the strict ``aicad.audit/1`` JSON the model returns
    (malformed ⇒ rejected, not silently accepted);
  * writes the audit as a ledger event and converts its verdicts into advisory
    items marked ``source.kind: "llm"`` so renderers flag them as review findings,
    never kernel facts;
  * tracks a repeat-misroute fingerprint counter — keyed on the deterministic
    ``targeted_reentry`` (stage/role/feature) when routing resolves one, else
    class+finding as a legacy fallback — so two matching non-``ok`` verdicts
    (any severity) trip mandatory ask_user escalation even when reworded.

The engine is deterministic around a **pluggable provider** (offline discipline:
tests inject a mock provider that returns canned audit JSON — real code paths, no
all-skip).  Every audit record logs ``tokens`` (cost control: audits are
frontier-model calls).

Routing seam (§14): overseer binds to ``strongest``. :func:`resolve_overseer_provider`
resolves that assignment from routing.yaml (T-W14) when a routing table is passed,
falling back to a hardcoded provider-preference list only when routing.yaml has no
configured sources.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from services.agent.chudcode_roles import run_role_turn


AUDIT_SCHEMA = "aicad.audit/1"
_CHARS_PER_TOKEN = 4  # repo-wide heuristic (matches ledger.py / render.py)

# The trigger points the overseer fires at (contract §11 + pipeline §2).  Never
# per-turn — the caller invokes :meth:`Overseer.audit` at exactly these points.
TRIGGERS = frozenset(
    {
        "plan_ratify",
        "stage_exit",
        "escalation",
        "preflight_override",
        "change_request",
        "manual",
    }
)

# Verdict classes (contract §11).
VERDICT_CLASSES = frozenset(
    {
        "plan_gap",
        "wrong_order",
        "spec_drift",
        "missed_requirement",
        "bad_fix",
        "cot_error",
        "ok",
    }
)

_SEVERITIES = frozenset({"info", "action", "blocker"})
_ACTION_KINDS = frozenset({"replan", "reorder", "edit", "ask_user", "none"})

# Two non-ok verdicts with the same fingerprint ⇒ mandatory ask_user (§11).
_BLOCKER_ESCALATE_AT = 2


# ---------------------------------------------------------------------------
# Role-card loader — delegates to the canonical T-W7 role loader
# (services/agent/roles.py) so the overseer reads the same card — including
# Settings → Prompts overrides — as every other role consumer.
# ---------------------------------------------------------------------------


def load_role_card(role_id: str = "overseer", roles_dir: Path | None = None) -> dict:
    """Load a role card YAML into a dict with ``system_prompt`` (via the T-W7 loader).

    Returns ``{}`` if the card is missing or unparseable — the caller degrades to
    a built-in fallback system prompt rather than crashing a mission.
    """
    try:
        from services.agent.roles import load_role  # lazy: avoid import cost/cycles

        card = load_role(role_id, roles_dir=roles_dir)
        return {"id": card.id, "goal": card.goal, "system_prompt": card.system_prompt}
    except Exception:  # noqa: BLE001 — missing/broken card must not break the run
        return {}


# Terse fallback if the role card cannot be read (keeps the auditor honest even
# without the rich authored prompt).  The authored card is the real prompt.
_FALLBACK_SYSTEM = (
    "You are the OVERSEER: a senior engineering reviewer who audits a CAD "
    "mission's plan, reasoning, and fixes against EVIDENCE ONLY (ledger, "
    "handoffs with reasoning summaries, advisories, preflight, build graph — "
    "never raw chat). Report findings with evidence refs. 'ok' is a full "
    "verdict; invented findings are worse than none. Output strict "
    "aicad.audit/1 JSON only."
)


# ---------------------------------------------------------------------------
# Routing seam (§14) — resolved via routing.yaml 'overseer: [strongest]' (T-W14).
# ---------------------------------------------------------------------------

# Fallback preference order used when routing.yaml is unavailable or has no sources
# configured.  This is a last-resort fallback ONLY — contract §14 forbids hardcoded
# role→model bindings; routing.yaml is the canonical source of truth.
_STRONGEST_PREFERENCE_FALLBACK = (
    "anthropic",
    "openai",
    "openrouter",
    "gemini",
    "vllm",
    "llamacpp",
    "mock",
)


def resolve_overseer_provider(
    available: dict[str, Any],
    *,
    routing: Any | None = None,
    availability: dict[str, bool] | None = None,
) -> Any | None:
    """Pick the 'strongest' provider for the overseer from *available*.

    *available* maps provider-name / source-id → provider instance.
    *routing* is an optional ``services.agent.routing.RoutingTable`` — when
    provided, it resolves the 'overseer' assignment from routing.yaml (contract §14:
    all role→model bindings live only in routing.yaml).  When routing is not provided
    or has no configured sources, falls back to ``_STRONGEST_PREFERENCE_FALLBACK``
    so the overseer still works in unconfigured / test environments.

    Returns ``None`` if no provider is configured (the caller skips the audit rather
    than fabricating a verdict).
    """
    if not available:
        return None

    # Try routing.yaml resolution first (§14 contract: routing.yaml is canonical).
    if routing is not None:
        try:
            resolution = routing.resolve("overseer", availability=availability)
            if resolution is not None:
                # resolution.source_id maps to an available provider key.
                src_id = resolution.source_id
                if src_id in available and available[src_id] is not None:
                    return available[src_id]
                # source_id may be a provider kind (e.g. "anthropic") in older callers.
                kind_prov = available.get(resolution.model_tier)  # tier is not a key
                if kind_prov is not None:
                    return kind_prov
        except Exception:  # noqa: BLE001 — routing failure never blocks the auditor
            pass

    # Fallback: walk known-strong provider names (last resort, no routing.yaml sources).
    for name in _STRONGEST_PREFERENCE_FALLBACK:
        if name in available and available[name] is not None:
            return available[name]
    # Single-source collapse (§14): any available provider.
    for prov in available.values():
        if prov is not None:
            return prov
    return None


# ---------------------------------------------------------------------------
# Evidence assembly (budgeted)
# ---------------------------------------------------------------------------


@dataclass
class AuditScope:
    """What one audit covers (mirrors the ``scope`` object in §11)."""

    run_id: str = ""
    stage: str = ""
    roles: list[str] = field(default_factory=list)
    buildgraph_nodes: list[str] = field(default_factory=list)


@dataclass
class EvidenceBudget:
    """Token budget per evidence section — degrades by tier (§ step 3).

    Frontier default; a caller on a smaller tier passes a shrunken budget.
    """

    ledger: int = 2500
    buildgraph: int = 800
    handoffs: int = 1500
    history: int = 400
    max_handoffs: int = 6


def _slice_buildgraph(graph: dict, scope: AuditScope, budget_chars: int) -> str:
    """Render the active build-graph slice — the plan the audit is about.

    Selects nodes named in ``scope.buildgraph_nodes`` (if any) plus their direct
    ``requires`` (the local dependency neighbourhood, which is where wrong_order
    lives), else the non-``done`` frontier.  Whole-node truncation, budget-bounded.
    """
    if not graph or not isinstance(graph, dict):
        return ""
    nodes = graph.get("nodes") or []
    if not nodes:
        return ""
    by_id = {n.get("id"): n for n in nodes if n.get("id")}

    if scope.buildgraph_nodes:
        keep_ids: set[str] = set()
        for nid in scope.buildgraph_nodes:
            keep_ids.add(nid)
            node = by_id.get(nid) or {}
            for req in node.get("requires") or []:
                keep_ids.add(req)
        selected = [n for n in nodes if n.get("id") in keep_ids]
    else:
        # Frontier: everything not yet done (that is what an audit reasons about).
        selected = [n for n in nodes if n.get("status") != "done"]
        if not selected:
            selected = list(nodes)

    lines = [f"[BUILDGRAPH mission={graph.get('mission', '')}]"]
    used = len(lines[0])
    dropped = 0
    for n in selected:
        req = ",".join(n.get("requires") or [])
        line = (
            f"  {n.get('id', '?')} [{n.get('kind', '?')}/{n.get('status', '?')}] "
            f"{n.get('label', '')}"
            + (f"  requires: {req}" if req else "")
            + (f"  why: {n.get('why')}" if n.get("why") else "")
        )
        if used + len(line) + 1 > budget_chars and len(lines) > 1:
            dropped += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if dropped:
        lines.append(f"  (+{dropped} build-graph node(s) dropped for budget)")
    lines.append("[/BUILDGRAPH]")
    return "\n".join(lines)


def _render_handoffs(handoffs: list[dict], budget_chars: int, max_n: int) -> str:
    """Render the last *max_n* role handoffs, budget-bounded, newest last.

    Handoffs carry each role's reasoning summary (T-W7 handoff schema).  If a
    handoff lacks a ``reasoning_summary`` we note the gap so a cot_error audit
    cannot be silently starved of the field it needs (task hint).
    """
    if not handoffs:
        return ""
    recent = handoffs[-max_n:]
    lines = ["[HANDOFFS]"]
    used = len(lines[0])
    dropped = 0
    missing_reasoning = 0
    for h in recent:
        role = h.get("role") or h.get("from_role") or "?"
        status = h.get("status", "?")
        summary = (h.get("summary") or "").strip()
        reasoning = (h.get("reasoning_summary") or "").strip()
        if not reasoning:
            missing_reasoning += 1
        hid = h.get("id") or h.get("handoff_id") or ""
        block = [f"  handoff {hid} role={role} status={status}"]
        if summary:
            block.append(f"    summary: {summary}")
        if reasoning:
            block.append(f"    reasoning: {reasoning}")
        else:
            block.append(
                "    reasoning: (none surfaced — cannot audit CoT for this handoff)"
            )
        text = "\n".join(block)
        if used + len(text) + 1 > budget_chars and len(lines) > 1:
            dropped += 1
            continue
        lines.append(text)
        used += len(text) + 1
    if dropped:
        lines.append(f"  (+{dropped} older handoff(s) dropped for budget)")
    lines.append("[/HANDOFFS]")
    return "\n".join(lines)


def _render_history(
    prior_audits: list[dict], prior_advisories: list[dict], budget_chars: int
) -> str:
    """Fingerprints of prior audits/advisories — cheap dedupe + repeat detection.

    We do not re-inject full prior findings (budget); a one-line fingerprint per
    prior finding lets the model see "you already flagged X" and avoid re-flagging.
    """
    lines: list[str] = []
    for aud in prior_audits[-8:]:
        for v in aud.get("verdicts", []):
            fp = _fingerprint(v)
            lines.append(
                f"  audit {aud.get('id', '?')}: {v.get('class', '?')}/{v.get('severity', '?')} [{fp}]"
            )
    for adv in prior_advisories[-8:]:
        for it in adv.get("items", []):
            lines.append(
                f"  advisory {it.get('id', '?')}: {it.get('severity', '?')} {it.get('check', '')}"
            )
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > budget_chars:
        body = body[:budget_chars] + "\n  (history truncated for budget)"
    return "[PRIOR-FINDINGS]\n" + body + "\n[/PRIOR-FINDINGS]"


def render_master_intent(
    brief: dict | None, requirements: list[dict] | None, *, max_reqs: int = 12
) -> str:
    """Render the PINNED, non-truncatable master-intent block (T-DP5 §3.1).

    The overseer "holds and re-grounds the master intent so fresh-context stages
    never drift" (doc §3.1).  Master intent = the brief + the requirements.  It rides
    in ``ledger_text`` today, which the budget truncates on a small local context —
    exactly the case where drift happens.  So we pin it as its OWN block that
    ``assemble_evidence`` never drops, giving the auditor an un-truncatable ground
    truth to compare a possibly-drifted stage output against (the ``spec_drift``
    catch).  Returns ``""`` when there is nothing to pin.
    """
    brief = brief or {}
    reqs = requirements or []
    title = (brief.get("title") or "").strip()
    summary = (brief.get("summary") or "").strip()
    crit = brief.get("success_criteria") or []
    if not (title or summary or reqs or crit):
        return ""
    lines = ["[MASTER-INTENT — pinned, the goal every stage must still serve]"]
    if title:
        lines.append(f"  goal: {title}")
    if summary:
        lines.append(f"  summary: {summary}")
    for c in crit:
        lines.append(f"  success: {c if isinstance(c, str) else c.get('text', c)}")
    for r in reqs[:max_reqs]:
        rid = r.get("req_id", "?")
        txt = (r.get("text") or "").strip()
        lines.append(f"  requirement {rid}: {txt}")
    if len(reqs) > max_reqs:
        lines.append(f"  (+{len(reqs) - max_reqs} more requirement(s) — see ledger)")
    lines.append("[/MASTER-INTENT]")
    return "\n".join(lines)


def assemble_evidence(
    trigger: str,
    scope: AuditScope,
    *,
    ledger_text: str = "",
    master_intent: str = "",
    buildgraph: dict | None = None,
    handoffs: list[dict] | None = None,
    prior_audits: list[dict] | None = None,
    prior_advisories: list[dict] | None = None,
    change_plan: dict | None = None,
    org_concerns: list[dict] | None = None,
    budget: EvidenceBudget | None = None,
) -> str:
    """Assemble the budgeted evidence bundle the audit prompt sees.

    ``ledger_text`` is the caller's ``ledger_render(ledger, budget.ledger)`` output
    (passed in so this module stays decoupled from the Ledger class).  Everything
    else is optional — an audit degrades gracefully when a section is absent.

    ``master_intent`` — the PINNED, non-truncatable brief+requirements block
    (``render_master_intent``).  It is placed FIRST and never budget-dropped, so the
    auditor always sees the goal even on a tiny local context where ``ledger_text``
    truncates (T-DP5 §3.1 re-grounding).

    ``org_concerns`` — list of unresolved-concern context dicts from
    ``org.unresolved_concern_contexts()``.  When non-empty the concerns are
    appended as an [ORG-CONCERNS] block so the auditor can flag long-running
    worker issues (T-W22, §18 concern-aging rule).
    """
    budget = budget or EvidenceBudget()
    parts: list[str] = [
        f"TRIGGER: {trigger}",
        f"SCOPE: run={scope.run_id} stage={scope.stage} "
        f"roles={scope.roles} nodes={scope.buildgraph_nodes}",
    ]
    if master_intent:
        # Pinned first, never truncated — the ground truth for drift detection.
        parts.append(master_intent)
    if ledger_text:
        parts.append(ledger_text)
    bg = _slice_buildgraph(
        buildgraph or {}, scope, budget.buildgraph * _CHARS_PER_TOKEN
    )
    if bg:
        parts.append(bg)
    ho = _render_handoffs(
        handoffs or [], budget.handoffs * _CHARS_PER_TOKEN, budget.max_handoffs
    )
    if ho:
        parts.append(ho)
    hist = _render_history(
        prior_audits or [], prior_advisories or [], budget.history * _CHARS_PER_TOKEN
    )
    if hist:
        parts.append(hist)
    if change_plan is not None:
        # Self-audit of a change plan (change_request trigger): the plan IS the
        # evidence to audit — surface it verbatim.
        parts.append(
            "[CHANGE-PLAN UNDER AUDIT]\n"
            + json.dumps(change_plan, ensure_ascii=False, indent=2)
            + "\n[/CHANGE-PLAN]"
        )
    if org_concerns:
        # Unresolved worker concerns that have aged past one manager turn (§18).
        # Surface them so the auditor can decide whether to escalate.
        lines = [
            f"  • [{c['request_id']}] from={c['from']} "
            f"raised_turn={c['raised_turn']} current_turn={c['current_turn']}: "
            f"{c['body'][:120]}"
            for c in org_concerns
        ]
        parts.append("[ORG-CONCERNS]\n" + "\n".join(lines) + "\n[/ORG-CONCERNS]")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Verdict parsing + validation (strict aicad.audit/1)
# ---------------------------------------------------------------------------


class AuditValidationError(ValueError):
    """Raised when a model reply is not a valid aicad.audit/1 record."""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerant of fences/prose)."""
    if not text or not text.strip():
        raise AuditValidationError("empty audit reply")
    s = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    else:
        # Otherwise take the outermost {...}.
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise AuditValidationError("no JSON object in audit reply")
        s = s[start : end + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise AuditValidationError(f"audit JSON parse error: {exc}") from exc
    if not isinstance(obj, dict):
        raise AuditValidationError("audit reply is not a JSON object")
    return obj


def validate_audit(obj: dict) -> dict:
    """Validate + normalize a parsed audit dict against aicad.audit/1 (§11).

    Enforces (strict — a malformed verdict is rejected, never silently accepted):
      * schema tag, trigger in TRIGGERS, verdict classes/severities/action kinds;
      * every non-``ok`` verdict cites ≥1 evidence ref (an evidence-less finding is
        itself a cot_error, per the role card rule "verify before you accuse").
    Fills defaults for ``id``/``model``/``tokens``.  Returns the normalized dict.
    """
    if obj.get("schema") != AUDIT_SCHEMA:
        raise AuditValidationError(
            f"schema must be {AUDIT_SCHEMA!r}, got {obj.get('schema')!r}"
        )
    trigger = obj.get("trigger")
    if trigger not in TRIGGERS:
        raise AuditValidationError(f"trigger {trigger!r} not in {sorted(TRIGGERS)}")
    verdicts = obj.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        raise AuditValidationError("verdicts must be a non-empty list")

    norm_verdicts = []
    for i, v in enumerate(verdicts):
        if not isinstance(v, dict):
            raise AuditValidationError(f"verdict[{i}] is not an object")
        cls = v.get("class")
        if cls not in VERDICT_CLASSES:
            raise AuditValidationError(
                f"verdict[{i}].class {cls!r} not in {sorted(VERDICT_CLASSES)}"
            )
        sev = v.get("severity")
        if sev not in _SEVERITIES:
            raise AuditValidationError(
                f"verdict[{i}].severity {sev!r} not in {sorted(_SEVERITIES)}"
            )
        action = v.get("required_action") or {}
        akind = action.get("kind", "none")
        if akind not in _ACTION_KINDS:
            raise AuditValidationError(
                f"verdict[{i}].required_action.kind {akind!r} not in {sorted(_ACTION_KINDS)}"
            )
        evidence = v.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise AuditValidationError(f"verdict[{i}].evidence is not an object")
        # Evidence-refs rule (role card §HOW TO RULE 1): a non-ok finding without
        # any evidence ref is itself a cot_error — reject it.
        if cls != "ok":
            any_ref = any(
                evidence.get(k)
                for k in ("ledger_events", "advisories", "handoffs", "preflight")
            )
            if not any_ref:
                raise AuditValidationError(
                    f"verdict[{i}] class={cls!r} has no evidence refs — an audit "
                    "finding without evidence is itself a cot_error (role card rule)"
                )
        norm_verdicts.append(
            {
                "class": cls,
                "finding": str(v.get("finding", "")),
                "evidence": {
                    "ledger_events": list(evidence.get("ledger_events") or []),
                    "advisories": list(evidence.get("advisories") or []),
                    "handoffs": list(evidence.get("handoffs") or []),
                    "preflight": list(evidence.get("preflight") or []),
                },
                "required_action": {
                    "kind": akind,
                    "detail": str(action.get("detail", "")),
                    "dispatch_role": action.get("dispatch_role"),
                },
                "severity": sev,
            }
        )

    model = obj.get("model") or {}
    return {
        "schema": AUDIT_SCHEMA,
        "id": obj.get("id") or f"aud_{uuid.uuid4().hex[:8]}",
        "trigger": trigger,
        "scope": obj.get("scope") or {},
        "verdicts": norm_verdicts,
        "model": {
            "provider": str(model.get("provider", "")),
            "id": str(model.get("id", "")),
        },
        "tokens": int(obj.get("tokens") or 0),
    }


def _fingerprint(verdict: dict) -> str:
    """Stable fingerprint for the repeat-misroute counter.

    Keyed on the DETERMINISTIC reentry decision the routing engine already
    computed (target stage/role/feature from ``targeted_reentry``, T-DP5) —
    never the model's free-text finding prose, so a repeatedly-misrouted
    correction still counts as a repeat when the LLM just rewords its finding
    (or picks a different severity). Falls back to class+finding only when no
    reentry resolved (no DP stage owns the verdict — nothing deterministic to
    key on, e.g. a legacy non-DP run).
    """
    reentry = verdict.get("targeted_reentry")
    if isinstance(reentry, dict) and reentry.get("stage"):
        raw = (
            f"reentry|{reentry.get('stage', '')}|{reentry.get('role', '')}|"
            f"{reentry.get('feature') or ''}"
        )
    else:
        raw = f"class|{verdict.get('class', '')}|{verdict.get('finding', '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def audit_to_advisory(audit: dict, seq: int = 0) -> dict:
    """Convert an audit record's verdicts into an aicad.advisory/1 block.

    Verdicts land as advisory items with ``source.kind: "llm"`` so the renderer
    marks them as review findings (§11) — kernel facts outrank the overseer.
    ``ok`` verdicts are dropped (nothing for the working role to act on).
    """
    sev_map = {"info": "info", "action": "action", "blocker": "blocker"}
    items = []
    for v in audit.get("verdicts", []):
        if v.get("class") == "ok":
            continue
        act = v.get("required_action") or {}
        sev = sev_map.get(v.get("severity"), "info")
        akind = act.get("kind", "none")
        if akind not in (None, "none"):
            fixes = [
                {
                    "action": akind,
                    "params": {"dispatch_role": act.get("dispatch_role")}
                    if act.get("dispatch_role")
                    else {},
                    "basis": "overseer audit finding",
                    "predicted_effect": act.get("detail", ""),
                    "confidence": "llm",
                }
            ]
        elif sev != "info":
            # §3 invariant: a non-info advisory item MUST carry ≥1 candidate fix.
            # A non-info verdict with required_action.kind=="none" would violate it,
            # so we emit a "review" fix — the honest action is "a human/role should
            # look at this finding" — rather than an illegal empty-fix item.
            fixes = [
                {
                    "action": "review_finding",
                    "params": {},
                    "basis": "overseer flagged this but named no specific action",
                    "predicted_effect": v.get("finding", ""),
                    "confidence": "llm",
                }
            ]
        else:
            fixes = []
        items.append(
            {
                "id": f"adv_{_fingerprint(v)}",
                "severity": sev,
                "check": v.get("class", "audit"),
                "finding": v.get("finding", ""),
                "refs": {"audit_id": audit.get("id", ""), **(v.get("evidence") or {})},
                "numbers": {},
                "candidate_fixes": fixes,
                "helper_tools": [],
                "source": {"kind": "llm", "detail": f"overseer:{audit.get('id', '')}"},
            }
        )
    return {
        "schema": "aicad.advisory/1",
        "seq": seq,
        "stage": (audit.get("scope") or {}).get("stage", ""),
        "items": items,
        "user_override": False,
    }


# ---------------------------------------------------------------------------
# Overseer engine
# ---------------------------------------------------------------------------

# A LedgerLike is anything exposing ``append(actor, kind, payload)`` and ``events``.
LedgerLike = Any


@dataclass
class AuditResult:
    """The outcome of one :meth:`Overseer.audit` call."""

    audit: dict  # validated aicad.audit/1 record (or {} if skipped/failed)
    advisory: dict  # aicad.advisory/1 block (verdicts as review findings)
    escalated: bool = False  # a repeat-blocker forced ask_user
    escalation_text: str = ""  # human-facing text when escalated
    skipped: str = ""  # non-empty reason when the audit did not run
    error: str = ""  # non-empty when the reply was rejected


class Overseer:
    """Trigger-driven auditor with per-run debounce and persisted blocker state.
    Online audits are one-shot Chudcode role turns. Direct provider injection is
    treated as an explicit offline test/local request unless ``offline=False`` is
    supplied; provider objects are never used by the online path.
    """

    def __init__(
        self,
        provider: Any | None = None,
        *,
        role_card: dict | None = None,
        ledger: LedgerLike | None = None,
        budget: EvidenceBudget | None = None,
        max_tokens: int = 4000,
        session_id: str = "",
        on_event: Callable[..., None] | None = None,
        options: dict[str, Any] | None = None,
        offline: bool | None = None,
    ) -> None:
        self.offline = provider is not None if offline is None else bool(offline)
        self.offline_provider = provider if self.offline else None
        self.role_card = (
            role_card if role_card is not None else load_role_card("overseer")
        )
        self.ledger = ledger
        self.budget = budget or EvidenceBudget()
        self.max_tokens = max_tokens
        self.session_id = session_id
        self.on_event = on_event
        self.options = dict(options or {})
        self._system = (self.role_card.get("system_prompt") or _FALLBACK_SYSTEM).strip()
        # Debounce: one audit per (trigger, scope-fingerprint) — a stage-exit that
        # fires twice for the same stage does not double-audit.
        self._seen_triggers: set[str] = set()
        # Repeat-blocker counter, keyed by verdict fingerprint.
        self._blocker_counts: dict[str, int] = {}
        # Rehydrate the blocker counter from persisted audits.  CRITICAL: the in-app
        # entry points build a FRESH Overseer per call (audit_now / the dag seam), so
        # without this, the counter would reset to 0 every trigger and escalation-at-2
        # could never fire through a real call site.  The ledger persists every audit,
        # so we replay prior blocker fingerprints on construction.
        self._rehydrate_from_ledger()

    def _rehydrate_from_ledger(self) -> None:
        """Prime ``_blocker_counts`` from prior ``audit`` events on the ledger."""
        if self.ledger is None:
            return
        for ev in getattr(self.ledger, "events", []) or []:
            if ev.get("kind") != "audit":
                continue
            for v in (ev.get("payload") or {}).get("verdicts", []):
                if v.get("class") != "ok":
                    fp = _fingerprint(v)
                    self._blocker_counts[fp] = self._blocker_counts.get(fp, 0) + 1

    def prior_findings_from_ledger(self) -> tuple[list[dict], list[dict]]:
        """Return (prior_audits, prior_advisories) recorded on the ledger.

        Used to feed ``assemble_evidence`` so the prompt-level "you already flagged
        X" dedupe works across fresh-Overseer-per-call in-app entries.
        """
        audits: list[dict] = []
        advisories: list[dict] = []
        for ev in getattr(self.ledger, "events", []) or []:
            k = ev.get("kind")
            if k == "audit":
                audits.append(ev.get("payload") or {})
            elif k == "advisory":
                advisories.append(ev.get("payload") or {})
        return audits, advisories

    # -- debounce -----------------------------------------------------------

    def _trigger_key(self, trigger: str, scope: AuditScope) -> str:
        raw = f"{trigger}|{scope.run_id}|{scope.stage}|{sorted(scope.roles)}|{sorted(scope.buildgraph_nodes)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def should_run(self, trigger: str, scope: AuditScope) -> bool:
        """True if this (trigger, scope) has not already been audited (debounce).

        ``manual`` and ``change_request`` always run — an explicit request is never
        debounced away.
        """
        if trigger in ("manual", "change_request"):
            return True
        return self._trigger_key(trigger, scope) not in self._seen_triggers

    # -- main entry ---------------------------------------------------------

    def audit(
        self,
        trigger: str,
        scope: AuditScope,
        *,
        ledger_text: str = "",
        buildgraph: dict | None = None,
        handoffs: list[dict] | None = None,
        prior_audits: list[dict] | None = None,
        prior_advisories: list[dict] | None = None,
        change_plan: dict | None = None,
        org_concerns: list[dict] | None = None,
    ) -> AuditResult:
        """Run one audit at a trigger point.  Never raises — degrades to a result.

        Returns an :class:`AuditResult`.  A missing provider or a debounced trigger
        yields ``skipped``; a malformed reply yields ``error`` (with an empty audit,
        so the mission proceeds — a broken auditor must not block work).

        ``org_concerns`` — optional list of aged unresolved concerns from
        ``org.unresolved_concern_contexts()``.  Forwarded to ``assemble_evidence``
        so the [ORG-CONCERNS] block is visible to the auditor (T-W22, §18).
        """
        if trigger not in TRIGGERS:
            return AuditResult(
                audit={},
                advisory=_empty_advisory(scope),
                skipped=f"unknown trigger {trigger!r}",
            )
        if not self.should_run(trigger, scope):
            return AuditResult(
                audit={},
                advisory=_empty_advisory(scope),
                skipped="debounced (already audited this trigger/scope)",
            )
        self._seen_triggers.add(self._trigger_key(trigger, scope))

        # Prior findings default to what the ledger already recorded, so the
        # "you already flagged X" dedupe survives fresh-Overseer-per-call in-app use.
        if (
            prior_audits is None
            and prior_advisories is None
            and self.ledger is not None
        ):
            prior_audits, prior_advisories = self.prior_findings_from_ledger()

        # Pin the master intent (brief + requirements) so a small-context audit never
        # loses the goal to budget truncation — the ground truth for drift detection
        # (§3.1 re-grounding).  Pulled from the ledger state when available.
        master_intent = ""
        if self.ledger is not None:
            state = getattr(self.ledger, "state", None) or {}
            master_intent = render_master_intent(
                state.get("brief"), state.get("requirements")
            )

        evidence = assemble_evidence(
            trigger,
            scope,
            ledger_text=ledger_text,
            master_intent=master_intent,
            buildgraph=buildgraph,
            handoffs=handoffs,
            prior_audits=prior_audits,
            prior_advisories=prior_advisories,
            change_plan=change_plan,
            org_concerns=org_concerns,
            budget=self.budget,
        )
        user_msg = _audit_user_message(trigger, evidence)
        turn_key = self._trigger_key(trigger, scope)
        turn_node_id = f"overseer:{turn_key}"
        turn_run_id = (
            "overseer-"
            + hashlib.sha256(f"{scope.run_id}|{turn_key}".encode("utf-8")).hexdigest()[
                :24
            ]
        )

        turn_options = {
            **self.options,
            "ledger": self.ledger,
            "phase": scope.stage,
            "node_id": turn_node_id,
            "subagent_id": turn_node_id,
            "offline": self.offline,
        }
        if self.offline_provider is not None:
            turn_options["offline_provider"] = self.offline_provider
        try:
            turn = run_role_turn(
                user_msg,
                "overseer",
                session_id=self.session_id or str(self.options.get("session_id") or ""),
                run_id=turn_run_id,
                parent_id=f"{scope.run_id}:{turn_node_id}",
                on_event=self.on_event,
                options=turn_options,
                max_tokens=self.max_tokens,
                actor="overseer",
                close_session=True,
            )
        except Exception as exc:  # noqa: BLE001 — auditor failure never blocks mission
            return AuditResult(
                audit={},
                advisory=_empty_advisory(scope),
                error=f"provider error: {exc}",
            )
        if not isinstance(turn, dict) or not turn.get("success"):
            reason = ""
            if isinstance(turn, dict):
                payload = turn.get("result")
                if isinstance(payload, dict):
                    reason = str(payload.get("error") or payload.get("reason") or "")
                reason = reason or str(turn.get("stop_reason") or "")
            return AuditResult(
                audit={},
                advisory=_empty_advisory(scope),
                error=f"provider error: {reason or 'agent overseer turn failed'}",
            )
        reply = _role_turn_reply(turn)
        if not reply:
            return AuditResult(
                audit={},
                advisory=_empty_advisory(scope),
                error="provider error: agent overseer returned no audit",
            )

        try:
            parsed = _extract_json(reply)
            # Trigger/scope come from the harness, not the model — stamp them so a
            # model that omits/forges them cannot mislead routing.
            parsed["trigger"] = trigger
            parsed.setdefault("scope", {})
            parsed["scope"] = {
                "run_id": scope.run_id,
                "stage": scope.stage,
                "roles": scope.roles,
                "buildgraph_nodes": scope.buildgraph_nodes,
            }
            audit = validate_audit(parsed)
        except AuditValidationError as exc:
            return AuditResult(
                audit={}, advisory=_empty_advisory(scope), error=str(exc)
            )

        # Prefer authoritative Chudcode usage when the audit omitted its count.
        if not audit["tokens"]:
            audit["tokens"] = int(turn.get("tokens_used") or 0)
        if not audit["tokens"]:
            audit["tokens"] = _estimate_tokens(self._system, user_msg, reply)

        # T-DP5 reroute: attach the deterministic ``targeted_reentry`` (owning DP
        # stage + role) to every non-ok verdict BEFORE persisting, so the ledger
        # ``audit`` event records where each finding routes (never a blind restart).
        # Computed from the evidence ledger-kinds, not the model's prose (validate
        # already stripped any model-supplied reroute — the harness owns routing).
        from .reroute import route_audit

        route_audit(audit, ledger=self.ledger)

        # Persist as a ledger event (audits are ledger events, §11).
        if self.ledger is not None:
            try:
                self.ledger.append("overseer", "audit", audit)
            except Exception:  # noqa: BLE001
                pass

        # Repeat-misroute escalation: two non-ok verdicts with the same deterministic
        # reroute fingerprint ⇒ ask_user — not just blocker-severity, since a
        # repeatedly-misrouted correction loops regardless of the severity the LLM
        # assigns it.
        escalated, esc_text = self._check_repeat_blockers(audit)

        advisory = audit_to_advisory(
            audit, seq=len(getattr(self.ledger, "events", []) or [])
        )
        return AuditResult(
            audit=audit,
            advisory=advisory,
            escalated=escalated,
            escalation_text=esc_text,
        )

    def _check_repeat_blockers(self, audit: dict) -> tuple[bool, str]:
        """Bump the fingerprint counter for each non-ok verdict; escalate at 2.

        Counts every non-``ok`` verdict (any severity) — a correction that keeps
        getting misrouted loops regardless of the severity the LLM assigns it.
        Safe to widen from blocker-only because ``_fingerprint`` now keys on the
        deterministic reroute target, not prose, so unrelated findings never
        collide into a false escalation.
        """
        escalated = False
        texts: list[str] = []
        for v in audit.get("verdicts", []):
            if v.get("class") == "ok":
                continue
            fp = _fingerprint(v)
            self._blocker_counts[fp] = self._blocker_counts.get(fp, 0) + 1
            if self._blocker_counts[fp] >= _BLOCKER_ESCALATE_AT:
                escalated = True
                texts.append(
                    f"The overseer flagged this the same way {self._blocker_counts[fp]} times "
                    f"and it is still unresolved:\n  • {v.get('finding', '')}\n"
                    f"  Suggested action: {(v.get('required_action') or {}).get('kind', '?')} — "
                    f"{(v.get('required_action') or {}).get('detail', '')}"
                )
        esc_text = ""
        if escalated:
            esc_text = (
                "This needs a human decision.\n\n"
                + "\n\n".join(texts)
                + "\n\nYour options: approve the suggested change, redirect the work, "
                "or tell the mission to proceed anyway."
            )
            if self.ledger is not None:
                try:
                    self.ledger.add_question(
                        "overseer",
                        f"q_esc_{uuid.uuid4().hex[:6]}",
                        esc_text,
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
        return escalated, esc_text


# ---------------------------------------------------------------------------
# Prompt + helpers
# ---------------------------------------------------------------------------


def _role_turn_reply(turn: dict[str, Any]) -> str:
    """Extract the terminal audit text without weakening the audit schema."""
    payload = turn.get("result")
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        if payload.get("schema") == AUDIT_SCHEMA:
            return json.dumps(payload)
        handoff = payload.get("handoff")
        if isinstance(handoff, dict):
            for key in ("summary", "text", "content"):
                value = handoff.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("text", "content", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _audit_user_message(trigger: str, evidence: str) -> str:
    return (
        f"Audit this mission at trigger point: {trigger}.\n\n"
        "EVIDENCE (audit only against what is below — cite refs for every finding):\n\n"
        f"{evidence}\n\n"
        "Return ONE strict aicad.audit/1 JSON object and nothing else. Every "
        "non-ok verdict MUST cite evidence refs (ledger_events / advisories / "
        "handoffs / preflight). If the plan holds, return a single verdict with "
        'class "ok". Do not invent findings.'
    )


def _empty_advisory(scope: AuditScope) -> dict:
    return {
        "schema": "aicad.advisory/1",
        "seq": 0,
        "stage": scope.stage,
        "items": [],
        "user_override": False,
    }


def _estimate_tokens(system: str, user_msg: str, reply: str) -> int:
    return (len(system) + len(user_msg) + len(reply)) // _CHARS_PER_TOKEN
