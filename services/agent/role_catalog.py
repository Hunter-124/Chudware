"""Compact catalog of canonical roles available for delegation."""
from __future__ import annotations
from typing import Iterable


# Roles the main agent should prefer for common work (injected into harness blurb).
PRIMARY_BUILD_ROLES = ("model3d", "sketch2d", "feature_modeler", "sketcher", "reviewer", "advisor", "memory")

# Common direct-work specialists. Other loaded cards remain valid delegate roles,
# but pipeline orchestration and internal stages need not occupy every tool schema.
DELEGATE_SPECIALISTS = (
    "model3d", "sketch2d", "feature_modeler", "sketcher", "reviewer",
    "assembly_fit_engineer", "sim_engineer", "drawing_detailer", "cam_programmer",
    "ecad_engineer", "pcb_engineer", "firmware_engineer", "sourcing_specialist",
    "re_modeler", "re_datasheet_reader", "re_geometry_verifier", "project_organizer",
)


def format_delegate_specialists() -> str:
    """Summarize common direct-work roles using their live card goals."""
    from services.agent.roles import load_all_roles

    cards = load_all_roles()
    stopwords = {"a", "an", "the", "and", "or", "of", "to", "for", "with", "into", "on", "by"}
    lines = ["Common specialists (other role cards also accepted):"]
    for rid in DELEGATE_SPECIALISTS:
        if rid in cards:
            words = [
                word for word in (cards[rid].goal or "").split()
                if word.strip(".,;:—-") and word.strip(".,;:—-").lower() not in stopwords
            ][:5]
            lines.append(f"{rid}: {' '.join(words).rstrip('.,;:—-')}")
    return "\n".join(lines)




def format_role_catalog_block(
    *,
    role_ids: Iterable[str] | None = None,
    max_roles: int | None = 28,
    include_header: bool = True,
) -> str:
    """Compact catalog for system prompts — id + one-line when-to-use."""
    from services.agent.roles import load_all_roles

    cards = load_all_roles()
    if role_ids is not None:
        ids = [str(r).strip() for r in role_ids if str(r).strip() in cards]
    else:
        pri = [r for r in PRIMARY_BUILD_ROLES if r in cards]
        ids = pri + sorted(r for r in cards if r not in pri)
    lines: list[str] = []
    if include_header:
        lines.append("## Available roles (delegate)")
        lines.append("Use delegate(role=..., brief=..., budget_tokens=...) for an independent specialist.")
        lines.append("Catalog:")
    n = 0
    for rid in ids:
        if max_roles is not None and n >= max_roles:
            lines.append(f"- … ({len(ids) - n} more via list_editable_prompts / roles)")
            break
        brief = " ".join((cards[rid].goal or "").split())
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
