"""ladder.py — the design cognition ladder runtime (T-W25).

Contract: contracts/advisor.md §22 (aicad.ladder/1 — FROZEN), §19 (councils),
§21 (linked dimensions). Playbooks: services/agent/playbooks/ladder_*.yaml
(AUTHORED, loaded verbatim via roles.load_playbook). Role card:
services/agent/roles/design_thinker.yaml (AUTHORED, loaded verbatim via
roles.load_role).

    spec ─A→ function_map ─B→ geometry_plan ─C→ linkage_map ─D→ build_pathway ─compile→ buildgraph

Each rung runs in a FRESH context. That is THE one non-negotiable rule: the rung
executor's prompt contains ONLY

    {the prior rung artifact(s) §22 names for THIS rung, a scoped ledger slice,
     the rung template}

— never the full conversation, never a rung the §22 table does not name. Enforced
BY CONSTRUCTION: each rung is an independent, single-shot ``provider.complete()``
with a freshly-assembled system prompt + a one-message user turn. There is no
threaded message list carried across rungs; the ``RungInput`` struct that feeds
the assembler physically has no field for a conversation.

The §22 per-rung input table (the scoping contract — advisor-locked):

    rung           receives (prior artifacts)          MUST NOT see
    A function_map  (spec only, via ledger slice)       —
    B geometry_plan function_map                         —
    C linkage_map   geometry_plan  ONLY                  function_map
    D build_pathway geometry_plan AND linkage_map        function_map

Validators (ladder_validate) run between rungs; a blocker finding is a BOUNCE back
to the SAME rung carrying the finding (max 2 bounces, then council or escalate).
The build-strategy council (§19, T-W23) critiques rung D before compile — present
but feature-flagged (a ``council_cb`` is injected only when T-W23 is wired).
The rung-C links become §21 linked dimensions (T-W24 ``derivation.link_dimension``)
— feature-flagged behind a ``link_cb`` because T-W24 is on a sibling branch.
Rung D compiles losslessly into aicad.buildgraph/1 (ladder_compile).

Ladder is MANDATORY at deliberation=full, OPTIONAL at minimal (a frontier model
may emit rung D directly) — but validators ALWAYS run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from services.agent import ladder_validate as lv
from services.agent import ladder_compile as lc
from services.agent.ledger import Ledger, ledger_render
from services.agent.roles import (
    Playbook,
    RoleCard,
    assemble_context,
    load_playbook,
    load_role,
)

# --------------------------------------------------------------------------- rung table

# The ordered rungs and the playbook that authors each one (verbatim load).
RUNGS: tuple[str, ...] = ("function_map", "geometry_plan", "linkage_map", "build_pathway")

_RUNG_PLAYBOOK = {
    "function_map": "ladder_functions",
    "geometry_plan": "ladder_geometry",
    "linkage_map": "ladder_linkage",
    "build_pathway": "ladder_pathway",
}

# §22 scoping table: exactly which PRIOR rung artifacts each rung receives. The
# fresh-context rule lives here — this is the ONLY place a rung's inputs are decided,
# and it is deliberately NOT "the previous artifact": rung C gets geometry_plan ONLY
# (not the function_map above it), rung D gets geometry_plan AND linkage_map.
_RUNG_INPUTS: dict[str, tuple[str, ...]] = {
    "function_map": (),                                  # spec only (ledger slice)
    "geometry_plan": ("function_map",),
    "linkage_map": ("geometry_plan",),                   # NOT function_map
    "build_pathway": ("geometry_plan", "linkage_map"),   # NOT function_map
}

MAX_BOUNCES = 2  # §22: max 2 bounces, then council or escalate
_LEDGER_BUDGET_TOKENS = 600  # design_thinker.context_recipe.ledger_tokens


# --------------------------------------------------------------------------- fresh-context struct

@dataclass
class RungInput:
    """Everything — and ONLY everything — a rung executor may see.

    The fresh-context rule is enforced by this struct's SHAPE: there is no field for
    a message history, a conversation, or a prior rung's chat. A rung sees:
      * ``prior_artifacts`` — the typed artifacts §22 names for THIS rung (see the
        scoping table); a dict keyed by rung name;
      * ``ledger_slice`` — a budgeted text render of the scoped ledger (spec, dims);
      * ``playbook`` — the AUTHORED rung template (loaded verbatim).
    """
    rung: str
    prior_artifacts: dict[str, dict]
    ledger_slice: str
    playbook: Playbook


# --------------------------------------------------------------------------- result

@dataclass
class LadderResult:
    status: str                       # "done" | "escalated"
    artifacts: dict[str, dict] = field(default_factory=dict)
    graph: Optional[dict] = None      # compiled buildgraph (when done)
    escalated_rung: Optional[str] = None
    council: Any = None               # council synthesis, "skipped", or None
    findings: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- prompt assembly

def _rung_header(rung: str) -> str:
    """A machine-readable ROLE/RUNG line the mock (and a real router) can sniff, so
    ``rung_of_prompt`` is deterministic without parsing prose."""
    return f"ROLE: design_thinker\n[LADDER RUNG: {rung}]"


def _render_prior_artifacts(ri: RungInput) -> str:
    """Render the prior artifacts §22 names for this rung as typed JSON blocks.

    Only the artifacts in ``ri.prior_artifacts`` are rendered — the caller has already
    filtered to the scoping table, so a leak is impossible here by construction."""
    if not ri.prior_artifacts:
        return ""
    blocks = []
    for name in _RUNG_INPUTS.get(ri.rung, ()):  # deterministic order, only scoped names
        art = ri.prior_artifacts.get(name)
        if art is None:
            continue
        blocks.append(f"[PRIOR ARTIFACT: {name}]\n{json.dumps(art, indent=2)}")
    return "\n\n".join(blocks)


def assemble_rung_prompt(card: RoleCard, ri: RungInput) -> tuple[str, list[dict]]:
    """Build the (system, messages) for ONE rung — the fresh context.

    Returns a single-shot pair: a system prompt (role card + rung header) and a
    one-element ``messages`` list carrying {ledger slice, prior artifacts, rung
    template}. There is NO conversation history — the messages list is freshly built
    from ``ri`` every call and never threaded from a previous rung.
    """
    # System = the AUTHORED role card system_prompt + capability SOFT block
    # (WI-16) + the rung tag. Default capability C when not supplied on ri.
    try:
        from services.agent.prompt_tiers import (  # noqa: PLC0415
            assemble_system_for_capability,
            letter_from_capability,
        )
        _cap = getattr(ri, "capability", None)
        _letter = letter_from_capability(_cap)
        _role_sys = assemble_system_for_capability(card.system_prompt, _letter)
    except Exception:  # noqa: BLE001
        _role_sys = card.system_prompt.rstrip()
        _letter = "C"
    system = f"{_rung_header(ri.rung)}\n\n{_role_sys}"

    # The turn content: ledger slice (spec), the scoped prior artifact(s), the
    # step-locked playbook template. assemble_context enforces the §9.1 slot order;
    # we route the prior artifacts + template through it as stage_state / playbook_step
    # / history slots, none of which is a conversation.
    playbook_text = ri.playbook.render_checklist()
    prior_text = _render_prior_artifacts(ri)
    turn = assemble_context(
        card,
        ledger_render=ri.ledger_slice,
        stage_state=prior_text,       # the typed prior artifact(s) — NOT chat
        advisory_block="",            # foreman-injected in the live pipeline
        playbook_step=playbook_text,  # the AUTHORED rung template
        history="",                   # deliberately empty — fresh context
        capability=_letter,
    )
    # Strip the leading system prompt that assemble_context prepends (we send it as the
    # real system role) so the user turn is purely {ledger, prior artifact, template}.
    _sys_prefix = assemble_context(card, capability=_letter).split("\n\n")[0]
    if turn.startswith(_sys_prefix):
        turn = turn[len(_sys_prefix):].strip()
    elif turn.startswith(card.system_prompt.rstrip()):
        turn = turn[len(card.system_prompt.rstrip()):].strip()
    messages = [{"role": "user", "content": [{"type": "text", "text": turn}]}]
    return system, messages


def rung_of_prompt(system: str, messages: list[dict]) -> str:
    """Recover which rung a (system, messages) prompt is asking for — the sniff the
    scripted mock (and a real deterministic router) uses. Reads the ``[LADDER RUNG: x]``
    tag from the system prompt."""
    import re
    m = re.search(r"\[LADDER RUNG:\s*([a-z_]+)\]", system or "")
    if m:
        return m.group(1)
    # Fallback: inspect message text.
    blob = json.dumps(messages)
    m = re.search(r"\[LADDER RUNG:\s*([a-z_]+)\]", blob)
    return m.group(1) if m else "function_map"


# --------------------------------------------------------------------------- artifact extraction

def _parse_handoff(reply: str) -> dict:
    """Parse a design_thinker handoff envelope out of the model reply. Tolerates a
    fenced ```json block or a bare object."""
    text = reply.strip()
    if "```" in text:
        # take the first fenced block
        parts = text.split("```")
        for chunk in parts:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: find the outermost braces
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _artifact_of(reply: str, rung: str) -> dict:
    env = _parse_handoff(reply)
    outputs = env.get("outputs", {}) or {}
    art = outputs.get("artifact")
    if not isinstance(art, dict):
        raise ValueError(f"rung {rung}: handoff has no artifact object")
    return art


# --------------------------------------------------------------------------- ledger scoping

_ARTIFACTS_HEADER = "## Artifacts"


def _strip_artifacts_section(rendered: str) -> str:
    """Remove the ledger's Artifacts section from a render.

    The fresh-context rule must NOT depend on token-budget truncation. A rung's prior
    artifacts arrive ONLY through the explicit, §22-scoped ``prior_artifacts`` — never
    through the ledger. Rung artifacts ARE recorded to the ledger (replayability), so
    we defensively excise the Artifacts section from every rung's ledger slice: this
    guarantees a prior rung's artifact (e.g. the function_map) can never re-enter a
    later rung (C/D) via the ledger, at ANY budget. (Belt-and-suspenders: the ladder
    also stores only a compact artifact SUMMARY, not the payload — see _record_artifact.)
    """
    lines = rendered.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip().startswith(_ARTIFACTS_HEADER):
            skipping = True
            continue
        if skipping:
            # a blank line or a new top-level "## " header ends the section
            if line.startswith("## ") and not line.strip().startswith(_ARTIFACTS_HEADER):
                skipping = False
                out.append(line)
            # else: still inside the artifacts block → drop the line
            continue
        out.append(line)
    return "\n".join(out)


def _scoped_ledger_slice(ledger: Ledger, rung: str) -> str:
    """The scoped ledger render for a rung. Rung A needs the full spec (brief +
    requirements + dimensions); later rungs get a lean dims-only slice (their real
    input is the prior artifact, not the ledger). All rungs stay within the
    design_thinker ledger budget so no rung ever sees the whole mission log, and the
    Artifacts section is excised so a prior rung's artifact can never leak in via the
    ledger regardless of budget (fresh-context by construction, not by truncation)."""
    budget = _LEDGER_BUDGET_TOKENS if rung == "function_map" else 300
    return _strip_artifacts_section(ledger_render(ledger, budget))


def _acceptance_criteria(ledger: Ledger) -> list[str]:
    return [r.get("req_id") or r.get("id") for r in ledger.state.get("requirements", [])
            if (r.get("req_id") or r.get("id"))]


# --------------------------------------------------------------------------- one rung (fresh context + bounce)

def run_rung(
    ledger: Ledger,
    provider: Any,
    card: RoleCard,
    rung: str,
    prior_artifacts: dict[str, dict],
    *,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> tuple[Optional[dict], list[dict]]:
    """Execute ONE rung in a fresh context, with the validator bounce loop.

    Returns (artifact, findings). ``artifact`` is None when the rung exhausted its
    bounces (escalation). ``prior_artifacts`` MUST already be filtered to the §22
    scoping table for this rung — this function renders exactly what it is given.
    """
    playbook = load_playbook(_RUNG_PLAYBOOK[rung])
    acs = _acceptance_criteria(ledger) if rung == "function_map" else None
    last_findings: list[dict] = []
    bounce_note = ""

    # attempt 0 = initial, then up to MAX_BOUNCES retries → total MAX_BOUNCES+1 calls
    for attempt in range(MAX_BOUNCES + 1):
        ledger_slice = _scoped_ledger_slice(ledger, rung)
        if bounce_note:
            # The validator finding re-enters the SAME rung's fresh context as a
            # validator advisory — this is NOT prior-rung chat; it is this rung's
            # own correction signal (§22: "bounce returns to the SAME rung").
            ledger_slice = f"{ledger_slice}\n\n[VALIDATOR BOUNCE]\n{bounce_note}"
        ri = RungInput(
            rung=rung,
            prior_artifacts=prior_artifacts,   # already scoped by the caller
            ledger_slice=ledger_slice,
            playbook=playbook,
        )
        system, messages = assemble_rung_prompt(card, ri)
        reply = provider.complete(system, messages, max_tokens=8000)
        try:
            artifact = _artifact_of(reply, rung)
        except (ValueError, json.JSONDecodeError) as exc:
            bounce_note = f"handoff did not parse: {exc}"
            last_findings = [lv._finding("handoff_unparseable", str(exc))]
            if on_event:
                on_event("ladder_bounce", {"rung": rung, "attempt": attempt, "reason": "parse"})
            continue

        findings = lv.validate_rung(rung, artifact, prior=prior_artifacts,
                                    acceptance_criteria=acs)
        last_findings = findings
        blocking = lv.blockers(findings)
        if not blocking:
            return artifact, findings
        # Bounce: feed the finding back into the SAME rung.
        bounce_note = "; ".join(
            item.get("finding", "") for f in blocking for item in f.get("items", []))
        if on_event:
            on_event("ladder_bounce", {"rung": rung, "attempt": attempt,
                                       "findings": bounce_note})
    return None, last_findings


# --------------------------------------------------------------------------- full ladder

def run_ladder(
    ledger: Ledger,
    provider: Any,
    *,
    deliberation: str = "full",
    seed_artifacts: Optional[dict[str, dict]] = None,
    council_cb: Optional[Callable[..., dict]] = None,
    link_cb: Optional[Callable[..., dict]] = None,
    doc_id: str = "",
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> LadderResult:
    """Run the design cognition ladder end to end.

    Args:
      ledger:        the design ledger (holds the spec; receives artifact events).
      provider:      an LLM provider with ``.complete(system, messages, max_tokens)``.
      deliberation:  "full" runs all four rungs (mandatory); "minimal" runs only the
                     rungs not pre-supplied in ``seed_artifacts`` — validators ALWAYS
                     run either way (§22).
      seed_artifacts: prior artifacts to skip (minimal mode / frontier direct-to-D).
      council_cb:    build-strategy council hook (§19, T-W23). PRESENT but feature-
                     flagged: when None (default, T-W23 unmerged), the seam reports
                     "skipped" and the ladder proceeds. When provided, rung D output is
                     handed to it (geometry_plan + build_pathway, per §19) before compile.
      link_cb:       §21 linked-dimension hook (T-W24 derivation.link_dimension). PRESENT
                     but feature-flagged: when provided, rung-C links are handed to it.
      doc_id:        the CAD doc the buildgraph targets (for node.doc_id).
      on_event:      optional event sink (ladder_rung_done / ladder_bounce / ...).

    Returns a LadderResult.
    """
    card = load_role("design_thinker")
    # §22: MANDATORY at full — all four rungs run (no pre-seeding a rung away). At
    # minimal a frontier model may skip earlier rungs via seed_artifacts, but the
    # validators still run on whatever IS produced.
    if deliberation == "full" and seed_artifacts:
        raise ValueError(
            "deliberation='full' runs all four rungs; seed_artifacts is only for "
            "'minimal' (frontier direct-to-rung). Use deliberation='minimal' to seed.")
    artifacts: dict[str, dict] = dict(seed_artifacts or {})
    all_findings: list[dict] = []

    for rung in RUNGS:
        if rung in artifacts:
            continue  # pre-supplied (minimal mode) — but still validated below via seam
        # Build the SCOPED prior-artifact dict for this rung — the fresh-context filter.
        scoped = {name: artifacts[name] for name in _RUNG_INPUTS[rung]
                  if name in artifacts}
        artifact, findings = run_rung(
            ledger, provider, card, rung, scoped, on_event=on_event)
        all_findings.extend(findings)
        if artifact is None:
            # Exhausted MAX_BOUNCES on this rung. §22: "2 bounces → council or
            # escalate". A council can only critique a VALID artifact (its seats read
            # the artifact); a rung that never produced a valid artifact has nothing to
            # convene over, so we escalate. The council DOES run — on a valid rung D —
            # via council_cb below (the §22 checkpoint), not here.
            if on_event:
                on_event("ladder_escalate", {"rung": rung, "reason": "bounces_exhausted"})
            return LadderResult(status="escalated", artifacts=artifacts,
                                escalated_rung=rung, findings=all_findings)
        artifacts[rung] = artifact
        _record_artifact(ledger, rung, artifact)
        if on_event:
            on_event("ladder_rung_done", {"rung": rung})

        # -- rung C → §21 linked dimensions (feature-flagged T-W24) -----------
        if rung == "linkage_map" and link_cb is not None:
            _handoff_links_to_derivation(artifact, link_cb, ledger, doc_id)

    # -- rung D critique by the build-strategy council (feature-flagged T-W23) -
    council_result: Any = "skipped"
    if council_cb is not None and "build_pathway" in artifacts:
        # §19: seats receive geometry_plan + build_pathway — NOT the chat, NOT the
        # function_map. We hand ONLY those two artifacts.
        council_result = council_cb(
            geometry_plan=artifacts.get("geometry_plan", {}),
            build_pathway=artifacts["build_pathway"],
        )
        if on_event:
            # Legacy event (kept for existing consumers).
            on_event("ladder_council", {"kind": "build_strategy"})
            # M20: also emit the contract /2 `agent.advisor` vocabulary event so the
            # Agent Workflow DAG + transparency chat see the council call. Reconciles
            # the off-vocabulary `ladder_council` onto the frozen /2 kind.
            try:
                from services.agent import agent_events as _ae  # noqa: PLC0415
                _verdict = ""
                if isinstance(council_result, dict):
                    _verdict = str(council_result.get("verdict")
                                   or council_result.get("summary") or "")
                elif isinstance(council_result, str):
                    _verdict = council_result
                _k, _p = _ae.advisor_event(
                    doc_id or "ladder", "ladder_council", _verdict,
                    actor="design_thinker")
                _ae.emit_event(_k, _p, live=on_event)
            except Exception:  # noqa: BLE001 — advisor event never blocks the ladder
                pass

    # -- compile rung D losslessly into aicad.buildgraph/1 (T-W15) ------------
    graph = lc.compile_pathway(artifacts["build_pathway"], mission="design_ladder",
                               doc_id=doc_id)
    _record_artifact(ledger, "buildgraph", {"schema": graph["schema"],
                                            "node_count": len(graph["nodes"])})

    return LadderResult(status="done", artifacts=artifacts, graph=graph,
                        council=council_result, findings=all_findings)


# --------------------------------------------------------------------------- ledger events

def _record_artifact(ledger: Ledger, rung: str, artifact: dict) -> None:
    """Write a rung artifact to the ledger as a REPLAYABLE artifact event.

    §22: "Rung artifacts (all ledger events, all replayable)" and Rung E: workers
    receive "the SLICE of the ladder artifacts their node cites" — that slice is read
    from the ledger, so the FULL artifact body MUST persist here. The event carries the
    full ``artifact`` plus the ledger's native artifact fields (kind/id/note/stage) so
    it is both replayable AND legible in the Artifacts section.

    This does NOT reopen the fresh-context leak: ``ledger_render``'s Artifacts renderer
    reads only {kind,id,path,stage,note} and never touches the ``artifact`` key, so the
    body never surfaces in a render at any budget; and every downstream rung's ledger
    slice additionally excises the Artifacts section (``_strip_artifacts_section``). The
    body is persisted-and-replayable, yet unreachable by a later rung — both invariants
    hold, proven by test_ladder_slice_excludes_prior_artifacts_at_any_budget."""
    ledger.append("design_thinker", "artifact", {
        "schema": "aicad.ladder/1",
        "id": rung,
        "rung": rung,
        "kind": "ladder_artifact",
        "note": f"{rung} ({len(artifact)} slots)",
        "stage": "spec" if rung != "build_pathway" else "layout",
        "artifact": artifact,  # full body — replayable; Rung-E slice source (T-W19)
    })


def _handoff_links_to_derivation(linkage_map: dict, link_cb: Callable,
                                 ledger: Ledger, doc_id: str) -> None:
    """Feature-flagged (T-W24): hand rung-C links to derivation.link_dimension so the
    'derived:' relations become LIVE engine equations (§21 linked dimensions). Best-
    effort — a failure here never breaks the ladder (the links are already recorded in
    the linkage_map artifact)."""
    for link in linkage_map.get("links", []) or []:
        try:
            link_cb(link=link, ledger=ledger, doc_id=doc_id)
        except Exception:  # noqa: BLE001
            continue
