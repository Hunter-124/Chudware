"""pipeline/linkage_integrity.py — Stage-3 linkage checkers.

Three checkers for the LINKAGE phase (docs/design/AGENT_DESIGN_PIPELINE.md §2 Stage 3,
contract design_pipeline.md §3.3):

  check_reference_integrity   — no dangling refs, no cycles in reference_edges
  check_datum_completeness    — every datum_ref in geometry_specs resolves to a
                                 datum in datum_tree
  check_interface_consistency — two geometry_specs that share an interface on the
                                 same named dim agree on value AND tolerance

Each returns a dataclass (never raises) and is consumed by
``gate_context_from_ledger`` which builds the dict that ``_gate_linkage`` in
phases.py extends without re-signing its interface (design_pipeline.md §5 rule).

The gate's presence check is intentionally separate: these checkers only layer
*diagnostic detail* on top of a linkage that already exists.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 1. Reference-integrity checker
# ---------------------------------------------------------------------------

@dataclass
class ReferenceIntegrityResult:
    """Outcome of ``check_reference_integrity``.

    ``passed``          — True when no dangling references AND no cycles.
    ``dangling_refs``   — list of "{from}→{to}" edges where ``to`` is not a
                          known geometry_spec id or datum id.
    ``cycles``          — list of cycle descriptions, each a string of the form
                          "G1 → G2 → G1" identifying the cycle.
    """
    passed: bool
    dangling_refs: list[str] = field(default_factory=list)
    cycles:        list[str] = field(default_factory=list)


def check_reference_integrity(
    linkage:       dict[str, Any],
    geometry_specs: list[dict[str, Any]],
) -> ReferenceIntegrityResult:
    """Check that every reference_edge endpoint resolves and the graph has no cycles.

    Args:
        linkage:        A single linkage dict (``state.linkages[i]``).  Must have:
                          ``reference_edges`` — list of {from, to, relation, why}
                          ``datum_tree``       — list of {id, kind, defined_by, why}
        geometry_specs: List of geometry_spec dicts from ``state.geometry_specs``.
                        Each must have at minimum ``id``.

    Returns:
        ``ReferenceIntegrityResult`` — never raises.
    """
    # Build the set of valid endpoint ids: geometry spec ids + datum ids.
    spec_ids: set[str] = {s.get("id", "") for s in geometry_specs if s.get("id")}
    datum_ids: set[str] = {
        d.get("id", "") for d in (linkage.get("datum_tree") or []) if d.get("id")
    }
    valid_ids = spec_ids | datum_ids

    edges = linkage.get("reference_edges") or []
    dangling: list[str] = []
    # Adjacency list for cycle detection (only edges between known nodes).
    adj: dict[str, list[str]] = defaultdict(list)

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            dangling.append(
                f"reference_edges[{index}] must be an object with string endpoints")
            continue

        raw_src = edge.get("from")
        raw_dst = edge.get("to")
        invalid = False
        if not isinstance(raw_src, str) or not raw_src.strip():
            dangling.append(
                f"reference_edges[{index}].from must be a non-empty string")
            invalid = True
        if not isinstance(raw_dst, str) or not raw_dst.strip():
            dangling.append(
                f"reference_edges[{index}].to must be a non-empty string")
            invalid = True
        if invalid:
            continue

        src = raw_src.strip()
        dst = raw_dst.strip()
        label = f"{src}→{dst}"
        has_dangling = False
        if src not in valid_ids:
            dangling.append(label)
            has_dangling = True
        if dst not in valid_ids:
            if label not in dangling:
                dangling.append(label)
            has_dangling = True
        if not has_dangling:
            adj[src].append(dst)

    # Cycle detection via iterative DFS.
    cycles: list[str] = []
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def _dfs(node: str, path: list[str]) -> None:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for neighbour in adj.get(node, []):
            if neighbour not in visited:
                _dfs(neighbour, path)
            elif neighbour in rec_stack:
                # Found a back-edge — extract the cycle portion.
                cycle_start = path.index(neighbour)
                cycle_path = path[cycle_start:] + [neighbour]
                cycle_str = " → ".join(cycle_path)
                if cycle_str not in cycles:
                    cycles.append(cycle_str)
        path.pop()
        rec_stack.discard(node)

    all_nodes: set[str] = set(adj.keys()) | {n for ns in adj.values() for n in ns}
    for node in all_nodes:
        if node not in visited:
            _dfs(node, [])

    passed = not dangling and not cycles
    return ReferenceIntegrityResult(passed=passed, dangling_refs=dangling, cycles=cycles)


# ---------------------------------------------------------------------------
# 2. Datum-completeness checker
# ---------------------------------------------------------------------------

@dataclass
class DatumCompletenessResult:
    """Outcome of ``check_datum_completeness``.

    ``passed``          — True when every datum_ref in every geometry_spec resolves
                          to an entry in the linkage's datum_tree.
    ``unresolved_refs`` — list of "{spec_id}→{datum_id}" for each reference that
                          does not resolve.
    ``unused_datums``   — list of datum ids declared in datum_tree but never
                          referenced by any geometry_spec (informational — not a
                          failure, but surfaces orphan declarations for cleanup).
    """
    passed: bool
    unresolved_refs: list[str] = field(default_factory=list)
    unused_datums:   list[str] = field(default_factory=list)


def check_datum_completeness(
    linkage:       dict[str, Any],
    geometry_specs: list[dict[str, Any]],
) -> DatumCompletenessResult:
    """Check that every datum_ref in geometry_specs resolves to the datum_tree.

    Args:
        linkage:        A single linkage dict with ``datum_tree``.
        geometry_specs: List of geometry_spec dicts; each may carry ``datum_refs``.

    Returns:
        ``DatumCompletenessResult`` — never raises.
    """
    datum_ids: set[str] = {
        d.get("id", "") for d in (linkage.get("datum_tree") or []) if d.get("id")
    }
    referenced_datums: set[str] = set()
    unresolved: list[str] = []

    for spec in geometry_specs:
        spec_id = spec.get("id") or "<unknown>"
        for dr in (spec.get("datum_refs") or []):
            if dr:
                referenced_datums.add(dr)
                if dr not in datum_ids:
                    unresolved.append(f"{spec_id}→{dr}")

    unused: list[str] = sorted(datum_ids - referenced_datums)
    passed = not unresolved
    return DatumCompletenessResult(
        passed=passed,
        unresolved_refs=unresolved,
        unused_datums=unused,
    )


# ---------------------------------------------------------------------------
# 3. Interface-consistency checker
# ---------------------------------------------------------------------------

@dataclass
class InterfaceConsistencyResult:
    """Outcome of ``check_interface_consistency``.

    ``passed``      — True when every shared_interface has all participating
                      geometry_specs agreeing on the named dimension value AND
                      tolerance.
    ``mismatches``  — list of human-readable mismatch descriptions, one per
                      conflicting interface dim.
    """
    passed: bool
    mismatches: list[str] = field(default_factory=list)


def check_interface_consistency(
    linkage:       dict[str, Any],
    geometry_specs: list[dict[str, Any]],
) -> InterfaceConsistencyResult:
    """Check that geometry_specs sharing an interface agree on dimension+tolerance.

    For each entry in ``linkage.shared_interfaces`` the checker:
      1. Identifies all geometry_specs that realize the features listed in
         ``interface.features`` (or, if the interface references a spec id directly
         via ``on`` like "G1.bore_d", uses that spec).
      2. Finds the named dim (the part after the dot in ``on``, e.g. "bore_d").
      3. Compares nominal value and tolerance across all specs — they must match.

    Args:
        linkage:        A single linkage dict with ``shared_interfaces``.
        geometry_specs: List of geometry_spec dicts; each has ``id``, ``realizes``,
                        ``nominal_dims``, ``tolerances``.

    Returns:
        ``InterfaceConsistencyResult`` — never raises.
    """
    # Index geometry_specs by id and by feature-id they realize.
    spec_by_id: dict[str, dict] = {s.get("id", ""): s for s in geometry_specs if s.get("id")}
    spec_by_feature: dict[str, list[dict]] = defaultdict(list)
    for spec in geometry_specs:
        for ft_id in (spec.get("realizes") or []):
            if ft_id:
                spec_by_feature[ft_id].append(spec)

    mismatches: list[str] = []

    for iface in (linkage.get("shared_interfaces") or []):
        on_field = iface.get("on") or ""
        # Parse "G1.bore_d" → spec_ref="G1", dim_name="bore_d"
        # or just "bore_d" (no dot) → resolve via feature list.
        if "." in on_field:
            spec_ref, dim_name = on_field.split(".", 1)
        else:
            spec_ref = ""
            dim_name = on_field

        if not dim_name:
            continue

        # Collect candidate specs: from explicit spec_ref OR from features list.
        candidate_specs: list[dict] = []
        if spec_ref and spec_ref in spec_by_id:
            candidate_specs.append(spec_by_id[spec_ref])
        for ft_id in (iface.get("features") or []):
            for s in spec_by_feature.get(ft_id, []):
                if s not in candidate_specs:
                    candidate_specs.append(s)

        if len(candidate_specs) < 2:
            # Cannot compare a single spec — not a failure.
            continue

        # Extract (nominal_value, tolerance_upper, tolerance_lower) for dim_name
        # from each candidate spec.
        def _dim_info(spec: dict, name: str) -> tuple[float | None, float | None, float | None]:
            for d in (spec.get("nominal_dims") or []):
                if d.get("name") == name:
                    val = d.get("value")
                    # Find matching tolerance entry.
                    for t in (spec.get("tolerances") or []):
                        if t.get("name") == name:
                            return val, t.get("upper"), t.get("lower")
                    return val, None, None
            return None, None, None

        infos = [(_dim_info(s, dim_name), s.get("id")) for s in candidate_specs]
        # Filter to specs that actually declare this dim.
        declared = [(info, sid) for info, sid in infos if info[0] is not None]

        if len(declared) < 2:
            continue

        # Compare all declared infos against the first.
        ref_info, ref_sid = declared[0]
        for info, sid in declared[1:]:
            if info != ref_info:
                mismatches.append(
                    f"interface '{on_field}' mismatch between {ref_sid} and {sid}: "
                    f"{ref_sid} has ({ref_info[0]}, +{ref_info[1]}/{ref_info[2]}) "
                    f"but {sid} has ({info[0]}, +{info[1]}/{info[2]})"
                )

    passed = not mismatches
    return InterfaceConsistencyResult(passed=passed, mismatches=mismatches)


# ---------------------------------------------------------------------------
# Gate context builder (consumed by _gate_linkage via extend-not-sign rule)
# ---------------------------------------------------------------------------

def gate_context_from_ledger(ledger_state: dict[str, Any]) -> dict[str, Any]:
    """Build the LINKAGE-gate context dict from a materialised ledger state.

    The ``_gate_linkage`` in phases.py reads pre-computed keys from the context
    dict.  Call this helper to build the dict:

        ctx = gate_context_from_ledger(ledger.materialize())
        result = machine.run_gate(Phase.LINKAGE, ctx)

    Keys set:
        ``linkages`` / ``geometry_specs`` — the source artifact lists
        ``*_results`` — checker results for every linkage
        ``ref_integrity`` / ``datum_completeness`` /
        ``interface_consistency`` — first-linkage compatibility views
        ``dangling_refs`` / ``ref_cycles`` / ``datum_gaps`` /
        ``interface_mismatches`` — aggregate findings, each prefixed by linkage id
    """
    linkages:       list[dict] = ledger_state.get("linkages") or []
    geometry_specs: list[dict] = ledger_state.get("geometry_specs") or []

    ctx: dict[str, Any] = {
        "linkages":       linkages,
        "geometry_specs": geometry_specs,
    }

    if not linkages:
        return ctx

    ri_results: list[ReferenceIntegrityResult] = []
    dc_results: list[DatumCompletenessResult] = []
    ic_results: list[InterfaceConsistencyResult] = []
    dangling_refs: list[str] = []
    ref_cycles: list[str] = []
    datum_gaps: list[str] = []
    interface_mismatches: list[str] = []

    for index, linkage in enumerate(linkages):
        linkage_id = (
            str(linkage.get("id") or f"linkage[{index}]")
            if isinstance(linkage, dict)
            else f"linkage[{index}]"
        )
        prefix = f"{linkage_id}: "
        if not isinstance(linkage, dict):
            dangling_refs.append(prefix + "linkage artifact must be an object")
            continue

        ri = check_reference_integrity(linkage, geometry_specs)
        dc = check_datum_completeness(linkage, geometry_specs)
        ic = check_interface_consistency(linkage, geometry_specs)
        ri_results.append(ri)
        dc_results.append(dc)
        ic_results.append(ic)
        dangling_refs.extend(prefix + finding for finding in ri.dangling_refs)
        ref_cycles.extend(prefix + finding for finding in ri.cycles)
        datum_gaps.extend(prefix + finding for finding in dc.unresolved_refs)
        interface_mismatches.extend(prefix + finding for finding in ic.mismatches)

    ctx["ref_integrity_results"] = ri_results
    ctx["datum_completeness_results"] = dc_results
    ctx["interface_consistency_results"] = ic_results
    if ri_results:
        ctx["ref_integrity"] = ri_results[0]
        ctx["datum_completeness"] = dc_results[0]
        ctx["interface_consistency"] = ic_results[0]

    ctx["dangling_refs"] = dangling_refs
    ctx["ref_cycles"] = ref_cycles
    ctx["datum_gaps"] = datum_gaps
    ctx["interface_mismatches"] = interface_mismatches

    return ctx
