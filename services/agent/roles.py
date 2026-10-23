"""roles.py — Role card + playbook loader, validator, context assembler,
handoff validator, and a JSON-schema → GBNF grammar emitter.

Contract: contracts/advisor.md §4 (role card `aicad.role/1`), §6 (playbook
`aicad.playbook/1`), §9 (injection & precedence), §1 (stage vocabulary).
Pipeline composition: docs/design/COPILOT_MISSION_PIPELINE.md.

Roles are authored data. Developer runs load ``services/agent/roles/*.yaml``;
packaged workers load their deterministic build-generated representation. Their
``system_prompt`` values are preserved and the loader validates the surrounding
mechanical fields. Playbooks in ``services/agent/playbooks/*.yaml`` are the
scaffolds referenced by ``playbook_default``.

What this module gives the hosts (services/dag node runner, services/agent loop):

  * ``load_role(id)`` / ``load_all_roles()``    → validated ``RoleCard`` objects.
  * ``load_playbook(id)`` / ``load_all_playbooks()`` → validated ``Playbook`` objects.
  * ``validate_role(card, tool_names)``          → list of mechanical findings.
  * ``RoleCard.apply_tool_policy()``             → binds the allowlist into the MCP
    tool_registry (the ONE enforcement point) so an out-of-scope call is refused.
  * ``assemble_context(card, ...)``              → the §9.1-ordered turn context
    (system prompt → ledger render → stage state → advisory → playbook step → history).
  * ``validate_handoff(card, envelope)``         → structural + constraint validation
    of a role's emitted handoff against its ``handoff_schema``.
  * ``schema_to_gbnf(schema)``                    → a GBNF grammar that constrains a
    local model's JSON output to the handoff schema's *structure* (required keys,
    enum literals, primitive types). String-length / numeric-range constraints are
    NOT expressed in the grammar (grammars can't count); ``validate_handoff`` enforces
    those post-parse.

Design notes:
  * ``$ref`` (e.g. ``aicad.deliverables/1``) has no loadable JSON-Schema file — the
    schemas live as prose examples in the contract. The GBNF emitter treats an
    unresolved ``$ref`` as an opaque JSON object node (permissive); it never crashes.
  * ``delegate`` is a real registered tool (services/mcp/delegation_tools.py).
    ``workspace_status`` is registered too. The org-layer tools (channel_send,
    request_resolve, council_convene, request_raise) are registered via
    services/mcp/org_tools.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from services.agent import protected_assets

# --------------------------------------------------------------------------- constants

ROLE_SCHEMA = "aicad.role/1"
PLAYBOOK_SCHEMA = "aicad.playbook/1"
HANDOFF_SCHEMA = "aicad.handoff/1"

# Stage vocabulary (contract §1) — frozen. Every stage_scope entry and every
# playbook.stage must be one of these. Defined here from the contract rather than
# imported from services/agent/advisor/stages.py (that module owns stage logic;
# the vocabulary itself is the frozen contract).
STAGES = (
    "intake", "spec", "decompose", "layout", "sketch", "feature", "linkage",
    "part_verify", "assembly", "sim", "cam", "deliver", "fix",
)

# Per-role tool-allowlist cap (contract §8 tier profile `max_tools_per_role`).

# Seed tool names for family tokens when the MCP catalog is empty (unit tests,
# pre-register). Kept short — real runs expand from tool_registry.catalog().
# mega-1 TOP-5: analysis/specify roles need ecad/fw/em offered on the tools_slot.
_FAMILY_SEED_TOOLS: dict[str, tuple[str, ...]] = {
    "ecad": (
        "ecad_new_schematic", "ecad_new_pcb", "ecad_find_part", "ecad_place_symbol",
        "ecad_wire", "ecad_add_power", "ecad_label", "ecad_status", "ecad_run_erc",
        "ecad_simulate", "ecad_run_drc", "ecad_make_board", "board_from_schematic",
        "ecad_autoroute", "ecad_antenna_design", "ecad_antenna_analyze",
        "ecad_export", "ecad_to_3d", "ecad_run_pdn",
    ),
    "em": (
        "em_status", "em_eigenmode", "em_driven", "em_timedomain",
        "ecad_antenna_design", "ecad_antenna_analyze",
    ),
    "fw": (
        "fw_status", "fw_new_project", "fw_build", "fw_sources", "fw_sim_run",
        "fw_verify_board", "fw_sensor_scenario", "fw_power_profile",
    ),
    "firmware": (
        "fw_status", "fw_new_project", "fw_build", "fw_sources", "fw_sim_run",
        "fw_verify_board", "fw_sensor_scenario", "fw_power_profile",
    ),
}
# Per-card ceiling on CONCRETE tools after family expansion. Two jobs: bound the
# rendered tools block, and keep the model's choice space small enough to pick
# well. Raised 14 -> 16 on 2026-08-27: 13 of the 32 cards sat EXACTLY on 14, so
# the cap had stopped bounding cards and started designing them — ecad_engineer
# was ordered by its own UNIVERSAL RULE 3 to `ledger_note` every artifact while
# holding no ledger_note seat, an unsatisfiable mandate created purely by the
# ceiling. Verified before raising: zero cards were losing tools to truncation
# at 14, so no existing grant changes; the two extra seats cost a few hundred
# tokens of schema on the cards that use them. `validate_role` reports an
# over-cap allowlist as an ERROR and test_roles.py asserts zero errors, so this
# stays a gate rather than the silent truncation it once was.
MAX_TOOLS_PER_ROLE = 16

# Allowlist tools that do not yet resolve to a registered MCP tool. Reported by
# the loader as feature-flagged rather than as errors, so the authored cards load
# cleanly.
# NOTE: ``delegate`` is no longer in this set
# (services/mcp/delegation_tools.py registers it in the catalog).
FEATURE_FLAGGED_TOOLS = frozenset({
    # delegate and workspace_status are registered now.
    # org layer tools (channel_send, request_resolve, council_convene) are
    # registered in the catalog by services/mcp/org_tools.py.
    # request_raise is also registered; it is not in department_manager's allowlist
    # but workers call it, so it must resolve.
})

_ROLES_DIR = Path(__file__).resolve().parent / "roles"

def _prompts_override_dir() -> Path:
    """User prompt overrides (same location C++ core/agent/roles.cpp uses)."""
    import os
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        root = Path(xdg) / "Chudware"
    else:
        root = Path.home() / ".local" / "share" / "Chudware"
    # Match C++ paths_root()/prompts when CHUDWARE_APPDATA set
    app = os.environ.get("CHUDWARE_APPDATA") or os.environ.get("CHUDWARE_DATA")
    if app:
        root = Path(app)
    return root / "prompts"


def _prompt_override_path(role_id: str) -> Path:
    """Native/core prompt override path consumed by C++ ``load_prompt``."""
    return _prompts_override_dir() / f"{role_id}.md"


def _mission_prompt_override_path(role_id: str) -> Path:
    """Mission/YAML prompt override path, isolated from native output contracts."""
    return _prompts_override_dir() / f"{role_id}.mission.md"


def _prompt_builtin_md_path(role_id: str) -> Path:
    """Developer-source path for a native/core prompt."""
    return Path(__file__).resolve().parent / "prompts" / f"{role_id}.md"


def _builtin_prompt_text(role_id: str) -> str:
    if protected_assets.packaged():
        return (protected_assets.prompt(f"{role_id}.md") or "").strip()
    path = _prompt_builtin_md_path(role_id)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def resolve_role_system_prompt(role_id: str, yaml_prompt: str) -> str:
    """Resolve mission override, authored role text, then the built-in prompt.

    Production reads the built-in fallback from the compiled asset module. Mission
    overrides remain user-authored appdata files and are intentionally namespaced
    from native/core ``<id>.md`` overrides.
    """
    ov = _mission_prompt_override_path(role_id)
    if ov.is_file():
        text = ov.read_text(encoding="utf-8").strip()
        if text:
            return text
    yaml_text = (yaml_prompt or "").strip()
    if yaml_text:
        return yaml_text
    text = _builtin_prompt_text(role_id)
    lines = text.splitlines()
    while lines and lines[0].strip().startswith("<!--"):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def list_editable_prompts() -> list[dict]:
    """Catalog distinct mission/YAML and native/core override surfaces."""
    out: list[dict] = []

    for rid, card in load_all_roles().items():
        ov = _mission_prompt_override_path(rid)
        overridden = ov.is_file() and bool(ov.read_text(encoding="utf-8").strip())
        role_source = (
            f"compiled://services/agent/roles/{rid}.yaml"
            if protected_assets.packaged()
            else str(_ROLES_DIR / f"{rid}.yaml")
        )
        out.append({
            "role": f"mission:{rid}",
            "path": role_source,
            "override_path": str(ov),
            "text": resolve_role_system_prompt(rid, card.system_prompt),
            "overridden": overridden,
            "source": "yaml",
        })

    if protected_assets.packaged():
        prompt_ids = tuple(
            Path(name).stem for name in protected_assets.prompt_names()
            if "/" not in name and name.endswith(".md")
        )
    else:
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        prompt_ids = tuple(path.stem for path in sorted(prompts_dir.glob("*.md")))
    for rid in prompt_ids:
        ov = _prompt_override_path(rid)
        overridden = ov.is_file() and bool(ov.read_text(encoding="utf-8").strip())
        text = (
            ov.read_text(encoding="utf-8").strip()
            if overridden
            else _builtin_prompt_text(rid)
        )
        builtin_source = (
            f"compiled://services/agent/prompts/{rid}.md"
            if protected_assets.packaged()
            else str(_prompt_builtin_md_path(rid))
        )
        out.append({
            "role": f"core:{rid}",
            "path": builtin_source,
            "override_path": str(ov),
            "text": text,
            "overridden": overridden,
            "source": "md",
        })

    out.sort(key=lambda row: row["role"])
    return out

_PLAYBOOKS_DIR = Path(__file__).resolve().parent / "playbooks"


# --------------------------------------------------------------------------- errors

class RoleError(ValueError):
    """A role or playbook card is structurally invalid (not merely missing a
    not-yet-shipped tool)."""


# --------------------------------------------------------------------------- dataclasses

@dataclass(frozen=True)
class ToolRef:
    """One entry of a role's ``tool_allowlist`` — either a concrete tool name or a
    ``family:<name>`` token (both understood by the MCP tool_registry policy)."""
    tool: Optional[str] = None
    family: Optional[str] = None

    @property
    def token(self) -> str:
        if self.family:
            return f"family:{self.family}"
        return self.tool or ""

    @property
    def is_family(self) -> bool:
        return self.family is not None


@dataclass(frozen=True)
class PlaybookStep:
    id: str
    goal: str
    helper_tools: tuple[str, ...] = ()
    checklist: tuple[str, ...] = ()
    output: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Playbook:
    id: str
    stage: str
    steps: tuple[PlaybookStep, ...]
    exit_gate: tuple[str, ...] = ()

    def step(self, step_id: str) -> Optional[PlaybookStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def render_step(self, index: int) -> str:
        """Render one step for step-locked injection (§9.1 slot 5)."""
        if index < 0 or index >= len(self.steps):
            return ""
        s = self.steps[index]
        lines = [f"[PLAYBOOK {self.id} step {index + 1}/{len(self.steps)}: {s.id}]",
                 f"goal: {s.goal}"]
        if s.helper_tools:
            lines.append(f"helper tools: {', '.join(s.helper_tools)}")
        for item in s.checklist:
            lines.append(f"  - {item}")
        if s.output:
            lines.append(f"emit: {json.dumps(s.output, separators=(',', ':'))}")
        return "\n".join(lines)

    def render_checklist(self) -> str:
        """Render the whole playbook as a compact checklist (§8 `checklist` tier)."""
        lines = [f"[PLAYBOOK {self.id} — {len(self.steps)} steps]"]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. {s.id}: {s.goal}")
            for item in s.checklist:
                lines.append(f"     - {item}")
        if self.exit_gate:
            lines.append(f"exit gate: {', '.join(self.exit_gate)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class RoleCard:
    id: str
    goal: str
    system_prompt: str
    stage_scope: tuple[str, ...]
    tool_allowlist: tuple[ToolRef, ...]
    context_recipe: dict[str, Any]
    ledger_sections: dict[str, list[str]]
    handoff_schema: dict[str, Any]
    escalation_policy: dict[str, Any]
    playbook_default: Optional[str] = None

    # -- tools -------------------------------------------------------------
    @property
    def allow_tokens(self) -> list[str]:
        """The tokens to hand the MCP tool_registry ``set_policy(allow=...)``."""
        return [t.token for t in self.tool_allowlist if t.token]

    @property
    def concrete_tools(self) -> list[str]:
        """Concrete tool names for the tools_slot prompt.

        Expands ``family:<name>`` allowlist entries via the MCP tool catalog
        when populated; falls back to a curated seed list for families the
        catalog has not registered yet (e.g. firmware tools registered without
        a family view). Order preserves allowlist order; de-duped.

        mega-5: family expansion must NOT blow past ``MAX_TOOLS_PER_ROLE``.
        Live SPECIFY slots rendered 41 tools / ~2284 tokens under 5-way fan-out
        and timed out on xAI; the contract cap is 14. Explicit ``tool:`` entries
        always win a seat; family expansions fill remaining slots only.
        """
        explicit: list[str] = []
        # Per-family expansion queues (round-robin fill so one huge family cannot
        # monopolize the remaining seats — mega-5 SPECIFY need ecad+fw+em).
        family_queues: list[list[str]] = []
        seen: set[str] = set()

        def _add(dest: list[str], name: str) -> None:
            if name and name not in seen:
                seen.add(name)
                dest.append(name)

        catalog: dict[str, dict] = {}
        try:
            from services.mcp import tool_registry as _reg  # noqa: PLC0415
            _reg.warm_catalog()
            catalog = dict(_reg.catalog() or {})
        except Exception:  # noqa: BLE001
            catalog = {}

        for ref in self.tool_allowlist:
            if ref.tool:
                _add(explicit, ref.tool)
                continue
            if not ref.family:
                continue
            fam = ref.family
            fam_names = [
                name for name, meta in catalog.items()
                if (meta or {}).get("family") == fam
            ]
            if not fam_names:
                fam_names = list(_FAMILY_SEED_TOOLS.get(fam, ()))
            else:
                # The family's own namespace first: cross-tagged tools (e.g.
                # ecad_antenna_design, tagged family "em") must not take the
                # family's round-robin seat ahead of its ``<fam>_*`` tools.
                fam_names.sort(key=lambda n: not n.startswith(f"{fam}_"))
            q: list[str] = []
            for name in fam_names:
                if name and name not in seen and name not in q:
                    q.append(name)
            if q:
                family_queues.append(q)

        out = list(explicit)
        # Round-robin one tool per family until cap or queues empty.
        while len(out) < MAX_TOOLS_PER_ROLE and any(family_queues):
            progressed = False
            for q in family_queues:
                if len(out) >= MAX_TOOLS_PER_ROLE:
                    break
                while q:
                    name = q.pop(0)
                    if name not in seen:
                        seen.add(name)
                        out.append(name)
                        progressed = True
                        break
            if not progressed:
                break
        # Absolute ceiling for tools_slot prompt size (contract max_tools_per_role).
        return out[:MAX_TOOLS_PER_ROLE]

    def apply_tool_policy(self, registry: Any) -> list[str]:
        """Bind this role's allowlist into the MCP tool_registry — the ONE dispatch
        enforcement point (services/mcp/tool_registry.py). Returns the tokens set.

        After this, ``registry.is_allowed(name)`` is False for any tool outside the
        role's scope, and a dispatch of such a tool returns the contract-shaped
        refusal instead of running.

        Unions in :data:`copilot_mode_tools.SHARED_DEFAULT_TOOLS` (research_web_search,
        ask_user, doc lifecycle, fs_read/fs_glob/fs_grep, …) and
        :data:`copilot_mode_tools.AGENT_DEFAULT_TOOLS` (fs_write/fs_edit/fs_list/
        run_shell/research_fetch_page, the main-agent file-handling floor) — the
        copilot-mode layer already treats these floors as always-available, but
        ``set_policy`` is the actual dispatch gate and previously only saw a role's
        own ``tool_allowlist``. Without this union, any role card that doesn't
        explicitly list e.g. research_web_search or fs_write would have it offered
        by the mode layer yet REFUSED at dispatch — a silent floor/enforcement
        mismatch, not an intentional restriction.

        Settings → AI → Tools ``agent.role_tools``: family tokens for the mapped
        harness role are unioned in so the UI assignment actually reaches the
        dispatch gate. The main-chat ``general_agent`` / ``default`` harness role
        is never gated here (``allow=None``): its advertised surface is the core
        loadout plus whatever the model loads from the full menu, and a refusal
        at dispatch for a tool the model was shown would be a harness lie.
        """
        from services.agent.copilot_mode_tools import (  # noqa: PLC0415
            AGENT_DEFAULT_TOOLS,
            SHARED_DEFAULT_TOOLS,
        )
        from services.agent.config import (  # noqa: PLC0415
            get_role_tools,
            harness_role_for_yaml,
        )

        harness = harness_role_for_yaml(self.id)
        role_fams = get_role_tools(harness)
        fam_tokens = [f"family:{f}" for f in role_fams if f]

        tokens = list(dict.fromkeys([
            *self.allow_tokens, *fam_tokens,
            *SHARED_DEFAULT_TOOLS, *AGENT_DEFAULT_TOOLS,
        ]))
        # Settings → AI → Tools "Send bug reports": the registry policy is the
        # ONE dispatch gate, so the floor must be unioned here too or an
        # advertised submit_bug_report would be refused at dispatch.
        try:
            from services.agent.config import bugreport_floor  # noqa: PLC0415
            tokens = list(dict.fromkeys([*tokens, *bugreport_floor()]))
        except Exception:  # noqa: BLE001 — floor union is best-effort
            pass
        # Main chat: open the gate (per-tool enabled flags + legacy family
        # kill-switches still apply).
        registry.set_policy(allow=None if harness == "default" else tokens)
        return tokens

    def tools_for_mode(
        self,
        mode: str | None = None,
        *,
        plan_only: bool | None = None,
    ) -> list[str] | None:
        """Intersect this role's concrete tools with a copilot mode allowlist.

        Returns None when the mode is unrestricted and the role has no tools
        (caller treats as full). Otherwise a concrete list.
        """
        try:
            from services.agent.copilot_mode_tools import apply_mode_tools  # noqa: PLC0415
            return apply_mode_tools(
                mode,
                self.concrete_tools,
                plan_only=plan_only,
            )
        except Exception:  # noqa: BLE001
            return list(self.concrete_tools)

    # -- ledger scoping ----------------------------------------------------
    @property
    def read_sections(self) -> list[str]:
        return list(self.ledger_sections.get("read", []))

    @property
    def write_sections(self) -> list[str]:
        return list(self.ledger_sections.get("write", []))


# --------------------------------------------------------------------------- parsing

def _parse_tool_ref(raw: Any) -> ToolRef:
    if isinstance(raw, dict):
        if "tool" in raw:
            return ToolRef(tool=str(raw["tool"]))
        if "family" in raw:
            return ToolRef(family=str(raw["family"]))
        raise RoleError(f"tool_allowlist entry {raw!r} has neither 'tool' nor 'family'")
    if isinstance(raw, str):
        # Support bare "family:foo" or a plain tool name.
        if raw.startswith("family:"):
            return ToolRef(family=raw[len("family:"):])
        return ToolRef(tool=raw)
    raise RoleError(f"tool_allowlist entry {raw!r} is not a mapping or string")


def _role_from_dict(data: dict[str, Any], *, source: str) -> RoleCard:
    schema = data.get("schema")
    if schema != ROLE_SCHEMA:
        raise RoleError(f"{source}: schema is {schema!r}, expected {ROLE_SCHEMA!r}")
    for key in ("id", "goal", "system_prompt", "stage_scope", "tool_allowlist",
                "context_recipe", "ledger_sections", "handoff_schema",
                "escalation_policy"):
        if key not in data:
            raise RoleError(f"{source}: missing required field {key!r}")
    tools = tuple(_parse_tool_ref(t) for t in data["tool_allowlist"])
    sections = data["ledger_sections"] or {}
    if not isinstance(sections, dict):
        raise RoleError(f"{source}: ledger_sections must be a mapping")
    return RoleCard(
        id=str(data["id"]),
        goal=str(data["goal"]),
        system_prompt=str(data["system_prompt"]),
        stage_scope=tuple(str(s) for s in data["stage_scope"]),
        tool_allowlist=tools,
        context_recipe=dict(data["context_recipe"] or {}),
        ledger_sections={k: list(v) for k, v in sections.items()},
        handoff_schema=dict(data["handoff_schema"]),
        escalation_policy=dict(data["escalation_policy"] or {}),
        playbook_default=(str(data["playbook_default"])
                          if data.get("playbook_default") else None),
    )


def _playbook_from_dict(data: dict[str, Any], *, source: str) -> Playbook:
    schema = data.get("schema")
    if schema != PLAYBOOK_SCHEMA:
        raise RoleError(f"{source}: schema is {schema!r}, expected {PLAYBOOK_SCHEMA!r}")
    for key in ("id", "stage", "steps"):
        if key not in data:
            raise RoleError(f"{source}: missing required field {key!r}")
    steps = []
    for i, s in enumerate(data["steps"]):
        if "id" not in s or "goal" not in s:
            raise RoleError(f"{source}: step {i} missing 'id' or 'goal'")
        steps.append(PlaybookStep(
            id=str(s["id"]),
            goal=str(s["goal"]),
            helper_tools=tuple(str(h) for h in s.get("helper_tools", [])),
            checklist=tuple(str(c) for c in s.get("checklist", [])),
            output=dict(s.get("output", {})),
        ))
    if not steps:
        raise RoleError(f"{source}: playbook has no steps")
    return Playbook(
        id=str(data["id"]),
        stage=str(data["stage"]),
        steps=tuple(steps),
        exit_gate=tuple(str(g) for g in data.get("exit_gate", [])),
    )


# --------------------------------------------------------------------------- loading

def _load_role_document(role_name: str, roles_dir: Path | None) -> tuple[Any, str]:
    filename = f"{role_name}.yaml"
    if protected_assets.packaged():
        if roles_dir is not None:
            raise RoleError("filesystem role directories are disabled in packaged builds")
        raw = protected_assets.role_yaml(filename)
        source = f"compiled://services/agent/roles/{filename}"
        if raw is None:
            raise RoleError(f"role card not found: {source}")
        return json.loads(raw), source
    path = (roles_dir or _ROLES_DIR) / filename
    if not path.exists():
        raise RoleError(f"role card not found: {path}")
    return yaml.safe_load(path.read_text()), str(path)


def load_role(role_id: str, *, roles_dir: Path | None = None) -> RoleCard:
    data, source = _load_role_document(role_id, roles_dir)
    card = _role_from_dict(data, source=source)
    # Mission prompt overrides use appdata prompts/<id>.mission.md so a GUI edit
    # cannot overwrite a native/core role with the same id and output contract.
    resolved = resolve_role_system_prompt(role_id, card.system_prompt)
    if resolved != card.system_prompt:
        return RoleCard(
            id=card.id,
            goal=card.goal,
            system_prompt=resolved,
            stage_scope=card.stage_scope,
            tool_allowlist=card.tool_allowlist,
            context_recipe=card.context_recipe,
            ledger_sections=card.ledger_sections,
            handoff_schema=card.handoff_schema,
            escalation_policy=card.escalation_policy,
            playbook_default=card.playbook_default,
        )
    return card


def load_all_roles(*, roles_dir: Path | None = None) -> dict[str, RoleCard]:
    if protected_assets.packaged():
        if roles_dir is not None:
            raise RoleError("filesystem role directories are disabled in packaged builds")
        names = protected_assets.role_names()
    else:
        d = roles_dir or _ROLES_DIR
        names = tuple(path.name for path in sorted(d.glob("*.yaml")))
    out: dict[str, RoleCard] = {}
    for filename in names:
        data, source = _load_role_document(Path(filename).stem, roles_dir)
        # roles/ also holds non-card data files (e.g. council_seats.yaml).
        if not isinstance(data, dict) or data.get("schema") != ROLE_SCHEMA:
            continue
        card = _role_from_dict(data, source=source)
        out[card.id] = card
    return out


def load_playbook(playbook_id: str, *, playbooks_dir: Path | None = None) -> Playbook:
    path = (playbooks_dir or _PLAYBOOKS_DIR) / f"{playbook_id}.yaml"
    if not path.exists():
        raise RoleError(f"playbook not found: {path}")
    return _playbook_from_dict(yaml.safe_load(path.read_text()), source=str(path))


def load_all_playbooks(*, playbooks_dir: Path | None = None) -> dict[str, Playbook]:
    d = playbooks_dir or _PLAYBOOKS_DIR
    out: dict[str, Playbook] = {}
    for path in sorted(d.glob("*.yaml")):
        pb = _playbook_from_dict(yaml.safe_load(path.read_text()), source=str(path))
        out[pb.id] = pb
    return out


# --------------------------------------------------------------------------- validation

@dataclass
class Finding:
    """A mechanical validation finding. ``level`` is 'error' (card is broken) or
    'flag' (a not-yet-shipped tool / advisory-only concern that must be noted but
    does not fail the load)."""
    level: str  # "error" | "flag"
    role: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"[{self.level}] {self.role}: {self.message}"


def validate_role(
    card: RoleCard,
    *,
    tool_names: Iterable[str] | None = None,
    playbook_ids: Iterable[str] | None = None,
) -> list[Finding]:
    """Validate one role card's mechanical fields. Returns a list of Findings.

    Checks (contract §4, §8, §1):
      * every ``stage_scope`` entry is in the frozen stage vocabulary;
      * ``tool_allowlist`` ≤ MAX_TOOLS_PER_ROLE;
      * every concrete allowlist tool resolves to a registered tool, OR is
        feature-flagged (delegate / workspace_status) → reported as a 'flag';
      * ``playbook_default`` (if set) resolves to a known playbook;
      * ``handoff_schema`` parses (top-level object with 'required');
      * ``escalation_policy`` has max_attempts + on_exhaust.
    """
    findings: list[Finding] = []
    known_tools = set(tool_names) if tool_names is not None else None
    known_playbooks = set(playbook_ids) if playbook_ids is not None else None

    for st in card.stage_scope:
        if st not in STAGES:
            findings.append(Finding("error", card.id,
                f"stage_scope entry {st!r} is not in the stage vocabulary {STAGES}"))

    if len(card.tool_allowlist) > MAX_TOOLS_PER_ROLE:
        findings.append(Finding("error", card.id,
            f"tool_allowlist has {len(card.tool_allowlist)} tools "
            f"(> cap {MAX_TOOLS_PER_ROLE})"))

    if known_tools is not None:
        # Only explicit `tool:` allowlist entries must resolve. Family tokens
        # expand at tools_slot time (catalog or seed) and are not required to
        # appear in the static catalog during suite validation.
        for name in (t.tool for t in card.tool_allowlist if t.tool):
            if name in known_tools:
                continue
            if name in FEATURE_FLAGGED_TOOLS:
                findings.append(Finding("flag", card.id,
                    f"allowlist tool {name!r} is not yet registered "
                    f"(feature-flagged; lands with a sibling W task)"))
            else:
                findings.append(Finding("error", card.id,
                    f"allowlist tool {name!r} does not resolve to a registered tool"))

    if card.playbook_default and known_playbooks is not None:
        if card.playbook_default not in known_playbooks:
            findings.append(Finding("error", card.id,
                f"playbook_default {card.playbook_default!r} has no matching playbook"))

    hs = card.handoff_schema
    if not isinstance(hs, dict) or hs.get("type") != "object" or "required" not in hs:
        findings.append(Finding("error", card.id,
            "handoff_schema must be a JSON object schema with a 'required' list"))
    else:
        # The emitter must be able to build a grammar from it.
        try:
            schema_to_gbnf(hs)
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("error", card.id,
                f"handoff_schema does not compile to GBNF: {exc}"))

    ep = card.escalation_policy
    if "max_attempts" not in ep or "on_exhaust" not in ep:
        findings.append(Finding("error", card.id,
            "escalation_policy must have 'max_attempts' and 'on_exhaust'"))

    return findings


def validate_playbook(
    pb: Playbook,
    *,
    role_helper_tools_ok: Iterable[str] | None = None,
) -> list[Finding]:
    """Validate a playbook: stage in vocabulary, steps have outputs, exit_gate present.

    ``role_helper_tools_ok`` (optional): the union of the owning role's allowlist —
    when provided, every step ``helper_tools`` entry must be inside it (the
    "helpers ⊆ allowlist" model; see the task note for the deliberate exception on
    tap_drill in cam_job)."""
    findings: list[Finding] = []
    if pb.stage not in STAGES:
        findings.append(Finding("error", pb.id,
            f"playbook stage {pb.stage!r} is not in the stage vocabulary"))
    if not pb.exit_gate:
        findings.append(Finding("error", pb.id, "playbook has no exit_gate"))
    if role_helper_tools_ok is not None:
        allowed = set(role_helper_tools_ok)
        for s in pb.steps:
            for h in s.helper_tools:
                if h not in allowed:
                    findings.append(Finding("flag", pb.id,
                        f"step {s.id!r} helper tool {h!r} is not in the owning role's "
                        f"allowlist (helpers ⊆ allowlist model)"))
    return findings


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]


def flags(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "flag"]


# --------------------------------------------------------------------------- context assembly

# Type of an injected describer callback: (tool_name, budget_tokens) -> rendered text.
DescriberFn = Callable[[str, int], str]


def system_prompt_for_capability(
    card: RoleCard,
    capability: Any = None,
) -> str:
    """Role system prompt + per-capability SOFT phrasing block.

    *capability* is a letter ``"A"|"B"|"C"``, a CapabilityProfile, or None
    (defaults to C — frontier freedom, minimal SOFT block).
    """
    from services.agent.prompt_tiers import (  # noqa: PLC0415
        assemble_system_for_capability,
    )
    return assemble_system_for_capability(card.system_prompt, capability)


def assemble_context(
    card: RoleCard,
    *,
    ledger_render: str = "",
    stage_state: str = "",
    advisory_block: str = "",
    playbook_step: str = "",
    history: str = "",
    capability: Any = None,
) -> str:
    """Assemble one turn's context in the frozen §9.1 order:

        role system prompt (+ capability SOFT block) → ledger render → stage
        state → ADVISORY block → playbook step (if step-locked) → history

    The advisory block and describer/stage-state renders are produced by the
    foreman and the describer tools respectively — this assembler takes
    them as already-rendered strings so it stays headless and testable (no live
    CAD doc, no import of services/agent/advisor/). Empty slots are omitted; the
    ordering of present slots is invariant.

    *capability*: letter or CapabilityProfile selecting the SOFT
    phrasing block appended to the role system prompt. Default None → C
    (minimal HARD-floor mirror only).
    """
    system = system_prompt_for_capability(card, capability)
    slots = [
        system.rstrip(),
        ledger_render.strip(),
        stage_state.strip(),
        advisory_block.strip(),
        playbook_step.strip(),
        history.strip(),
    ]
    return "\n\n".join(s for s in slots if s)


def render_stage_state(
    card: RoleCard,
    *,
    describer: DescriberFn | None = None,
) -> str:
    """Run the role's ``context_recipe.describers`` at their budgets and concatenate.

    ``describer(tool_name, budget)`` is injected (real describers live behind MCP /
    the CAD engine). Returns "" when no describer is available (headless / test).
    """
    if describer is None:
        return ""
    out: list[str] = []
    for spec in card.context_recipe.get("describers", []) or []:
        tool = spec.get("tool")
        budget = int(spec.get("budget", 800))
        if not tool:
            continue
        try:
            text = describer(tool, budget)
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            out.append(text.strip())
    return "\n\n".join(out)


# --------------------------------------------------------------------------- handoff

def make_handoff(
    card: RoleCard,
    *,
    status: str,
    summary: str,
    outputs: dict[str, Any],
    ledger_writes: list[int] | None = None,
    next_role_hint: str | None = None,
) -> dict[str, Any]:
    """Build an ``aicad.handoff/1`` envelope for this role (contract §4)."""
    env: dict[str, Any] = {
        "schema": HANDOFF_SCHEMA,
        "role": card.id,
        "status": status,
        "summary": summary,
        "outputs": outputs,
    }
    if ledger_writes is not None:
        env["ledger_writes"] = list(ledger_writes)
    if next_role_hint is not None:
        env["next_role_hint"] = next_role_hint
    return env


def validate_handoff(card: RoleCard, envelope: dict[str, Any]) -> list[str]:
    """Validate a handoff envelope against the role's ``handoff_schema`` plus the
    envelope-level rules (contract §4). Returns a list of error strings ([] = valid).

    Enforces both structure (required keys, enums, types — what the GBNF grammar
    guarantees for local tiers) AND the constraints grammars cannot express
    (``maxLength``, ``enum`` at nested levels, numeric ranges)."""
    errs: list[str] = []
    if envelope.get("role") != card.id:
        errs.append(f"envelope.role {envelope.get('role')!r} != card id {card.id!r}")
    status = envelope.get("status")
    if status not in ("done", "blocked", "escalated"):
        errs.append(f"status {status!r} not in [done, blocked, escalated]")
    # The card's handoff_schema validates {status, summary, outputs}.
    _validate_against_schema(envelope, card.handoff_schema, "handoff", errs)
    return errs


def append_handoff_to_ledger(ledger: Any, card: RoleCard,
                             envelope: dict[str, Any]) -> dict[str, Any]:
    """Append a validated handoff envelope to the design ledger as an event
    (actor=role id, kind='handoff'). Returns the ledger event.

    Raises ``RoleError`` if the envelope does not validate — a role never writes an
    invalid handoff to the mission's memory."""
    errs = validate_handoff(card, envelope)
    if errs:
        raise RoleError(f"handoff for role {card.id!r} is invalid: {errs}")
    return ledger.append(card.id, "handoff", envelope)


def _validate_against_schema(value: Any, schema: dict[str, Any],
                             path: str, errs: list[str]) -> None:
    """A small, dependency-free JSON-Schema subset validator covering exactly what the
    handoff schemas use: type, required, properties, enum, maxLength, items, $ref
    (opaque — an unresolved $ref accepts any object)."""
    if "$ref" in schema:
        # Unresolved contract $ref (aicad.deliverables/1 etc.) — accept any object.
        if not isinstance(value, dict):
            errs.append(f"{path}: expected object (for {schema['$ref']})")
        return

    stype = schema.get("type")
    if isinstance(stype, list):
        # e.g. ["string", "null"]
        if not any(_type_ok(value, t) for t in stype):
            errs.append(f"{path}: {value!r} is not one of types {stype}")
        return
    if stype and not _type_ok(value, stype):
        errs.append(f"{path}: expected {stype}, got {type(value).__name__}")
        return

    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")

    if stype == "string" and isinstance(value, str):
        mx = schema.get("maxLength")
        if mx is not None and len(value) > mx:
            errs.append(f"{path}: string length {len(value)} > maxLength {mx}")

    if stype == "object" and isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required key {req!r}")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                _validate_against_schema(value[key], subschema, f"{path}.{key}", errs)

    if stype == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate_against_schema(item, item_schema, f"{path}[{i}]", errs)


def _type_ok(value: Any, t: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(t, True)


# --------------------------------------------------------------------------- GBNF emitter

# A JSON-schema → GBNF grammar emitter. Constrains STRUCTURE ONLY: required keys,
# enum literals, and primitive types — the invariants a local (llama.cpp) tier needs
# so a 4B model emits parseable, correctly-shaped JSON. String-length and numeric
# ranges are NOT expressed (a context-free grammar cannot count) — validate_handoff
# enforces those after parsing.
#
# OSS survey (OSS-first directive): llama.cpp ships `json-schema-to-grammar`
# (grammars/json.gbnf + the Python port json_schema_to_grammar.py, MIT) and the
# `outlines` library builds regex/CFG from schemas. Both are heavier than this task
# needs (full JSON Schema, streaming). This emitter is a deliberately small,
# vendored-free subset targeting only the handoff-schema shapes the 13 cards use
# (object/array/string/number/integer/boolean/null/enum/$ref), producing llama.cpp
# GBNF that the same runtime consumes. No new dependency; MIT-idea-compatible.

_GBNF_PRIMITIVES = r"""
ws      ::= [ \t\n]*
string  ::= "\"" ( [^"\\] | "\\" ["\\/bfnrt] | "\\u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] )* "\""
integer ::= "-"? ("0" | [1-9] [0-9]*)
number  ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
boolean ::= "true" | "false"
null    ::= "null"
value   ::= object | array | string | number | boolean | null
object  ::= "{" ws ( string ws ":" ws value ( ws "," ws string ws ":" ws value )* )? ws "}"
array   ::= "[" ws ( value ( ws "," ws value )* )? ws "]"
""".strip()


def _gbnf_string_literal(s: str) -> str:
    """A GBNF terminal matching the exact JSON string literal ``s`` (for enums)."""
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"\\"{esc}\\""'


class _GbnfBuilder:
    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self._counter = 0

    def fresh(self, hint: str) -> str:
        self._counter += 1
        safe = "".join(c if c.isalnum() else "-" for c in hint).strip("-") or "node"
        return f"{safe}-{self._counter}"

    def add(self, name: str, body: str) -> str:
        self.rules[name] = body
        return name

    def emit(self, schema: dict[str, Any], hint: str = "root") -> str:
        """Return the rule name that matches ``schema`` (creating rules as needed)."""
        if not isinstance(schema, dict):
            return "value"

        if "$ref" in schema:
            # Unresolved contract $ref — accept any JSON object (permissive; never crash).
            return "object"

        if "enum" in schema:
            alts = " | ".join(_gbnf_enum_alt(v) for v in schema["enum"])
            return self.add(self.fresh(hint + "-enum"), alts)

        stype = schema.get("type")
        if isinstance(stype, list):
            alts = " | ".join(self._primitive_rule(t) for t in stype)
            return self.add(self.fresh(hint + "-union"), alts)

        if stype == "object":
            return self._emit_object(schema, hint)
        if stype == "array":
            item = schema.get("items")
            item_rule = self.emit(item, hint + "-item") if isinstance(item, dict) else "value"
            name = self.fresh(hint + "-arr")
            return self.add(
                name,
                f'"[" ws ( {item_rule} ( ws "," ws {item_rule} )* )? ws "]"',
            )
        if stype in ("string", "number", "integer", "boolean", "null"):
            return self._primitive_rule(stype)
        # Unspecified type → any JSON value.
        return "value"

    def _primitive_rule(self, t: str) -> str:
        return {
            "string": "string", "number": "number", "integer": "integer",
            "boolean": "boolean", "null": "null", "object": "object",
            "array": "array",
        }.get(t, "value")

    def _emit_object(self, schema: dict[str, Any], hint: str) -> str:
        props: dict[str, Any] = schema.get("properties", {}) or {}
        required: list[str] = list(schema.get("required", []))
        # Guaranteed structure = the REQUIRED keys, in a fixed order, each with its
        # typed sub-rule (JSON key order is free but a fixed order is valid JSON and
        # keeps the grammar deterministic). Optional/extra members — crucially
        # ``replan_request``, the anti-grind flexibility valve every worker prompt
        # can emit (contract §3.7 / §17) — are PERMITTED via a trailing
        # ``( "," key ":" value )*`` production. Without this the grammar would
        # forbid a grammar-constrained local model from ever emitting a replan.
        # Structure stays: required keys are still guaranteed present and typed.
        if not required and not props:
            return "object"  # unconstrained object

        optional_tail = '( ws "," ws string ws ":" ws value )*'

        if not required:
            # No required keys: any members (all props are optional) — but we still
            # know it's an object. Accept zero-or-more string:value members.
            name = self.fresh(hint + "-obj")
            return self.add(
                name,
                f'"{{" ws ( string ws ":" ws value {optional_tail} )? ws "}}"',
            )

        parts: list[str] = ['"{" ws']
        for i, key in enumerate(required):
            subschema = props.get(key, {})
            sub_rule = self.emit(subschema, f"{hint}-{key}") if isinstance(subschema, dict) else "value"
            sep = ' ws "," ws ' if i > 0 else " "
            parts.append(f'{sep}{_gbnf_string_literal(key)} ws ":" ws {sub_rule}')
        # Allow any optional/extra members after the required ones (e.g. replan_request).
        parts.append(f' {optional_tail} ws "}}"')
        name = self.fresh(hint + "-obj")
        return self.add(name, "".join(parts))


def _gbnf_enum_alt(v: Any) -> str:
    if isinstance(v, str):
        return _gbnf_string_literal(v)
    if isinstance(v, bool):
        return '"true"' if v else '"false"'
    if v is None:
        return '"null"'
    if isinstance(v, (int, float)):
        return f'"{json.dumps(v)}"'
    return "value"


def schema_to_gbnf(schema: dict[str, Any]) -> str:
    """Compile a JSON (sub)schema into a llama.cpp GBNF grammar string.

    The entry rule is ``root``. Structure only — see the module note. Never raises on
    a ``$ref`` (treated as an opaque object)."""
    builder = _GbnfBuilder()
    root = builder.emit(schema, "root")
    lines = [f"root ::= ws {root} ws"]
    for name, body in builder.rules.items():
        lines.append(f"{name} ::= {body}")
    lines.append("")
    lines.append(_GBNF_PRIMITIVES)
    return "\n".join(lines)


# --------------------------------------------------------------------------- suite validation

def validate_suite(
    *,
    roles_dir: Path | None = None,
    playbooks_dir: Path | None = None,
    tool_names: Iterable[str] | None = None,
) -> list[Finding]:
    """Load and validate every role card and playbook together, including the
    cross-references between them (playbook_default resolves, helper tools ⊆ allowlist,
    the 6 default playbooks exist). Returns all findings (errors + flags)."""
    roles = load_all_roles(roles_dir=roles_dir)
    playbooks = load_all_playbooks(playbooks_dir=playbooks_dir)
    pb_ids = set(playbooks)
    findings: list[Finding] = []
    for card in roles.values():
        findings.extend(validate_role(card, tool_names=tool_names, playbook_ids=pb_ids))
    for pb in playbooks.values():
        # Find the owning role (the one whose playbook_default == pb.id) for the
        # helper-⊆-allowlist check; if several/none, skip that check.
        owners = [c for c in roles.values() if c.playbook_default == pb.id]
        allow = None
        if len(owners) == 1:
            allow = set(owners[0].concrete_tools)
        findings.extend(validate_playbook(pb, role_helper_tools_ok=allow))
    return findings
