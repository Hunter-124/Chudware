"""Per-cycle gym report (T-W27): markdown + JSON.

Consumes a :class:`~tools.flywheel.bench.gym.runner.CycleResult` and produces:
  * pass rates per challenge x tier (and per seed),
  * taxonomy counts with the evidence refs that fired them,
  * RECOMMENDED DIAL ADJUSTMENTS — each mapped to a CONCRETE config/data knob
    (never a prompt-wording change; those are lead-owned and filed as work notes).

Dial knobs the recommender may touch (all config/data, per the discipline rule):
  * ``AI_CAD_COUNCIL_FEATURE_THRESHOLD`` env / councils._DEFAULT_FEATURE_THRESHOLD
  * tiers.yaml per-slot ``budgets`` (advisory/ledger/state), ``deliberation``,
    ``playbook_enforcement``, ``max_tools_per_role``
  * trigger-pack enablement (foreman advisory injection)
  * gym ``grind_threshold`` (anti-grind sensitivity)

Prompt/workflow/role-card changes are NOT recommended here — they are surfaced in the
returned ``lead_findings`` for the lead to own.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .taxonomy import TAXONOMY

# Map each failure class → the concrete config/data dial it argues for. Prompt-wording
# levers are deliberately excluded (lead-owned; see LEAD_LEVERS).
CLASS_TO_DIAL: dict[str, dict[str, str]] = {
    "wrong_question": {
        "knob": "playbook_enforcement / clarifier batch gate",
        "action": "keep step_locked on local tiers; ensure the clarifier playbook's "
                  "blocking-ambiguity check is enforced before Phase 2 (config, not prose)",
    },
    "incoherent_geometry": {
        "knob": "AI_CAD_COUNCIL_FEATURE_THRESHOLD",
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
        "action": "keep the foreman advisory injection on (deliberation=full) and ensure "
                  "the advisory slot budget is not shrunk below its floor",
    },
    "execution_stall": {
        "knob": "budgets.tools (tools slot) + mission-dispatch coverage",
        "action": "verify the turn prompt carries trimmed schemas for the node's full "
                  "allowlist (no tools omitted for budget — raise budgets.tools if the "
                  "tools_slot event reports omissions) and that every allowlisted tool "
                  "resolves (mission-scoped dispatcher + registry fallback, T-W32)",
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

def build_report(cycle_result, *, applied_dials: list[dict] | None = None,
                 deltas: dict | None = None) -> dict[str, Any]:
    """Assemble the JSON report dict from a CycleResult."""
    cells = [c.to_dict() if hasattr(c, "to_dict") else c for c in cycle_result.cells]
    tax = _taxonomy_counts(cells)
    return {
        "cycle": cycle_result.cycle,
        "tiers": cycle_result.tiers,
        "execution_mode": cycle_result.probe.mode,
        "execution_probe": cycle_result.probe.to_dict(),
        "pass_rates": _pass_rates(cells),
        "taxonomy": tax,
        "recommended_dials": recommend_dials(tax["counts"]),
        "lead_findings": lead_findings(tax["counts"]),
        "applied_dials": applied_dials or [],
        "deltas": deltas or {},
        "n_cells": len(cells),
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
