"""ledger_vocab.py — ONE source of truth for the ledger section / kind vocabulary.

``services.agent.mission_dispatch`` (the run-scoped mission shadow) and
``services.mcp.ledger_tools`` (the MCP tool) both expose ``ledger_get`` /
``ledger_note`` and used to carry two hand-copied vocabulary tables that had
already drifted (``plan_ruling`` existed in only one of them).  A model that
learned a spelling from one path hit "unknown section"/"unknown kind" on the
other.  Both now import from here, so they cannot drift again.

Measured motivation (93 recorded bench runs, 347 failed tool results): 27
``ledger_get unknown section`` + 24 ``ledger_note unknown kind`` — 15% of every
failure in the corpus.  Both checks already returned the full accepted set, so
the vocabulary itself was the defect, not the error shape.  Two live examples:

    ledger_get(section="/project/mission-123/design/specs/")   -> unknown section
    ledger_note(kind="design_spec")                            -> unknown kind

Resolution order (see :func:`resolve_section` / :func:`resolve_kind`):

  1. **Normalise** — strip whitespace / slashes / quoting, lowercase, collapse
     ``-`` and spaces to ``_``, drop ``ledger_``/``section_``/``state_``
     decoration.
  2. **Segment** — a path-like value is walked most-specific-segment-first, so
     ``/project/mission-123/design/specs/`` tries ``specs`` then ``design``.
  3. **Alias / plural** — the widened alias tables below, then a singular⇄plural
     variant of each candidate.
  4. **Reject, teaching** — :func:`section_error` / :func:`kind_error` keep the
     pre-existing ``allowed``/``aliases``/``valid_kinds``/``aliased_to`` keys and
     ADD ``did_you_mean`` (difflib) plus a ``hint`` with the corrected call.

Fuzzy matching only ever *suggests*: nothing is silently accepted on a guess.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Iterable

# ---------------------------------------------------------------- sections
#: The real ledger state keys ``ledger_get`` can return (``services.agent.ledger``
#: ``Ledger.state`` keys + the ``manifest`` / ``events`` surfaces).
LEDGER_SECTIONS: frozenset[str] = frozenset({
    "brief", "requirements", "decisions", "dimensions", "questions",
    "stage_log", "artifacts", "criterion_results",
    "features", "geometry_specs", "linkages", "build_nodes", "mfg_plans",
    # mega-2: intake manifest surface (also lives under artifacts)
    "manifest", "events",
})

#: Pseudo-section meaning "no section — render the whole ledger".
ALL_SECTION = "all"

_SECTION_TARGETS: frozenset[str] = LEDGER_SECTIONS | {ALL_SECTION}

#: Model-facing spellings → canonical section.  Every value MUST be a member of
#: :data:`_SECTION_TARGETS`; this table never invents a section.
SECTION_ALIASES: dict[str, str] = {
    # -- whole ledger -----------------------------------------------------
    "*": ALL_SECTION,
    "full": ALL_SECTION,
    "entire": ALL_SECTION,
    "everything": ALL_SECTION,
    "ledger": ALL_SECTION,
    "full_ledger": ALL_SECTION,
    "whole_ledger": ALL_SECTION,
    "design_ledger": ALL_SECTION,
    "complete": ALL_SECTION,
    "overview": ALL_SECTION,
    # Measured (bench-live C1): section="design" — a design-stage model asking
    # for "the design".  No single section is that, and guessing one would hand
    # back the wrong data; the whole rendered ledger is the honest answer.
    "design": ALL_SECTION,
    "design_state": ALL_SECTION,
    # Measured: section="mission" — the run's whole state, not one key.
    "mission": ALL_SECTION,
    # -- brief ------------------------------------------------------------
    "mission_brief": "brief",
    "task_brief": "brief",
    "charter": "brief",
    "objective": "brief",
    "objectives": "brief",
    "goal": "brief",
    "goals": "brief",
    "scope": "brief",
    # -- requirements -----------------------------------------------------
    "reqs": "requirements",
    "req": "requirements",
    "constraint": "requirements",
    "constraints": "requirements",
    "needs": "requirements",
    # -- decisions --------------------------------------------------------
    "choice": "decisions",
    "choices": "decisions",
    "ruling": "decisions",
    "rulings": "decisions",
    "verdict": "decisions",
    "verdicts": "decisions",
    "rationale": "decisions",
    "design_decisions": "decisions",
    "decision_log": "decisions",
    "tradeoffs": "decisions",
    # -- dimensions (mega-3 residual: interfaces / fits thrash) -----------
    "interfaces": "dimensions",
    "interface": "dimensions",
    "fits": "dimensions",
    "fit": "dimensions",
    "dims": "dimensions",
    "dim": "dimensions",
    "measurement": "dimensions",
    "measurements": "dimensions",
    "sizes": "dimensions",
    "tolerances": "dimensions",
    "parameters": "dimensions",
    "params": "dimensions",
    # -- questions --------------------------------------------------------
    "open_questions": "questions",
    "unknowns": "questions",
    "asks": "questions",
    "clarifications": "questions",
    # -- stage_log (mega-3 residual: notes thrash) ------------------------
    "notes": "stage_log",
    "note": "stage_log",
    "log": "stage_log",
    "stage": "stage_log",
    "stages": "stage_log",
    "stage_history": "stage_log",
    "stage_notes": "stage_log",
    "journal": "stage_log",
    "worklog": "stage_log",
    "progress": "stage_log",
    "activity": "stage_log",
    # -- artifacts (mega-3 residual: results thrash) ----------------------
    "results": "artifacts",
    "result": "artifacts",
    "outcomes": "artifacts",
    "outcome": "artifacts",
    "deliverable": "artifacts",
    "deliverables": "artifacts",
    "output": "artifacts",
    "outputs": "artifacts",
    "files": "artifacts",
    "exports": "artifacts",
    "reports": "artifacts",
    "acceptance": "artifacts",
    "acceptance_criteria": "artifacts",
    "ac": "artifacts",
    "bom": "artifacts",
    "bill_of_materials": "artifacts",
    "airframe components": "artifacts",
    "airframe_components": "artifacts",
    "components": "artifacts",
    # -- criterion_results ------------------------------------------------
    "criterion_result": "criterion_results",
    "ac_results": "criterion_results",
    "acceptance_results": "criterion_results",
    "criteria": "criterion_results",
    "criteria_results": "criterion_results",
    "checks": "criterion_results",
    "check_results": "criterion_results",
    "grading": "criterion_results",
    "grades": "criterion_results",
    "test_results": "criterion_results",
    "verification": "criterion_results",
    "verifications": "criterion_results",
    # -- features ---------------------------------------------------------
    "feature_list": "features",
    "feature_architecture": "features",
    # -- geometry_specs (the "/design/specs/" family) ---------------------
    "spec": "geometry_specs",
    "specs": "geometry_specs",
    "design_spec": "geometry_specs",
    "design_specs": "geometry_specs",
    "geometry_spec": "geometry_specs",
    "geo_spec": "geometry_specs",
    "geo_specs": "geometry_specs",
    "geometry": "geometry_specs",
    "shape_specs": "geometry_specs",
    "specification": "geometry_specs",
    "specifications": "geometry_specs",
    # -- linkages ---------------------------------------------------------
    "link": "linkages",
    "links": "linkages",
    "relation": "linkages",
    "relations": "linkages",
    "relationships": "linkages",
    "mates": "linkages",
    # -- build_nodes ------------------------------------------------------
    "plan": "build_nodes",
    "build_plan": "build_nodes",
    "buildgraph": "build_nodes",
    "build_graph": "build_nodes",
    "build_steps": "build_nodes",
    "nodes": "build_nodes",
    "steps": "build_nodes",
    "operations": "build_nodes",
    # -- mfg_plans --------------------------------------------------------
    "manufacturing_plan": "mfg_plans",
    "manufacturing_plans": "mfg_plans",
    "mfg_plan": "mfg_plans",
    "process_plan": "mfg_plans",
    "route_plan": "mfg_plans",
    # -- manifest ---------------------------------------------------------
    "deliverables_manifest": "manifest",
    "intake_manifest": "manifest",
    # -- events -----------------------------------------------------------
    "event": "events",
    "event_log": "events",
    "event_history": "events",
    "history": "events",
    "timeline": "events",
}

# ------------------------------------------------------------------- kinds
#: The event kinds ``ledger_note`` accepts (mirrors ``services.agent.ledger``
#: materialization, including the four design-pipeline stage kinds).
LEDGER_NOTE_KINDS: frozenset[str] = frozenset({
    "brief", "decision", "dimension", "question", "requirement", "stage",
    "artifact", "note", "feature", "geometry_spec", "linkage", "build_node",
    "mfg_plan", "criterion_result",
})

#: Model-facing spellings → canonical kind.  Values MUST be in
#: :data:`LEDGER_NOTE_KINDS`.
#:
#: ``spec``/``design_spec`` deliberately land on ``decision``, NOT
#: ``geometry_spec``: a Stage-2 ``geometry_spec`` is a structured record with a
#: HARD ``provenance`` requirement (contract §3.2) plus ``realizes``/``shapes``,
#: and the traces show ``design_spec`` carrying prose ({id, text}) — exactly the
#: ``decision`` payload shape.  Routing prose into ``geometry_spec`` would only
#: trade an unknown-kind error for a provenance error while writing a junk row.
#: A model that really means a geometry spec spells it ``geometry_spec``.
KIND_ALIASES: dict[str, str] = {
    # -- decision (mega-2 verdict / grok45-2 review_verdict, audit) --------
    "verdict": "decision",
    "review_verdict": "decision",
    "audit": "decision",
    "ruling": "decision",
    "plan_ruling": "decision",
    "spec": "decision",
    "specs": "decision",
    "design_spec": "decision",
    "design_specs": "decision",
    "specification": "decision",
    "specifications": "decision",
    "design_decision": "decision",
    "choice": "decision",
    "selection": "decision",
    "tradeoff": "decision",
    "trade_off": "decision",
    "rationale": "decision",
    "conclusion": "decision",
    "resolution": "decision",
    "determination": "decision",
    "assumption": "decision",
    # mega-3 residual: intake/organize models write kind=manifest_confirm
    "manifest_confirm": "decision",
    "confirm": "decision",
    "confirmation": "decision",
    "sign_off": "decision",
    "approval": "decision",
    # -- artifact ----------------------------------------------------------
    "deliverables_manifest": "artifact",
    "manifest": "artifact",
    "report": "artifact",
    "battery": "artifact",
    "verification": "artifact",
    "verification_result": "artifact",
    "deliverable": "artifact",
    "output": "artifact",
    "file": "artifact",
    "export": "artifact",
    "result": "artifact",
    "results": "artifact",
    # -- criterion_result --------------------------------------------------
    "criterion": "criterion_result",
    "ac_result": "criterion_result",
    "acceptance": "criterion_result",
    "acceptance_result": "criterion_result",
    "criteria": "criterion_result",
    "check": "criterion_result",
    "check_result": "criterion_result",
    "grade": "criterion_result",
    "test_result": "criterion_result",
    # -- dimension ---------------------------------------------------------
    "measurement": "dimension",
    "measure": "dimension",
    "dim": "dimension",
    "size": "dimension",
    "tolerance": "dimension",
    "parameter": "dimension",
    "value": "dimension",
    # -- requirement -------------------------------------------------------
    "req": "requirement",
    "constraint": "requirement",
    "need": "requirement",
    "user_requirement": "requirement",
    # -- question ----------------------------------------------------------
    "query": "question",
    "ask": "question",
    "open_question": "question",
    "clarification": "question",
    "blocker": "question",
    # -- stage -------------------------------------------------------------
    "phase": "stage",
    "phase_change": "stage",
    "stage_transition": "stage",
    "milestone": "stage",
    "status": "stage",
    "progress": "stage",
    # -- note --------------------------------------------------------------
    "comment": "note",
    "observation": "note",
    "analysis": "note",
    "finding": "note",
    "info": "note",
    "message": "note",
    "memo": "note",
    "log": "note",
    "context": "note",
    # Measured: kind="error" — a model recording a failure it hit.  `note` is
    # the catch-all event kind; the payload survives verbatim.
    "error": "note",
    "failure": "note",
    "issue": "note",
    "problem": "note",
    "warning": "note",
    "findings": "note",
    # -- brief -------------------------------------------------------------
    "mission_brief": "brief",
    "task_brief": "brief",
    "objective": "brief",
    "goal": "brief",
    "scope": "brief",
    "charter": "brief",
    # -- design-pipeline stage kinds ---------------------------------------
    "feature_spec": "feature",
    "geo_spec": "geometry_spec",
    "geometry": "geometry_spec",
    "shape_spec": "geometry_spec",
    "link": "linkage",
    "relation": "linkage",
    "relationship": "linkage",
    "mate": "linkage",
    "build_step": "build_node",
    "build_plan": "build_node",
    "plan_node": "build_node",
    "operation": "build_node",
    "node": "build_node",
    # -- manufacturing plan -----------------------------------------------
    "manufacturing_plan": "mfg_plan",
    "manufacturing_route": "mfg_plan",
    "process_plan": "mfg_plan",
    "route_plan": "mfg_plan",
}

# -------------------------------------------------------------- normalising
#: Characters a model wraps a value in (quotes, brackets, punctuation, slashes).
_DECORATION = " \t\r\n\"'`*:;,!?.()[]{}<>/\\|"
#: Path-ish separators — a value carrying one is walked segment by segment.
_SEGMENT_SPLIT = re.compile(r"(?:::|[/\\>.|])+")
#: Word separators that collapse to ``_``.
_WORD_SEP = re.compile(r"[\s\-]+")
_UNDERSCORES = re.compile(r"_+")
#: Decoration a model prefixes/suffixes onto an otherwise correct key.
_PREFIXES = ("ledger_", "section_", "state_", "the_", "get_")
_SUFFIXES = ("_section", "_list", "_data")


def normalise(token: Any) -> str:
    """Canonical lowercase ``snake_case`` form of one model-supplied token."""
    text = str(token if token is not None else "").strip().strip(_DECORATION)
    text = _WORD_SEP.sub("_", text.strip().lower())
    return _UNDERSCORES.sub("_", text).strip("_")


def candidates(raw: Any) -> list[str]:
    """Normalised spellings to try for *raw*, most specific first.

    ``"/project/mission-123/design/specs/"`` → ``["project/mission_123/design/specs",
    "specs", "design", "mission_123", "project"]``
    — the whole value first (for the common non-path case), then each path
    segment from the most specific (rightmost) to the least.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return []
    out: list[str] = []
    whole = normalise(text)
    if whole:
        out.append(whole)
    segments = [s for s in _SEGMENT_SPLIT.split(text) if s.strip()]
    if len(segments) > 1:
        for seg in reversed(segments):
            norm = normalise(seg)
            if norm and not norm.isdigit() and norm not in out:
                out.append(norm)
    # Punctuation-only spellings ("*") normalise to nothing but ARE vocabulary.
    bare = text.lower()
    if bare not in out:
        out.append(bare)
    return out


def _variants(cand: str) -> list[str]:
    """Cheap deterministic re-spellings of *cand* (plural⇄singular, decoration)."""
    out: list[str] = []

    def _add(value: str) -> None:
        if value and value != cand and value not in out:
            out.append(value)

    for prefix in _PREFIXES:
        if cand.startswith(prefix) and len(cand) > len(prefix):
            _add(cand[len(prefix):])
    for suffix in _SUFFIXES:
        if cand.endswith(suffix) and len(cand) > len(suffix):
            _add(cand[: -len(suffix)])
    if cand.endswith("ies"):
        _add(cand[:-3] + "y")
    if cand.endswith("s") and not cand.endswith("ss"):
        _add(cand[:-1])
    else:
        _add(cand + "s")
    return out


def _lookup(cand: str, vocab: frozenset[str], aliases: dict[str, str]) -> str | None:
    if cand in vocab:
        return cand
    target = aliases.get(cand)
    if target in vocab:
        return target
    return None


def _tails(cand: str) -> list[str]:
    """Trailing word-groups of a compound spelling, most specific first.

    Measured: ``design_specifications`` / ``mission_specifications`` /
    ``audit_findings`` / ``verify_result`` — a real vocabulary word with a
    qualifier bolted on the front.  The HEAD is never tried on its own: for
    ``design_specifications`` the tail (``specifications`` → geometry_specs) is
    what the model meant, while the head (``design``) would hand back something
    else entirely.
    """
    parts = cand.split("_")
    return ["_".join(parts[i:]) for i in range(1, len(parts))]


def _resolve_token(cand: str, vocab: frozenset[str],
                   aliases: dict[str, str]) -> str | None:
    hit = _lookup(cand, vocab, aliases)
    if hit:
        return hit
    for variant in _variants(cand):
        hit = _lookup(variant, vocab, aliases)
        if hit:
            return hit
    return None


def _resolve(raw: Any, vocab: frozenset[str], aliases: dict[str, str]) -> str | None:
    """Canonical member of *vocab* for *raw*, or ``None`` when nothing resolves."""
    for cand in candidates(raw):
        hit = _resolve_token(cand, vocab, aliases)
        if hit:
            return hit
        for tail in _tails(cand):
            hit = _resolve_token(tail, vocab, aliases)
            if hit:
                return hit
    return None


def _did_you_mean(raw: Any, vocab: Iterable[str], aliases: dict[str, str],
                  limit: int = 3) -> list[str]:
    """Fuzzy suggestions (canonical forms) for an unresolvable *raw*.

    Suggestion only — a fuzzy hit is NEVER auto-accepted, because guessing which
    section a model meant would write real data to the wrong place.
    """
    pool = sorted(set(vocab) | set(aliases))
    hits: list[str] = []
    for cand in candidates(raw) or [normalise(raw)]:
        if not cand:
            continue
        for match in difflib.get_close_matches(cand, pool, n=limit, cutoff=0.6):
            canonical = match if match in vocab else aliases.get(match, match)
            if canonical not in hits:
                hits.append(canonical)
        if hits:
            break
    return hits[:limit]


# ------------------------------------------------------------ public surface
def resolve_section(raw: Any) -> str | None:
    """Canonical ledger section for *raw*, ``"all"`` for a whole-ledger request,
    or ``None`` when nothing resolves."""
    return _resolve(raw, _SECTION_TARGETS, SECTION_ALIASES)


def resolve_kind(raw: Any) -> str | None:
    """Canonical ``ledger_note`` kind for *raw*, or ``None``."""
    return _resolve(raw, LEDGER_NOTE_KINDS, KIND_ALIASES)


def section_error(raw: Any) -> dict:
    """Teaching rejection for an unresolvable section.

    Keeps the long-standing ``allowed`` / ``aliases`` keys (consumers read them)
    and adds ``did_you_mean`` + a ``hint`` naming the corrected call.
    """
    suggestions = _did_you_mean(raw, _SECTION_TARGETS, SECTION_ALIASES)
    pick = suggestions[0] if suggestions else ALL_SECTION
    if suggestions:
        hint = (f"ledger_get(section={pick!r}) — one bare section name, "
                "not a path")
    else:
        # Measured: models pass a deliverable/part id ("DL1", "flanged_bushing")
        # or a search query where a section name belongs.  Name the confusion.
        hint = (f"ledger_get(section={ALL_SECTION!r}) — section names a LEDGER "
                "SECTION (see 'allowed'), not a deliverable/part id, a project "
                "name, or a search query; omit it or use 'all' for everything")
    return {
        "ok": False,
        "error": f"unknown section {raw!r}",
        "allowed": sorted(LEDGER_SECTIONS) + [ALL_SECTION],
        "aliases": dict(SECTION_ALIASES),
        "did_you_mean": suggestions,
        "hint": hint,
    }


def kind_error(raw: Any) -> dict:
    """Teaching rejection for an unresolvable ``ledger_note`` kind."""
    suggestions = _did_you_mean(raw, LEDGER_NOTE_KINDS, KIND_ALIASES)
    pick = suggestions[0] if suggestions else "note"
    return {
        "ok": False,
        "error": f"unknown kind {raw!r}",
        "valid_kinds": sorted(LEDGER_NOTE_KINDS | set(KIND_ALIASES)),
        "aliased_to": dict(KIND_ALIASES),
        "did_you_mean": suggestions,
        "hint": f"ledger_note(kind={pick!r}, payload={{...}})",
    }
