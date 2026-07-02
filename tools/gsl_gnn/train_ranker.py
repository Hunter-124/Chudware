#!/usr/bin/env python3
"""Train the micro-evaluator MLP ranker on synthetic candidates.

Does NOT require the live engine — reads directly from corpora/gsl_dataset.
Generates synthetic positive/negative candidates from labeled face groups,
trains a binary cross-entropy MLP, and exports weights to ranker_weights.json.

Usage:
    python3 tools/gsl_gnn/train_ranker.py [--data PATH] [--epochs N] \\
                                           [--hidden N] [--seed N] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Iterator

# Ensure package-relative imports resolve when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ranker import (  # noqa: E402
    FEATURE_DIM,
    KIND_VOCAB,
    build_ranker_model,
    candidate_features,
    export_weights,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_torch():
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:
        raise ImportError(
            "train_ranker.py requires torch. "
            f"Original import error: {exc}"
        ) from exc
    return torch, nn


def _bbox_diag(meta: dict) -> float:
    size = ((meta.get("bbox") or {}).get("size")) or [1.0, 1.0, 1.0]
    diag = math.sqrt(sum(float(s) ** 2 for s in size))
    return diag or 1.0


def _sample_dirs(root: Path) -> Iterator[tuple]:
    """Yield (family, seed, sample_dir) for every complete sample under root."""
    if not root.is_dir():
        return
    for family_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for sample_dir in sorted(p for p in family_dir.iterdir() if p.is_dir()):
            if (
                (sample_dir / "faces.json").exists()
                and (sample_dir / "meta.json").exists()
            ):
                yield family_dir.name, sample_dir.name, sample_dir


def label_to_kind(label: str) -> str:
    """Map face label strings to candidate kind vocab."""
    mapping = {
        "hole": "hole",
        "fillet": "fillet",
        "chamfer": "chamfer",
    }
    return mapping.get(label, "other")


def _wrong_kind(rng: random.Random, correct_kind: str) -> str:
    options = [k for k in KIND_VOCAB if k != correct_kind]
    return rng.choice(options)


def _build_candidates(faces: list, rng: random.Random) -> list[tuple[dict, int]]:
    """Generate (candidate_dict, label) pairs from a list of face matches.

    Positives:  one candidate per unique owner_feature_id group.
    Negatives:  1–3 random cross-group face mixes per sample (label=0).
    """
    # Group faces by owner_feature_id
    groups: dict[str, list] = {}
    for face in faces:
        oid = str(face.get("owner_feature_id") or face.get("hint_index") or "?")
        groups.setdefault(oid, []).append(face)

    group_list = list(groups.values())
    if not group_list:
        return []

    results: list[tuple[dict, int]] = []

    # Positives
    for group in group_list:
        sids = [str(f.get("sid") or f.get("hint_index") or "?") for f in group]
        kind = label_to_kind(group[0].get("label") or "other")
        results.append(({"sids": sids, "kind": kind}, 1))

    # Negatives: randomly sample across different groups
    if len(group_list) >= 2:
        n_neg = min(3, len(group_list))
        for _ in range(n_neg):
            # pick faces from 2–3 different groups
            n_groups = rng.randint(2, min(3, len(group_list)))
            chosen_groups = rng.sample(group_list, n_groups)
            sids = []
            for g in chosen_groups:
                face = rng.choice(g)
                sids.append(str(face.get("sid") or face.get("hint_index") or "?"))
            # Assign a wrong kind relative to any group
            correct = label_to_kind(group_list[0][0].get("label") or "other")
            kind = _wrong_kind(rng, correct)
            results.append(({"sids": sids, "kind": kind}, 0))

    return results


def load_dataset(root: Path, rng: random.Random) -> list[tuple[dict, list, float, int, str]]:
    """Return list of (candidate, faces_by_sid, diag, label, family)."""
    records = []
    for family, _seed, sample_dir in _sample_dirs(root):
        faces_raw = json.loads((sample_dir / "faces.json").read_text())
        faces = faces_raw.get("matches", [])
        meta = json.loads((sample_dir / "meta.json").read_text())
        diag = _bbox_diag(meta)

        # Build faces_by_sid index
        faces_by_sid: dict = {}
        for face in faces:
            sid = str(face.get("sid") or face.get("hint_index") or "?")
            faces_by_sid[sid] = face

        pairs = _build_candidates(faces, rng)
        for candidate, label in pairs:
            records.append((candidate, faces_by_sid, diag, label, family))

    return records


def family_stratified_split(
    records: list, val_frac: float, rng: random.Random
) -> tuple[list, list]:
    """80/20 split stratified per family."""
    by_family: dict = {}
    for rec in records:
        family = rec[4]
        by_family.setdefault(family, []).append(rec)

    train, val = [], []
    for items in by_family.values():
        items = items[:]
        rng.shuffle(items)
        n_val = max(1, round(len(items) * val_frac)) if len(items) > 1 else 0
        val.extend(items[:n_val])
        train.extend(items[n_val:])
    return train, val


def records_to_tensors(torch, records: list):
    """Convert records to (X, y) tensors."""
    xs = []
    ys = []
    for candidate, faces_by_sid, diag, label, _family in records:
        feat = candidate_features(candidate, faces_by_sid, diag, len(faces_by_sid))
        xs.append(feat)
        ys.append(float(label))
    X = torch.tensor(xs, dtype=torch.float32)
    y = torch.tensor(ys, dtype=torch.float32).unsqueeze(1)
    return X, y


def compute_auc(scores: list[float], labels: list[int]) -> float:
    """Simple trapezoidal AUC (ROC) — no sklearn needed."""
    if len(set(labels)) < 2:
        return float("nan")
    pairs = sorted(zip(scores, labels), reverse=True)
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    tp = fp = 0
    auc = 0.0
    prev_fp = 0
    prev_tp = 0
    for _score, lbl in pairs:
        if lbl == 1:
            tp += 1
        else:
            fp += 1
        auc += (fp - prev_fp) * (tp + prev_tp) / 2.0
        prev_fp, prev_tp = fp, tp
    return auc / (n_pos * n_neg)


def det_prec_at_1(records: list) -> float:
    """Deterministic precision@1: first candidate per sample."""
    # Group by (faces_by_sid id, diag) proxy — but we have a flat list.
    # Since we can't easily regroup here, we treat each record independently
    # and ask: is the first positive candidate in the original order correct?
    # Approximation: fraction of positives that appear first in their group.
    # For a flat list, this is simply the fraction of positive records
    # since synthetic positives are interleaved with negatives.
    if not records:
        return 0.0
    # First record per (family, sid_tuple) group
    seen: set = set()
    first_labels = []
    for candidate, faces_by_sid, diag, label, family in records:
        key = (family, tuple(sorted(candidate.get("sids", []))))
        if key not in seen:
            seen.add(key)
            first_labels.append(label)
    if not first_labels:
        return 0.0
    return sum(first_labels) / len(first_labels)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="corpora/gsl_dataset")
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="tools/gsl_gnn/ranker_weights.json")
    p.add_argument("--lr", type=float, default=1e-3)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    torch, nn = _require_torch()

    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)

    root = Path(args.data)
    print(f"Loading dataset from {root} ...")
    all_records = load_dataset(root, rng)

    if not all_records:
        print("WARNING: no samples found — writing untrained weights and exiting.")
        model = build_ranker_model(in_dim=FEATURE_DIM, hidden=args.hidden)
        export_weights(model, args.out)
        print(f"Untrained weights written to {args.out}")
        return

    print(f"  {len(all_records)} candidate records from {root}")

    train_recs, val_recs = family_stratified_split(all_records, 0.2, rng)
    print(f"  train={len(train_recs)}  val={len(val_recs)}")

    X_train, y_train = records_to_tensors(torch, train_recs)
    X_val, y_val = records_to_tensors(torch, val_recs)

    model = build_ranker_model(in_dim=FEATURE_DIM, hidden=args.hidden)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.BCEWithLogitsLoss()

    for epoch in range(1, args.epochs + 1):
        model.train()
        optimizer.zero_grad()
        logits = model(X_train)
        loss = criterion(logits, y_train)
        loss.backward()
        optimizer.step()

        if epoch % 10 == 0 or epoch == args.epochs:
            model.eval()
            with torch.no_grad():
                val_logits = model(X_val)
                val_loss = criterion(val_logits, y_val).item()
                val_scores = torch.sigmoid(val_logits).squeeze(1).tolist()
            val_labels = [int(v) for v in y_val.squeeze(1).tolist()]
            auc = compute_auc(val_scores, val_labels)
            print(
                f"epoch {epoch:3d}/{args.epochs}  "
                f"train_loss={loss.item():.4f}  "
                f"val_loss={val_loss:.4f}  "
                f"val_auc={auc:.3f}"
            )

    # Deterministic baseline
    det_p1 = det_prec_at_1(val_recs)

    # Ranker top-1 precision on val set (per candidate — is highest-scored one positive?)
    model.eval()
    with torch.no_grad():
        val_scores_final = torch.sigmoid(model(X_val)).squeeze(1).tolist()
    val_labels_final = [int(v) for v in y_val.squeeze(1).tolist()]
    if val_scores_final:
        best_idx = max(range(len(val_scores_final)), key=lambda i: val_scores_final[i])
        ranker_p1 = float(val_labels_final[best_idx])
    else:
        ranker_p1 = 0.0

    print(f"\nDeterministic baseline prec@1 (val): {det_p1:.3f}")
    print(f"Ranker prec@1 (val, global best):   {ranker_p1:.3f}")

    out_path = args.out
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    export_weights(model, out_path)
    print(f"\nWeights exported to {out_path}")


if __name__ == "__main__":
    main()
