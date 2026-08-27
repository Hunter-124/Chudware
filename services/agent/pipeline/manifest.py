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
    doc; an authoritative ``False`` is not-found, while resolver exceptions are
    surfaced separately as harness-resolution failures;
  * no dependency cycles among deliverables (``depends_on`` edges);
  * ``depends_on`` references resolve to real deliverable ids.

Bounce-once-then-escalate: the intake_analyst's ``escalation_policy`` is
``{max_attempts: 2, on_exhaust: ask_user}``.  ``BounceTracker`` records validator
bounces so the pipeline bounces the manifest back to the role ONCE, then escalates
to the user instead of grinding.

Findings carry a ``level``: ``"error"`` is malformed user/model data,
``"blocking"`` routes a well-formed ambiguity to CLARIFY, ``"harness_error"``
means validation infrastructure was unavailable, and ``"hint"`` is advisory.
Harness errors block progress but are never charged as a manifest bounce.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

SCHEMA = "aicad.deliverables/1"

# Deliverable kinds (contract §16).
VALID_KINDS = frozenset({
    "part", "assembly", "drawing", "cam_program",
    "analysis", "report", "modification",
    # 2026-08-26 additive (contracts/advisor.md §16 version note): structured
    # shop-instruction deliverables built by services/deliverables templates.
    "work_instruction", "weldment_traveler",
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
      * ``"error"``         — structurally invalid manifest (bounce);
      * ``"blocking"``      — user input is required before Phase 2;
      * ``"harness_error"`` — validator infrastructure failed (never a bounce);
      * ``"hint"``          — advisory, never blocks.
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
        if not isinstance(d, dict):
            # Say what is actually wrong. This used to fall through to "missing an
            # 'id'", which asks the model to add a key to a string -- unsatisfiable
            # without restructuring -- and the per-deliverable rules below skip
            # non-dicts, so kind/acceptance_criteria were never even reported.
            # Live 2026-07-30 (run-4530697933837): six string deliverables produced
            # six impossible id errors and nothing else.
            findings.append(ManifestFinding(
                "error", "deliverable_shape",
                f"deliverable at index {i} is a {type(d).__name__}, not an object; "
                "expected {'id', 'kind', 'title', 'acceptance_criteria': [...]}"))
            continue
        did = d.get("id")
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
        except Exception as exc:  # noqa: BLE001 — distinguish harness from not-found
            findings.append(ManifestFinding(
                "harness_error",
                "modification_target_resolution",
                f"modification deliverable {did!r} target resolver unavailable: "
                f"{_bounded_error(exc)}",
                refs={
                    "id": did,
                    "target_refs": target_refs,
                    "outcome_kind": "internal_error",
                    "executed": False,
                },
            ))
            return
        if resolved is False:
            findings.append(ManifestFinding(
                "error", "modification_target",
                f"modification deliverable {did!r} names doc "
                f"{target_refs.get('doc_id')!r} but it does not resolve to an "
                "existing doc", refs={"id": did, "target_refs": target_refs}))
        elif resolved is not True:
            findings.append(ManifestFinding(
                "harness_error",
                "modification_target_resolution",
                f"modification deliverable {did!r} target resolver returned "
                f"non-boolean result {type(resolved).__name__}",
                refs={
                    "id": did,
                    "target_refs": target_refs,
                    "outcome_kind": "internal_error",
                    "executed": False,
                },
            ))




def _bounded_error(exc: Exception, limit: int = 240) -> str:
    detail = " ".join(str(exc).split())
    rendered = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return rendered[:limit]
def manifest_template() -> dict[str, Any]:
    """A minimal manifest that passes :func:`validate_manifest` cleanly.

    Exists because "pass the intake_analyst's manifest" is unactionable advice to
    a model: the shape lives in contract §16 and in this validator, nowhere the
    model can read at call time. Live 2026-07-29 (case bench, K3): the model's own
    reasoning said "manifest shape - unknown schema, but likely {deliverables:
    [...]}", it never risked the call, narrated instead, and the turn died with the
    tool budget untouched. ``mission_start`` returns this alongside its refusal so
    the very next completion can construct a valid manifest.

    Keep it VALID: ``test_manifest`` asserts ``validate_manifest`` finds no
    error/blocking finding here, so a new rule cannot silently make the template
    the harness advertises unusable.
    """
    return {
        "schema": SCHEMA,
        "request_verbatim": "<the user's request, unedited>",
        "deliverables": [
            {
                "id": "DL1",
                "kind": "part",                      # one of VALID_KINDS
                "title": "<what this deliverable is>",
                "depends_on": [],                    # other deliverable ids
                "acceptance_criteria": [
                    {
                        "id": "AC1",
                        "text": "<objectively checkable statement>",
                        "check": {
                            "tool": "measure_part",  # one of KNOWN_CHECK_TOOLS
                            "pass_rule": "<PASS/FAIL rule over that tool's output>",
                        },
                    },
                ],
            },
        ],
        "assumptions": [],   # [{"text": ..., "needs_confirm": bool}]
        "ambiguities": [],   # [{"text": ..., "blocking": bool, "question_id": ...}]
    }


def normalize_manifest(
    manifest: Any,
    *,
    request: str | None = None,
) -> tuple[Any, list[str]]:
    """Fill in a manifest's MECHANICAL fields, returning ``(manifest, interpreted)``.

    Bounces a model on semantics, never on boilerplate. Live 2026-07-30
    (run-4530697933837, OptiPlex case, K3): the model authored a perfectly
    reasonable manifest and got eight errors back, six of which the harness could
    have filled itself --

        {"request": "Design a manufacturable custom case ...",
         "manifest": {"deliverables": ["component dimensions and clearance
                      assumptions", "enclosure layout plan", ...],
                      "constraints": [...]}}

      * ``schema`` is a CONSTANT. Rejecting a manifest for omitting a value with
        exactly one legal setting is pure friction.
      * ``request_verbatim`` was sitting right there in the ``request`` argument.
      * ids are POSITIONAL labels; "deliverable at index 0 is missing an 'id'" x6
        is noise that crowds out the two findings that actually needed the model.

    Worse, those six were the ONLY findings: a string deliverable fails the
    ``isinstance(d, dict)`` guard in :func:`validate_manifest`, so it reports a
    missing 'id' and then skips the kind/acceptance_criteria rules entirely. The
    model was told to add an 'id' to a string -- impossible without restructuring,
    and nothing said the entry had to be an object. That is the unactionable
    refusal this module already fixed once for the no-manifest path.

    Only mechanical repairs happen here. ``kind`` and ``acceptance_criteria`` carry
    real meaning, so they are still the model's job -- but it now answers for those
    alone, in its own structure, via ``manifest_draft``.
    """
    interpreted: list[str] = []
    if not isinstance(manifest, dict):
        return manifest, interpreted
    m = copy.deepcopy(manifest)

    if m.get("schema") != SCHEMA:
        was = m.get("schema")
        m["schema"] = SCHEMA
        interpreted.append(
            f"schema: set to {SCHEMA!r}" + (f" (was {was!r})" if was else ""))

    if not str(m.get("request_verbatim") or "").strip() and str(request or "").strip():
        m["request_verbatim"] = request
        # Honest about the provenance: this is the `request` ARGUMENT, which is
        # whatever the caller wrote there. §16 wants the user's unedited words, so
        # say so rather than let a paraphrase pass as verbatim in silence.
        interpreted.append(
            "request_verbatim: copied from the `request` argument. If that was a "
            "paraphrase, resend with the user's exact words.")

    deliverables = m.get("deliverables")
    if isinstance(deliverables, list) and deliverables:
        coerced = 0
        for i, d in enumerate(deliverables):
            if isinstance(d, str) and d.strip():
                deliverables[i] = {"title": d.strip()}
                coerced += 1
        if coerced:
            interpreted.append(
                f"deliverables: {coerced} plain string(s) read as a deliverable "
                "title; each still needs a `kind` and `acceptance_criteria`.")

        taken = {d["id"] for d in deliverables
                 if isinstance(d, dict) and d.get("id")}
        assigned = 0
        n = 0
        for d in deliverables:
            if not isinstance(d, dict) or d.get("id"):
                continue
            while True:
                n += 1
                cand = f"DL{n}"
                if cand not in taken:
                    break
            d["id"] = cand
            taken.add(cand)
            assigned += 1
        if assigned:
            interpreted.append(
                f"deliverables: assigned {assigned} positional id(s) "
                "(DL1..DLn); reference them from `depends_on`.")
    return m, interpreted


def manifest_draft(manifest: dict[str, Any]) -> dict[str, Any]:
    """The caller's own (normalized) manifest with missing required fields marked.

    Answers "what does right look like?" in the caller's OWN structure. A generic
    :func:`manifest_template` makes a model re-derive six deliverables it had
    already written down correctly; this hands its work back with only the blanks
    to fill, so the next completion is an edit rather than a rewrite.
    """
    d = copy.deepcopy(manifest)
    for dl in d.get("deliverables") or []:
        if not isinstance(dl, dict):
            continue
        if dl.get("kind") not in VALID_KINDS:
            dl["kind"] = f"<REQUIRED: one of {sorted(VALID_KINDS)}>"
        acs = dl.get("acceptance_criteria")
        if not isinstance(acs, list) or not acs:
            dl["acceptance_criteria"] = [{
                "id": "AC1",
                "text": "<objectively checkable statement>",
                "check": {
                    "tool": f"<REQUIRED: one of {sorted(KNOWN_CHECK_TOOLS)}>",
                    "pass_rule": "<PASS/FAIL rule over that tool's output>",
                },
            }]
        else:
            # The observed SECOND bounce (run-4433662287777, same session): the
            # model supplied criteria but wrote them as prose without a `check`,
            # and got "DL1/AC1: criterion must name a 'check' with a tool" x4.
            # Marking the gap in place keeps that a one-line edit per criterion.
            for ac in acs:
                if not isinstance(ac, dict):
                    continue
                chk = ac.get("check")
                if not isinstance(chk, dict) or not chk.get("tool"):
                    ac["check"] = {
                        "tool": f"<REQUIRED: one of {sorted(KNOWN_CHECK_TOOLS)}>",
                        "pass_rule": (chk or {}).get("pass_rule")
                        or "<PASS/FAIL rule over that tool's output>",
                    }
    return d


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
    """True when neither malformed-data nor harness-error findings are present."""
    return not any(f.level in {"error", "harness_error"} for f in findings)


def blocking_ambiguities(findings: Iterable[ManifestFinding]) -> list[ManifestFinding]:
    """The ``blocking``-level findings — these route the mission to CLARIFY."""
    return [f for f in findings if f.level == "blocking"]


def errors(findings: Iterable[ManifestFinding]) -> list[ManifestFinding]:
    """Findings that block INTAKE; callers bounce only the ``error`` subset."""
    return [f for f in findings if f.level in {"error", "harness_error"}]


# ---------------------------------------------------------------------------
# Bounce-once-then-escalate (intake_analyst escalation_policy)
# ---------------------------------------------------------------------------

@dataclass
class BounceTracker:
    """Track manifest-data bounces without charging harness failures.

    ``record_bounce(findings)`` returns ``"harness_error"`` and preserves the
    attempt count when validation infrastructure was unavailable.
    """
    max_attempts: int = 2
    attempts: int = 0

    def record_bounce(
        self,
        findings: Iterable[ManifestFinding] | None = None,
    ) -> str:
        if findings is not None and any(
            finding.level == "harness_error" for finding in findings
        ):
            return "harness_error"
        self.attempts += 1
        if self.attempts >= self.max_attempts:
            return "escalate"
        return "retry"

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts
