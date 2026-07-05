"""mission_template.py — turn a validated deliverable manifest into an
``aicad.dag/1`` mission graph whose nodes carry role assignments (T-W19).

The mission DAG is the executable form of the phase sequence
(docs/COPILOT_MISSION_PIPELINE.md §1).  Each phase becomes one or more DAG nodes;
each node names the ROLE that runs it.  Role system prompts are loaded VERBATIM
from ``services/agent/roles/*.yaml`` via the T-W7 loader — never inlined in code
(contract rule; docs §5).  The node's ``inputs.role`` + ``inputs.role_prompt``
carry the card to the node runner, which applies the role's tool allowlist.

Phase → node mapping:
  * INTAKE   → one ``plan`` node, role ``intake_analyst`` (produces the manifest).
  * CLARIFY  → one ``plan`` node, role ``clarifier`` (only if blocking ambiguity).
  * SPECIFY  → one ``design`` node per deliverable, role ``spec_writer``.
  * ORGANIZE → one ``plan`` node, role ``department_manager`` (feature-flagged).
  * PLAN     → one ``plan`` node, role ``orchestrator`` (build graphs) — for
               assemblies a preceding ``design`` node, role ``layout_engineer``.
  * BUILD    → one ``design`` node per part deliverable, role by kind
               (part → ``feature_modeler``; assembly → ``assembly_fit_engineer``).
               These fan out — they only depend on their SPECIFY node (and the
               PLAN node), so ready-set execution runs them in parallel when
               multi-doc leases are available (T-W16), sequentially otherwise.
  * VERIFY   → one ``verify`` node per deliverable, role ``reviewer``.
  * DELIVER  → one ``report`` node per deliverable by kind, role by kind
               (cam_program → ``cam_programmer``; else ``reviewer``/report).

The graph is validated against ``aicad.dag/1`` before return (raises on invalid).
Fan-out at Phase 4 is expressed structurally: BUILD nodes for independent parts
share no forward edge, so the executor's ready-set schedules them together.
"""
from __future__ import annotations

from typing import Any, Optional

from .graph_loader import validate_graph

# aicad.dag/1 node types (graph_loader.VALID_NODE_TYPES).  We map mission roles onto
# the closest structural type.
_TYPE_INTAKE = "plan"
_TYPE_CLARIFY = "plan"
_TYPE_SPEC = "design"
_TYPE_ORGANIZE = "plan"
_TYPE_LAYOUT = "design"
_TYPE_PLAN = "plan"
_TYPE_BUILD = "design"
_TYPE_VERIFY = "verify"
_TYPE_DELIVER = "report"

# Role assignment by deliverable kind (BUILD + DELIVER phases).
_BUILD_ROLE_BY_KIND = {
    "part": "feature_modeler",
    "assembly": "assembly_fit_engineer",
    "drawing": "feature_modeler",       # drawing build handled at deliver
    "cam_program": "feature_modeler",
    "analysis": "sim_engineer",
    "report": "reviewer",
    "modification": "feature_modeler",
}
_DELIVER_ROLE_BY_KIND = {
    "cam_program": "cam_programmer",
    "drawing": "reviewer",
    "analysis": "sim_engineer",
    "report": "reviewer",
}


def _role_prompt(role_id: str) -> str:
    """Load a role card's system prompt VERBATIM (T-W7 loader).  Best-effort: a
    missing card leaves the prompt empty so the node runner falls back to the
    default — the mission still runs (a role prompt is an enhancement, not a hard
    dependency of the DAG shape)."""
    try:
        from services.agent.roles import load_role
        return load_role(role_id).system_prompt
    except Exception:  # noqa: BLE001
        return ""


def _node(
    node_id: str,
    node_type: str,
    name: str,
    role: str,
    *,
    inputs: Optional[dict[str, Any]] = None,
    on_fail: str = "pause",
    autonomy: str = "auto",
) -> dict[str, Any]:
    """Build one aicad.dag/1 node with its role assignment carried in ``inputs``.

    The role's system prompt is loaded verbatim and placed in ``inputs.role_prompt``
    so the node runner (services/dag/node_runner) can scope the turn to the role
    without re-reading the card.  ``inputs.role`` is the id (the runner applies the
    tool allowlist from it)."""
    node_inputs: dict[str, Any] = dict(inputs or {})
    node_inputs["role"] = role
    node_inputs["role_prompt"] = _role_prompt(role)
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "inputs": node_inputs,
        "on_fail": on_fail,
        "autonomy": autonomy,
    }


def build_mission_graph(
    manifest: dict[str, Any],
    *,
    graph_id: str = "mission",
    is_assembly: Optional[bool] = None,
    org_layer: bool = False,
    default_autonomy: str = "auto",
) -> dict[str, Any]:
    """Compile a validated deliverable manifest into an ``aicad.dag/1`` mission graph.

    Args:
        manifest:        an ``aicad.deliverables/1`` manifest (already validated by
                         ``services.agent.pipeline.manifest.validate_manifest`` —
                         this function does NOT re-validate the manifest, it maps
                         it).
        graph_id:        the DAG id.
        is_assembly:     force the assembly path (layout_engineer + assembly build).
                         When ``None``, inferred from the presence of an
                         ``assembly``-kind deliverable.
        org_layer:       insert the ORGANIZE (department_manager) node (Phase 2.5,
                         feature-flagged on T-W22/T-W23).
        default_autonomy: node autonomy floor ("auto" | "approve" | "manual").

    Returns:
        An ``aicad.dag/1`` graph dict (validated).  Raises ``GraphValidationError``
        if the assembled graph is malformed (a bug in this mapper, not the manifest).
    """
    deliverables = manifest.get("deliverables") or []
    has_blocking = any(
        isinstance(a, dict) and a.get("blocking")
        for a in manifest.get("ambiguities", []) or []
    )
    if is_assembly is None:
        is_assembly = any(d.get("kind") == "assembly" for d in deliverables)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # --- Phase 0: INTAKE ---------------------------------------------------
    intake_id = "n_intake"
    nodes.append(_node(intake_id, _TYPE_INTAKE, "INTAKE: build manifest",
                       "intake_analyst", autonomy=default_autonomy))
    tail = intake_id

    # --- Phase 1: CLARIFY (only when a blocking ambiguity exists) ----------
    if has_blocking:
        clarify_id = "n_clarify"
        nodes.append(_node(clarify_id, _TYPE_CLARIFY, "CLARIFY: resolve ambiguities",
                           "clarifier", on_fail="pause", autonomy=default_autonomy))
        edges.append({"from": tail, "to": clarify_id})
        tail = clarify_id

    # --- Phase 2: SPECIFY (one node per deliverable) -----------------------
    spec_ids: dict[str, str] = {}
    for d in deliverables:
        did = d["id"]
        sid = f"n_spec_{did}"
        spec_ids[did] = sid
        nodes.append(_node(sid, _TYPE_SPEC, f"SPECIFY {did}: {d.get('name', '')}",
                           "spec_writer", inputs={"deliverable": did},
                           autonomy=default_autonomy))
        edges.append({"from": tail, "to": sid})
    # A join point so the following phases depend on ALL specs.  It is a plan-type
    # node (role overseer) rather than a DAG 'gate' node: the SPECIFY exit gate is
    # enforced by the pipeline PhaseMachine (deterministic), and the overseer AUDIT
    # is a feature-flagged trigger (T-W13) — neither should hard-pause an
    # autonomous run.  The run-level autonomy_default still applies (raise the whole
    # run to approve/manual to gate every node, including this one).
    spec_join = "n_spec_done"
    nodes.append(_node(spec_join, "plan", "SPECIFY complete (overseer audit point)",
                       "overseer", on_fail="pause", autonomy=default_autonomy))
    for sid in spec_ids.values():
        edges.append({"from": sid, "to": spec_join})
    tail = spec_join

    # --- Phase 2.5: ORGANIZE (feature-flagged) -----------------------------
    if org_layer:
        org_id = "n_organize"
        nodes.append(_node(org_id, _TYPE_ORGANIZE, "ORGANIZE: instantiate departments",
                           "department_manager", autonomy=default_autonomy))
        edges.append({"from": tail, "to": org_id})
        tail = org_id

    # --- Phase 3: PLAN (layout first for assemblies, then orchestrator) ----
    if is_assembly:
        layout_id = "n_layout"
        nodes.append(_node(layout_id, _TYPE_LAYOUT, "PLAN: layout skeleton",
                           "layout_engineer", autonomy=default_autonomy))
        edges.append({"from": tail, "to": layout_id})
        tail = layout_id
    plan_id = "n_plan"
    nodes.append(_node(plan_id, _TYPE_PLAN, "PLAN: mission DAG + build graphs",
                       "orchestrator", on_fail="pause", autonomy=default_autonomy))
    edges.append({"from": tail, "to": plan_id})

    # --- Phase 4: BUILD (fan-out per buildable deliverable) ----------------
    build_ids: dict[str, str] = {}
    buildable = [d for d in deliverables
                 if d.get("kind") in ("part", "assembly", "modification")]
    for d in buildable:
        did = d["id"]
        bid = f"n_build_{did}"
        build_ids[did] = bid
        role = _BUILD_ROLE_BY_KIND.get(d.get("kind"), "feature_modeler")
        # BUILD depends on PLAN (build graphs) + this deliverable's SPEC. Independent
        # parts share no edge → the executor ready-set runs them in parallel.
        nodes.append(_node(bid, _TYPE_BUILD, f"BUILD {did}: {d.get('name', '')}",
                           role, inputs={"deliverable": did},
                           on_fail="pause", autonomy=default_autonomy))
        edges.append({"from": plan_id, "to": bid})

    # --- Phase 5: VERIFY (one per buildable deliverable, runs the ACs) -----
    verify_ids: dict[str, str] = {}
    for d in buildable:
        did = d["id"]
        vid = f"n_verify_{did}"
        verify_ids[did] = vid
        nodes.append(_node(vid, _TYPE_VERIFY, f"VERIFY {did}: run acceptance checks",
                           "reviewer", inputs={"deliverable": did},
                           on_fail="pause", autonomy=default_autonomy))
        edges.append({"from": build_ids[did], "to": vid})

    # --- Phase 7: DELIVER (one per deliverable, role by kind) --------------
    for d in deliverables:
        did = d["id"]
        kind = d.get("kind")
        del_id = f"n_deliver_{did}"
        role = _DELIVER_ROLE_BY_KIND.get(kind, "reviewer")
        nodes.append(_node(del_id, _TYPE_DELIVER, f"DELIVER {did}: {kind}",
                           role, inputs={"deliverable": did},
                           on_fail="skip", autonomy=default_autonomy))
        # Deliver depends on this deliverable's VERIFY when it was built; otherwise
        # on its SPEC (e.g. a report deliverable with no build node).
        upstream = verify_ids.get(did) or spec_ids.get(did) or plan_id
        edges.append({"from": upstream, "to": del_id})

    graph = {
        "schema": "aicad.dag/1",
        "id": graph_id,
        "nodes": nodes,
        "edges": edges,
    }
    validate_graph(graph)
    return graph
