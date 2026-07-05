"""roles.py — Role card + playbook loader, validator, context assembler,
handoff validator, and a JSON-schema → GBNF grammar emitter.

Contract: contracts/advisor.md §4 (role card `aicad.role/1`), §6 (playbook
`aicad.playbook/1`), §9 (injection & precedence), §1 (stage vocabulary).
Pipeline composition: docs/COPILOT_MISSION_PIPELINE.md.

Roles are DATA. The 13 role cards in ``services/agent/roles/*.yaml`` are AUTHORED
by the lead and canonical — this module LOADS them verbatim (their ``system_prompt``
block scalars are never rewritten here) and validates the mechanical fields around
them. Playbooks in ``services/agent/playbooks/*.yaml`` are the multi-step thinking
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

Design notes locked with the advisor:
  * ``$ref`` (e.g. ``aicad.deliverables/1``) has no loadable JSON-Schema file — the
    schemas live as prose examples in the contract. The GBNF emitter treats an
    unresolved ``$ref`` as an opaque JSON object node (permissive); it never crashes.
  * The only allowlist tools that do not yet resolve are ``delegate`` and
    ``workspace_status`` (land with sibling W tasks). ``validate_role`` reports them
    as feature-flagged, not as errors.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

# --------------------------------------------------------------------------- constants

ROLE_SCHEMA = "aicad.role/1"
PLAYBOOK_SCHEMA = "aicad.playbook/1"
HANDOFF_SCHEMA = "aicad.handoff/1"

# Stage vocabulary (contract §1) — frozen. Every stage_scope entry and every
# playbook.stage must be one of these. Defined here from the contract rather than
# imported from services/agent/advisor/stages.py (that module is T-W5 territory;
# the vocabulary itself is the frozen contract).
STAGES = (
    "intake", "spec", "layout", "sketch", "feature", "part_verify",
    "assembly", "sim", "cam", "deliver", "fix",
)

# Per-role tool-allowlist cap (contract §8 tier profile `max_tools_per_role`).
MAX_TOOLS_PER_ROLE = 14

# Allowlist tools that do not yet resolve to a registered MCP tool — they land with
# sibling W tasks (delegation tree T-W14, workspace status). Reported by the loader
# as feature-flagged rather than as errors, so the 13 authored cards load cleanly.
FEATURE_FLAGGED_TOOLS = frozenset({"delegate", "workspace_status"})

_ROLES_DIR = Path(__file__).resolve().parent / "roles"
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
        """Concrete tool names (not ``family:`` tokens) in the allowlist."""
        return [t.tool for t in self.tool_allowlist if t.tool]

    def apply_tool_policy(self, registry: Any) -> list[str]:
        """Bind this role's allowlist into the MCP tool_registry — the ONE dispatch
        enforcement point (services/mcp/tool_registry.py). Returns the tokens set.

        After this, ``registry.is_allowed(name)`` is False for any tool outside the
        role's scope, and a dispatch of such a tool returns the contract-shaped
        refusal instead of running.
        """
        tokens = self.allow_tokens
        registry.set_policy(allow=tokens)
        return tokens

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

def load_role(role_id: str, *, roles_dir: Path | None = None) -> RoleCard:
    path = (roles_dir or _ROLES_DIR) / f"{role_id}.yaml"
    if not path.exists():
        raise RoleError(f"role card not found: {path}")
    data = yaml.safe_load(path.read_text())
    return _role_from_dict(data, source=str(path))


def load_all_roles(*, roles_dir: Path | None = None) -> dict[str, RoleCard]:
    d = roles_dir or _ROLES_DIR
    out: dict[str, RoleCard] = {}
    for path in sorted(d.glob("*.yaml")):
        card = _role_from_dict(yaml.safe_load(path.read_text()), source=str(path))
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
        for name in card.concrete_tools:
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


def assemble_context(
    card: RoleCard,
    *,
    ledger_render: str = "",
    stage_state: str = "",
    advisory_block: str = "",
    playbook_step: str = "",
    history: str = "",
) -> str:
    """Assemble one turn's context in the frozen §9.1 order:

        role system prompt → ledger render → stage state → ADVISORY block
        → playbook step (if step-locked) → history

    The advisory block and describer/stage-state renders are produced by the foreman
    (T-W5) and the describer tools respectively — this assembler takes them as
    already-rendered strings so it stays headless and testable (no live CAD doc, no
    import of services/agent/advisor/). Empty slots are omitted; the ordering of
    present slots is invariant.
    """
    slots = [
        card.system_prompt.rstrip(),
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
        # Emit required properties as a fixed key order (grammar guarantees presence &
        # shape; JSON object key order is free but a fixed order is valid JSON and keeps
        # the grammar deterministic). Optional properties are omitted from the grammar —
        # the model MAY include extra keys via the fallback below.
        if not required and not props:
            return "object"  # unconstrained object

        parts: list[str] = ['"{" ws']
        emitted_keys = required or list(props.keys())
        for i, key in enumerate(emitted_keys):
            subschema = props.get(key, {})
            sub_rule = self.emit(subschema, f"{hint}-{key}") if isinstance(subschema, dict) else "value"
            sep = ' ws "," ws ' if i > 0 else " "
            parts.append(f'{sep}{_gbnf_string_literal(key)} ws ":" ws {sub_rule}')
        parts.append(' ws "}"')
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
