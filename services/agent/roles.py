"""roles.py — Role card loader, validator, and handoff validator.

Contract: contracts/advisor.md §4 (role card `aicad.role/1`), §9 (injection &
precedence), §1 (stage vocabulary).
Pipeline composition: docs/design/COPILOT_MISSION_PIPELINE.md.

Roles are authored data. Developer runs load ``services/agent/roles/*.yaml``;
packaged workers load their deterministic build-generated representation. Their
``system_prompt`` values are preserved and the loader validates the surrounding
mechanical fields.

What this module gives the hosts (services/dag node runner, services/agent loop):

  * ``load_role(id)`` / ``load_all_roles()``    → validated ``RoleCard`` objects.
  * ``validate_role(card, tool_names)``          → list of mechanical findings.
  * ``RoleCard.apply_tool_policy()``             → binds the allowlist into the MCP
    tool_registry (the ONE enforcement point) so an out-of-scope call is refused.
  * ``validate_handoff(card, envelope)``         → structural + constraint validation
    of a role's emitted handoff against its ``handoff_schema``.

Design note: ``$ref`` (e.g. ``aicad.deliverables/1``) has no loadable JSON-Schema
file — the schemas live as prose examples in the contract. ``validate_handoff``
treats an unresolved ``$ref`` as an opaque JSON object node (permissive).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from services.agent import protected_assets

# --------------------------------------------------------------------------- constants

ROLE_SCHEMA = "aicad.role/1"

# Stage vocabulary (contract §1) — frozen. Every stage_scope entry must be one
# of these. Defined here from the contract rather than
# imported from services/agent/advisor/stages.py (that module owns stage logic;
# the vocabulary itself is the frozen contract).
STAGES = (
    "intake", "spec", "decompose", "layout", "sketch", "feature", "linkage",
    "part_verify", "assembly", "sim", "cam", "deliver", "fix",
)

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

_ROLES_DIR = Path(__file__).resolve().parent / "roles"

def _prompts_override_dir() -> Path:
    """User prompt overrides directory (``<appdata>/Chudware/prompts``)."""
    import os
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        root = Path(xdg) / "Chudware"
    else:
        root = Path.home() / ".local" / "share" / "Chudware"
    # CHUDWARE_APPDATA / CHUDWARE_DATA, when set, name the appdata root directly.
    app = os.environ.get("CHUDWARE_APPDATA") or os.environ.get("CHUDWARE_DATA")
    if app:
        root = Path(app)
    return root / "prompts"


def _mission_prompt_override_path(role_id: str) -> Path:
    """Mission/YAML prompt override path in the appdata prompts dir."""
    return _prompts_override_dir() / f"{role_id}.mission.md"


def _prompt_builtin_md_path(role_id: str) -> Path:
    """Developer-source path for a built-in prompt file."""
    return Path(__file__).resolve().parent / "prompts" / f"{role_id}.md"


def _builtin_prompt_text(role_id: str) -> str:
    if protected_assets.packaged():
        return (protected_assets.prompt(f"{role_id}.md") or "").strip()
    path = _prompt_builtin_md_path(role_id)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def resolve_role_system_prompt(role_id: str, yaml_prompt: str) -> str:
    """Resolve mission override, authored role text, then the built-in prompt.

    Production reads the built-in fallback from the compiled asset module. Mission
    overrides are user-authored appdata files (``<id>.mission.md``).
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
    """Catalog mission/YAML role prompts and their appdata overrides."""
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

    out.sort(key=lambda row: row["role"])
    return out


# --------------------------------------------------------------------------- errors

class RoleError(ValueError):
    """A role card is structurally invalid."""


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
        and timed out on xAI. Explicit ``tool:`` entries
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
        # Absolute ceiling for tools_slot prompt size.
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
        card = _role_from_dict(data, source=source)
        out[card.id] = card
    return out


# --------------------------------------------------------------------------- validation

@dataclass
class Finding:
    """A mechanical validation finding: ``level`` is 'error' (the card is broken)."""
    level: str  # "error"
    role: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"[{self.level}] {self.role}: {self.message}"


def validate_role(
    card: RoleCard,
    *,
    tool_names: Iterable[str] | None = None,
) -> list[Finding]:
    """Validate one role card's mechanical fields. Returns a list of Findings.

    Checks (contract §4, §8, §1):
      * every ``stage_scope`` entry is in the frozen stage vocabulary;
      * ``tool_allowlist`` ≤ MAX_TOOLS_PER_ROLE;
      * every concrete allowlist tool resolves to a registered tool;
      * ``handoff_schema`` is a top-level object schema with 'required';
      * ``escalation_policy`` has max_attempts + on_exhaust.
    """
    findings: list[Finding] = []
    known_tools = set(tool_names) if tool_names is not None else None

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
            if name not in known_tools:
                findings.append(Finding("error", card.id,
                    f"allowlist tool {name!r} does not resolve to a registered tool"))

    hs = card.handoff_schema
    if not isinstance(hs, dict) or hs.get("type") != "object" or "required" not in hs:
        findings.append(Finding("error", card.id,
            "handoff_schema must be a JSON object schema with a 'required' list"))

    ep = card.escalation_policy
    if "max_attempts" not in ep or "on_exhaust" not in ep:
        findings.append(Finding("error", card.id,
            "escalation_policy must have 'max_attempts' and 'on_exhaust'"))

    return findings


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "error"]


def validate_handoff(card: RoleCard, envelope: dict[str, Any]) -> list[str]:
    """Validate a handoff envelope against the role's ``handoff_schema`` plus the
    envelope-level rules (contract §4). Returns a list of error strings ([] = valid).

    Enforces structure (required keys, enums, types) and the constraints a plain
    schema shape cannot express (``maxLength``, nested ``enum``)."""
    errs: list[str] = []
    if envelope.get("role") != card.id:
        errs.append(f"envelope.role {envelope.get('role')!r} != card id {card.id!r}")
    status = envelope.get("status")
    if status not in ("done", "blocked", "escalated"):
        errs.append(f"status {status!r} not in [done, blocked, escalated]")
    # The card's handoff_schema validates {status, summary, outputs}.
    _validate_against_schema(envelope, card.handoff_schema, "handoff", errs)
    return errs


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


# --------------------------------------------------------------------------- suite validation

def validate_suite(
    *,
    roles_dir: Path | None = None,
    tool_names: Iterable[str] | None = None,
) -> list[Finding]:
    """Load and validate every role card. Returns all findings."""
    findings: list[Finding] = []
    for card in load_all_roles(roles_dir=roles_dir).values():
        findings.extend(validate_role(card, tool_names=tool_names))
    return findings
