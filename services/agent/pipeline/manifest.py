"""manifest.py — the ``aicad.deliverables/1`` validator (contract §16).

The deliverable manifest is the mission's definition of done.  The intake_analyst
role *produces* it (from the user request); this module *validates* it —
deterministically, with no model call.  A manifest that passes here is the
contract everything downstream cites (DL/AC ids) instead of re-reading the
request.

Rules enforced (contract §16, verbatim):
  * every deliverable has ≥1 acceptance criterion;
  * every acceptance criterion names an executable ``check`` tool;
  * a ``check.tool == "manual"`` criterion must be paired with a ``needs_confirm``
    assumption OR an ``ask_user`` question (the manual-kind pairing rule);
  * ``modification`` deliverables must resolve ``target_refs`` against an existing
    doc (a ``doc_resolver`` callback supplies the resolution; unresolved →
    blocking);
  * no dependency cycles among deliverables (``depends_on`` edges);
  * ``depends_on`` references resolve to real deliverable ids.

Bounce-once-then-escalate: the intake_analyst's ``escalation_policy`` is
``{max_attempts: 2, on_exhaust: ask_user}``.  ``BounceTracker`` records validator
bounces so the pipeline bounces the manifest back to the role ONCE, then escalates
to the user instead of grinding.

Findings carry a ``level`` (``"error"`` blocks the phase exit; ``"blocking"`` means
a blocking ambiguity that routes to CLARIFY, not a structural error) so the caller
can distinguish "the manifest is malformed" from "the manifest is fine but needs a
user answer before Phase 2".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

SCHEMA = "aicad.deliverables/1"

# Deliverable kinds (contract §16).
VALID_KINDS = frozenset({
    "part", "assembly", "drawing", "cam_program",
    "analysis", "report", "modification",
})

# The check tools a criterion may name (contract §16 example enumerates these; the
# validator accepts any non-empty tool string but flags an unknown one as a hint —
# a new check tool may land after this table).  "manual" is the special kind that
# triggers the pairing rule.
KNOWN_CHECK_TOOLS = frozenset({
    "fit_report", "verify_battery", "measure_part", "measure_between",
    "query_geometry", "cam_simulate", "run_fea_static", "run_fea_modal",
    "run_cfd_flow", "review_drawing", "check_interference", "wall_thickness",
    "design_review", "manual",
    # mega-11: ECAD / firmware / antenna / report-card gradeable tools
    "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
    "ecad_antenna_design", "ecad_antenna_analyze",
    "fw_build", "fw_sim_run", "fw_verify_board",
    "report_card",
    # compound multi-tool form used by AC4
    "ecad_run_drc+ecad_run_erc",
})


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------

@dataclass
class ManifestFinding:
    """One validation finding.

    ``level``:
      * ``"error"``    — the manifest is structurally invalid (blocks phase exit);
      * ``"blocking"`` — a blocking ambiguity: the manifest is well-formed but a
        user answer is required before Phase 2 (routes to CLARIFY, not a bounce);
      * ``"hint"``     — advisory (e.g. an unrecognised check tool), never blocks.
    """
    level: str
    rule: str
    message: str
    refs: dict[str, Any] | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience
        r = f" {self.refs}" if self.refs else ""
        return f"[{self.level}] {self.rule}: {self.message}{r}"


# A doc_resolver resolves a modification deliverable's target_refs to a real doc.
# It receives the target_refs dict and returns True if it resolves to an existing
# doc, False otherwise.  None (the default) means "no project context available",
# in which case unresolved target_refs are BLOCKING (route to clarifier) rather
# than errors — the analyst cannot resolve a doc that isn't open.
DocResolver = Callable[[dict[str, Any]], bool]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_manifest(
    manifest: dict[str, Any],
    *,
    doc_resolver: Optional[DocResolver] = None,
) -> list[ManifestFinding]:
    """Validate a manifest against every contract §16 rule.

    Returns a list of :class:`ManifestFinding`.  ``[]`` = clean and ready for
    Phase 2.  Any ``error``-level finding blocks the INTAKE exit gate (bounce to
    the role); ``blocking``-level findings route to CLARIFY (Phase 1); ``hint``s
    are informational.
    """
    findings: list[ManifestFinding] = []

    if not isinstance(manifest, dict):
        return [ManifestFinding("error", "shape",
                                "manifest must be a JSON object")]

    if manifest.get("schema") != SCHEMA:
        findings.append(ManifestFinding(
            "error", "schema",
            f"schema is {manifest.get('schema')!r}, expected {SCHEMA!r}"))

    if not str(manifest.get("request_verbatim") or "").strip():
        findings.append(ManifestFinding(
            "error", "request_verbatim",
            "request_verbatim is required and must be the user's words unedited"))

    deliverables = manifest.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        findings.append(ManifestFinding(
            "error", "deliverables",
            "manifest must have a non-empty 'deliverables' list"))
        # Without deliverables the remaining per-deliverable rules cannot run.
        return findings

    # Collect ids first (needed for depends_on resolution + cycle check).
    del_ids: list[str] = []
    for i, d in enumerate(deliverables):
        did = d.get("id") if isinstance(d, dict) else None
        if not did:
            findings.append(ManifestFinding(
                "error", "deliverable_id",
                f"deliverable at index {i} is missing an 'id'"))
            continue
        if did in del_ids:
            findings.append(ManifestFinding(
                "error", "deliverable_id",
                f"duplicate deliverable id {did!r}", refs={"id": did}))
        del_ids.append(did)
    del_id_set = set(del_ids)

    assumptions = manifest.get("assumptions") or []
    questions_present = _has_ask_user(manifest)

    for d in deliverables:
        if not isinstance(d, dict):
            continue
        did = d.get("id") or "?"

        # kind
        kind = d.get("kind")
        if kind not in VALID_KINDS:
            findings.append(ManifestFinding(
                "error", "kind",
                f"deliverable {did!r} has invalid kind {kind!r}; "
                f"expected one of {sorted(VALID_KINDS)}", refs={"id": did}))

        # ≥1 acceptance criterion
        acs = d.get("acceptance_criteria")
        if not isinstance(acs, list) or not acs:
            findings.append(ManifestFinding(
                "error", "acceptance_criteria",
                f"deliverable {did!r} must have ≥1 acceptance criterion",
                refs={"id": did}))
            acs = []

        for ac in acs:
            _validate_criterion(did, ac, assumptions, questions_present, findings)

        # depends_on resolution
        for dep in d.get("depends_on", []) or []:
            if dep not in del_id_set:
                findings.append(ManifestFinding(
                    "error", "depends_on",
                    f"deliverable {did!r} depends_on {dep!r} which is not a "
                    "deliverable id in this manifest",
                    refs={"id": did, "depends_on": dep}))

        # modification deliverables must resolve target_refs
        if kind == "modification":
            _validate_modification_target(did, d, doc_resolver, findings)

    # dependency cycles
    _detect_cycles(deliverables, del_id_set, findings)

    # blocking ambiguities → route to clarifier (informational at INTAKE; the
    # phase machine reads these to decide CLARIFY vs skip-to-SPECIFY).
    for amb in manifest.get("ambiguities", []) or []:
        if isinstance(amb, dict) and amb.get("blocking"):
            findings.append(ManifestFinding(
                "blocking", "ambiguity",
                f"blocking ambiguity: {amb.get('text', '')!r}",
                refs={"question_id": amb.get("question_id")}))

    return findings


def _validate_criterion(
    did: str,
    ac: Any,
    assumptions: Iterable[dict],
    questions_present: bool,
    findings: list[ManifestFinding],
) -> None:
    """Validate one acceptance criterion (contract §16 criterion rules)."""
    if not isinstance(ac, dict):
        findings.append(ManifestFinding(
            "error", "criterion",
            f"deliverable {did!r} has a non-object acceptance criterion",
            refs={"id": did}))
        return
    ac_id = ac.get("id") or "?"
    ref = {"id": did, "ac": ac_id}

    if not str(ac.get("text") or "").strip():
        findings.append(ManifestFinding(
            "error", "criterion_text",
            f"{did}/{ac_id}: acceptance criterion has no 'text'", refs=ref))

    check = ac.get("check")
    if not isinstance(check, dict):
        findings.append(ManifestFinding(
            "error", "criterion_check",
            f"{did}/{ac_id}: criterion must name a 'check' with a tool", refs=ref))
        return

    tool = check.get("tool")
    if not tool or not str(tool).strip():
        findings.append(ManifestFinding(
            "error", "criterion_check_tool",
            f"{did}/{ac_id}: check names no tool — every criterion must name an "
            "executable check tool", refs=ref))
        return

    if tool not in KNOWN_CHECK_TOOLS:
        findings.append(ManifestFinding(
            "hint", "criterion_check_tool_unknown",
            f"{did}/{ac_id}: check tool {tool!r} is not in the known check-tool "
            "table — verify it resolves at Phase 5", refs=ref))

    # pass_rule is required for non-manual checks — a check without a pass rule is
    # not verifiable ("verification is running the named checks, not vibes").
    if tool != "manual" and not str(check.get("pass_rule") or "").strip():
        findings.append(ManifestFinding(
            "error", "criterion_pass_rule",
            f"{did}/{ac_id}: check tool {tool!r} has no 'pass_rule' — the reviewer "
            "cannot produce a PASS/FAIL without one", refs=ref))

    # manual-kind pairing rule: a manual criterion must be paired with a
    # needs_confirm assumption OR an ask_user question.
    if tool == "manual":
        paired = questions_present or any(
            isinstance(a, dict) and a.get("needs_confirm")
            for a in assumptions
        )
        if not paired:
            findings.append(ManifestFinding(
                "error", "manual_pairing",
                f"{did}/{ac_id}: a 'manual' check must be paired with a "
                "needs_confirm assumption or an ask_user question (§16)", refs=ref))


def _validate_modification_target(
    did: str,
    d: dict[str, Any],
    doc_resolver: Optional[DocResolver],
    findings: list[ManifestFinding],
) -> None:
    """A modification deliverable must resolve its target_refs to an existing doc."""
    target_refs = d.get("target_refs")
    if not isinstance(target_refs, dict) or not target_refs.get("doc_id"):
        # No doc named at all — the analyst could not resolve which doc; this is a
        # blocking ambiguity (route to clarifier), not a structural error.
        findings.append(ManifestFinding(
            "blocking", "modification_target",
            f"modification deliverable {did!r} does not resolve target_refs to a "
            "doc — which doc should be modified?", refs={"id": did}))
        return
    if doc_resolver is not None:
        try:
            resolved = doc_resolver(target_refs)
        except Exception:  # noqa: BLE001 — resolver failure is treated as unresolved
            resolved = False
        if not resolved:
            findings.append(ManifestFinding(
                "error", "modification_target",
                f"modification deliverable {did!r} names doc "
                f"{target_refs.get('doc_id')!r} but it does not resolve to an "
                "existing doc", refs={"id": did, "target_refs": target_refs}))


def _detect_cycles(
    deliverables: list[dict],
    del_id_set: set[str],
    findings: list[ManifestFinding],
) -> None:
    """Kahn topological sort over depends_on edges; any remaining node = a cycle."""
    in_degree: dict[str, int] = {d.get("id"): 0 for d in deliverables
                                 if isinstance(d, dict) and d.get("id")}
    succ: dict[str, list[str]] = {k: [] for k in in_degree}
    for d in deliverables:
        if not isinstance(d, dict):
            continue
        did = d.get("id")
        if not did:
            continue
        for dep in d.get("depends_on", []) or []:
            if dep in del_id_set and dep != did:
                succ[dep].append(did)
                in_degree[did] += 1
    queue = [k for k, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        cur = queue.pop(0)
        visited += 1
        for nxt in succ.get(cur, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    if visited < len(in_degree):
        cyc = sorted(k for k, deg in in_degree.items() if deg > 0)
        findings.append(ManifestFinding(
            "error", "dependency_cycle",
            f"deliverable depends_on cycle among {cyc}", refs={"cycle": cyc}))


def _has_ask_user(manifest: dict[str, Any]) -> bool:
    """True if the manifest carries an ask_user question (a non-null question_id on
    an ambiguity, or a non-empty top-level 'questions' list)."""
    for amb in manifest.get("ambiguities", []) or []:
        if isinstance(amb, dict) and amb.get("question_id"):
            return True
    return bool(manifest.get("questions"))


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------

def manifest_valid(findings: Iterable[ManifestFinding]) -> bool:
    """True when no ``error``-level finding is present (blocking/hint are allowed —
    they do not make the manifest malformed, they route it)."""
    return not any(f.level == "error" for f in findings)


def blocking_ambiguities(findings: Iterable[ManifestFinding]) -> list[ManifestFinding]:
    """The ``blocking``-level findings — these route the mission to CLARIFY (Phase 1)."""
    return [f for f in findings if f.level == "blocking"]


def errors(findings: Iterable[ManifestFinding]) -> list[ManifestFinding]:
    return [f for f in findings if f.level == "error"]


# ---------------------------------------------------------------------------
# Bounce-once-then-escalate (intake_analyst escalation_policy)
# ---------------------------------------------------------------------------

@dataclass
class BounceTracker:
    """Enforces the intake_analyst escalation_policy ``{max_attempts: 2,
    on_exhaust: ask_user}``.

    The manifest may be bounced back to the role ONCE on a validator failure; a
    second failure escalates to the user rather than grinding a third attempt.
    ``attempts`` counts validation attempts (starts at 0; ``record_bounce`` after
    each failed validation).
    """
    max_attempts: int = 2
    attempts: int = 0

    def record_bounce(self) -> str:
        """Register one failed validation attempt and return the next action:

          * ``"retry"``    — bounce the manifest back to the role for a fix;
          * ``"escalate"`` — the budget is exhausted; escalate to the user
            (``on_exhaust: ask_user``).
        """
        self.attempts += 1
        if self.attempts >= self.max_attempts:
            return "escalate"
        return "retry"

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts
