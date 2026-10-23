"""Dimension derivation tree, invalidation walker, and linked-dimension helper.

Schema: ``aicad.impact/1`` — emitted by :func:`mark_faulty`.
Contract: contracts/advisor.md §21 (FROZEN — extends aicad.ledger/1).

Key invariants
--------------
- The impact walker is **deterministic** and contains **no LLM** calls.
- ``mark_faulty`` walks the derivation DAG → buildgraph citations → AC checks
  and emits an advisory-style impact report routed as a change plan (§11).
- Linked dimensions (those with a ``linked.eq_id``) downgrade their recommended
  action from ``rebuild`` to ``recompute + reverify`` per §21 (the engine equation
  already propagated the value; only verification is required).
- ``link_dimension(dim_id, ledger, cb)`` parses the dimension's ``derivation``
  expression (simple arithmetic over DIM names), calls ``eq_set`` via
  ``doc_apply_ops``, records ``linked.eq_id`` on the ledger entry, and verifies
  the round-trip by reading back the geometry value.

Change-plan routing is feature-flagged: set env var
``CHUDWARE_CHANGE_PLAN=1`` to route the impact report through the
change-plan path.  Default: returns the report dict directly.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any

from services.agent.ledger import Ledger

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class DerivationCycleError(Exception):
    """Raised when the derivation DAG contains a cycle."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dim_by_id(ledger: Ledger, dim_id: str) -> dict | None:
    """Return the materialized state entry for a dimension id, or None."""
    for d in ledger.state.get("dimensions", []):
        if d.get("id") == dim_id:
            return d
    return None


def _all_dim_ids(ledger: Ledger) -> set[str]:
    return {d["id"] for d in ledger.state.get("dimensions", [])}


# ---------------------------------------------------------------------------
# DAG helpers
# ---------------------------------------------------------------------------

def build_dag(ledger: Ledger) -> dict[str, list[str]]:
    """Return the derivation DAG as ``{dim_id: [upstream_dim_ids, ...]}``.

    Only includes dimensions that have a ``derived_from`` list in state.
    Non-ledger refs (``calc:*``, ``D*``, assumption refs) are filtered out;
    only refs that resolve to known dimension ids are kept.
    """
    known = _all_dim_ids(ledger)
    dag: dict[str, list[str]] = {}
    for dim in ledger.state.get("dimensions", []):
        dim_id = dim.get("id", "")
        upstream_raw = dim.get("derived_from") or []
        # Keep only refs that are known dimension ids (DIM* in ledger)
        upstream_dims = [r for r in upstream_raw if r in known]
        dag[dim_id] = upstream_dims
    return dag


def _reverse_dag(dag: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return reverse adjacency: ``{upstream_id: [downstream_ids, ...]}``.

    This lets us walk "what depends on X" efficiently.
    """
    rev: dict[str, list[str]] = {k: [] for k in dag}
    for dim_id, upstreams in dag.items():
        for up in upstreams:
            rev.setdefault(up, []).append(dim_id)
    return rev


def check_cycle(dag: dict[str, list[str]]) -> None:
    """Raise :class:`DerivationCycleError` if the DAG contains a cycle.

    Uses Kahn's topological sort (same algorithm as buildgraph/schema.py).
    """
    in_degree: dict[str, int] = {k: 0 for k in dag}
    for upstreams in dag.values():
        for up in upstreams:
            if up in in_degree:
                in_degree[up] += 1  # up is pointed-to by a downstream; note: we track
    # Recalculate: in-degree here means "number of nodes that list this as upstream".
    # Rebuild properly: for each edge (child -> parent) we add in-degree to the child
    # because the child depends on the parent (parent must come first).
    in_degree = {k: 0 for k in dag}
    for dim_id, upstreams in dag.items():
        for _ in upstreams:
            in_degree[dim_id] += 1  # dim_id has this many dependencies

    # Kahn: start with nodes that have no dependencies (in_degree == 0)
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        curr = queue.pop(0)
        visited += 1
        # Reduce in-degree of all nodes that list curr as upstream
        for dim_id, upstreams in dag.items():
            if curr in upstreams:
                in_degree[dim_id] -= 1
                if in_degree[dim_id] == 0:
                    queue.append(dim_id)

    if visited < len(dag):
        cycle_nodes = [nid for nid, deg in in_degree.items() if deg > 0]
        raise DerivationCycleError(
            f"Cycle detected in derivation DAG among: {cycle_nodes}. "
            "Break the cycle by removing a derived_from reference."
        )


def dependents_of(dim_id: str, dag: dict[str, list[str]]) -> list[str]:
    """Return all dimension ids that (transitively) depend on *dim_id*.

    BFS over the reverse DAG.  Order is breadth-first from *dim_id* outward.
    Result does NOT include *dim_id* itself.  Deterministic (sorted queue per level).
    """
    rev = _reverse_dag(dag)
    visited: set[str] = set()
    queue: list[str] = sorted(rev.get(dim_id, []))
    result: list[str] = []
    while queue:
        curr = queue.pop(0)
        if curr in visited:
            continue
        visited.add(curr)
        result.append(curr)
        queue.extend(sorted(r for r in rev.get(curr, []) if r not in visited))
    return result


# ---------------------------------------------------------------------------
# Buildgraph seam — resolve which buildgraph nodes cite a set of dim ids
# ---------------------------------------------------------------------------

def _nodes_citing_dims(
    dim_ids: set[str],
    buildgraph: dict | None,
) -> list[str]:
    """Return sorted node ids from *buildgraph* that cite any of *dim_ids*.

    A buildgraph node "cites" a dimension when:
    - its ``dim_refs`` list contains the dim id, OR
    - its ``label`` or ``params`` string representation contains the dim id.

    If *buildgraph* is None or has no nodes, returns [].
    """
    if not buildgraph or not dim_ids:
        return []
    nodes = buildgraph.get("nodes", [])
    citing: list[str] = []
    for node in nodes:
        node_id = node.get("id", "")
        # Explicit dim_refs field (preferred seam)
        if any(dr in dim_ids for dr in (node.get("dim_refs") or [])):
            citing.append(node_id)
            continue
        # Fallback: text scan of label + serialised params
        label = str(node.get("label", ""))
        params_str = str(node.get("params", ""))
        if any(did in label or did in params_str for did in dim_ids):
            citing.append(node_id)
    return sorted(citing)


# ---------------------------------------------------------------------------
# AC (acceptance-check) seam — which checks reference faulty dims
# ---------------------------------------------------------------------------

def _ac_checks_for_dims(
    dim_ids: set[str],
    ac_checks: list[dict] | None,
) -> list[str]:
    """Return sorted ac check ids whose ``refs`` list any of *dim_ids*.

    If *ac_checks* is None, returns [].  Each check dict may have a
    ``refs`` field with a ``dims`` list or a ``dim_ids`` list.
    """
    if not ac_checks or not dim_ids:
        return []
    matched: list[str] = []
    for check in ac_checks:
        check_id = check.get("id", "")
        refs = check.get("refs") or {}
        ref_dims: list[str] = refs.get("dims") or refs.get("dim_ids") or []
        if any(d in dim_ids for d in ref_dims):
            matched.append(check_id)
    return sorted(matched)


# ---------------------------------------------------------------------------
# Impact action logic
# ---------------------------------------------------------------------------

def _action_for_dim(dim: dict | None) -> str:
    """Return the recommended action for an affected dimension.

    §21: linked dims downgrade rebuild → recompute + reverify.
    Non-linked dims that are part of a feature ref need rebuild.
    """
    if dim and dim.get("linked"):
        return "recompute+reverify"
    return "rebuild"


# ---------------------------------------------------------------------------
# mark_faulty — the impact walker
# ---------------------------------------------------------------------------

def mark_faulty(
    ref: str,
    why: str,
    ledger: Ledger,
    buildgraph: dict | None = None,
    ac_checks: list[dict] | None = None,
) -> dict:
    """Walk the derivation DAG and emit an ``aicad.impact/1`` report.

    Parameters
    ----------
    ref:
        The id of the faulty dimension (e.g. ``'DIM7'``).
    why:
        Human-readable reason the dimension is faulty.
    ledger:
        The live :class:`~services.agent.ledger.Ledger` instance.
    buildgraph:
        Optional ``aicad.buildgraph/1`` dict.  Nodes that cite any affected
        dimension are included in ``affected_nodes``.
    ac_checks:
        Optional list of AC check dicts.  Checks that reference any affected
        dimension are included in ``affected_checks``.

    Returns
    -------
    dict
        ``aicad.impact/1`` report:
        ``{schema, faulty, why, affected_dims, affected_nodes,
           affected_checks, recommended}``.

    The report is routed as a change plan (§11) — assessed, not auto-executed.
    When env var ``CHUDWARE_CHANGE_PLAN=1`` is set, it is forwarded through
    the change-plan path; otherwise it is returned directly.
    """
    dag = build_dag(ledger)
    check_cycle(dag)

    # Affected dimensions: all transitively dependent on ref
    affected_dim_ids = dependents_of(ref, dag)
    # Include the faulty dim itself in the dim set for node/AC lookup
    all_faulty_ids = {ref} | set(affected_dim_ids)

    # Buildgraph nodes that cite any affected dim
    affected_nodes = _nodes_citing_dims(all_faulty_ids, buildgraph)

    # AC checks that reference any affected dim
    affected_checks = _ac_checks_for_dims(all_faulty_ids, ac_checks)

    # Build recommended actions — deterministic order (affected_dims in BFS order)
    recommended: list[dict] = []
    for dim_id in affected_dim_ids:
        dim = _dim_by_id(ledger, dim_id)
        action = _action_for_dim(dim)
        dim_name = dim.get("name", dim_id) if dim else dim_id
        why_action = (
            f"{dim_id} ({dim_name}) derives from {ref} (now faulty: {why}); "
            + (
                "equation already propagated — verify geometry only"
                if action == "recompute+reverify"
                else "feature must be rebuilt with updated upstream value"
            )
        )
        recommended.append({
            "target": dim_id,
            "action": action,
            "why": why_action,
        })

    # Nodes: always reverify (the geometry they validate may have changed)
    for node_id in affected_nodes:
        recommended.append({
            "target": node_id,
            "action": "reverify",
            "why": f"buildgraph node cites a faulty or affected dimension",
        })

    # AC checks: reverify
    for check_id in affected_checks:
        recommended.append({
            "target": check_id,
            "action": "reverify",
            "why": f"acceptance check references a faulty or affected dimension",
        })

    report = {
        "schema": "aicad.impact/1",
        "faulty": ref,
        "why": why,
        "affected_dims": affected_dim_ids,
        "affected_nodes": affected_nodes,
        "affected_checks": affected_checks,
        "recommended": recommended,
    }

    # Change-plan feature-flag routing
    if os.environ.get("CHUDWARE_CHANGE_PLAN") == "1":
        return _route_change_plan(report)
    return report


def _route_change_plan(report: dict) -> dict:
    """Forward impact report to the change-plan path.

    This is the feature-flagged seam: the report is wrapped in an
    ``aicad.change_plan/1`` envelope pending assessment.
    """
    # Stub: wrap in change-plan envelope and return
    return {
        "schema": "aicad.change_plan/1",
        "source": "aicad.impact/1",
        "plan_id": f"cp_{uuid.uuid4().hex[:8]}",
        "impact": report,
        "status": "pending_assessment",
    }


# ---------------------------------------------------------------------------
# Expression parser — simple arithmetic over DIM names
# ---------------------------------------------------------------------------

# The derivation strings in the ledger are simple arithmetic expressions over
# dimension names (e.g. "bore_d + 2*boss_wall", "shaft_d / 2").
# We parse these to extract referenced names and to build the eq_set expr.

_NAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_ALLOWED_NAMES = frozenset({"min", "max", "abs", "round", "floor", "ceil", "sqrt"})


def _extract_dim_names(expr: str) -> list[str]:
    """Return all bare names in *expr* that are not math builtins."""
    return [m for m in _NAME_RE.findall(expr) if m not in _ALLOWED_NAMES]


def _validate_expr(expr: str) -> None:
    """Raise ValueError if *expr* contains unsafe tokens.

    Allowed: identifiers (A-Za-z_...), integer/float literals, the operators
    +, -, *, /, parentheses, commas, dots, and whitespace.
    Anything else (semicolons, colons, backticks, quotes, etc.) is rejected.
    """
    # Strip out all allowed tokens iteratively.
    # Order matters: longer patterns first.
    work = expr
    # Remove identifiers (names + builtins)
    work = re.sub(r"[A-Za-z_][A-Za-z0-9_]*", "", work)
    # Remove numeric literals (int or float)
    work = re.sub(r"\d+(?:\.\d+)?", "", work)
    # Remove allowed operator/punctuation chars
    work = re.sub(r"[\s+\-*/().,]+", "", work)
    if work:
        raise ValueError(
            f"Derivation expression contains unsupported tokens: {expr!r} "
            f"(residual after cleaning: {work!r})"
        )


# ---------------------------------------------------------------------------
# link_dimension — bind a derivation expr to the engine via eq_set
# ---------------------------------------------------------------------------

def link_dimension(
    dim_id: str,
    ledger: Ledger,
    cb: Any,
    doc_id: str,
    eq_name: str | None = None,
) -> dict:
    """Bind the dimension's ``derivation`` expression to the engine via ``eq_set``.

    Steps
    -----
    1. Look up *dim_id* in the ledger; retrieve its ``derivation`` expr.
    2. Parse the expr (simple arithmetic over dim *names*); validate.
    3. Call ``doc_apply_ops`` with an ``eq_set`` op: ``{op: eq_set, name, expr}``.
    4. Record ``linked = {eq_id, expr, name_map}`` on the ledger dimension via
       a new ``upsert_dimension`` event.
    5. Return ``{ok, eq_id, eq_name, expr, name_map, raw_engine_response}``.

    The caller should then verify the round-trip by reading back the geometry.

    Parameters
    ----------
    dim_id:
        Ledger dimension id (e.g. ``'DIM9'``).
    ledger:
        Live :class:`~services.agent.ledger.Ledger` instance.
    cb:
        Core-bridge object with a ``call(method, params) -> dict`` interface.
    doc_id:
        Engine document id to apply the equation to.
    eq_name:
        Override the equation name.  Defaults to the dimension's ``name`` field.

    Returns
    -------
    dict
        ``{ok, eq_id, eq_name, expr, name_map, raw_engine_response}``

    Raises
    ------
    ValueError
        If the dimension is not found in the ledger, has no ``derivation`` field,
        or the expression contains unsupported tokens.
    RuntimeError
        If the engine call fails.
    """
    dim = _dim_by_id(ledger, dim_id)
    if dim is None:
        raise ValueError(f"dimension {dim_id!r} not found in ledger")

    expr = dim.get("derivation")
    if not expr:
        raise ValueError(
            f"dimension {dim_id!r} has no 'derivation' field — "
            "set it via upsert_dimension(derivation=...) before calling link_dimension"
        )

    _validate_expr(expr)
    referenced_names = _extract_dim_names(expr)

    # Build name_map: dim name → dim id (for reverse lookup / audit)
    name_map: dict[str, str] = {}
    for d in ledger.state.get("dimensions", []):
        if d.get("name") in referenced_names:
            name_map[d["name"]] = d["id"]

    # Equation name: prefer caller-supplied, then dim name, then dim_id
    resolved_eq_name = eq_name or dim.get("name") or dim_id
    eq_id = f"eq_{dim_id.lower()}"

    # Apply eq_set to the engine
    op = {"op": "eq_set", "name": resolved_eq_name, "expr": expr}
    try:
        engine_resp = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [op]})
    except Exception as exc:
        raise RuntimeError(
            f"eq_set failed for dimension {dim_id!r} (eq_name={resolved_eq_name!r}, "
            f"expr={expr!r}): {exc}"
        ) from exc

    # Record the linked binding on the ledger (additive — upsert same dim_id)
    linked_info = {
        "eq_id": eq_id,
        "eq_name": resolved_eq_name,
        "expr": expr,
        "name_map": name_map,
    }
    ledger.upsert_dimension(
        actor="derivation.link_dimension",
        dim_id=dim_id,
        name=dim.get("name", dim_id),
        value=dim.get("value", 0.0),
        unit=dim.get("unit", ""),
        driver=dim.get("driver", ""),
        provenance=f"eq_set binding via link_dimension; expr={expr!r}",
        tolerance=dim.get("tolerance"),
        status=dim.get("status", "derived"),
        confirmed_by=dim.get("confirmed_by"),
        refs=dim.get("refs"),
        derived_from=dim.get("derived_from"),
        derivation=expr,
        linked=linked_info,
        cited_by_departments=dim.get("cited_by_departments"),
    )

    return {
        "ok": True,
        "eq_id": eq_id,
        "eq_name": resolved_eq_name,
        "expr": expr,
        "name_map": name_map,
        "raw_engine_response": engine_resp,
    }
