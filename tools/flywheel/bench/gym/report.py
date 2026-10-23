"""Per-cycle gym report: markdown + JSON.

Consumes a :class:`~tools.flywheel.bench.gym.runner.CycleResult` and produces:
  * pass rates per challenge x tier (and per seed),
  * taxonomy counts with the evidence refs that fired them,
  * RECOMMENDED DIAL ADJUSTMENTS — each mapped to a CONCRETE config/data knob
    (never a prompt-wording change; those are lead-owned and surfaced as findings).

Dial knobs the recommender may touch (all config/data):
  * ``CHUDWARE_COUNCIL_FEATURE_THRESHOLD`` env / councils._DEFAULT_FEATURE_THRESHOLD
  * tiers.yaml per-slot ``budgets`` (advisory/ledger/state), ``deliberation``,
    ``playbook_enforcement``, ``max_tools_per_role``
  * trigger-pack enablement (advisor advisory injection)
  * gym ``grind_threshold`` (anti-grind sensitivity)

Prompt/workflow/role-card changes are NOT recommended here — they are surfaced in the
returned ``lead_findings`` for the lead to own.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .taxonomy import TAXONOMY, misuse_fingerprints, _tool_call_events, _call_succeeded, _events, classify_ledger

# Map each failure class → the concrete config/data dial it argues for. Prompt-wording
# levers are deliberately excluded (lead-owned; see LEAD_LEVERS).
CLASS_TO_DIAL: dict[str, dict[str, str]] = {
    "wrong_question": {
        "knob": "playbook_enforcement / clarifier batch gate",
        "action": "keep step_locked on local tiers; ensure the clarifier playbook's "
                  "blocking-ambiguity check is enforced before Phase 2 (config, not prose)",
    },
    "incoherent_geometry": {
        "knob": "CHUDWARE_COUNCIL_FEATURE_THRESHOLD",
        "action": "lower the build-strategy-council feature threshold so more parts get "
                  "the four-seat review before build (more machinery for weaker models)",
    },
    "tool_misuse": {
        "knob": "tiers.yaml max_tools_per_role / tool_schema_trim",
        "action": "tighten the per-role tool allowlist so the wrong tool is not in scope; "
                  "keep preflight on (config), and enable the tool-choice trigger pack",
    },
    "context_loss": {
        "knob": "tiers.yaml budgets.ledger / budgets.state",
        "action": "raise the ledger/state slot budget so cited refs stay in the render "
                  "(shrink history first, never advisory) — assembly-time budget assertion",
    },
    "abstraction_skip": {
        "knob": "deliberation (ladder mandatory) + ladder validator max-bounce",
        "action": "confirm deliberation=full pins the design-cognition ladder mandatory; "
                  "the validator bounce is working — no relaxation warranted",
    },
    "grind": {
        "knob": "gym grind_threshold / anti-grind replan gate",
        "action": "the anti-grind rule (2 attempts then replan) is the lever; ensure the "
                  "replan gate is enabled so a 3rd identical attempt cannot happen",
    },
    "advisory_ignored": {
        "knob": "trigger-pack enablement + advisory slot budget",
        "action": "keep the advisor's advisory injection on (deliberation=full) and ensure "
                  "the advisory slot budget is not shrunk below its floor",
    },
    "execution_stall": {
        "knob": "budgets.tools (tools slot) + mission-dispatch coverage",
        "action": "verify the turn prompt carries trimmed schemas for the node's full "
                  "allowlist (no tools omitted for budget — raise budgets.tools if the "
                  "tools_slot event reports omissions) and that every allowlisted tool "
                  "resolves (mission-scoped dispatcher + registry fallback)",
    },
}

# Levers that would require a prompt/workflow/role-card edit → LEAD-OWNED, never applied.
LEAD_LEVERS: dict[str, str] = {
    "wrong_question": "clarifier role card prompt: strengthen 'ask about the blocking "
                      "ambiguity FIRST, suppress cosmetic questions' wording",
    "tool_misuse": "role card prompt: add the 'each AC names its check tool; use THAT "
                   "tool' reminder to the relevant worker cards",
    "advisory_ignored": "worker role card prompt: 'if you disagree with an advisory, file "
                        "a replan_request — do not silently proceed'",
}


# ---------------------------------------------------------------------------

def _pass_rates(cells: list[dict]) -> dict[str, Any]:
    """pass rate per challenge x tier (and overall per challenge/tier)."""
    per_ct: dict[tuple[str, str], list[bool]] = defaultdict(list)
    per_ch: dict[str, list[bool]] = defaultdict(list)
    per_tier: dict[str, list[bool]] = defaultdict(list)
    for c in cells:
        per_ct[(c["challenge"], c["tier"])].append(c["passed"])
        per_ch[c["challenge"]].append(c["passed"])
        per_tier[c["tier"]].append(c["passed"])

    def rate(xs: list[bool]) -> float:
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    matrix = {f"{ch}|{tier}": {"pass_rate": rate(v), "n": len(v)}
              for (ch, tier), v in sorted(per_ct.items())}
    return {
        "matrix": matrix,
        "per_challenge": {k: rate(v) for k, v in sorted(per_ch.items())},
        "per_tier": {k: rate(v) for k, v in sorted(per_tier.items())},
        "overall": rate([c["passed"] for c in cells]),
    }


def _taxonomy_counts(cells: list[dict]) -> dict[str, Any]:
    counts: Counter = Counter()
    per_class_evidence: dict[str, list[dict]] = defaultdict(list)
    for c in cells:
        for cl in c["classifications"]:
            fc = cl["failure_class"]
            counts[fc] += 1
            per_class_evidence[fc].append({
                "challenge": c["challenge"], "tier": c["tier"], "seed": c["seed"],
                "detail": cl["detail"], "evidence_seqs": cl["evidence_seqs"],
            })
    # ensure every taxonomy key appears (0 counts are informative)
    full = {k: counts.get(k, 0) for k in TAXONOMY}
    return {"counts": full, "evidence": per_class_evidence}


def recommend_dials(taxonomy_counts: dict[str, int]) -> list[dict[str, Any]]:
    """Data-driven dial recommendations: a class that fired ≥1 argues for its knob."""
    recs = []
    for fc, n in taxonomy_counts.items():
        if n <= 0:
            continue
        dial = CLASS_TO_DIAL.get(fc)
        if not dial:
            continue
        recs.append({
            "trigger_class": fc, "occurrences": n,
            "knob": dial["knob"], "action": dial["action"],
            "kind": "config/data",
        })
    return recs


def lead_findings(taxonomy_counts: dict[str, int]) -> list[dict[str, str]]:
    """Prompt/workflow findings for the lead — filed, never applied by the gym."""
    out = []
    for fc, n in taxonomy_counts.items():
        if n > 0 and fc in LEAD_LEVERS:
            out.append({"trigger_class": fc, "occurrences": str(n),
                        "lead_lever": LEAD_LEVERS[fc]})
    return out




# ---------------------------------------------------------------------------
# Discipline Index (WI-2) + instrumentation (WI-11)
# ---------------------------------------------------------------------------

_CHECKPOINT_TOOLS = frozenset({
    "plm_checkpoint", "plm_restore", "undo", "doc_checkpoint", "redo",
})
_DESTRUCTIVE_TOOLS = frozenset({
    "boolean", "cut", "delete_feature", "remove_feature", "shell",
    "doc_apply_ops", "replace_feature", "pattern",
})
_READ_TOOLS = frozenset({
    "describe_part", "describe_sketch", "describe_scene", "describe_assembly",
    "measure_part", "query_geometry", "sketch_health", "check_sketch",
    "verify_battery", "check_interference", "inspect_part", "look_around",
    "render_part", "contact_sheet", "measure_between",
})


def _ledger_from_cell(cell: dict) -> dict:
    """Load ledger JSON for a cell if present; else empty."""
    from pathlib import Path as _P
    lp = cell.get("ledger_path") or ""
    if not lp:
        rd = cell.get("run_dir") or ""
        if rd:
            lp = str(_P(rd) / "ledger.json")
    if lp:
        p = _P(lp)
        if p.is_file():
            try:
                import json as _json
                return _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {"events": []}
    # graded may embed nothing; allow synthetic fixtures via cell["ledger"]
    if isinstance(cell.get("ledger"), dict):
        return cell["ledger"]
    return {"events": []}


def _mission_from_cell(cell: dict) -> dict:
    from pathlib import Path as _P
    rd = cell.get("run_dir") or ""
    if rd:
        p = _P(rd) / "mission.json"
        if p.is_file():
            try:
                import json as _json
                return _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return cell.get("mission") or {}


def _is_destructive_tool(tool: str) -> bool:
    t = (tool or "").lower()
    if t in _DESTRUCTIVE_TOOLS:
        return True
    for key in ("boolean", "cut", "delete", "subtract", "shell", "replace"):
        if key in t:
            return True
    return False


def _is_checkpoint_event(kind: str, payload: dict, tool: str = "") -> bool:
    """True for checkpoint-class evidence (GAP-CP-EVENT reader)."""
    if kind == "auto_checkpoint":
        return True
    if kind == "hard_guardrail" and (payload.get("name") == "auto_checkpoint"
                                     and payload.get("outcome") in ("passed", "waived")):
        return True
    if kind == "artifact" and payload.get("kind") in ("checkpoint", "auto_checkpoint"):
        return True
    if kind == "decision" and str(payload.get("id") or "") in ("auto_checkpoint", "checkpoint"):
        return True
    if tool in _CHECKPOINT_TOOLS:
        return True
    return False


def compute_discipline_index(ledger: dict, *,
                             mission: dict | None = None,
                             graded: dict | None = None,
                             classifications: list | None = None) -> dict[str, Any]:
    """Compute Discipline Index H1–H6 for one cell (WI-2).

    N/A components are skipped (no assembly → drop H3; no destructive ops → drop H4).
    Returns ``{score, components, n_applicable}`` where score is mean of applicable.
    """
    events = _events(ledger)
    calls = _tool_call_events(events)
    mission = mission or {}
    graded = graded or {}
    components: dict[str, float | None] = {}

    # H1 verify_before_done: battery PASS|WAIVED artifact before stop
    bat_ok = False
    for e in events:
        p = e.get("payload") or {}
        if e.get("kind") == "artifact" and p.get("kind") == "battery":
            v = str(p.get("verdict") or p.get("result") or p.get("status") or "").upper()
            if v in ("PASS", "WAIVED", "OK"):
                bat_ok = True
        if e.get("kind") == "hard_guardrail" and p.get("name") == "verify_battery":
            if p.get("outcome") in ("passed", "waived"):
                bat_ok = True
    # also accept graded battery AC pass
    if not bat_ok and graded:
        for ac in graded.get("criteria") or []:
            if "battery" in str(ac.get("id", "")).lower() and ac.get("result") == "PASS":
                bat_ok = True
            if "battery" in str(ac.get("text", "")).lower() and ac.get("result") == "PASS":
                bat_ok = True
    components["H1_verify_before_done"] = 1.0 if bat_ok else 0.0

    # H2 no_orphan_sketch: 1 if evidence of clean sketch OR no sketch activity; N/A if no sketch tools
    sketch_tools = {"sketch_health", "check_sketch", "describe_sketch", "sketch_auto_constrain"}
    sketch_calls = [(e, p) for e, p in calls if (p.get("tool") or "") in sketch_tools]
    orphan_hit = False
    for e in events:
        p = e.get("payload") or {}
        if e.get("kind") == "hard_guardrail" and p.get("name") in ("orphan_sketch", "no_orphan_sketch"):
            if p.get("outcome") == "blocked":
                orphan_hit = True
        if e.get("kind") == "artifact" and p.get("kind") in ("sketch_status", "orphan_sketch"):
            if p.get("dof") not in (None, 0, "0") and p.get("orphan"):
                orphan_hit = True
            if p.get("dof") == 0 or p.get("status") == "fully_constrained":
                orphan_hit = False
    if not sketch_calls and not any(
        (e.get("payload") or {}).get("kind") in ("sketch_status", "orphan_sketch")
        for e in events if e.get("kind") == "artifact"
    ):
        components["H2_no_orphan_sketch"] = None  # N/A
    else:
        components["H2_no_orphan_sketch"] = 0.0 if orphan_hit else 1.0

    # H3 interference_if_assembly: 1 if non-assembly OR interference artifact present
    is_assembly = False
    for e in events:
        p = e.get("payload") or {}
        if e.get("kind") == "artifact" and p.get("kind") == "manifest":
            for dl in p.get("deliverables") or []:
                if str(dl.get("kind") or dl.get("type") or "").lower() in (
                        "assembly", "asm"):
                    is_assembly = True
        if (p.get("tool") or "") in ("check_interference", "describe_assembly",
                                     "asm_mate", "solve_mates"):
            is_assembly = True
    # mission stop / manifest hint
    if "assembly" in str(mission.get("kind", "")).lower():
        is_assembly = True
    has_interference = any(
        e.get("kind") == "artifact" and (e.get("payload") or {}).get("kind") == "interference"
        for e in events
    ) or any((p.get("tool") or "") == "check_interference" for _, p in calls)
    if not is_assembly:
        components["H3_interference_if_assembly"] = None  # N/A
    else:
        components["H3_interference_if_assembly"] = 1.0 if has_interference else 0.0

    # H4 checkpoint_before_destructive: fraction of destructive calls preceded by checkpoint
    # GAP-CP-EVENT: read auto_checkpoint / hard_guardrail{name:auto_checkpoint}
    destructive_idxs: list[int] = []
    checkpoint_idxs: list[int] = []
    seq_events = list(events)
    for i, e in enumerate(seq_events):
        p = e.get("payload") or {}
        kind = e.get("kind") or ""
        tool = ""
        if kind == "tool_call":
            tool = str(p.get("tool") or "")
        elif kind == "agent_turn" and p.get("event") == "tool_call":
            tool = str(p.get("tool") or "")
        if _is_checkpoint_event(kind, p, tool):
            checkpoint_idxs.append(i)
        if tool and _is_destructive_tool(tool) and not p.get("injected"):
            destructive_idxs.append(i)
    if not destructive_idxs:
        components["H4_checkpoint_before_destructive"] = None  # N/A
    else:
        ok = 0
        for di in destructive_idxs:
            if any(ci < di for ci in checkpoint_idxs):
                ok += 1
        components["H4_checkpoint_before_destructive"] = ok / len(destructive_idxs)

    # H5 referential_integrity: 1 - (context_loss fired)
    cls_list = classifications
    if cls_list is None:
        cls_list = [c if isinstance(c, dict) else getattr(c, "to_dict", lambda: {})()
                    for c in (graded.get("classifications") or [])]
        # prefer explicit classifications on caller path
    context_loss = False
    for cl in cls_list or []:
        fc = cl.get("failure_class") if isinstance(cl, dict) else getattr(cl, "failure_class", None)
        if fc == "context_loss":
            context_loss = True
            break
    components["H5_referential_integrity"] = 0.0 if context_loss else 1.0

    # H6 hard_invariant_clean: 1 if not verify_failed with battery FAIL without waiver
    stop = str(mission.get("stop_reason") or mission.get("status") or "")
    hard_fail = False
    if "verify_failed" in stop.lower():
        # battery FAIL without waiver?
        waived = any(
            e.get("kind") == "hard_guardrail"
            and (e.get("payload") or {}).get("outcome") == "waived"
            for e in events
        )
        bat_fail = any(
            e.get("kind") == "artifact"
            and (e.get("payload") or {}).get("kind") == "battery"
            and str((e.get("payload") or {}).get("verdict")
                    or (e.get("payload") or {}).get("result") or "").upper() == "FAIL"
            for e in events
        )
        if bat_fail and not waived:
            hard_fail = True
    # also: hard_guardrail blocked without recovery
    blocked = [
        e for e in events
        if e.get("kind") == "hard_guardrail"
        and (e.get("payload") or {}).get("outcome") == "blocked"
    ]
    # blocked alone is not unclean if model recovered; only flag verify_failed path
    components["H6_hard_invariant_clean"] = 0.0 if hard_fail else 1.0

    applicable = {k: v for k, v in components.items() if v is not None}
    score = (sum(applicable.values()) / len(applicable)) if applicable else 0.0
    return {
        "score": round(float(score), 4),
        "components": components,
        "n_applicable": len(applicable),
    }


def _phase_mortality(cells: list[dict]) -> dict[str, Any]:
    """P(stop in phase P | entered P) from mission.json stop_reason / phases."""
    entered: dict[str, int] = defaultdict(int)
    stopped: dict[str, int] = defaultdict(int)
    for c in cells:
        mission = _mission_from_cell(c)
        phases = list(mission.get("phases_completed") or mission.get("phases") or [])
        phase = mission.get("phase") or c.get("mission_phase") or ""
        stop = str(mission.get("stop_reason") or "")
        if phase and phase not in phases:
            phases = list(phases) + [phase]
        for p in phases:
            entered[str(p)] += 1
        # infer stop phase from stop_reason prefix (e.g. specify_incomplete)
        stop_phase = ""
        for cand in ("intake", "clarify", "specify", "plan", "build", "verify",
                     "deliver", "correct", "organize", "decompose", "geometry"):
            if cand in stop.lower():
                stop_phase = cand
                break
        if not stop_phase and phase:
            stop_phase = str(phase)
        if stop_phase and str(mission.get("status") or "") in ("error", "failed", "incomplete", ""):
            if not c.get("passed"):
                stopped[stop_phase] += 1
        elif stop_phase and not c.get("passed"):
            stopped[stop_phase] += 1
    rates = {}
    for p, n in sorted(entered.items()):
        rates[p] = round(stopped.get(p, 0) / n, 4) if n else 0.0
    # also include stop-only phases
    for p, n in stopped.items():
        if p not in rates:
            rates[p] = 1.0
    return {"entered": dict(entered), "stopped": dict(stopped), "rates": rates}


def _verify_efficacy(cells: list[dict]) -> dict[str, Any]:
    """Rate at which a successful read/verify changed the next non-read tool."""
    changed = 0
    total = 0
    for c in cells:
        ledger = _ledger_from_cell(c)
        calls = _tool_call_events(_events(ledger))
        for i, (e, p) in enumerate(calls):
            tool = str(p.get("tool") or "")
            if tool not in _READ_TOOLS or not _call_succeeded(p):
                continue
            # look for next non-read tool
            prev_mut = None
            for j in range(i - 1, -1, -1):
                t2 = str(calls[j][1].get("tool") or "")
                if t2 not in _READ_TOOLS:
                    prev_mut = calls[j][1]
                    break
            next_mut = None
            for j in range(i + 1, len(calls)):
                t2 = str(calls[j][1].get("tool") or "")
                if t2 not in _READ_TOOLS:
                    next_mut = calls[j][1]
                    break
            if prev_mut is None or next_mut is None:
                continue
            total += 1
            prev_sig = (prev_mut.get("tool"), str(prev_mut.get("args") or prev_mut.get("arguments") or ""))
            next_sig = (next_mut.get("tool"), str(next_mut.get("args") or next_mut.get("arguments") or ""))
            if prev_sig != next_sig:
                changed += 1
    rate = round(changed / total, 4) if total else None
    return {"changed": changed, "total": total, "rate": rate}


def _budget_to_progress(cells: list[dict]) -> dict[str, Any]:
    """Steps (tool_calls) per stage; tokens when present (GAP-USAGE flagged)."""
    steps_per_stage: dict[str, list[int]] = defaultdict(list)
    tokens_seen = 0
    tokens_missing = 0
    per_cell = []
    for c in cells:
        ledger = _ledger_from_cell(c)
        events = _events(ledger)
        calls = _tool_call_events(events)
        n_steps = len(calls)
        tokens = None
        for e in events:
            p = e.get("payload") or {}
            if "tokens" in p or "usage" in p:
                usage = p.get("usage") if isinstance(p.get("usage"), dict) else p
                for k in ("total_tokens", "tokens", "prompt_tokens"):
                    if usage.get(k) is not None:
                        try:
                            tokens = int(usage[k])
                            break
                        except (TypeError, ValueError):
                            pass
            if e.get("kind") == "effort_profile" and p.get("tokens") is not None:
                try:
                    tokens = int(p["tokens"])
                except (TypeError, ValueError):
                    pass
        if tokens is None:
            tokens_missing += 1
        else:
            tokens_seen += 1
        # stage from actor prefix / stage events
        stage_counts: dict[str, int] = defaultdict(int)
        for e, p in calls:
            actor = str(e.get("actor") or "unknown")
            stage_counts[actor.split(".")[0]] += 1
        for st, n in stage_counts.items():
            steps_per_stage[st].append(n)
        per_cell.append({
            "challenge": c.get("challenge"), "division": c.get("division"),
            "seed": c.get("seed"), "steps": n_steps, "tokens": tokens,
        })
    avg_steps = {k: round(sum(v) / len(v), 2) for k, v in sorted(steps_per_stage.items())}
    out = {
        "avg_steps_per_stage": avg_steps,
        "cells": per_cell,
        "tokens_present": tokens_seen,
        "tokens_missing": tokens_missing,
    }
    if tokens_missing and not tokens_seen:
        out["gap"] = "GAP-USAGE"
    elif tokens_missing:
        out["gap"] = "GAP-USAGE-partial"
    return out


def _checkpoint_util(cells: list[dict]) -> dict[str, Any]:
    """Fraction of cells (esp. C12) that used undo/plm_*/checkpoint tools."""
    def _used_cp(ledger: dict) -> bool:
        for e, p in _tool_call_events(_events(ledger)):
            if str(p.get("tool") or "") in _CHECKPOINT_TOOLS:
                return True
        for e in _events(ledger):
            if e.get("kind") == "auto_checkpoint":
                return True
            if e.get("kind") == "artifact" and (e.get("payload") or {}).get("kind") in (
                    "checkpoint", "auto_checkpoint"):
                return True
        return False

    all_cells = cells
    c12 = [c for c in cells if str(c.get("challenge") or "").upper().startswith("C12")]
    def rate(xs):
        if not xs:
            return None
        n = sum(1 for c in xs if _used_cp(_ledger_from_cell(c)))
        return round(n / len(xs), 4)

    return {
        "overall": rate(all_cells),
        "c12": rate(c12),
        "c12_n": len(c12),
        "n": len(all_cells),
    }


def _discipline_vs_success(cells: list[dict], di_scores: list[dict]) -> dict[str, Any]:
    """Scatter summary: pass_rate vs DI; flag high-success/low-DI."""
    pairs = []
    for c, di in zip(cells, di_scores):
        pairs.append({
            "challenge": c.get("challenge"),
            "division": c.get("division"),
            "tier": c.get("tier"),
            "seed": c.get("seed"),
            "passed": bool(c.get("passed")),
            "di": di.get("score"),
        })
    passed = [p for p in pairs if p["passed"]]
    failed = [p for p in pairs if not p["passed"]]
    def mean_di(xs):
        if not xs:
            return None
        return round(sum(p["di"] for p in xs if p["di"] is not None) / max(1, len(xs)), 4)
    high_success_low_di = [
        p for p in passed
        if p["di"] is not None and p["di"] < 0.5
    ]
    return {
        "n": len(pairs),
        "mean_di_passed": mean_di(passed),
        "mean_di_failed": mean_di(failed),
        "high_success_low_di": high_success_low_di[:20],
        "pairs": pairs,
    }


def compute_instrumentation(cells: list[dict]) -> dict[str, Any]:
    """Build the cycle-level instrumentation block (WI-11)."""
    di_scores = []
    misuse_all: list[dict] = []
    for c in cells:
        ledger = _ledger_from_cell(c)
        mission = _mission_from_cell(c)
        graded = c.get("graded") or {}
        classifications = c.get("classifications") or []
        di = compute_discipline_index(
            ledger, mission=mission, graded=graded, classifications=classifications)
        di_scores.append(di)
        c["discipline_index"] = di.get("score")
        c["discipline_components"] = di.get("components")
        misuse_all.extend(misuse_fingerprints(ledger))

    # aggregate misuse fingerprints
    from collections import Counter
    fp_counts: Counter = Counter()
    for m in misuse_all:
        fp_counts[m["fingerprint"]] += m["count"]
    top_misuse = [
        {"fingerprint": fp, "count": n}
        for fp, n in fp_counts.most_common(10)
    ]

    # DI by division
    di_by_div: dict[str, list[float]] = defaultdict(list)
    for c, di in zip(cells, di_scores):
        div = c.get("division") or "local"
        di_by_div[div].append(float(di["score"]))

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 4) if xs else None

    di_local = _mean(di_by_div.get("local", []))
    di_open = _mean(di_by_div.get("open", []))
    cal_delta = None
    if di_local is not None and di_open is not None:
        cal_delta = round(di_local - di_open, 4)

    phase_mort = _phase_mortality(cells)
    verify_eff = _verify_efficacy(cells)
    budget = _budget_to_progress(cells)
    cp_util = _checkpoint_util(cells)
    dvs = _discipline_vs_success(cells, di_scores)

    # top phase mortality for summary
    rates = phase_mort.get("rates") or {}
    top_phase = max(rates.items(), key=lambda kv: kv[1]) if rates else None

    return {
        "phase_mortality": phase_mort,
        "misuse_fingerprints": top_misuse,
        "verify_efficacy": verify_eff,
        "budget_curves": budget,
        "checkpoint_util": cp_util,
        "discipline_vs_success": dvs,
        "discipline_index": {
            "local": di_local,
            "open": di_open,
            "calibration_delta": cal_delta,
            "per_cell": [
                {"challenge": c.get("challenge"), "division": c.get("division"),
                 "tier": c.get("tier"), "seed": c.get("seed"),
                 "score": di["score"], "components": di["components"]}
                for c, di in zip(cells, di_scores)
            ],
        },
        "summary": {
            "phase_mortality_top": {top_phase[0]: top_phase[1]} if top_phase else {},
            "top_misuse_fingerprint": top_misuse[0]["fingerprint"] if top_misuse else None,
        },
    }


def _execution_mode(cells: list[dict]) -> str:
    """Summarize actual cell execution, never machine capability."""
    modes = {str(cell.get("mode") or "mock").lower() for cell in cells}
    modes &= {"real", "mock"}
    if modes == {"real"}:
        return "real"
    if modes == {"real", "mock"}:
        return "mixed"
    return "mock"


def build_report(cycle_result, *, applied_dials: list[dict] | None = None,
                 deltas: dict | None = None) -> dict[str, Any]:
    """Assemble the JSON report dict from a CycleResult."""
    cells = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
             for c in cycle_result.cells]
    tax = _taxonomy_counts(cells)
    instrumentation = compute_instrumentation(cells)
    di = instrumentation.get("discipline_index") or {}
    return {
        "cycle": cycle_result.cycle,
        "tiers": cycle_result.tiers,
        "execution_mode": _execution_mode(cells),
        "execution_probe": cycle_result.probe.to_dict(),
        "pass_rates": _pass_rates(cells),
        "taxonomy": tax,
        "recommended_dials": recommend_dials(tax["counts"]),
        "lead_findings": lead_findings(tax["counts"]),
        "applied_dials": applied_dials or [],
        "deltas": deltas or {},
        "n_cells": len(cells),
        "discipline_index": {
            "local": di.get("local"),
            "open": di.get("open"),
            "calibration_delta": di.get("calibration_delta"),
        },
        "instrumentation": {
            "phase_mortality": instrumentation.get("phase_mortality"),
            "misuse_fingerprints": instrumentation.get("misuse_fingerprints"),
            "verify_efficacy": instrumentation.get("verify_efficacy"),
            "budget_curves": instrumentation.get("budget_curves"),
            "checkpoint_util": instrumentation.get("checkpoint_util"),
            "discipline_vs_success": instrumentation.get("discipline_vs_success"),
        },
        "cells": cells,
    }


def render_markdown(report: dict[str, Any]) -> str:
    L: list[str] = []
    L.append(f"# Design Gym — {report['cycle']} report")
    L.append("")
    mode = report["execution_mode"]
    L.append(f"**Execution mode: `{mode.upper()}`** "
             f"(tiers: {', '.join(report['tiers'])}, {report['n_cells']} cells)")
    L.append("")
    if mode == "mock":
        L.append("> HONESTY NOTE: this cycle ran in MOCK mode. The mission pipeline drives "
                 "INTAKE + manifest validation + DAG compile for real, but the per-node "
                 "reasoning is REPLAYED from scripted turns (no tool-executing agent_fn, "
                 "no provisioned model, no C++ core in this environment — see the probe "
                 "below). Pass rates and taxonomy counts are the harness + classifier "
                 "exercised end to end on labeled fixtures. A model-behaviour DELTA from a "
                 "dial change is only measurable once a real model is provisioned; "
                 "structural-dial deltas (council threshold, budgets) ARE real.")
        L.append("")
        L.append("> TIER IS A PASS-THROUGH DIMENSION IN MOCK MODE: scripted turns are "
                 "tier-invariant, so a challenge's `local-24k` and `frontier-200k` rows "
                 "are identical by construction. The two-tier matrix proves the harness "
                 "runs per-tier; it does NOT show tier-dependent model behaviour (that "
                 "needs a real model). Read the tier split as scaffolding, not signal.")
        L.append("")
    elif mode == "mixed":
        L.append("> HONESTY NOTE: this report contains both REAL and MOCK cells. "
                 "Each cell's `mode` is authoritative; MOCK rows are excluded from "
                 "training-usable telemetry and real leaderboard scores.")
        L.append("")
    p = report["execution_probe"]
    L.append("| probe | value |")
    L.append("|---|---|")
    for k in ("mode", "real_agent_fn", "frontier_key", "local_model", "core_available"):
        L.append(f"| {k} | `{p[k]}` |")
    L.append("")

    L.append("## Pass rates — challenge x tier")
    L.append("")
    pr = report["pass_rates"]
    L.append("| challenge×tier | pass rate | n |")
    L.append("|---|---|---|")
    for key, v in pr["matrix"].items():
        L.append(f"| {key} | {v['pass_rate']:.3f} | {v['n']} |")
    L.append("")
    L.append(f"- per-challenge: " +
             ", ".join(f"{k}={v:.2f}" for k, v in pr["per_challenge"].items()))
    L.append(f"- per-tier: " +
             ", ".join(f"{k}={v:.2f}" for k, v in pr["per_tier"].items()))
    L.append(f"- **overall pass rate: {pr['overall']:.3f}**")
    L.append("")

    L.append("## Failure taxonomy")
    L.append("")
    L.append("| class | count |")
    L.append("|---|---|")
    for k, n in report["taxonomy"]["counts"].items():
        L.append(f"| {k} | {n} |")
    L.append("")
    L.append("### Evidence (top refs per fired class)")
    for fc, evs in report["taxonomy"]["evidence"].items():
        L.append(f"- **{fc}** ({len(evs)}):")
        for e in evs[:4]:
            L.append(f"  - {e['challenge']}/{e['tier']}/s{e['seed']}: {e['detail']} "
                     f"(evidence seqs {e['evidence_seqs']})")
    L.append("")

    L.append("## Recommended dial adjustments (config/data only)")
    L.append("")
    if not report["recommended_dials"]:
        L.append("_none — no failure classes fired._")
    for r in report["recommended_dials"]:
        L.append(f"- **{r['trigger_class']}** ×{r['occurrences']} → knob `{r['knob']}`: "
                 f"{r['action']}")
    L.append("")

    if report.get("applied_dials"):
        L.append("## Applied dials + measured deltas")
        L.append("")
        for a in report["applied_dials"]:
            L.append(f"- applied `{a.get('knob')}`: {a.get('change')}")
        if report.get("deltas"):
            L.append("")
            L.append("```json")
            L.append(json.dumps(report["deltas"], indent=2))
            L.append("```")
        L.append("")

    L.append("## Lead-owned findings (prompt/workflow — NOT applied here)")
    L.append("")
    if not report["lead_findings"]:
        L.append("_none._")
    for f in report["lead_findings"]:
        L.append(f"- **{f['trigger_class']}** ×{f['occurrences']}: {f['lead_lever']}")
    L.append("")
    return "\n".join(L)


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cycle = report["cycle"]
    json_path = out_dir / f"{cycle}.json"
    md_path = out_dir / f"{cycle}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return md_path, json_path
