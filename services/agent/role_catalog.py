"""Compact catalog of canonical roles available for delegation."""
from __future__ import annotations
from typing import Iterable

# Brief one-liners for the system prompt (id → when to use).
# Keep short; full cards live in services/agent/roles/*.yaml.
ROLE_BRIEFS: dict[str, str] = {
    "model3d": "3D feature planner/builder — owns solids; delegates sketches to sketch2d; vision-checks placement.",
    "sketch2d": "2D only — closed constrained sketches, ECAD 2D, drawing profiles. Never builds solids.",
    "sketcher": "Pipeline sketcher — DOF-0 closed profiles driven by ledger dimensions.",
    "feature_modeler": "Build solids feature-by-feature in buildgraph order with verify_battery.",
    "feature_architect": "Enumerate functions as feature[] ledger events before geometry exists.",
    "geometry_designer": "Realize ONE feature cluster as concrete geometry with provenance.",
    "intake_analyst": "Break the request into a measurable deliverable manifest.",
    "clarifier": "One batch of ≤4 typed questions for blocking ambiguities.",
    "spec_writer": "Turn one deliverable into a full engineering spec.",
    "orchestrator": "Mission graphs, role/model assignments, dispatch.",
    "layout_engineer": "Assembly envelopes, interface planes, clearance budgets.",
    "linkage_planner": "Datum schemes, reference chains, shared interfaces.",
    "assembly_fit_engineer": "Mates, fits, clearances vs layout budget.",
    "sim_engineer": "Set up and interpret sims that prove the spec.",
    "drawing_detailer": "2D drawings — views, dims, GD&T, title block, export.",
    "cam_programmer": "Table-driven CAM with simulated-clean toolpaths.",
    "reviewer": "Prove/refute acceptance criteria with named tool runs.",
    "fixer": "One failure card → one candidate fix per attempt.",
    "overseer": "Audit plans/reasoning; route change requests.",
    "advisor": "Short steering notes; does not do the work.",
    "ecad_engineer": "Schematic capture, ERC/SPICE, netlist/BOM/plot; hands sch_doc_id to pcb_engineer.",
    "pcb_engineer": "Schematic handoff to routed, zone-filled, DRC-clean PCB + fabrication artifacts.",
    "firmware_engineer": "Firmware project + ELF / sensor-loop sim.",
    "sourcing_specialist": "Ranked purchase candidates with verified specs.",
    "memory": "Distill project decisions/notes into durable memory (never invents geometry).",
    "re_modeler": "Rebuild STEP/mesh into parametric features.",
    "re_datasheet_reader": "Extract dims/tolerances/materials from datasheets.",
    "re_geometry_verifier": "Certify rebuilt part vs source solid.",
    "department_manager": "Run one department of workers.",
    "design_thinker": "One rung of the design cognition ladder.",
}

# Roles the main agent should prefer for common work (injected into harness blurb).
PRIMARY_BUILD_ROLES = ("model3d", "sketch2d", "feature_modeler", "sketcher", "reviewer", "advisor", "memory")




def format_role_catalog_block(
    *,
    role_ids: Iterable[str] | None = None,
    max_roles: int = 28,
    include_header: bool = True,
) -> str:
    """Compact catalog for system prompts — id + one-line when-to-use."""
    ids: list[str] = []
    if role_ids is not None:
        ids = [str(r).strip() for r in role_ids if str(r).strip()]
    else:
        # Prefer live YAML cards; fall back to static briefs.
        try:
            from services.agent.roles import load_all_roles  # noqa: PLC0415
            cards = load_all_roles()
            # Prioritize primary build roles, then alpha
            pri = [r for r in PRIMARY_BUILD_ROLES if r in cards]
            rest = sorted(r for r in cards if r not in pri)
            ids = pri + rest
        except Exception:  # noqa: BLE001
            ids = list(PRIMARY_BUILD_ROLES) + sorted(
                k for k in ROLE_BRIEFS if k not in PRIMARY_BUILD_ROLES
            )
    lines: list[str] = []
    if include_header:
        lines.append("## Available roles (delegate)")
        lines.append(
            "Use delegate(role=..., brief=...) for an independent specialist. "
            "Prefer model3d for 3D parts (it calls sketch2d for profiles); use "
            "sketch2d alone for pure 2D/ECAD drawings."
        )
        lines.append("Catalog:")
    n = 0
    for rid in ids:
        if n >= max_roles:
            lines.append(f"- … ({len(ids) - n} more via list_editable_prompts / roles)")
            break
        brief = ROLE_BRIEFS.get(rid)
        if not brief:
            try:
                from services.agent.roles import load_role  # noqa: PLC0415
                card = load_role(rid)
                brief = (card.goal or "").strip().split("\n")[0]
                if len(brief) > 120:
                    # Truncate at a word boundary — never mid-word.
                    brief = brief[:120].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"
            except Exception:  # noqa: BLE001
                brief = "specialist role"
        lines.append(f"- **{rid}**: {brief}")
        n += 1
    return "\n".join(lines)


def harness_context_block() -> str:
    """Big-picture harness facts for the main system prompt."""
    return """## Chudware harness (how this program works)
- Kernel: OpenCASCADE B-rep via the CAD engine; geometry is parametric (features + sketches), not meshes.
- Units: millimeters unless the user says otherwise.
- Tools: MCP tool families (cad, ecad, cam, sim, plm, …). Call real tools — never invent measurements.
- Verify-before-done: after mutations run describe_part / measure / verify_battery / check_sketch as appropriate.
- Project memory: durable notes under the PLM project `.plm/memory.db` (token-budgeted recall each turn).
- Roles: specialized YAML cards (see the role catalog). `delegate(role, brief, budget_tokens)` spawns a subagent — nesting capped by agent.max_subagent_depth.
- Ledger / mission: dimensions and decisions go to the ledger with provenance — never recall a number without a driver.
- Docs: user/GUI-owned docs are sacred; agent-minted docs can be cleaned up. Prefer editing the active part.
"""
