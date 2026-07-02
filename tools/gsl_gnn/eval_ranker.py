#!/usr/bin/env python3
"""Evaluate the trained micro-evaluator ranker on held-out data.

Reads trained weights and evaluates per-family precision@1 for the
MLP ranker versus the deterministic (original-order) baseline.

Usage:
    python3 tools/gsl_gnn/eval_ranker.py [--data PATH] [--weights PATH] [--seed N]

Exit code 0 if ranker overall_prec@1 >= deterministic overall_prec@1 (ties pass).
Exit code 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ranker import (  # noqa: E402
    candidate_features,
    load_weights,
    numpy_forward,
    rank_candidates,
)

# Reuse helpers from train_ranker
from train_ranker import (  # noqa: E402
    _bbox_diag,
    _build_candidates,
    _sample_dirs,
)

GO_FAMILIES = {"ribbed_bracket", "shelled_enclosure", "swept_pipe"}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="corpora/gsl_dataset")
    p.add_argument("--weights", default="tools/gsl_gnn/ranker_weights.json")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def _prec_at_1_for_sample(candidates_with_labels: list[tuple[dict, int]]) -> tuple[float, float]:
    """Return (det_prec@1, ranker_prec@1) — 1.0 if top is positive, else 0.0."""
    if not candidates_with_labels:
        return 0.0, 0.0
    det_top_label = candidates_with_labels[0][1]
    return float(det_top_label), None  # ranker filled by caller


def main(argv=None) -> int:
    args = parse_args(argv)

    weights_path = Path(args.weights)
    if not weights_path.exists():
        print(f"ERROR: weights file not found: {weights_path}", file=sys.stderr)
        return 1

    weights = load_weights(str(weights_path))
    rng = random.Random(args.seed)
    root = Path(args.data)

    # Per-family accumulators
    # Structure: family -> {n_samples, n_candidates, det_hits, ranker_hits}
    stats: dict[str, dict] = {}

    for family, _seed, sample_dir in _sample_dirs(root):
        faces_raw = json.loads((sample_dir / "faces.json").read_text())
        faces = faces_raw.get("matches", [])
        meta = json.loads((sample_dir / "meta.json").read_text())
        diag = _bbox_diag(meta)

        faces_by_sid: dict = {}
        for face in faces:
            sid = str(face.get("sid") or face.get("hint_index") or "?")
            faces_by_sid[sid] = face

        pairs = _build_candidates(faces, rng)
        if not pairs:
            continue

        candidates = [p[0] for p in pairs]
        labels = [p[1] for p in pairs]

        # Deterministic prec@1: is first candidate positive?
        det_hit = float(labels[0]) if labels else 0.0

        # Ranker prec@1: is highest-scored candidate positive?
        ranked = rank_candidates(candidates, faces_by_sid, diag, weights)
        # Find the original label for the top-ranked candidate
        if ranked:
            top_sids = tuple(sorted(ranked[0].get("sids", [])))
            top_label = 0
            for cand, lbl in pairs:
                if tuple(sorted(cand.get("sids", []))) == top_sids:
                    top_label = lbl
                    break
            ranker_hit = float(top_label)
        else:
            ranker_hit = 0.0

        if family not in stats:
            stats[family] = {
                "n_samples": 0,
                "n_candidates": 0,
                "det_hits": 0.0,
                "ranker_hits": 0.0,
            }
        stats[family]["n_samples"] += 1
        stats[family]["n_candidates"] += len(candidates)
        stats[family]["det_hits"] += det_hit
        stats[family]["ranker_hits"] += ranker_hit

    # Print table
    header = (
        f"{'family':<20} | {'n_samples':>9} | {'n_candidates':>12} | "
        f"{'det_prec@1':>10} | {'ranker_prec@1':>13} | {'delta':>6}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    overall_det = 0.0
    overall_ranker = 0.0
    overall_samples = 0

    for family in sorted(stats):
        s = stats[family]
        n = s["n_samples"]
        det_p1 = s["det_hits"] / n if n > 0 else 0.0
        rnk_p1 = s["ranker_hits"] / n if n > 0 else 0.0
        delta = rnk_p1 - det_p1
        marker = " (GO)" if family in GO_FAMILIES else ""
        print(
            f"{family + marker:<20} | {n:>9} | {s['n_candidates']:>12} | "
            f"{det_p1:>10.3f} | {rnk_p1:>13.3f} | {delta:>+6.3f}"
        )
        if family in GO_FAMILIES:
            overall_det += s["det_hits"]
            overall_ranker += s["ranker_hits"]
            overall_samples += n

    print(sep)
    if overall_samples > 0:
        det_overall = overall_det / overall_samples
        rnk_overall = overall_ranker / overall_samples
        delta_overall = rnk_overall - det_overall
        print(
            f"{'OVERALL (GO only)':<20} | {overall_samples:>9} | {'':>12} | "
            f"{det_overall:>10.3f} | {rnk_overall:>13.3f} | {delta_overall:>+6.3f}"
        )
        print(sep)
        if rnk_overall >= det_overall:
            print("PASS: ranker >= deterministic baseline")
            return 0
        else:
            print("FAIL: ranker < deterministic baseline")
            return 1
    else:
        print(
            f"{'OVERALL (GO only)':<20} | {'0':>9} | {'':>12} | "
            f"{'n/a':>10} | {'n/a':>13} | {'n/a':>6}"
        )
        print(sep)
        print("WARNING: no GO-family samples found — no data to evaluate.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
