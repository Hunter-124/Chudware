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
  * tracks the blocker fingerprint (``class+finding``) repeat counter — two
    matching ``blocker`` audits ⇒ mandatory ask_user escalation.

The engine is deterministic around a **pluggable provider** (offline discipline:
tests inject a mock provider that returns canned audit JSON — real code paths, no
all-skip).  Every audit record logs ``tokens`` (cost control: audits are
frontier-model calls).

Routing seam (§14): overseer binds to ``strongest``.  Until T-W14 (model routing)
lands, :func:`resolve_overseer_provider` uses a hardcoded provider-preference list;
the seam is clearly marked so T-W14 can drop in ``routing.yaml`` resolution.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

AUDIT_SCHEMA = "aicad.audit/1"
_CHARS_PER_TOKEN = 4  # repo-wide heuristic (matches ledger.py / render.py)

# The trigger points the overseer fires at (contract §11 + pipeline §2).  Never
# per-turn — the caller invokes :meth:`Overseer.audit` at exactly these points.
TRIGGERS = frozenset({
    "plan_ratify", "stage_exit", "escalation",
    "preflight_override", "change_request", "manual",
})

# Verdict classes (contract §11).
VERDICT_CLASSES = frozenset({
    "plan_gap", "wrong_order", "spec_drift", "missed_requirement",
    "bad_fix", "cot_error", "ok",
})

_SEVERITIES = frozenset({"info", "action", "blocker"})
_ACTION_KINDS = frozenset({"replan", "reorder", "edit", "ask_user", "none"})

# Two blockers with the same fingerprint ⇒ mandatory ask_user (§11).
_BLOCKER_ESCALATE_AT = 2


# ---------------------------------------------------------------------------
# Role-card loader — minimal local YAML read.
#   TODO(T-W7): replace this whole helper with the T-W7 role loader
#   (services/agent/roles loader) once its branch merges.  It must return the
#   same shape (a dict with ``system_prompt``); do NOT import T-W7's branch here.
# ---------------------------------------------------------------------------

_ROLES_DIR = Path(__file__).resolve().parents[1] / "roles"


def load_role_card(role_id: str = "overseer", roles_dir: Path | None = None) -> dict:
    """Load a role card YAML into a dict.  Minimal local read (T-W7 seam).

    Returns ``{}`` if the card is missing or unparseable — the caller degrades to
    a built-in fallback system prompt rather than crashing a mission.
    """
    d = roles_dir or _ROLES_DIR
    path = d / f"{role_id}.yaml"
    try:
        import yaml  # PyYAML is already a repo dependency (used across roles/)
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
# Routing seam (§14) — TODO(T-W14): resolve via routing.yaml 'overseer: [strongest]'.
# ---------------------------------------------------------------------------

# Provider-preference order used until T-W14's routing.yaml resolver lands.  This
# is a SEAM, not a role→model binding baked into logic: the moment routing.yaml is
# available, resolve_overseer_provider should consult it and this list becomes the
# fallback only.  (Contract §14: no role→model binding hardcoded outside routing.yaml.)
_STRONGEST_PREFERENCE = ("anthropic", "openai", "openrouter", "gemini", "vllm",
                         "llamacpp", "mock")


def resolve_overseer_provider(available: dict[str, Any]) -> Any | None:
    """Pick the 'strongest' provider for the overseer from *available*.

    *available* maps provider-name → provider instance.  Until T-W14 lands, this
    walks :data:`_STRONGEST_PREFERENCE` and returns the first present.  Returns
    ``None`` if none are configured (the caller then skips the audit rather than
    fabricating a verdict).

    TODO(T-W14): replace the preference walk with routing.yaml resolution of the
    ``overseer`` assignment (``[strongest]``) against configured sources.
    """
    if not available:
        return None
    for name in _STRONGEST_PREFERENCE:
        if name in available and available[name] is not None:
            return available[name]
    # No known-strong provider configured but something exists — collapse onto it
    # (§14: "if only one source exists, all roles collapse onto it").
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

    lines = [f"[BUILDGRAPH mission={graph.get('mission','')}]"]
    used = len(lines[0])
    dropped = 0
    for n in selected:
        req = ",".join(n.get("requires") or [])
        line = (f"  {n.get('id','?')} [{n.get('kind','?')}/{n.get('status','?')}] "
                f"{n.get('label','')}" + (f"  requires: {req}" if req else "")
                + (f"  why: {n.get('why')}" if n.get("why") else ""))
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
            block.append("    reasoning: (none surfaced — cannot audit CoT for this handoff)")
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


def _render_history(prior_audits: list[dict], prior_advisories: list[dict],
                    budget_chars: int) -> str:
    """Fingerprints of prior audits/advisories — cheap dedupe + repeat detection.

    We do not re-inject full prior findings (budget); a one-line fingerprint per
    prior finding lets the model see "you already flagged X" and avoid re-flagging.
    """
    lines: list[str] = []
    for aud in prior_audits[-8:]:
        for v in aud.get("verdicts", []):
            fp = _fingerprint(v)
            lines.append(f"  audit {aud.get('id','?')}: {v.get('class','?')}/{v.get('severity','?')} [{fp}]")
    for adv in prior_advisories[-8:]:
        for it in adv.get("items", []):
            lines.append(f"  advisory {it.get('id','?')}: {it.get('severity','?')} {it.get('check','')}")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > budget_chars:
        body = body[:budget_chars] + "\n  (history truncated for budget)"
    return "[PRIOR-FINDINGS]\n" + body + "\n[/PRIOR-FINDINGS]"


def assemble_evidence(
    trigger: str,
    scope: AuditScope,
    *,
    ledger_text: str = "",
    buildgraph: dict | None = None,
    handoffs: list[dict] | None = None,
    prior_audits: list[dict] | None = None,
    prior_advisories: list[dict] | None = None,
    change_plan: dict | None = None,
    budget: EvidenceBudget | None = None,
) -> str:
    """Assemble the budgeted evidence bundle the audit prompt sees.

    ``ledger_text`` is the caller's ``ledger_render(ledger, budget.ledger)`` output
    (passed in so this module stays decoupled from the Ledger class).  Everything
    else is optional — an audit degrades gracefully when a section is absent.
    """
    budget = budget or EvidenceBudget()
    parts: list[str] = [f"TRIGGER: {trigger}",
                        f"SCOPE: run={scope.run_id} stage={scope.stage} "
                        f"roles={scope.roles} nodes={scope.buildgraph_nodes}"]
    if ledger_text:
        parts.append(ledger_text)
    bg = _slice_buildgraph(buildgraph or {}, scope, budget.buildgraph * _CHARS_PER_TOKEN)
    if bg:
        parts.append(bg)
    ho = _render_handoffs(handoffs or [], budget.handoffs * _CHARS_PER_TOKEN, budget.max_handoffs)
    if ho:
        parts.append(ho)
    hist = _render_history(prior_audits or [], prior_advisories or [],
                           budget.history * _CHARS_PER_TOKEN)
    if hist:
        parts.append(hist)
    if change_plan is not None:
        # Self-audit of a change plan (change_request trigger): the plan IS the
        # evidence to audit — surface it verbatim.
        parts.append("[CHANGE-PLAN UNDER AUDIT]\n"
                     + json.dumps(change_plan, ensure_ascii=False, indent=2)
                     + "\n[/CHANGE-PLAN]")
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
        s = s[start:end + 1]
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
        raise AuditValidationError(f"schema must be {AUDIT_SCHEMA!r}, got {obj.get('schema')!r}")
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
            raise AuditValidationError(f"verdict[{i}].class {cls!r} not in {sorted(VERDICT_CLASSES)}")
        sev = v.get("severity")
        if sev not in _SEVERITIES:
            raise AuditValidationError(f"verdict[{i}].severity {sev!r} not in {sorted(_SEVERITIES)}")
        action = v.get("required_action") or {}
        akind = action.get("kind", "none")
        if akind not in _ACTION_KINDS:
            raise AuditValidationError(f"verdict[{i}].required_action.kind {akind!r} not in {sorted(_ACTION_KINDS)}")
        evidence = v.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise AuditValidationError(f"verdict[{i}].evidence is not an object")
        # Evidence-refs rule (role card §HOW TO RULE 1): a non-ok finding without
        # any evidence ref is itself a cot_error — reject it.
        if cls != "ok":
            any_ref = any(evidence.get(k) for k in ("ledger_events", "advisories", "handoffs", "preflight"))
            if not any_ref:
                raise AuditValidationError(
                    f"verdict[{i}] class={cls!r} has no evidence refs — an audit "
                    "finding without evidence is itself a cot_error (role card rule)"
                )
        norm_verdicts.append({
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
        })

    model = obj.get("model") or {}
    return {
        "schema": AUDIT_SCHEMA,
        "id": obj.get("id") or f"aud_{uuid.uuid4().hex[:8]}",
        "trigger": trigger,
        "scope": obj.get("scope") or {},
        "verdicts": norm_verdicts,
        "model": {"provider": str(model.get("provider", "")), "id": str(model.get("id", ""))},
        "tokens": int(obj.get("tokens") or 0),
    }


def _fingerprint(verdict: dict) -> str:
    """Stable ``class+finding`` fingerprint used for the repeat-blocker counter."""
    raw = f"{verdict.get('class','')}|{verdict.get('finding','').strip().lower()}"
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
        items.append({
            "id": f"adv_{_fingerprint(v)}",
            "severity": sev_map.get(v.get("severity"), "info"),
            "check": v.get("class", "audit"),
            "finding": v.get("finding", ""),
            "refs": {"audit_id": audit.get("id", ""), **(v.get("evidence") or {})},
            "numbers": {},
            "candidate_fixes": [{
                "action": act.get("kind", "none"),
                "params": {"dispatch_role": act.get("dispatch_role")} if act.get("dispatch_role") else {},
                "basis": "overseer audit finding",
                "predicted_effect": act.get("detail", ""),
                "confidence": "llm",
            }] if act.get("kind") not in (None, "none") else [],
            "helper_tools": [],
            "source": {"kind": "llm", "detail": f"overseer:{audit.get('id','')}"},
        })
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
    audit: dict                       # validated aicad.audit/1 record (or {} if skipped/failed)
    advisory: dict                    # aicad.advisory/1 block (verdicts as review findings)
    escalated: bool = False           # a repeat-blocker forced ask_user
    escalation_text: str = ""         # human-facing text when escalated
    skipped: str = ""                 # non-empty reason when the audit did not run
    error: str = ""                   # non-empty when the reply was rejected


class Overseer:
    """Trigger-driven auditor.  Holds a provider, the role card, and per-run state.

    The provider is a ``services.agent.providers.base.Provider`` (mockable) — the
    engine is deterministic around it (offline discipline).  ``ledger`` is optional;
    when present, every audit is appended as a ledger event.
    """

    def __init__(
        self,
        provider: Any | None = None,
        *,
        role_card: dict | None = None,
        ledger: LedgerLike | None = None,
        budget: EvidenceBudget | None = None,
        max_tokens: int = 4000,
    ) -> None:
        self.provider = provider
        self.role_card = role_card if role_card is not None else load_role_card("overseer")
        self.ledger = ledger
        self.budget = budget or EvidenceBudget()
        self.max_tokens = max_tokens
        self._system = (self.role_card.get("system_prompt") or _FALLBACK_SYSTEM).strip()
        # Debounce: one audit per (trigger, scope-fingerprint) — a stage-exit that
        # fires twice for the same stage does not double-audit.
        self._seen_triggers: set[str] = set()
        # Repeat-blocker counter, keyed by verdict fingerprint.
        self._blocker_counts: dict[str, int] = {}

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
    ) -> AuditResult:
        """Run one audit at a trigger point.  Never raises — degrades to a result.

        Returns an :class:`AuditResult`.  A missing provider or a debounced trigger
        yields ``skipped``; a malformed reply yields ``error`` (with an empty audit,
        so the mission proceeds — a broken auditor must not block work).
        """
        if trigger not in TRIGGERS:
            return AuditResult(audit={}, advisory=_empty_advisory(scope),
                               skipped=f"unknown trigger {trigger!r}")
        if not self.should_run(trigger, scope):
            return AuditResult(audit={}, advisory=_empty_advisory(scope),
                               skipped="debounced (already audited this trigger/scope)")
        self._seen_triggers.add(self._trigger_key(trigger, scope))

        if self.provider is None:
            return AuditResult(audit={}, advisory=_empty_advisory(scope),
                               skipped="no provider configured for overseer (routing seam)")

        evidence = assemble_evidence(
            trigger, scope,
            ledger_text=ledger_text, buildgraph=buildgraph, handoffs=handoffs,
            prior_audits=prior_audits, prior_advisories=prior_advisories,
            change_plan=change_plan, budget=self.budget,
        )
        user_msg = _audit_user_message(trigger, evidence)

        try:
            reply = self.provider.complete(self._system, [{"role": "user", "content": user_msg}],
                                           max_tokens=self.max_tokens)
        except Exception as exc:  # noqa: BLE001 — provider failure never blocks the mission
            return AuditResult(audit={}, advisory=_empty_advisory(scope),
                               error=f"provider error: {exc}")

        try:
            parsed = _extract_json(reply)
            # Trigger/scope come from the harness, not the model — stamp them so a
            # model that omits/forges them cannot mislead routing.
            parsed["trigger"] = trigger
            parsed.setdefault("scope", {})
            parsed["scope"] = {
                "run_id": scope.run_id, "stage": scope.stage,
                "roles": scope.roles, "buildgraph_nodes": scope.buildgraph_nodes,
            }
            audit = validate_audit(parsed)
        except AuditValidationError as exc:
            return AuditResult(audit={}, advisory=_empty_advisory(scope),
                               error=str(exc))

        # Token accounting (cost control §step 3): trust the model's count if it
        # gave one, else estimate from evidence+reply length.
        if not audit["tokens"]:
            audit["tokens"] = _estimate_tokens(self._system, user_msg, reply)

        # Persist as a ledger event (audits are ledger events, §11).
        if self.ledger is not None:
            try:
                self.ledger.append("overseer", "audit", audit)
            except Exception:  # noqa: BLE001
                pass

        # Repeat-blocker escalation: two blockers with the same fingerprint ⇒ ask_user.
        escalated, esc_text = self._check_repeat_blockers(audit)

        advisory = audit_to_advisory(audit, seq=len(getattr(self.ledger, "events", []) or []))
        return AuditResult(audit=audit, advisory=advisory,
                           escalated=escalated, escalation_text=esc_text)

    def _check_repeat_blockers(self, audit: dict) -> tuple[bool, str]:
        """Bump the fingerprint counter for each blocker; escalate at exactly 2."""
        escalated = False
        texts: list[str] = []
        for v in audit.get("verdicts", []):
            if v.get("severity") != "blocker":
                continue
            fp = _fingerprint(v)
            self._blocker_counts[fp] = self._blocker_counts.get(fp, 0) + 1
            if self._blocker_counts[fp] >= _BLOCKER_ESCALATE_AT:
                escalated = True
                texts.append(
                    f"The overseer flagged this the same way {self._blocker_counts[fp]} times "
                    f"and it is still unresolved:\n  • {v.get('finding','')}\n"
                    f"  Suggested action: {(v.get('required_action') or {}).get('kind','?')} — "
                    f"{(v.get('required_action') or {}).get('detail','')}"
                )
        esc_text = ""
        if escalated:
            esc_text = ("This needs a human decision.\n\n" + "\n\n".join(texts) +
                        "\n\nYour options: approve the suggested change, redirect the work, "
                        "or tell the mission to proceed anyway.")
            if self.ledger is not None:
                try:
                    self.ledger.add_question("overseer",
                                             f"q_esc_{uuid.uuid4().hex[:6]}",
                                             esc_text, blocking=True)
                except Exception:  # noqa: BLE001
                    pass
        return escalated, esc_text


# ---------------------------------------------------------------------------
# Prompt + helpers
# ---------------------------------------------------------------------------

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
    return {"schema": "aicad.advisory/1", "seq": 0, "stage": scope.stage,
            "items": [], "user_override": False}


def _estimate_tokens(system: str, user_msg: str, reply: str) -> int:
    return (len(system) + len(user_msg) + len(reply)) // _CHARS_PER_TOKEN
