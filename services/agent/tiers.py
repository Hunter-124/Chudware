"""Model tier profiles — loader, budget enforcer, and provider tier detection.

Contract: contracts/advisor.md §8 + 1.3 addendum (deliberation dial).
Spec: docs/ADVISOR_ARCHITECTURE.md §2.9.

What this module provides
-------------------------
* ``load_tier(name)`` / ``load_all_tiers()``
  Load and validate tier profiles from ``services/agent/tiers.yaml``.

* ``TierProfile`` dataclass
  Carries all per-tier dials: budgets, enforcement, structured-output mode,
  verbosity, deliberation level, tool-schema-trim flag.

* ``detect_tier(context_length)`` / ``detect_tier_from_env()``
  Map a model's context length to a tier (AI_CAD_TIER env override honoured).

* ``TurnAssembler``
  Assembles one model turn slot-by-slot in §9.1 order, applying the budget
  from the tier profile.  Overflow policy (per contract §8):
    1. shrink history first  (never below floor=0)
    2. then shrink state     (never below STATE_FLOOR)
    never shrink system or advisory below their floors.

  Reuses the ``_CHARS_PER_TOKEN = 4`` heuristic from ledger.py / advisor/render.py
  (repo-wide constant — only one measuring stick).

* ``trim_tool_schema(schema)``
  Strip tool description to first sentence for local tiers.

* ``grammar_for_tier(tier, handoff_schema)``
  Return the GBNF grammar string for local tiers (via roles.schema_to_gbnf),
  or ``None`` for non-GBNF tiers.

Provider tier auto-detection
-----------------------------
``detect_tier(context_length)`` maps context window → tier name using the
thresholds defined in ``CONTEXT_LENGTH_TIER_MAP``. Providers should call:

    tier_name = detect_tier_from_env() or detect_tier(model_context_length)
    profile   = load_tier(tier_name)

Grammar pass-through (llama.cpp / LM Studio)
---------------------------------------------
For ``structured_output: gbnf`` tiers, callers pass ``grammar=<gbnf_string>``
to ``Provider.complete_ex``.  The OpenAI-compatible providers (LM Studio /
llama.cpp) accept this as ``extra_body={"grammar": ...}`` (llama.cpp server)
or ``response_format={"type": "json_schema", "json_schema": {...}}`` (LM Studio
json_schema fallback when grammar is not supported).  See
``services/agent/providers/openai_provider.py`` for the call site.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# --------------------------------------------------------------------------- constants

TIER_SCHEMA = "aicad.tier/1"

_TIERS_PATH = Path(__file__).resolve().parent / "tiers.yaml"

# Repo-wide token-counting heuristic (matches ledger.py and advisor/render.py).
_CHARS_PER_TOKEN = 4

# Overflow guard floors — slots never shrunk below these (tokens).
# system and advisory are NEVER shrunk (floor = their full budget as loaded).
STATE_FLOOR_TOKENS = 200   # minimum stage-state tokens to keep
HISTORY_FLOOR_TOKENS = 0   # history can be emptied entirely if needed

# Env override for manual tier selection (e.g. AI_CAD_TIER=frontier).
TIER_ENV_VAR = "AI_CAD_TIER"

# Context-length thresholds → tier name.  Checked in order; first match wins.
# A model with context_length < 32768 → local-24k (treated conservatively).
CONTEXT_LENGTH_TIER_MAP: list[tuple[int, str]] = [
    (32_768,  "local-24k"),   # ≤ 32k → local-24k
    (65_536,  "local-48k"),   # ≤ 64k → local-48k
    (150_000, "mid"),         # ≤ 150k → mid
]
FRONTIER_TIER = "frontier"


# --------------------------------------------------------------------------- errors

class TierError(ValueError):
    """A tier profile is missing or structurally invalid."""


# --------------------------------------------------------------------------- dataclasses

@dataclass(frozen=True)
class SlotBudgets:
    """Token budgets for each §9.1 context slot."""
    system:   int
    ledger:   int
    state:    int
    advisory: int
    tools:    int
    history:  int

    @property
    def total(self) -> int:
        return self.system + self.ledger + self.state + self.advisory + self.tools + self.history

    def __iter__(self):
        return iter((
            ("system",   self.system),
            ("ledger",   self.ledger),
            ("state",    self.state),
            ("advisory", self.advisory),
            ("tools",    self.tools),
            ("history",  self.history),
        ))


@dataclass(frozen=True)
class TierProfile:
    """One tier profile loaded from tiers.yaml."""
    name: str
    context_tokens: int
    generation_headroom: int
    budgets: SlotBudgets
    playbook_enforcement: str   # step_locked | checklist | off
    structured_output: str      # gbnf | json_mode | free
    advisory_verbosity: str     # full | compact
    max_tools_per_role: int
    deliberation: str           # full | standard | minimal  (§8 1.3 addendum)
    tool_schema_trim: bool

    @property
    def available_tokens(self) -> int:
        """Context tokens minus generation headroom = the assembly budget."""
        return self.context_tokens - self.generation_headroom

    def budget_ok(self) -> bool:
        """Verify slot sum ≤ available_tokens (the §8 invariant)."""
        return self.budgets.total <= self.available_tokens

    @property
    def uses_gbnf(self) -> bool:
        return self.structured_output == "gbnf"

    @property
    def uses_json_mode(self) -> bool:
        return self.structured_output == "json_mode"


# --------------------------------------------------------------------------- loading

def _parse_budgets(raw: dict, source: str) -> SlotBudgets:
    keys = ("system", "ledger", "state", "advisory", "tools", "history")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise TierError(f"{source}: budgets missing keys: {missing}")
    return SlotBudgets(**{k: int(raw[k]) for k in keys})


def _profile_from_dict(name: str, raw: dict, source: str) -> TierProfile:
    required = ("context_tokens", "generation_headroom", "budgets",
                "playbook_enforcement", "structured_output", "advisory_verbosity",
                "max_tools_per_role", "deliberation", "tool_schema_trim")
    missing = [k for k in required if k not in raw]
    if missing:
        raise TierError(f"{source} [{name}]: missing fields: {missing}")

    budgets = _parse_budgets(raw["budgets"], f"{source} [{name}]")
    profile = TierProfile(
        name=name,
        context_tokens=int(raw["context_tokens"]),
        generation_headroom=int(raw["generation_headroom"]),
        budgets=budgets,
        playbook_enforcement=str(raw["playbook_enforcement"]),
        structured_output=str(raw["structured_output"]),
        advisory_verbosity=str(raw["advisory_verbosity"]),
        max_tools_per_role=int(raw["max_tools_per_role"]),
        deliberation=str(raw["deliberation"]),
        tool_schema_trim=bool(raw["tool_schema_trim"]),
    )
    # Enforce the §8 budget invariant.
    if not profile.budget_ok():
        raise TierError(
            f"{source} [{name}]: budget overflow — "
            f"slots sum {profile.budgets.total} + headroom {profile.generation_headroom} "
            f"= {profile.budgets.total + profile.generation_headroom} "
            f"> context_tokens {profile.context_tokens}"
        )
    return profile


def load_tier(name: str, *, path: Path | None = None) -> TierProfile:
    """Load a single tier profile by name from tiers.yaml."""
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    profiles = data.get("profiles", {})
    if name not in profiles:
        raise TierError(f"tier {name!r} not found in {p}; available: {sorted(profiles)}")
    return _profile_from_dict(name, profiles[name], str(p))


def load_all_tiers(*, path: Path | None = None) -> dict[str, TierProfile]:
    """Load all tier profiles from tiers.yaml."""
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    out: dict[str, TierProfile] = {}
    for name, raw in (data.get("profiles") or {}).items():
        out[name] = _profile_from_dict(name, raw, str(p))
    return out


def load_local_models(*, path: Path | None = None) -> dict:
    """Return tiers.yaml's ``local_models`` block — the shipped bundled-model
    defaults per role shape (T-W31, applying the T-W29 tokenmaxx bench).

    Keys: ``gbnf_worker`` / ``fanout`` (bundled model ids) and
    ``parallel_slots`` (recommended llama-server ``--parallel``). Returns ``{}``
    when the block is absent (older tiers.yaml). Shipped defaults only —
    routing.yaml role->model bindings and T-W28 user overrides always win.
    """
    p = path or _TIERS_PATH
    data = yaml.safe_load(p.read_text())
    schema = data.get("schema")
    if schema != TIER_SCHEMA:
        raise TierError(f"{p}: schema is {schema!r}, expected {TIER_SCHEMA!r}")
    block = data.get("local_models") or {}
    if not isinstance(block, dict):
        raise TierError(f"{p}: 'local_models' must be a mapping")
    return dict(block)


# --------------------------------------------------------------------------- tier detection

def detect_tier(context_length: int) -> str:
    """Map a model's context window length to a tier name.

    Uses ``CONTEXT_LENGTH_TIER_MAP`` thresholds (≤ threshold → tier).
    Falls back to ``frontier`` for very large contexts.
    """
    for threshold, tier_name in CONTEXT_LENGTH_TIER_MAP:
        if context_length <= threshold:
            return tier_name
    return FRONTIER_TIER


def detect_tier_from_env() -> Optional[str]:
    """Return the tier name from ``AI_CAD_TIER`` env var, or ``None`` if unset.

    The env var always wins over auto-detection (manual override).
    """
    val = os.environ.get(TIER_ENV_VAR, "").strip()
    return val if val else None


# --------------------------------------------------------------------------- tool schema trim

def trim_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Truncate a tool's description to its first sentence.

    Used by local tiers (``tool_schema_trim: true``) to shrink the tools slot.
    Operates on a copy; never mutates the original registry entry.

    Expected schema shape: ``{"description": "...", ...}`` (MCP tool schema).
    """
    if not isinstance(schema, dict):
        return schema
    desc = schema.get("description")
    if not desc:
        return schema
    # First sentence = text up to (and including) the first period, '!', or '?',
    # OR the entire string if no sentence boundary found.
    for i, ch in enumerate(desc):
        if ch in ".!?":
            short = desc[: i + 1].strip()
            if short and short != desc.strip():
                out = dict(schema)
                out["description"] = short
                return out
            break
    return schema


# --------------------------------------------------------------------------- grammar helper

def grammar_for_tier(
    profile: TierProfile,
    handoff_schema: dict[str, Any],
) -> Optional[str]:
    """Return the GBNF grammar string for the handoff schema, or ``None``.

    Only produces a grammar for ``structured_output: gbnf`` tiers (local-24k,
    local-48k).  The grammar is built by ``roles.schema_to_gbnf`` (T-W7 emitter)
    and passed to the provider as ``grammar=``.

    Import is deferred to avoid a circular import (roles → tiers → roles).
    """
    if not profile.uses_gbnf:
        return None
    from .roles import schema_to_gbnf  # deferred — tiers is imported by roles in tests
    return schema_to_gbnf(handoff_schema)


# --------------------------------------------------------------------------- effort resolution (T-W28)

def resolve_effort(tier: str, **kwargs):
    """Resolve the effective harness effort profile for *tier*.

    Shipped defaults (this module's tiers.yaml + the shipped code constants)
    layered under the active effort preset (services/agent/effort_presets.yaml)
    and per-tier user overrides (config.json ``ai_effort``).  The mission
    pipeline, the design gym and the settings GUI all resolve through this ONE
    path.  Import is deferred (mirrors grammar_for_tier) to avoid cycles —
    effort.py imports this module.
    """
    from services.agent.effort import resolve_effort as _resolve
    return _resolve(tier, **kwargs)


# --------------------------------------------------------------------------- turn assembler

def _count_tokens(text: str) -> int:
    """Estimate token count using the repo-wide 4-chars/token heuristic."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _compact_messages(messages: list[dict], level: int) -> list[dict]:
    """Compact a message list in place (mirrors loop.py _apply_compaction).

    Level 1: drop image parts.
    Level 2: also truncate text parts to 1200 chars.
    Level 3: also truncate text parts to 600 chars.

    Returns a new list (non-destructive — the assembler works on a copy).
    """
    import copy
    msgs = copy.deepcopy(messages)
    for msg in msgs:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        if level >= 1:
            msg["content"] = [p for p in content if p.get("type") != "image"]
            content = msg["content"]
        limit = None
        if level >= 3:
            limit = 600
        elif level >= 2:
            limit = 1200
        if limit is not None:
            for p in content:
                if p.get("type") == "text" and len(p["text"]) > limit:
                    p["text"] = p["text"][:limit] + "\n...[truncated]"
    return msgs


def _render_messages(messages: list[dict]) -> str:
    """Render a message list to a plain-text history string for token counting.

    Format mirrors what most providers see: alternating role: content lines.
    Used only for budget estimation — not the wire format.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                p["text"] for p in content if p.get("type") == "text"
            )
        else:
            text = str(content)
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


@dataclass
class AssembledTurn:
    """Result of ``TurnAssembler.assemble()``."""
    text: str                        # the full assembled context string
    slot_tokens: dict[str, int]      # per-slot token counts (post-compaction)
    total_tokens: int                # sum of slot_tokens
    overflow_applied: bool           # True if shrink was needed
    compaction_level: int            # 0 = no compaction, 1-3 = loop.py levels


class TurnAssembler:
    """Assemble one model turn's context in §9.1 slot order, within the tier budget.

    Usage::

        profile = load_tier("local-24k")
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system=card.system_prompt,
            ledger_text=ledger_render(ledger, profile.budgets.ledger),
            state_text=render_stage_state(card, describer=describer),
            advisory_text=render_advisory(advisory, profile.budgets.advisory),
            tools_text="",          # rendered tool schemas, if applicable
            history_messages=[...], # raw message dicts from the session
        )
        # result.text is what goes into Provider.complete_ex(system=..., messages=[...])
        # For local tiers, split result.text back into system + history at the seam.

    Overflow policy (§8):
      1. Shrink history first (compact message list, levels 1→3, then empty entirely).
      2. Shrink state (down to STATE_FLOOR_TOKENS).
      3. Never shrink system or advisory below their allocated budgets.

    Note: the renderers (ledger_render, render_advisory) already truncate to their
    slot budgets.  The assembler's job is to count the assembled total, detect
    overflow against ``available_tokens``, and apply the residual guard.
    """

    def __init__(self, profile: TierProfile) -> None:
        self.profile = profile

    def assemble(
        self,
        *,
        system: str = "",
        ledger_text: str = "",
        state_text: str = "",
        advisory_text: str = "",
        tools_text: str = "",
        history_messages: list[dict] | None = None,
    ) -> AssembledTurn:
        """Assemble the turn context.  Returns ``AssembledTurn``."""
        profile = self.profile
        budget = profile.budgets
        available = profile.available_tokens

        # ---- Slot 1-5: fixed (system, ledger, state, advisory, tools) -----
        # These are pre-rendered by their owners to their slot budgets.
        # We count them as-is; overflow must come from history or state.
        slots_fixed = {
            "system":   system,
            "ledger":   ledger_text,
            "state":    state_text,
            "advisory": advisory_text,
            "tools":    tools_text,
        }
        fixed_tokens = sum(_count_tokens(v) for v in slots_fixed.values())

        # ---- Slot 6: history — compaction loop ----------------------------
        msgs = list(history_messages or [])
        overflow_applied = False
        compaction_level = 0

        history_budget = available - fixed_tokens
        if history_budget < 0:
            # Fixed slots already exceed budget; compaction cannot help here —
            # mark overflow but don't touch history (the individual renderers
            # are responsible for their slot budgets).
            overflow_applied = True
            history_text = _render_messages(msgs)
        else:
            history_text = _render_messages(msgs)
            history_tokens = _count_tokens(history_text)

            if history_tokens > history_budget:
                overflow_applied = True
                # Try compaction levels 1 → 3 until we fit.
                for level in (1, 2, 3):
                    msgs = _compact_messages(list(history_messages or []), level)
                    history_text = _render_messages(msgs)
                    history_tokens = _count_tokens(history_text)
                    compaction_level = level
                    if history_tokens <= history_budget:
                        break
                # If still over, truncate to budget_chars directly.
                if history_tokens > history_budget:
                    limit_chars = history_budget * _CHARS_PER_TOKEN
                    history_text = history_text[:limit_chars] + "\n...[history truncated for budget]"
                    compaction_level = 3

        # ---- State shrink (second resort) ----------------------------------
        # If fixed_tokens (including state) still exceed available after history
        # compaction, shrink state down to STATE_FLOOR_TOKENS.
        state_tokens = _count_tokens(state_text)
        if fixed_tokens + _count_tokens(history_text) > available:
            state_floor_chars = STATE_FLOOR_TOKENS * _CHARS_PER_TOKEN
            if len(state_text) > state_floor_chars:
                state_text = state_text[:state_floor_chars] + "\n...[state truncated for budget]"
                slots_fixed["state"] = state_text
                overflow_applied = True

        # ---- Assemble final text in §9.1 order ----------------------------
        ordered = [
            slots_fixed["system"],
            slots_fixed["ledger"],
            slots_fixed["state"],
            slots_fixed["advisory"],
            slots_fixed["tools"],
            history_text,
        ]
        assembled = "\n\n".join(s.strip() for s in ordered if s.strip())

        # ---- Slot token counts (for transparency / ledger logging) --------
        slot_tokens = {name: _count_tokens(v) for name, v in slots_fixed.items()}
        slot_tokens["history"] = _count_tokens(history_text)
        total_tokens = sum(slot_tokens.values())

        return AssembledTurn(
            text=assembled,
            slot_tokens=slot_tokens,
            total_tokens=total_tokens,
            overflow_applied=overflow_applied,
            compaction_level=compaction_level,
        )
