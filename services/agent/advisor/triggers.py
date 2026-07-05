"""Trigger-based context injection — aicad.trigger/1 (T-W21).

Contract: ``contracts/advisor.md`` §20 (FROZEN).

Design
------
* **Cheap matching**: all keyword patterns are compiled once at module load into a
  single list of ``re.Pattern`` per trigger.  One pass over the input text per
  trigger; no LLM in the hot path.
* **Cooldown**: the caller passes a mutable ``cooldown_state`` dict
  ``{trigger_id: last_fired_seq}`` so suppression is testable without host state.
* **Prerun calcs**: per-trigger input extraction via named-group regexes; only
  runs if the match succeeded.  ``ok=False`` from a calc → pack injected without
  numbers (fail-soft per §20).
* **Budget enforcement**: per-trigger ``budget_tokens`` caps individual pack text
  (truncation by whole lines).  The global advisory budget (render.py) does the
  final drop-first pass.
* **Ledger events**: ``match_triggers`` returns structured fire records
  ``{trigger_id, matched, tokens}``; the host writes them via
  ``ledger_note(run_id, "trigger_fired", …)`` (§2).
* **Zero-cost no-match path**: no file read, no calc, no allocation when no
  trigger fires.

Public API
----------
- :func:`match_triggers` — evaluate all triggers against an observation turn;
  returns a list of injected ``Finding``-shaped dicts and a list of ledger events.
- :func:`load_table` — parse ``triggers.yaml`` (cached after first call).
"""
from __future__ import annotations

import re
import pathlib
from typing import Any, Optional

_YAML_PATH = pathlib.Path(__file__).parent / "triggers.yaml"   # spec file (human-readable)
_PACKS_DIR = pathlib.Path(__file__).parent / "context_packs"

# ---------------------------------------------------------------------------
# Builtin trigger table (mirrors triggers.yaml exactly).
# This is the runtime source; triggers.yaml is the canonical spec for review.
# No PyYAML dep required — the venv does not include it.
# ---------------------------------------------------------------------------

_BUILTIN_TRIGGERS: list[dict] = [
    {
        "id": "thread_mention",
        "match": {
            "keywords": [
                r"\btap\b", r"\bthread\b", r"\bthreaded\b",
                r"M\d+(?:\.\d+)?(?:[xX×]\d+(?:\.\d+)?)?",
                r"\bUNC\b", r"\bUNF\b", r"\bpilot hole\b", r"\btap drill\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "threads",
            "prerun_calcs": [
                {"calc": "tap_drill", "inputs_from": "match",
                 "input_map": {"thread": "thread_designation"}},
            ],
            "exemplar": "M6x1.0 → pilot 5.0 mm (Machinery’s Handbook; ISO 2306 75% engagement)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "bearing_seat",
        "match": {
            "keywords": [
                r"\bbearing\b", r"\bbearing seat\b", r"\bbore.*press",
                r"\binner race\b", r"\bouter race\b", r"\bshaft seat\b",
            ],
            "tools": ["part_search", "part_place"],
            "events": [],
        },
        "inject": {
            "context_pack": "bearing_fits",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "literal",
                 "input_map": {"nominal": None, "fit": "H7/g6"}},
            ],
            "exemplar": "Shaft seat k6/m6 (press); housing bore H7/J7 (easy removal); source: iso286.json",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "press_fit_mention",
        "match": {
            "keywords": [
                r"\bpress fit\b", r"\bpress.fit\b", r"\binterference fit\b",
                r"\bknurled\b", r"\bpressed in\b", r"\bforced fit\b",
                r"\bp6\b", r"\bs6\b", r"\br6\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "press_fit",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "literal",
                 "input_map": {"nominal": None, "fit": "H7/p6"}},
            ],
            "exemplar": "H7/p6 on Ø20: min interference 0.017 mm, max 0.042 mm (ISO 286, fit_calc)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "snap_fit_mention",
        "match": {
            "keywords": [
                r"\bsnap fit\b", r"\bsnap.fit\b", r"\bcantilever hook\b",
                r"\blatch\b", r"\bdetent\b", r"\bsnap hook\b",
                r"\bsnap arm\b", r"\bclip\b",
            ],
            "tools": [],
            "events": [],
        },
        "inject": {
            "context_pack": "snap_fit",
            "prerun_calcs": [],
            "exemplar": "ABS cantilever: ε_allow ≈ 0.6×(yield/E); viable if ε_root ≤ ε_allow (snap_fit calc)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "thin_wall",
        "match": {
            "keywords": [
                r"\bthin wall\b", r"\bthin-wall\b", r"\bwall thickness\b",
                r"\bshell\b", r"\bminimum wall\b",
            ],
            "tools": ["wall_thickness", "shell"],
            "events": ["thin_wall"],
        },
        "inject": {
            "context_pack": "thin_wall",
            "prerun_calcs": [],
            "exemplar": "FDM: min wall 1.2 mm; SLA: 0.6 mm; CNC: depends on tool; source: wall_thickness.py",
        },
        "budget_tokens": 200,
        "cooldown_turns": 6,
    },
    {
        "id": "fastener_pattern",
        "match": {
            "keywords": [
                r"\bbolt circle\b", r"\bbolt pattern\b", r"\bscrew pattern\b",
                r"\bfastener pattern\b", r"\bPCD\b", r"\bflange bolt\b",
                r"\bclearance hole\b", r"\bcountersink\b", r"\bcounterbore\b",
            ],
            "tools": ["bolt_circle"],
            "events": [],
        },
        "inject": {
            "context_pack": "fastener_pattern",
            "prerun_calcs": [
                {"calc": "tap_drill", "inputs_from": "match",
                 "input_map": {"thread": "thread_designation"}},
            ],
            "exemplar": "M8 clearance hole: 8.5 mm pass-through (ISO 273 medium); pilot 6.8 mm (feeds_speeds.json)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 8,
    },
    {
        "id": "material_mention",
        "match": {
            "keywords": [
                r"\baluminum\b", r"\baluminium\b", r"\bsteel\b", r"\bstainless\b",
                r"\bABS\b", r"\bPLA\b", r"\bPETG\b", r"\btitanium\b",
                r"\bbr(?:ass|onze)\b", r"\bcast iron\b", r"\bpolycarbonate\b",
                r"\bPC\b", r"\bnylon\b",
            ],
            "tools": ["set_material", "research_material_lookup"],
            "events": [],
        },
        "inject": {
            "context_pack": "material_props",
            "prerun_calcs": [],
            "exemplar": "6061-T6 Al: E=68900 MPa, yield=276 MPa, ρ=2.70 g/cm³ (materials.json)",
        },
        "budget_tokens": 250,
        "cooldown_turns": 10,
    },
    {
        "id": "tolerance_mention",
        "match": {
            "keywords": [
                r"\btolerance\b", r"\bfit\b", r"\bH7\b", r"\bg6\b",
                r"\bf7\b", r"\bk6\b", r"\bp6\b", r"\bISO 286\b",
                r"\bclearance\b", r"\bsliding fit\b", r"\brunning fit\b",
            ],
            "tools": ["fit_calc", "measure_between"],
            "events": [],
        },
        "inject": {
            "context_pack": "tolerance_fits",
            "prerun_calcs": [
                {"calc": "fit_calc", "inputs_from": "match",
                 "input_map": {"nominal": "nominal_mm", "fit": "fit_designation"}},
            ],
            "exemplar": "H7/g6 sliding fit on Ø20: clearance 0.007–0.041 mm (ISO 286, iso286.json)",
        },
        "budget_tokens": 300,
        "cooldown_turns": 8,
    },
    {
        "id": "thread_engagement",
        "match": {
            "keywords": [
                r"\bengagement\b", r"\bthread length\b", r"\bstud\b",
                r"\bthread engagement\b", r"\bthrough-hole\b",
                r"\bblind hole\b", r"\bboss\b",
            ],
            "tools": ["research_thread_spec"],
            "events": [],
        },
        "inject": {
            "context_pack": "thread_engagement",
            "prerun_calcs": [],
            "exemplar": "Min engagement: 1.0×D steel-in-steel; 1.5×D aluminum; 2.0×D plastic (Machinery’s Handbook)",
        },
        "budget_tokens": 200,
        "cooldown_turns": 10,
    },
    # T-DP6 pre-op advisory injection triggers (append-only per DP3 coordination note).
    # Each fires BEFORE the model operates so relevant facts are in context at op time.
    {
        "id": "interference_pre_op",
        "match": {
            "keywords": [
                r"\bplace\b", r"\bassemble\b", r"\bmount\b", r"\bmate\b",
                r"\bcheck interference\b", r"\bclearance\b", r"\boverlap\b",
            ],
            "tools": ["part_place", "solve_mates", "check_interference"],
            "events": ["assembly_body_added"],
        },
        "inject": {
            "context_pack": "interference_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "After placing bodies run check_interference; interference > 0 mm³ is a "
                "blocker (advisor names axis + depth)"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 6,
    },
    {
        "id": "dfm_pre_op",
        "match": {
            "keywords": [
                r"\bmanufactur\b", r"\bmachine\b", r"\bprint\b", r"\bmold\b",
                r"\bprocess\b", r"\bDFM\b", r"\bdesign for\b",
                r"\bwall\b", r"\bdraft\b",
            ],
            "tools": ["set_process", "dfm_check", "wall_thickness", "resolve_process"],
            "events": [],
        },
        "inject": {
            "context_pack": "dfm_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "Run dfm_check before export; wall < process minimum and missing draft "
                "are blockers (advisor names delta + fix)"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 8,
    },
    {
        "id": "interface_check_pre_op",
        "match": {
            "keywords": [
                r"\binterface\b", r"\balign\b", r"\bhole pattern\b",
                r"\bbolt holes\b", r"\bcoaxial\b", r"\bcontact face\b",
                r"\bfastener\b", r"\bregister\b", r"\bpass.through\b",
            ],
            "tools": ["interface_check", "part_place", "solve_mates"],
            "events": [],
        },
        "inject": {
            "context_pack": "interface_check_pre_op",
            "prerun_calcs": [],
            "exemplar": (
                "interface_check catches hole-pattern offset, coaxiality error, "
                "contact-plane gap — all pass interference but are still wrong"
            ),
        },
        "budget_tokens": 200,
        "cooldown_turns": 8,
    },
]

_CHARS_PER_TOKEN = 4  # repo-wide heuristic (matches render.py / ledger.py)

# ---------------------------------------------------------------------------
# Named-group extraction regexes for prerun calc inputs
# ---------------------------------------------------------------------------

# Matches full metric thread designation: M6, M6x1.0, M6×1.0, M6 x 1.0
_THREAD_DESIGNATION_RE = re.compile(
    r"\b(M\d+(?:\.\d+)?(?:\s*[xX×]\s*\d+(?:\.\d+)?)?)\b"
)

# Matches ISO 286 fit designation: H7/g6, H7, g6, H8/f7
_FIT_DESIGNATION_RE = re.compile(
    r"\b([A-Z][A-Z]?\d+(?:[/\\][a-z][a-z]?\d+)?)\b"
)

# Matches bare nominal dimension (mm): "20 mm", "Ø20", "∅20", "d=25.4"
_NOMINAL_MM_RE = re.compile(
    r"(?:Ø|∅|ø|d\s*=\s*|\bdiameter\s+)(\d+(?:\.\d+)?)"
)

# ---------------------------------------------------------------------------
# Trigger table (parsed + compiled once)
# ---------------------------------------------------------------------------

_TABLE: Optional[list[dict]] = None


def _raw_triggers() -> list[dict]:
    """triggers.yaml is the runtime table when PyYAML is available (contract
    §20: the table is user-editable data); the builtin mirror is the fallback
    for yaml-less environments."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(_YAML_PATH.read_text())
        entries = data.get("triggers", []) if isinstance(data, dict) else []
        if entries:
            return entries
    except Exception:  # noqa: BLE001 — fail soft to the builtin mirror
        pass
    return _BUILTIN_TRIGGERS


def load_table() -> list[dict]:
    """Compile the trigger table and cache it.

    Runtime source: ``triggers.yaml`` when parseable (user-editable per
    contract §20), else :data:`_BUILTIN_TRIGGERS` (an exact mirror).

    Each entry is compiled from the raw dict into a matcher-ready form with
    pre-compiled ``re.Pattern`` objects.
    """
    global _TABLE
    if _TABLE is not None:
        return _TABLE

    compiled: list[dict] = []
    for t in _raw_triggers():
        match_spec = t.get("match", {})
        kw_patterns: list[re.Pattern] = []
        for kw in match_spec.get("keywords", []):
            try:
                kw_patterns.append(re.compile(kw, re.IGNORECASE))
            except re.error:
                pass  # skip malformed patterns silently

        compiled.append({
            "id": t["id"],
            "kw_patterns": kw_patterns,
            "tool_names": set(match_spec.get("tools", [])),
            "events": set(match_spec.get("events", [])),
            "inject": t.get("inject", {}),
            "budget_tokens": int(t.get("budget_tokens", 250)),
            "cooldown_turns": int(t.get("cooldown_turns", 8)),
        })

    _TABLE = compiled
    return _TABLE


# ---------------------------------------------------------------------------
# Pack loader + budgeter
# ---------------------------------------------------------------------------

def _load_pack(pack_name: str, budget_tokens: int) -> str:
    """Read a context pack file and truncate to *budget_tokens* whole lines."""
    pack_path = _PACKS_DIR / f"{pack_name}.md"
    try:
        text = pack_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"[context pack '{pack_name}' not found]"

    budget_chars = budget_tokens * _CHARS_PER_TOKEN
    if len(text) <= budget_chars:
        return text

    # Truncate by whole lines to stay within budget.
    lines: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = len(line) + 1
        if used + cost > budget_chars:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines) + "\n[… truncated to budget]"


# ---------------------------------------------------------------------------
# Prerun calc executor
# ---------------------------------------------------------------------------

def _extract_inputs(text: str, input_map: dict[str, str]) -> dict[str, Any]:
    """Extract calc inputs from *text* using named extraction groups.

    ``input_map`` maps calc param name → extraction-group name.
    Returns a partial dict; missing groups are omitted (caller decides skip).
    """
    extracted: dict[str, Any] = {}
    for param, group_name in input_map.items():
        if group_name == "thread_designation":
            m = _THREAD_DESIGNATION_RE.search(text)
            if m:
                extracted[param] = m.group(1).strip()
        elif group_name == "fit_designation":
            m = _FIT_DESIGNATION_RE.search(text)
            if m:
                extracted[param] = m.group(1).strip()
        elif group_name == "nominal_mm":
            m = _NOMINAL_MM_RE.search(text)
            if m:
                try:
                    extracted[param] = float(m.group(1))
                except ValueError:
                    pass
    return extracted


def _run_prerun_calc(calc_spec: dict, text: str) -> Optional[dict]:
    """Execute a single prerun calc spec against matched *text*.

    Returns the aicad.calc/1 answer dict, or ``None`` when the calc cannot
    fire (missing inputs, ok=False, or exception).  Always fails soft.
    """
    calc_id = calc_spec.get("calc")
    inputs_from = calc_spec.get("inputs_from", "literal")
    input_map = calc_spec.get("input_map", {})

    if not calc_id:
        return None

    # Build inputs
    inputs: dict[str, Any] = {}
    if inputs_from == "match":
        inputs = _extract_inputs(text, input_map)
        # If required inputs not extractable, skip (advertise only).
        if not inputs:
            return None
        # Remove None-valued items from input_map (literal nulls = advertise only)
        inputs = {k: v for k, v in inputs.items() if v is not None}
        if not inputs:
            return None
    elif inputs_from == "literal":
        # Only run if ALL mapped params have non-null literal values
        inputs = {k: v for k, v in input_map.items() if v is not None}
        if not inputs:
            return None

    try:
        if calc_id == "tap_drill":
            from engine_oracle.research.machining import tap_drill  # noqa: PLC0415
            result = tap_drill(inputs.get("thread", ""))
        elif calc_id == "fit_calc":
            from engine_oracle.research.fits import fit_calc  # noqa: PLC0415
            nominal = inputs.get("nominal")
            fit = inputs.get("fit", "H7/g6")
            if nominal is None:
                return None
            result = fit_calc(float(nominal), str(fit))
        elif calc_id == "snap_fit":
            from engine_oracle.research.calcs.snap_fit import snap_fit as _snap  # noqa: PLC0415
            result = _snap(**inputs)
        elif calc_id == "wall_thickness":
            from engine_oracle.research.calcs.wall_thickness import wall_thickness as _wt  # noqa: PLC0415
            result = _wt(**inputs)
        else:
            # Try the generic calc registry
            from engine_oracle.research.calcs.registry import run_calc  # noqa: PLC0415
            result = run_calc(calc_id, inputs)
    except Exception:  # noqa: BLE001
        return None

    if not result:
        return None
    # Some calcs (tap_drill) don't set ok=True explicitly — treat any result with
    # an "answer" key and no explicit ok=False as success.
    if result.get("ok") is False:
        return None
    answer = result.get("answer")
    if not answer or not isinstance(answer, dict):
        return None
    return answer


# ---------------------------------------------------------------------------
# Main matcher
# ---------------------------------------------------------------------------

def match_triggers(
    output_text: str,
    tool_names: Optional[list[str]] = None,
    events: Optional[list[str]] = None,
    seq: int = 0,
    cooldown_state: Optional[dict[str, int]] = None,
) -> tuple[list[dict], list[dict]]:
    """Evaluate all triggers against an observation turn.

    Parameters
    ----------
    output_text:
        The model's text output for this turn (and/or user text routed to the
        role).  Keyword patterns match against this string.
    tool_names:
        Tool names called this turn (tool-name trigger matching).
    events:
        Geometry event strings this turn (e.g. ``["thin_wall"]``).
    seq:
        The current advisory sequence counter (used for cooldown tracking).
    cooldown_state:
        Mutable ``{trigger_id: last_fired_seq}`` dict owned by the caller.
        Updated in-place when a trigger fires.  ``None`` → no cooldown
        (fires every time; useful in tests).

    Returns
    -------
    (context_items, ledger_events)
        *context_items* — list of advisory-item dicts (severity=info, check=trigger_id)
        ready to be appended to an advisory block's items list.
        *ledger_events* — list of ``{trigger_id, matched, tokens}`` fire records.
    """
    tool_set = set(tool_names or [])
    event_set = set(events or [])
    text = output_text or ""

    context_items: list[dict] = []
    ledger_events: list[dict] = []

    for trigger in load_table():
        tid = trigger["id"]

        # ── cooldown check ──────────────────────────────────────────────────
        if cooldown_state is not None:
            last = cooldown_state.get(tid, -9999)
            if (seq - last) < trigger["cooldown_turns"]:
                continue  # still in cooldown window

        # ── match ──────────────────────────────────────────────────────────
        matched: Optional[str] = None

        # Keyword match over text (single compiled pass per pattern)
        for pat in trigger["kw_patterns"]:
            m = pat.search(text)
            if m:
                matched = m.group(0)
                break

        # Tool-name match
        if matched is None and trigger["tool_names"] & tool_set:
            matched = next(iter(trigger["tool_names"] & tool_set))

        # Event match
        if matched is None and trigger["events"] & event_set:
            matched = next(iter(trigger["events"] & event_set))

        if matched is None:
            continue  # no match — zero cost from here

        # ── prerun calcs ────────────────────────────────────────────────────
        calc_numbers: dict[str, Any] = {}
        inject = trigger["inject"]
        for calc_spec in inject.get("prerun_calcs", []):
            answer = _run_prerun_calc(calc_spec, text)
            if answer and isinstance(answer, dict):
                calc_numbers.update(answer)

        # ── pack load + budget ─────────────────────────────────────────────
        pack_name = inject.get("context_pack", "")
        exemplar = inject.get("exemplar", "")
        budget = trigger["budget_tokens"]

        pack_text = _load_pack(pack_name, budget) if pack_name else ""
        if exemplar:
            pack_text = pack_text + f"\nExample: {exemplar}" if pack_text else f"Example: {exemplar}"

        # Measure token cost
        token_cost = (len(pack_text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN

        # ── build context item ─────────────────────────────────────────────
        finding_text = f"[context: {tid}] {pack_text}"
        item: dict[str, Any] = {
            "id": f"ctx_{tid}",
            "severity": "info",
            "check": f"context:{tid}",
            "finding": finding_text,
            "refs": {},
            "numbers": calc_numbers,
            "candidate_fixes": [],  # info items may be empty (§3)
            "helper_tools": [],
            "source": {"kind": "table", "detail": f"trigger:{tid}"},
        }
        context_items.append(item)

        # ── update cooldown ─────────────────────────────────────────────────
        if cooldown_state is not None:
            cooldown_state[tid] = seq

        # ── ledger event ───────────────────────────────────────────────────
        ledger_events.append({
            "trigger_id": tid,
            "matched": matched,
            "tokens": token_cost,
        })

    return context_items, ledger_events
