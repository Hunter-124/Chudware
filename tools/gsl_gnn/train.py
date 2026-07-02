#!/usr/bin/env python3
"""Train the GSL per-face GNN scaffold on the synthetic corpus.

GATE (see README.md, binding): train ONLY for part classes that
docs/RECOGNITION_RESIDUAL.md flags (median match_pct < 90% or
faces-claimed < 70%). This script performs no dataset-vs-report check
itself (that report is produced by a separate node/tool) — the operator
running it is responsible for restricting --data / --families to the
flagged classes.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset import NUM_NODE_FEATURES, build_label_vocab, load_samples  # noqa: E402


def _require_torch():
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - exercised only w/o deps
        raise ImportError(
            "train.py requires torch and torch-geometric. Install with "
            "`pip install -r tools/gsl_gnn/requirements.txt` (install torch "
            "first for your CUDA version per pytorch.org). "
            f"Original import error: {exc}"
        ) from exc
    return torch


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--data", default="corpora/gsl_dataset", help="corpus root (default: %(default)s)")
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument(
        "--device",
        default="cuda-if-available",
        help="'cpu', 'cuda', or 'cuda-if-available' (default: %(default)s)",
    )
    p.add_argument("--out", default="runs/gsl_gnn", help="checkpoint/report output dir (default: %(default)s)")
    p.add_argument("--hidden", type=int, default=128)
    p.add_argument("--dropout", type=float, default=0.2)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--val-frac", type=float, default=0.2, help="per-family holdout fraction (default: %(default)s)")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args(argv)


def resolve_device(torch, spec: str):
    if spec == "cuda-if-available":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(spec)


def family_stratified_split(samples: list, val_frac: float, seed: int) -> tuple:
    """80/20 (default) split stratified per-family so every family present
    in `samples` is represented in both the train and validation sets."""
    by_family: dict = {}
    for s in samples:
        by_family.setdefault(s.family, []).append(s)

    rng = random.Random(seed)
    train, val = [], []
    for items in by_family.values():
        items = items[:]
        rng.shuffle(items)
        n_val = max(1, round(len(items) * val_frac)) if len(items) > 1 else 0
        val.extend(items[:n_val])
        train.extend(items[n_val:])
    return train, val


def run_epoch(torch, model, loader, optimizer, device, train: bool) -> tuple:
    import torch.nn.functional as F

    model.train(train)
    total_loss = 0.0
    total_correct = 0
    total_nodes = 0
    for batch in loader:
        batch = batch.to(device)
        if train:
            optimizer.zero_grad()
        logits = model(batch.x, batch.edge_index)
        mask = batch.y >= 0
        if int(mask.sum()) == 0:
            continue
        loss = F.cross_entropy(logits[mask], batch.y[mask])
        if train:
            loss.backward()
            optimizer.step()
        n = int(mask.sum())
        total_loss += float(loss.item()) * n
        preds = logits[mask].argmax(dim=-1)
        total_correct += int((preds == batch.y[mask]).sum())
        total_nodes += n
    avg_loss = total_loss / max(total_nodes, 1)
    acc = total_correct / max(total_nodes, 1)
    return avg_loss, acc


def compute_confusion_matrix(torch, model, loader, device, num_classes: int) -> list:
    model.eval()
    cm = [[0] * num_classes for _ in range(num_classes)]
    with torch.no_grad():
        for batch in loader:
            batch = batch.to(device)
            logits = model(batch.x, batch.edge_index)
            mask = batch.y >= 0
            if int(mask.sum()) == 0:
                continue
            preds = logits[mask].argmax(dim=-1)
            targets = batch.y[mask]
            for t, p in zip(targets.tolist(), preds.tolist()):
                cm[t][p] += 1
    return cm


def format_confusion_matrix(cm: list, labels: list) -> str:
    width = max(6, max((len(l) for l in labels), default=6) + 1)
    header = " " * width + "".join(f"{l[: width - 1]:>{width}}" for l in labels)
    lines = [header]
    for label, row in zip(labels, cm):
        lines.append(f"{label:<{width}}" + "".join(f"{v:>{width}}" for v in row))
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    torch = _require_torch()
    from torch_geometric.loader import DataLoader

    from model import build_model

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    vocab = build_label_vocab(args.data)
    if not vocab:
        raise SystemExit(
            f"no labelled faces found under {args.data!r} — regenerate the corpus first "
            "(python3 tools/gsl_dataset_gen.py --count 50; see corpora/gsl_dataset/README.md)."
        )
    samples = load_samples(args.data, label_vocab=vocab)
    if not samples:
        raise SystemExit(f"no samples found under {args.data!r}")

    train_samples, val_samples = family_stratified_split(samples, args.val_frac, args.seed)
    print(
        f"{len(samples)} samples, {len(vocab)} classes: {vocab} "
        f"({len(train_samples)} train / {len(val_samples)} val, family-stratified)"
    )

    device = resolve_device(torch, args.device)
    train_loader = DataLoader(train_samples, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_samples, batch_size=args.batch_size, shuffle=False)

    model = build_model(
        NUM_NODE_FEATURES, len(vocab), hidden_channels=args.hidden, dropout=args.dropout
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    best_val_acc = -1.0
    best_state = None
    for epoch in range(1, args.epochs + 1):
        train_loss, train_acc = run_epoch(torch, model, train_loader, optimizer, device, train=True)
        val_loss, val_acc = run_epoch(torch, model, val_loader, optimizer, device, train=False)
        print(
            f"epoch {epoch:3d}/{args.epochs}  train_loss={train_loss:.4f} train_acc={train_acc:.4f}  "
            f"val_loss={val_loss:.4f} val_acc={val_acc:.4f}"
        )
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state is not None:
        model.load_state_dict(best_state)
        torch.save(
            {
                "state_dict": best_state,
                "label_vocab": vocab,
                "num_node_features": NUM_NODE_FEATURES,
                "hidden": args.hidden,
                "dropout": args.dropout,
                "best_val_acc": best_val_acc,
            },
            out_dir / "best.pt",
        )

    cm = compute_confusion_matrix(torch, model, val_loader, device, len(vocab))
    (out_dir / "confusion_matrix.json").write_text(json.dumps({"labels": vocab, "matrix": cm}, indent=2))
    (out_dir / "confusion_matrix.txt").write_text(format_confusion_matrix(cm, vocab) + "\n")

    print(f"best val_acc={best_val_acc:.4f}; checkpoint + confusion matrix written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
