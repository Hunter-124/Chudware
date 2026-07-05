"""ladder_validate.py — the deterministic inter-rung validators (T-W25).

Contract: contracts/advisor.md §22 (aicad.ladder/1 — FROZEN). Each validator is
LLM-free and deterministic; a non-empty list of blocker findings is a BOUNCE that
returns to the SAME rung (the ladder executor enforces the max-2-bounce rule).

Findings are ``aicad.advisory/1`` items (same shape the buildgraph validator emits,
§3): ``{schema, seq, stage, items:[{id, severity, check, finding, refs, ...}]}``.
``severity`` is "blocker" for a hard bounce and "hint"/"action" for advisory-only.

The §22 rules implemented, per rung:

  A. function_map
     - every acceptance criterion served by >=1 function (coverage)
     - omissions_check covers all 8 standard classes (the forcing function — a
       manifest without the full checklist is INVALID)
     - every feature serves >=1 function; every function realized by >=1 feature

  B. geometry_plan
     - every FT covered by >=1 element (coverage)
     - every element realizes >=1 FT (no orphan geometry)
     - every conflict carries a resolution before rung D

  C. linkage_map
     - no circular links (walk the link graph)
     - link relations parse as arithmetic over named dims
     - every driving dim has an owner + basis (derived_from)
     - every datum used >=1 time

  D. build_pathway
     - every element built exactly once (build-once)
     - datums/faces exist before use (exists-before-use — walk the sequence)
     (the lossless-compile check is in ladder_compile.py's round-trip test)
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

STAGE = "design_ladder"

# The 8 standard omission classes §22 mandates (rung A). Matched case-insensitively
# and tolerant of "location/alignment" vs "location", "manufacturability aids" etc.
OMISSION_CLASSES = (
    "mounting",
    "location/alignment",
    "retention",
    "sealing",
    "venting",
    "clearance/service-access",
    "strength/stiffening",
    "manufacturability aids",
)

# Names allowed in a relation expression that are NOT dimension references (builtins).
_EXPR_BUILTINS = frozenset({"min", "max", "abs", "round", "floor", "ceil", "sqrt"})
# A relation is parseable-arithmetic if it contains only identifiers, numbers, and
# arithmetic operators / parens / dots / commas. Mirrors derivation._validate_expr.
_EXPR_SAFE = re.compile(r"^[A-Za-z0-9_+\-*/().,\s=<>]*$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


# --------------------------------------------------------------------------- finding builders

_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _finding(check: str, finding: str, severity: str = "blocker",
             refs: dict | None = None) -> dict[str, Any]:
    """Build an aicad.advisory/1 item (single-item advisory, §3 shape)."""
    return {
        "schema": "aicad.advisory/1",
        "seq": _next_seq(),
        "stage": STAGE,
        "items": [{
            "id": f"adv_{uuid.uuid4().hex[:8]}",
            "severity": severity,
            "check": check,
            "finding": finding,
            "refs": refs or {},
            "numbers": {},
            "candidate_fixes": [],
            "helper_tools": [],
            "source": {"kind": "calc", "detail": "ladder_validator"},
        }],
    }


def blockers(findings: Iterable[dict]) -> list[dict]:
    """The subset of findings that force a bounce (severity == 'blocker')."""
    return [f for f in findings
            if any(i.get("severity") == "blocker" for i in f.get("items", []))]


# --------------------------------------------------------------------------- rung A

def validate_function_map(fm: dict[str, Any],
                          acceptance_criteria: list[str] | None = None) -> list[dict]:
    """§22 rung A validator."""
    out: list[dict] = []
    functions = fm.get("functions") or []
    features = fm.get("features") or []
    omissions = fm.get("omissions_check") or []

    # -- omissions_check completeness (the forcing function) ------------------
    covered = set()
    for entry in omissions:
        cls = str(entry.get("class", "")).strip().lower()
        for canonical in OMISSION_CLASSES:
            if cls == canonical or cls.split("/")[0] == canonical.split("/")[0]:
                covered.add(canonical)
        # 'yes' entries must point at an FT id; 'no' entries must carry a why
        considered = str(entry.get("considered", "")).strip().lower()
        if considered == "yes" and not entry.get("ft_refs"):
            out.append(_finding(
                "omission_yes_no_ft",
                f"omission class {entry.get('class')!r} considered 'yes' but names no FT.",
                refs={"class": entry.get("class")}))
        if considered == "no" and not str(entry.get("why", "")).strip():
            out.append(_finding(
                "omission_no_no_why",
                f"omission class {entry.get('class')!r} answered 'no' without a why.",
                refs={"class": entry.get("class")}))
    missing = [c for c in OMISSION_CLASSES if c not in covered]
    if missing:
        out.append(_finding(
            "omissions_incomplete",
            f"omissions_check is missing {len(missing)} of the 8 standard classes: "
            f"{missing}. The full checklist is mandatory (§22 rung A).",
            refs={"missing_classes": missing}))

    # -- AC coverage: every acceptance criterion served by >=1 function -------
    if acceptance_criteria:
        served: set[str] = set()
        for fn in functions:
            for ac in fn.get("driven_by", []) or []:
                served.add(str(ac))
        unserved = [ac for ac in acceptance_criteria if ac not in served]
        if unserved:
            out.append(_finding(
                "ac_uncovered",
                f"acceptance criteria with no function: {unserved}.",
                refs={"unserved": unserved}))

    # -- every function realized by >=1 feature; every feature serves >=1 fn --
    fn_ids = {f.get("id") for f in functions}
    realized: set[str] = set()
    for ft in features:
        serves = ft.get("serves", []) or []
        if not serves:
            out.append(_finding(
                "feature_serves_nothing",
                f"feature {ft.get('id')!r} serves no function.",
                refs={"ft": ft.get("id")}))
        for fn in serves:
            realized.add(str(fn))
    unrealized = [fn for fn in fn_ids if fn and fn not in realized]
    if unrealized:
        out.append(_finding(
            "function_unrealized",
            f"functions realized by no feature: {unrealized}.",
            refs={"functions": unrealized}))
    return out


# --------------------------------------------------------------------------- rung B

def validate_geometry_plan(gp: dict[str, Any], fm: dict[str, Any]) -> list[dict]:
    """§22 rung B validator: coverage, orphans, conflict resolutions."""
    out: list[dict] = []
    elements = gp.get("elements") or []
    ft_ids = {ft.get("id") for ft in (fm.get("features") or [])}

    # -- every element realizes >=1 FT (orphan geometry is a finding) ---------
    covered_fts: set[str] = set()
    for g in elements:
        realizes = g.get("realizes", []) or []
        if not realizes:
            out.append(_finding(
                "orphan_geometry",
                f"element {g.get('id')!r} realizes no feature (orphan geometry).",
                refs={"element": g.get("id")}))
        for ft in realizes:
            covered_fts.add(str(ft))

    # -- every FT covered by >=1 element --------------------------------------
    uncovered = [ft for ft in ft_ids if ft and ft not in covered_fts]
    if uncovered:
        out.append(_finding(
            "feature_uncovered",
            f"features realized by no geometry element: {uncovered}.",
            refs={"features": uncovered}))

    # -- conflicts must carry resolutions before rung D -----------------------
    for c in gp.get("conflicts", []) or []:
        if not str(c.get("resolution_needed", "")).strip():
            out.append(_finding(
                "conflict_unresolved",
                f"conflict {c.get('pair')!r} has no resolution — resolve before rung D.",
                refs={"pair": c.get("pair")}))
    return out


# --------------------------------------------------------------------------- rung C

def _relation_names(relation: str) -> list[str]:
    """Extract candidate dim names from a relation, excluding builtins/keywords."""
    body = relation
    if "=" in body:
        body = body.split("=", 1)[1]
    names = []
    for m in _IDENT.finditer(body):
        tok = m.group(0)
        base = tok.split(".")[0]
        if base in _EXPR_BUILTINS:
            continue
        names.append(tok)
    return names


def validate_linkage_map(lm: dict[str, Any], gp: dict[str, Any]) -> list[dict]:
    """§22 rung C validator: no circular links, parseable relations,
    every driving dim owner+basis, every datum used."""
    out: list[dict] = []
    links = lm.get("links") or []
    datums = lm.get("datums") or []
    driving = lm.get("driving_dims") or []

    # -- parseable relations --------------------------------------------------
    for link in links:
        rel = str(link.get("relation", ""))
        if not rel.strip():
            out.append(_finding(
                "relation_empty",
                f"link {link.get('from')!r} -> {link.get('to')!r} has no relation.",
                refs={"from": link.get("from")}))
            continue
        # Must read as arithmetic: only identifiers/numbers/operators, and must
        # actually contain an operator or a bare reference (not prose).
        if not _EXPR_SAFE.match(rel) or not re.search(r"[A-Za-z0-9_]", rel):
            out.append(_finding(
                "relation_unparseable",
                f"link relation {rel!r} is not parseable arithmetic over named dims.",
                refs={"from": link.get("from"), "relation": rel}))
        elif re.search(r"[A-Za-z]{4,}\s+[A-Za-z]{2,}", rel) and not re.search(r"[+\-*/]", rel):
            # prose like "make it bigger" — words with no arithmetic operator
            out.append(_finding(
                "relation_unparseable",
                f"link relation {rel!r} reads as prose, not arithmetic.",
                refs={"from": link.get("from"), "relation": rel}))

    # -- no circular links (walk the from->to graph over the 'to' target base) -
    #    Each link says `from` is DERIVED FROM the names in `relation` (its inputs).
    #    Build adjacency target-of-link -> its input names and look for a cycle.
    adj: dict[str, set[str]] = {}
    for link in links:
        frm = str(link.get("from", "")).strip()
        inputs = set(_relation_names(str(link.get("relation", ""))))
        # also include the explicit `to`
        to = str(link.get("to", "")).strip()
        if to:
            inputs.add(to)
        adj.setdefault(frm, set()).update(inputs)
    if _has_cycle(adj):
        out.append(_finding(
            "circular_link",
            "linkage_map has a circular link dependency (a dim derives from itself "
            "transitively). Break the cycle before rung D.",
            refs={"links": [l.get("from") for l in links]}))

    # -- every driving dim has owner + basis ----------------------------------
    for dd in driving:
        if not str(dd.get("owner", "")).strip():
            out.append(_finding(
                "driving_dim_no_owner",
                f"driving dim {dd.get('name')!r} has no owner (owning geometry).",
                refs={"name": dd.get("name")}))
        if not (dd.get("derived_from") or []):
            out.append(_finding(
                "driving_dim_no_basis",
                f"driving dim {dd.get('name')!r} has no derived_from basis.",
                refs={"name": dd.get("name")}))

    # -- every datum used >=1 time --------------------------------------------
    used = _datum_references(lm, gp)
    for d in datums:
        did = d.get("id")
        if did and did not in used:
            out.append(_finding(
                "datum_unused",
                f"datum {did!r} is never referenced (fewer is better; drop it or use it).",
                refs={"datum": did}))
    return out


def _datum_references(lm: dict, gp: dict) -> set[str]:
    """All datum-id references anywhere in the linkage_map + geometry plan text."""
    blob = str(lm.get("reference_tree", "")) + " " + " ".join(
        str(c) for c in (lm.get("constraint_plans") or []))
    for link in lm.get("links") or []:
        blob += " " + str(link.get("from", "")) + " " + str(link.get("to", ""))
    for dd in lm.get("driving_dims") or []:
        blob += " " + str(dd.get("owner", ""))
    used: set[str] = set()
    for d in lm.get("datums") or []:
        did = d.get("id")
        # a datum "defined_by" another datum counts as using that other datum
        for other in lm.get("datums") or []:
            if other is d:
                continue
            if other.get("id") and other["id"] in str(d.get("defined_by", "")):
                used.add(other["id"])
    for d in lm.get("datums") or []:
        did = d.get("id")
        if did and re.search(rf"\b{re.escape(did)}\b", blob):
            used.add(did)
    return used


def _has_cycle(adj: dict[str, set[str]]) -> bool:
    """DFS cycle detection over a name->deps adjacency map (only edges whose target
    is itself a key participate in a cycle)."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in adj}

    def visit(n: str) -> bool:
        color[n] = GREY
        for m in adj.get(n, ()):  # noqa: SIM118
            if m not in color:
                continue  # target is not a link source → not part of any cycle
            if color[m] == GREY:
                return True
            if color[m] == WHITE and visit(m):
                return True
        color[n] = BLACK
        return False

    return any(color[n] == WHITE and visit(n) for n in list(adj))


# --------------------------------------------------------------------------- rung D

def validate_build_pathway(bp: dict[str, Any], gp: dict[str, Any],
                           lm: dict[str, Any]) -> list[dict]:
    """§22 rung D validator: build-once + exists-before-use."""
    out: list[dict] = []
    steps = sorted(bp.get("steps") or [], key=lambda s: s.get("n", 0))
    all_elements = {g.get("id") for g in (gp.get("elements") or [])}
    known_datums = {d.get("id") for d in (lm.get("datums") or [])}

    # -- build-once: every element built exactly once -------------------------
    build_count: dict[str, int] = {}
    for s in steps:
        for e in s.get("elements", []) or []:
            build_count[e] = build_count.get(e, 0) + 1
    for e, n in build_count.items():
        if n > 1:
            out.append(_finding(
                "element_built_twice",
                f"element {e!r} is built {n} times; each element must be built exactly once.",
                refs={"element": e, "count": n}))
    never = [e for e in all_elements if e and e not in build_count]
    if never:
        out.append(_finding(
            "element_never_built",
            f"geometry elements never built by any step: {never}.",
            refs={"elements": never}))

    # -- exists-before-use: sketch_on datum/face exists by the time it's used --
    #    Walk the sequence. A sketch_on token resolves if it is: 'world', a known
    #    datum, or a face produced by an element built in an EARLIER step.
    built_so_far: set[str] = set()
    for s in steps:
        target = str(s.get("sketch_on", "")).strip()
        if target and target not in ("", "world"):
            resolved = (
                target in known_datums
                or target in built_so_far
                or any(target.startswith(f"{e}.") or target == e for e in built_so_far)
            )
            if not resolved:
                out.append(_finding(
                    "sketch_on_missing",
                    f"step {s.get('n')} sketches on {target!r} which does not exist yet "
                    f"(no such datum, and no earlier step built it).",
                    refs={"step": s.get("n"), "sketch_on": target}))
        for e in s.get("elements", []) or []:
            built_so_far.add(e)
    return out


# --------------------------------------------------------------------------- dispatch

# The validator to run after each rung, keyed by the rung name it VALIDATES.
def validate_rung(rung: str, artifact: dict[str, Any], *,
                  prior: dict[str, dict] | None = None,
                  acceptance_criteria: list[str] | None = None) -> list[dict]:
    """Run the validator for ``rung`` against its ``artifact``, given the prior
    artifacts it needs (geometry_plan/linkage_map for D, etc.)."""
    prior = prior or {}
    if rung == "function_map":
        return validate_function_map(artifact, acceptance_criteria=acceptance_criteria)
    if rung == "geometry_plan":
        return validate_geometry_plan(artifact, prior.get("function_map", {}))
    if rung == "linkage_map":
        return validate_linkage_map(artifact, prior.get("geometry_plan", {}))
    if rung == "build_pathway":
        return validate_build_pathway(artifact, prior.get("geometry_plan", {}),
                                      prior.get("linkage_map", {}))
    return []
