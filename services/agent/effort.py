"""effort.py - harness effort resolution: presets + per-tier user overrides (T-W28).

Contract: contracts/advisor.md §8 + 1.3 addendum (deliberation dial) + the §8.1
effort addendum added by T-W28 (additive 1.4 → 1.5).

The ONE resolution path
-----------------------
``resolve_effort(tier)`` is the single function through which the mission
pipeline (loop.run_mission), the design gym (tools/flywheel/bench/gym) and the
settings GUI (via the ``python3 -m services.agent.effort`` CLI) all obtain the
effective harness knob set. Precedence, lowest to highest:

    shipped baseline   - derived live from tiers.yaml + the shipped code
                         constants (ladder / councils / delegation / failures /
                         overseer / triggers). Nothing duplicated, nothing
                         invented: see ``_shipped_baseline``.
    preset overrides   - services/agent/effort_presets.yaml (aicad.effort/1),
                         ``overrides.all`` then ``overrides.<tier>``.
    user overrides     - config.json ``ai_effort.overrides.<tier>`` (the same
                         config.json config.py already owns; save_config's
                         read-merge-write keeps other keys intact).

Every resolved knob carries provenance ("shipped" | "preset:<level>" |
"override") so the GUI can show which layer supplies each value.

Freedom rule (warn-never-block)
-------------------------------
``warnings_for(profile)`` mirrors routing.warnings_for: it returns plain-string
advisories explaining WHY a combination is questionable. Nothing is ever
blocked or silently clamped - a user override above a contract cap is stored
verbatim and warned about; runtime enforcement stays where it always lived
(e.g. delegation.DEPTH_CAP).

Ledger snapshot
---------------
``record_effort_snapshot(ledger, ...)`` appends one ``kind="effort_profile"``
event with the fully-resolved per-tier values at mission start, so a replayed
ledger shows exactly which effort profile the mission ran under.

CLI (GUI seam, mirrors services/agent/routing.py)
-------------------------------------------------
    python3 -m services.agent.effort get        → view JSON on stdout
    python3 -m services.agent.effort set        → reads {"level","mode","overrides"}
                                                  JSON on stdin, persists, echoes view
    python3 -m services.agent.effort validate   → reads the same JSON on stdin,
                                                  echoes {"ok": true, "warnings": {...}}
                                                  WITHOUT persisting
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from services.agent.tiers import TierProfile, load_all_tiers

EFFORT_SCHEMA = "aicad.effort/1"

_PRESETS_PATH = Path(__file__).resolve().parent / "effort_presets.yaml"

# Simple-mode levels, in dial order.
LEVELS: tuple[str, ...] = ("eco", "standard", "deliberate", "maximum")
DEFAULT_LEVEL = "standard"
MODES: tuple[str, ...] = ("simple", "advanced")
DEFAULT_MODE = "simple"

# The config.json key this module owns (config.save_config merges shallowly,
# so writing {"ai_effort": {...}} never clobbers provider/api_key/etc.).
CONFIG_KEY = "ai_effort"


class EffortError(ValueError):
    """Structurally invalid effort config (unknown preset/knob/tier/type)."""


# --------------------------------------------------------------------------- knob table
#
# name → (group, type, choices) - the GUI's grouping contract (task DoD groups:
# Deliberation / Parallelization / Safety nets / Context). type is "enum",
# "bool" or "int"; enums list their choices. Structural int floors are NOT
# enforced here (freedom rule) - they only inform warnings.

KNOBS: dict[str, dict[str, Any]] = {
    # -- Deliberation --------------------------------------------------------
    "deliberation":            {"group": "deliberation", "type": "enum",
                                "choices": ("full", "standard", "minimal"),
                                "label": "Deliberation dial",
                                "doc": "Master dial (§8 1.3): gates councils, ponders, trigger packs."},
    "ladder":                  {"group": "deliberation", "type": "enum",
                                "choices": ("mandatory", "optional", "off"),
                                "label": "Cognition ladder",
                                "doc": "§22 rungs A-D. Validators always run regardless."},
    "ladder_max_bounces":      {"group": "deliberation", "type": "int",
                                "label": "Ladder bounces",
                                "doc": "Validator bounces per rung before escalation (shipped: ladder.MAX_BOUNCES)."},
    "council":                 {"group": "deliberation", "type": "enum",
                                "choices": ("always", "risk_gated", "off"),
                                "label": "Councils",
                                "doc": "§19 build-strategy councils: always / only when risk warrants / never."},
    "council_seats":           {"group": "deliberation", "type": "int",
                                "label": "Council seats",
                                "doc": "Seated personas per council (shipped: the 4 seats in council_seats.yaml)."},
    "council_feature_threshold": {"group": "deliberation", "type": "int",
                                "label": "Council feature threshold",
                                "doc": "Feature count above which a build-strategy council convenes."},
    "ponder_round1_tokens":    {"group": "deliberation", "type": "int",
                                "label": "Ponder budget, round 1",
                                "doc": "max_tokens per council seat, round 1 (shipped: councils.py)."},
    "ponder_round2_tokens":    {"group": "deliberation", "type": "int",
                                "label": "Ponder budget, round 2",
                                "doc": "max_tokens per council seat, round 2 / dissent (shipped: councils.py)."},
    # -- Parallelization -----------------------------------------------------
    "delegation_depth":        {"group": "parallelization", "type": "int",
                                "label": "Delegation depth",
                                "doc": "Sub-agent tree depth. Contract §12 hard-caps runtime at 3."},
    "worker_fan_out":          {"group": "parallelization", "type": "int",
                                "label": "Worker fan-out",
                                "doc": "Max concurrent children per node. 0 = uncapped (shipped: no cap exists)."},
    "multi_doc_parallel":      {"group": "parallelization", "type": "bool",
                                "label": "Parallel multi-doc parts",
                                "doc": "Allow parallel work on multiple documents via T-W16 doc leases."},
    # -- Safety nets ---------------------------------------------------------
    "preflight":               {"group": "safety", "type": "bool",
                                "label": "Preflight checks",
                                "doc": "§10 preflight before mutating tool calls."},
    "overseer_cadence":        {"group": "safety", "type": "enum",
                                "choices": ("every_trigger", "stage_exit_only", "manual_only"),
                                "label": "Overseer audit cadence",
                                "doc": "§11 audit trigger points honoured (shipped: all, debounced)."},
    "failure_max_attempts":    {"group": "safety", "type": "int_map",
                                "label": "Failure-card attempt budgets",
                                "doc": "Per failure class max fixer attempts (shipped: advisor/failures.py)."},
    # -- Context -------------------------------------------------------------
    "history_budget_tokens":   {"group": "context", "type": "int",
                                "label": "History budget",
                                "doc": "Per-turn history slot tokens (shipped: tiers.yaml budgets.history)."},
    "trigger_injection":       {"group": "context", "type": "bool",
                                "label": "Trigger injection",
                                "doc": "§20 domain trigger packs (threads, bearing fits, ...)."},
    "trigger_cooldown_scale":  {"group": "context", "type": "float",
                                "label": "Trigger cooldown scale",
                                "doc": "Multiplier on per-trigger cooldown_turns. 1.0 = shipped cadence."},
    "context_pack_verbosity":  {"group": "context", "type": "enum",
                                "choices": ("full", "compact"),
                                "label": "Context-pack verbosity",
                                "doc": "Advisory/context-pack rendering (shipped: tiers.yaml advisory_verbosity)."},
}

GROUPS: tuple[str, ...] = ("deliberation", "parallelization", "safety", "context")


# --------------------------------------------------------------------------- shipped baseline

def _ladder_for_deliberation(deliberation: str) -> str:
    """Shipped ladder mode per deliberation level (ladder.py docstring rule:
    MANDATORY at full, OPTIONAL at minimal; standard maps to optional)."""
    return "mandatory" if deliberation == "full" else "optional"


def _council_for_deliberation(deliberation: str) -> str:
    """Shipped council mode per deliberation level (tiers.yaml comments:
    full → always on; standard/minimal → risk-gated)."""
    return "always" if deliberation == "full" else "risk_gated"


def _shipped_baseline(profile: TierProfile) -> dict[str, Any]:
    """Derive the shipped knob values for one tier. Every number here comes
    from an existing shipped constant - see the import sites below."""
    # Deferred imports: pull the real shipped constants so the baseline can
    # never drift from the code (numbers provenance rule).
    from services.agent.ladder import MAX_BOUNCES                      # = 2
    from services.agent.delegation import DEPTH_CAP                   # = 3
    from services.agent.councils import _DEFAULT_FEATURE_THRESHOLD    # = 5
    from services.agent.advisor.failures import _DEFAULT_BUDGETS

    return {
        # Deliberation - tiers.yaml is the master source for the dial.
        "deliberation": profile.deliberation,
        "ladder": _ladder_for_deliberation(profile.deliberation),
        "ladder_max_bounces": MAX_BOUNCES,
        "council": _council_for_deliberation(profile.deliberation),
        "council_seats": 4,                      # the 4 authored seats in council_seats.yaml
        "council_feature_threshold": _DEFAULT_FEATURE_THRESHOLD,
        "ponder_round1_tokens": 1500,            # councils.py round-1 max_tokens
        "ponder_round2_tokens": 600,             # councils.py round-2/dissent max_tokens
        # Parallelization
        "delegation_depth": DEPTH_CAP,
        "worker_fan_out": 0,                     # shipped: no fan-out cap exists
        "multi_doc_parallel": True,              # shipped: no restriction exists
        # Safety nets
        "preflight": True,                       # shipped: preflight always runs
        "overseer_cadence": "every_trigger",     # shipped: all §11 trigger points
        "failure_max_attempts": {k: v["max_attempts"] for k, v in _DEFAULT_BUDGETS.items()},
        # Context
        "history_budget_tokens": profile.budgets.history,
        "trigger_injection": True,               # shipped: builtin trigger packs active
        "trigger_cooldown_scale": 1.0,           # 1.0 = shipped per-trigger cooldowns
        "context_pack_verbosity": profile.advisory_verbosity,
    }


# --------------------------------------------------------------------------- presets

def load_presets(*, path: Path | None = None) -> dict[str, Any]:
    """Load and structurally validate effort_presets.yaml."""
    p = path or _PRESETS_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != EFFORT_SCHEMA:
        raise EffortError(f"{p}: schema is {data.get('schema') if isinstance(data, dict) else data!r}, "
                          f"expected {EFFORT_SCHEMA!r}")
    presets = data.get("presets") or {}
    missing = [lvl for lvl in LEVELS if lvl not in presets]
    if missing:
        raise EffortError(f"{p}: presets missing levels: {missing}")
    for lvl, spec in presets.items():
        for scope, kv in (spec.get("overrides") or {}).items():
            _validate_knob_dict(kv, f"{p} [{lvl}.overrides.{scope}]")
    return data


def _validate_knob_dict(kv: Any, source: str) -> None:
    if not isinstance(kv, dict):
        raise EffortError(f"{source}: expected a knob mapping, got {type(kv).__name__}")
    for name, value in kv.items():
        spec = KNOBS.get(name)
        if spec is None:
            raise EffortError(f"{source}: unknown knob {name!r}; known: {sorted(KNOBS)}")
        t = spec["type"]
        if t == "enum" and value not in spec["choices"]:
            raise EffortError(f"{source}: {name}={value!r} not in {spec['choices']}")
        if t == "bool" and not isinstance(value, bool):
            raise EffortError(f"{source}: {name} must be bool, got {value!r}")
        if t == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise EffortError(f"{source}: {name} must be int, got {value!r}")
        if t == "float" and not isinstance(value, (int, float)):
            raise EffortError(f"{source}: {name} must be a number, got {value!r}")
        if t == "int_map" and (not isinstance(value, dict)
                               or any(isinstance(v, bool) or not isinstance(v, int)
                                      for v in value.values())):
            raise EffortError(f"{source}: {name} must map class → int attempts, got {value!r}")


# --------------------------------------------------------------------------- tier normalization

def normalize_tier(name: str, tiers: dict[str, TierProfile]) -> str:
    """Map a caller's tier string onto a tiers.yaml profile name.

    Accepts exact names plus prefixed aliases the gym uses (e.g.
    ``frontier-200k`` → ``frontier``). Raises EffortError when nothing matches
    (never guesses silently).
    """
    if name in tiers:
        return name
    for known in sorted(tiers, key=len, reverse=True):
        if name.startswith(known):
            return known
    raise EffortError(f"unknown tier {name!r}; known: {sorted(tiers)}")


# --------------------------------------------------------------------------- resolution

@dataclass(frozen=True)
class EffortProfile:
    """The fully-resolved knob set for one tier."""
    tier: str
    level: str                                   # active preset level
    values: dict[str, Any]                       # knob → resolved value
    sources: dict[str, str]                      # knob → "shipped"|"preset:<lvl>"|"override"
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {"tier": self.tier, "level": self.level,
                "values": self.values, "sources": self.sources,
                "warnings": list(self.warnings)}


def _effort_config(config: dict | None) -> dict:
    """Extract + default the ai_effort block from a config.json dict."""
    if config is None:
        from services.agent.config import load_config
        config = load_config()
    block = config.get(CONFIG_KEY) or {}
    level = block.get("level", DEFAULT_LEVEL)
    if level not in LEVELS:
        raise EffortError(f"config.json {CONFIG_KEY}.level={level!r} not in {LEVELS}")
    mode = block.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise EffortError(f"config.json {CONFIG_KEY}.mode={mode!r} not in {MODES}")
    overrides = block.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise EffortError(f"config.json {CONFIG_KEY}.overrides must be a mapping")
    return {"level": level, "mode": mode, "overrides": overrides}


def resolve_effort(
    tier: str,
    *,
    config: dict | None = None,
    presets_path: Path | None = None,
    tiers_path: Path | None = None,
) -> EffortProfile:
    """Resolve the effective effort profile for *tier*.

    Precedence: shipped baseline < preset.all < preset.<tier> < user override.
    Values are NEVER clamped (freedom rule); see ``warnings_for``.
    """
    tiers = load_all_tiers(path=tiers_path)
    tname = normalize_tier(tier, tiers)
    eff = _effort_config(config)
    presets = load_presets(path=presets_path)

    values = _shipped_baseline(tiers[tname])
    sources = {k: "shipped" for k in values}

    level = eff["level"]
    spec = presets["presets"][level]
    for scope in ("all", tname):
        for name, value in ((spec.get("overrides") or {}).get(scope) or {}).items():
            values[name] = value
            sources[name] = f"preset:{level}"

    user_kv = eff["overrides"].get(tname) or {}
    _validate_knob_dict(user_kv, f"config.json {CONFIG_KEY}.overrides.{tname}")
    for name, value in user_kv.items():
        values[name] = value
        sources[name] = "override"

    prof = EffortProfile(tier=tname, level=level, values=values, sources=sources)
    return EffortProfile(tier=tname, level=level, values=values, sources=sources,
                         warnings=warnings_for(prof, tiers[tname]))


# --------------------------------------------------------------------------- warnings

def warnings_for(profile: EffortProfile, tier_profile: TierProfile) -> list[str]:
    """Non-blocking advisories. Warn with WHY, never block, never clamp —
    mirrors routing.warnings_for."""
    v = profile.values
    out: list[str] = []

    from services.agent.delegation import DEPTH_CAP
    if v["delegation_depth"] > DEPTH_CAP:
        out.append(
            f"delegation depth {v['delegation_depth']} exceeds the contract §12 hard cap "
            f"({DEPTH_CAP}) - the runtime will still enforce the cap; the extra depth buys nothing")
    if v["council"] == "always" and v["deliberation"] == "minimal":
        out.append(
            "councils are set to 'always' while the deliberation dial is 'minimal' - "
            "every mission convenes councils yet the rest of the minimal dial skips the "
            "machinery that makes their output cheap to consume; consider 'risk_gated'")
    if v["council"] != "off" and v["council_seats"] < 2:
        out.append(
            f"a council with {v['council_seats']} seat(s) cannot dissent - "
            "two or more seats are needed for round-2 disagreement to mean anything")
    if not v["preflight"]:
        out.append(
            "preflight is off - mutating tool calls reach the engine unchecked; "
            "failures surface later as rebuild errors instead of being caught up front (§10)")
    if v["overseer_cadence"] == "manual_only":
        out.append(
            "overseer audits are manual-only - plan ratification and stage exits go "
            "unaudited unless someone asks (§11)")
    if not v["trigger_injection"] and v["deliberation"] == "full":
        out.append(
            "full deliberation with trigger injection off - councils run but domain "
            "guardrail packs (threads, bearing fits, ...) never inject (§20)")
    if any(a <= 0 for a in v["failure_max_attempts"].values()):
        zeroed = sorted(k for k, a in v["failure_max_attempts"].items() if a <= 0)
        out.append(
            f"failure classes {zeroed} have 0 fix attempts - every such failure escalates "
            "straight to the user (§5 on_exhaust)")
    # §8 budget invariant check: swapping the history budget must keep
    # slots + headroom ≤ context_tokens, else the assembler shrinks every turn.
    b = tier_profile.budgets
    fixed = b.system + b.ledger + b.state + b.advisory + b.tools
    if fixed + v["history_budget_tokens"] + tier_profile.generation_headroom > tier_profile.context_tokens:
        out.append(
            f"history budget {v['history_budget_tokens']} breaks the §8 invariant for "
            f"{profile.tier} (slots + headroom > context {tier_profile.context_tokens}) - "
            "the turn assembler will shrink history/state on every turn to compensate")
    if profile.level == "maximum" and profile.tier == "frontier":
        out.append(
            "Maximum effort on the frontier tier runs councils, ponders and full context "
            "packs on paid tokens - expect the highest possible per-mission cost")
    return out


# --------------------------------------------------------------------------- ledger snapshot

def record_effort_snapshot(
    ledger: Any,
    *,
    active_tier: str | None = None,
    config: dict | None = None,
    presets_path: Path | None = None,
) -> dict:
    """Append the resolved effort profile(s) as one ledger event at mission start.

    Records ALL tiers (the mission may delegate across tiers via routing), plus
    which tier is known-active if the caller knows it. Returns the event dict.
    Event kind: ``effort_profile`` (replayable - payload carries every resolved
    value + its source).
    """
    tiers = load_all_tiers()
    payload: dict[str, Any] = {
        "schema": EFFORT_SCHEMA,
        "active_tier": active_tier,
        "tiers": {},
    }
    level = None
    for name in tiers:
        prof = resolve_effort(name, config=config, presets_path=presets_path)
        payload["tiers"][name] = prof.to_payload()
        level = prof.level
    payload["level"] = level
    return ledger.append(actor="harness", kind="effort_profile", payload=payload)


# --------------------------------------------------------------------------- persistence + view

def load_effort_settings(*, config: dict | None = None) -> dict:
    """Return {"level","mode","overrides"} from config.json (defaults applied)."""
    return _effort_config(config)


def save_effort_settings(settings: dict) -> dict:
    """Validate + persist {"level","mode","overrides"} into config.json.

    Warnings never block a save (freedom rule); structural errors (unknown
    knob/level/tier) DO raise - those are malformed input, not user freedom.
    """
    level = settings.get("level", DEFAULT_LEVEL)
    if level not in LEVELS:
        raise EffortError(f"level {level!r} not in {LEVELS}")
    mode = settings.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise EffortError(f"mode {mode!r} not in {MODES}")
    overrides = settings.get("overrides") or {}
    tiers = load_all_tiers()
    clean: dict[str, dict] = {}
    for tname, kv in overrides.items():
        canon = normalize_tier(tname, tiers)
        _validate_knob_dict(kv, f"overrides.{tname}")
        clean.setdefault(canon, {}).update(kv)
    from services.agent.config import save_config
    block = {"level": level, "mode": mode, "overrides": clean}
    save_config({CONFIG_KEY: block})
    return block


def view(*, config: dict | None = None, presets_path: Path | None = None) -> dict:
    """The GUI handoff shape (mirrors routing.view): presets + per-tier
    resolved knobs with provenance + warnings + knob metadata for layout."""
    presets = load_presets(path=presets_path)
    eff = _effort_config(config)
    tiers = load_all_tiers()
    out: dict[str, Any] = {
        "schema": EFFORT_SCHEMA,
        "level": eff["level"],
        "mode": eff["mode"],
        "levels": [
            {"id": lvl,
             "label": presets["presets"][lvl].get("label", lvl.title()),
             "hint": presets["presets"][lvl].get("hint", "")}
            for lvl in LEVELS
        ],
        "groups": list(GROUPS),
        "knobs": {name: {k: (list(v) if isinstance(v, tuple) else v)
                         for k, v in spec.items()}
                  for name, spec in KNOBS.items()},
        "tiers": {},
    }
    for name in tiers:
        prof = resolve_effort(name, config=config, presets_path=presets_path)
        out["tiers"][name] = prof.to_payload()
    return out


# --------------------------------------------------------------------------- CLI (GUI seam)

def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m services.agent.effort")
    ap.add_argument("cmd", choices=("get", "set", "validate"))
    ap.add_argument("--presets", default=None, help="effort_presets.yaml path (default: shipped)")
    args = ap.parse_args(argv)
    presets_path = Path(args.presets) if args.presets else None

    try:
        if args.cmd == "get":
            print(json.dumps(view(presets_path=presets_path)))
            return 0
        raw = json.loads(sys.stdin.read() or "{}")
        settings = {
            "level": raw.get("level", DEFAULT_LEVEL),
            "mode": raw.get("mode", DEFAULT_MODE),
            "overrides": raw.get("overrides") or {},
        }
        if args.cmd == "validate":
            # Validate structurally + report warnings, WITHOUT persisting.
            tiers = load_all_tiers()
            for tname, kv in settings["overrides"].items():
                normalize_tier(tname, tiers)
                _validate_knob_dict(kv, f"overrides.{tname}")
            if settings["level"] not in LEVELS:
                raise EffortError(f"level {settings['level']!r} not in {LEVELS}")
            cfg = {CONFIG_KEY: settings}
            warns = {name: resolve_effort(name, config=cfg, presets_path=presets_path).warnings
                     for name in tiers}
            print(json.dumps({"ok": True, "warnings": warns}))
            return 0
        # set: persist, then echo the fresh view.
        save_effort_settings(settings)
        print(json.dumps(view(presets_path=presets_path)))
        return 0
    except (EffortError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli(sys.argv[1:]))
