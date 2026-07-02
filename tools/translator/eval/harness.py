#!/usr/bin/env python3
"""Slot-fidelity eval harness — runs the SER slot-accuracy scorer over a gold set and
emits the baseline numbers (Pillar 13: eval harness EXISTS before any training run).

What it measures (all reference-free — we hold the true state):

  1. Baseline fidelity: the deterministic GSL template (`render_caption`) and each
     example's human-audited caption must score ~100% slot accuracy. That is the
     harness self-validation — a template built FROM the slots cannot miss them, so
     <100% means the SCORER is broken, not the caption.
  2. Hallucination discrimination: for every example we synthesise an intrinsic
     (wrong-value) and an extrinsic (invented-feature) corruption; a clean caption must
     score a lower hallucination_score than its corruptions. Reported as example-level
     ROC-AUC (master plan: score ROC-AUC, not corpus averages) plus SEPARATE intrinsic
     and extrinsic tallies.
  3. Held-out STRUCTURES: examples are split by `structure_key`; metrics are reported on
     the held-out fold (unseen structures), not just held-out samples.

Gold examples: tools/translator/eval/gold/*.json (see gold/README for the schema). Each
carries a state dict, an optional human-audited caption, and optional explicit
corruptions; missing corruptions are generated deterministically here.

Usage:
    python3 -m tools.translator.eval.harness                 # print report to stdout
    python3 -m tools.translator.eval.harness --append-residual   # append to the residual doc
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.translator import groundedness as _gnd  # noqa: E402
from tools.translator.gsl_ser import (SER_VERSION, enumerate_slots, render_caption,  # noqa: E402
                                       roundtrip_diff)
from tools.translator.eval.slot_accuracy import ScoreReport, score  # noqa: E402

GOLD_DIR = Path(__file__).resolve().parent / "gold"
HELDOUT_FRACTION = 0.34   # ~1/3 of distinct structures held out (deterministic by key)


# ------------------------------------------------------------------ gold loading


def load_gold(gold_dir: Path = GOLD_DIR) -> list[dict]:
    examples: list[dict] = []
    for p in sorted(gold_dir.glob("*.json")):
        data = json.loads(p.read_text())
        for ex in (data if isinstance(data, list) else [data]):
            ex.setdefault("id", p.stem)
            ex.setdefault("structure_key", ex.get("domain", "brep"))
            examples.append(ex)
    return examples


def heldout_structures(examples: list[dict]) -> set[str]:
    """Deterministic ~HELDOUT_FRACTION of distinct structure keys (sorted, sliced) —
    so the held-out fold is reproducible and structure-disjoint from the rest."""
    keys = sorted({ex["structure_key"] for ex in examples})
    n = max(1, round(len(keys) * HELDOUT_FRACTION)) if len(keys) > 1 else 0
    return set(keys[:n])


# ------------------------------------------------------------ corruption synthesis


def _perturb_intrinsic(state: dict) -> Optional[str]:
    """A caption with ONE real slot given a wrong value (intrinsic hallucination)."""
    slots = [s for s in enumerate_slots(state) if s.numeric]
    if not slots:
        return None
    target = slots[len(slots) // 2]
    cap = render_caption(state)
    from tools.translator.gsl_ser import prose_token
    good = prose_token(target.value, target.unit)
    wrong_val = float(target.value) * 1.7 + 1.0
    bad = prose_token(wrong_val, target.unit)
    label = target.path.replace(".", " ")
    return cap.replace(f"{label} is {good}.", f"{label} is {bad}.")


def _perturb_extrinsic(state: dict) -> str:
    """A caption with an INVENTED feature/numeral (extrinsic hallucination)."""
    return render_caption(state) + " hidden phantom bore radius is 41.7mm."


def corruptions_for(ex: dict) -> list[dict]:
    if ex.get("corruptions"):
        return ex["corruptions"]
    out = [{"kind": "extrinsic", "caption": _perturb_extrinsic(ex["state"])}]
    intr = _perturb_intrinsic(ex["state"])
    if intr is not None:
        out.append({"kind": "intrinsic", "caption": intr})
    return out


# ------------------------------------------------------------------ metrics


def roc_auc(clean_scores: list[float], dirty_scores: list[float]) -> Optional[float]:
    """Example-level ROC-AUC that a hallucination_score separates clean (label 0) from
    corrupted (label 1). Mann-Whitney form; None if either class is empty."""
    if not clean_scores or not dirty_scores:
        return None
    wins = ties = 0
    for d in dirty_scores:
        for c in clean_scores:
            if d > c:
                wins += 1
            elif d == c:
                ties += 1
    return (wins + 0.5 * ties) / (len(clean_scores) * len(dirty_scores))


def _merge_strata(dst: dict[str, dict[str, int]], src: dict[str, dict[str, int]]) -> None:
    for k, row in src.items():
        d = dst.setdefault(k, {"correct": 0, "wrong": 0, "missing": 0})
        for st, n in row.items():
            d[st] = d.get(st, 0) + n


def evaluate(examples: list[dict]) -> dict[str, Any]:
    held = heldout_structures(examples)
    fold_stats: dict[str, dict] = {}
    per_example: list[dict] = []

    for fold in ("held_out", "seen"):
        subset = [e for e in examples
                  if (e["structure_key"] in held) == (fold == "held_out")]
        clean_h, dirty_h = [], []
        by_mag: dict[str, dict[str, int]] = {}
        by_unit: dict[str, dict[str, int]] = {}
        tmpl_accs: list[float] = []
        audited_accs: list[float] = []
        rt_failures: list[str] = []
        intrinsic_flagged = extrinsic_flagged = corr_total = 0

        for ex in subset:
            state = ex["state"]
            if roundtrip_diff(state):
                rt_failures.append(ex["id"])
            tmpl = render_caption(state)
            r_tmpl: ScoreReport = score(tmpl, state)
            tmpl_accs.append(r_tmpl.slot_accuracy)
            _merge_strata(by_mag, r_tmpl.by_magnitude)
            _merge_strata(by_unit, r_tmpl.by_unit)
            clean_h.append(r_tmpl.hallucination_score)

            caption = ex.get("gold_caption")
            if caption:
                audited_accs.append(score(caption, state).slot_accuracy)

            for corr in corruptions_for(ex):
                rc = score(corr["caption"], state)
                dirty_h.append(rc.hallucination_score)
                corr_total += 1
                if corr["kind"] == "intrinsic" and rc.wrong > 0:
                    intrinsic_flagged += 1
                if corr["kind"] == "extrinsic" and rc.extrinsic > 0:
                    extrinsic_flagged += 1

            per_example.append({"id": ex["id"], "fold": fold,
                                "structure_key": ex["structure_key"],
                                "template_slot_accuracy": round(r_tmpl.slot_accuracy, 4),
                                "n_reference": r_tmpl.n_reference})

        fold_stats[fold] = {
            "n_examples": len(subset),
            "structures": sorted({e["structure_key"] for e in subset}),
            "template_slot_accuracy_min": round(min(tmpl_accs), 4) if tmpl_accs else None,
            "template_slot_accuracy_mean": round(sum(tmpl_accs) / len(tmpl_accs), 4)
            if tmpl_accs else None,
            "audited_slot_accuracy_mean": round(sum(audited_accs) / len(audited_accs), 4)
            if audited_accs else None,
            "roc_auc_clean_vs_corrupt": roc_auc(clean_h, dirty_h),
            "intrinsic_detected": f"{intrinsic_flagged}",
            "extrinsic_detected": f"{extrinsic_flagged}",
            "corruptions": corr_total,
            "roundtrip_failures": rt_failures,
            "by_magnitude": by_mag, "by_unit": by_unit,
        }

    return {
        "ser_version": SER_VERSION,
        "groundedness": "STUB(T-H4 pending)" if _gnd.is_stub() else "T-H4 checker",
        "n_examples": len(examples),
        "held_out_structures": sorted(held),
        "folds": fold_stats,
        "per_example": per_example,
    }


# ------------------------------------------------------------------ report


def render_report(res: dict) -> str:
    L: list[str] = []
    L.append("## Slot-fidelity eval harness (gsl-ser/v1) — baseline")
    L.append("")
    L.append("GENERATED by `python3 -m tools.translator.eval.harness` — do not hand-edit.")
    L.append("")
    L.append(f"- Serialization: `{res['ser_version']}` (frozen; round-trip exact)")
    L.append(f"- Groundedness checker: `{res['groundedness']}` "
             "(extrinsic detection; production impl owned by T-H4)")
    L.append(f"- Gold examples: {res['n_examples']}  |  held-out structures: "
             f"{', '.join(res['held_out_structures']) or '(none)'}")
    L.append("")
    L.append("Primary metric = SER-style slot accuracy (unit-normalised) over the "
             "ground-truth slots. The deterministic GSL template scores ~1.0 BY "
             "CONSTRUCTION — that validates the scorer, not the model. Intrinsic "
             "(wrong value) and extrinsic (invented feature) hallucination are reported "
             "separately; discrimination is example-level ROC-AUC.")
    L.append("")
    L.append("| fold | examples | structures | template acc (min/mean) | audited acc | "
             "ROC-AUC clean/corrupt | intrinsic det | extrinsic det |")
    L.append("|---|---|---|---|---|---|---|---|")
    for fold in ("held_out", "seen"):
        s = res["folds"][fold]
        tmin = s["template_slot_accuracy_min"]
        tmean = s["template_slot_accuracy_mean"]
        aud = s["audited_slot_accuracy_mean"]
        auc = s["roc_auc_clean_vs_corrupt"]
        L.append(f"| {fold} | {s['n_examples']} | {len(s['structures'])} | "
                 f"{tmin}/{tmean} | {aud if aud is not None else '—'} | "
                 f"{auc if auc is not None else '—'} | "
                 f"{s['intrinsic_detected']}/{s['corruptions'] // 2 if s['corruptions'] else 0} | "
                 f"{s['extrinsic_detected']}/{s['corruptions'] // 2 if s['corruptions'] else 0} |")
    L.append("")
    # magnitude strata (held-out fold) — FERMAT-style numeric-accuracy-by-scale.
    mag = res["folds"]["held_out"]["by_magnitude"] or res["folds"]["seen"]["by_magnitude"]
    if mag:
        L.append("Numeric slot accuracy by magnitude bucket (template baseline):")
        L.append("")
        L.append("| bucket | correct | wrong | missing |")
        L.append("|---|---|---|---|")
        for b in ("<1", "1-10", "10-100", "100-1k", ">=1k"):
            if b in mag:
                r = mag[b]
                L.append(f"| {b} | {r.get('correct',0)} | {r.get('wrong',0)} | "
                         f"{r.get('missing',0)} |")
        L.append("")
    rt = [f for fold in res["folds"].values() for f in fold["roundtrip_failures"]]
    L.append(f"Round-trip re-extraction: {'ALL PASS' if not rt else 'FAILURES: ' + ', '.join(rt)}"
             " (state -> gsl-ser/v1 text -> slots reproduces enumerate_slots exactly).")
    L.append("")
    return "\n".join(L)


RESIDUAL_MARKER = "## Slot-fidelity eval harness (gsl-ser/v1) — baseline"


def append_residual(report: str, residual_path: Path) -> None:
    """Append (or replace) the harness block in docs/RECOGNITION_RESIDUAL.md — idempotent."""
    existing = residual_path.read_text() if residual_path.exists() else ""
    if RESIDUAL_MARKER in existing:
        head = existing.split(RESIDUAL_MARKER)[0].rstrip()
        residual_path.write_text(head + "\n\n" + report)
    else:
        sep = "" if existing.endswith("\n") else "\n"
        residual_path.write_text(existing + sep + "\n---\n\n" + report)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gold", type=str, default=str(GOLD_DIR))
    ap.add_argument("--append-residual", action="store_true",
                    help="append the baseline block to docs/RECOGNITION_RESIDUAL.md")
    ap.add_argument("--json", action="store_true", help="emit raw JSON instead of markdown")
    args = ap.parse_args(argv)

    examples = load_gold(Path(args.gold))
    if not examples:
        print(f"FATAL: no gold examples under {args.gold}", file=sys.stderr)
        return 1
    res = evaluate(examples)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    report = render_report(res)
    print(report)
    if args.append_residual:
        rp = _REPO_ROOT / "docs" / "RECOGNITION_RESIDUAL.md"
        append_residual(report, rp)
        print(f"\nAppended baseline block to {rp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
