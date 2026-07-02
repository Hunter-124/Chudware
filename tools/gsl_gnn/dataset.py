"""GSL synthetic-corpus loader -> PyTorch-Geometric graphs.

GATED SCAFFOLD — see README.md before training anything with this module.

Corpus layout consumed (see corpora/gsl_dataset/README.md):

    <root>/<family>/<seed>/{faces.json, adjacency.json, meta.json}

`faces.json["matches"][i]` and `adjacency.json["faces"][i]` come from the
same `describe_faces()` enumeration order in the C++ core (both `hint_index`
and `ord` walk 0..N-1 identically), so node `i` of the graph is simply
`faces.json["matches"][i]`, and every `adjacency.json["edges"][*].between`
pair indexes directly into that same list — no re-joining required.

`torch` / `torch_geometric` are imported lazily (inside functions), so this
module stays importable and `py_compile`-clean on a machine without either
package installed; only calling `load_samples`/`load_samples`'s Data
construction actually needs them.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterator, Optional, Union

# --- feature vocabularies (fixed, documented in README.md) -----------------

SURF_KINDS = ["plane", "cylinder", "cone", "sphere", "torus", "bspline", "other"]
CONCAVITY_KINDS = ["bore", "boss", "none"]

# surf one-hot(7) + area(1) + log1p(radius)(1) + concavity one-hot(3)
# + normal xyz(3) + centroid/diag xyz(3) = 18
NUM_NODE_FEATURES = len(SURF_KINDS) + 1 + 1 + len(CONCAVITY_KINDS) + 3 + 3
NUM_EDGE_FEATURES = 2  # [convex flag, length / diag]


def _require_torch():
    """Import torch + torch_geometric.data.Data, or raise a clear error."""
    try:
        import torch
        from torch_geometric.data import Data
    except ImportError as exc:  # pragma: no cover - exercised only w/o deps
        raise ImportError(
            "tools/gsl_gnn requires torch and torch-geometric. Install with "
            "`pip install -r tools/gsl_gnn/requirements.txt` (install torch "
            "first for your CUDA version per pytorch.org). "
            f"Original import error: {exc}"
        ) from exc
    return torch, Data


def _sample_dirs(root: Union[str, Path]) -> Iterator[tuple]:
    """Yield (family, seed, sample_dir) for every complete sample under root."""
    root = Path(root)
    if not root.is_dir():
        return
    for family_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for sample_dir in sorted(p for p in family_dir.iterdir() if p.is_dir()):
            if (
                (sample_dir / "faces.json").exists()
                and (sample_dir / "adjacency.json").exists()
                and (sample_dir / "meta.json").exists()
            ):
                yield family_dir.name, sample_dir.name, sample_dir


def build_label_vocab(root: Union[str, Path]) -> list:
    """Sorted, deterministic label vocabulary over every faces.json under root.

    Labels are the owning-feature TYPE strings gsl_dataset_gen.py already
    stamps onto each face match (free synthetic supervision, no hand-labels).
    """
    labels = set()
    for _family, _seed, sample_dir in _sample_dirs(root):
        faces = json.loads((sample_dir / "faces.json").read_text())
        for match in faces.get("matches", []):
            labels.add(match["label"])
    return sorted(labels)


def _one_hot(value: Optional[str], vocab: list) -> list:
    vec = [0.0] * len(vocab)
    key = value if value is not None else "none"
    if key in vocab:
        vec[vocab.index(key)] = 1.0
    # unseen kind at inference time -> all-zero row, never crash
    return vec


def _node_features(match: dict, diag: float) -> list:
    surf = _one_hot(match.get("surf"), SURF_KINDS)
    area = float(match.get("area_or_len") or 0.0) / max(diag * diag, 1e-9)
    radius = match.get("radius")
    log_radius = math.log1p(float(radius)) if radius else 0.0
    concavity = _one_hot(match.get("concavity"), CONCAVITY_KINDS)
    normal = [float(c) for c in (match.get("normal") or (0.0, 0.0, 0.0))]
    centroid = [
        float(c) / max(diag, 1e-9) for c in (match.get("centroid") or (0.0, 0.0, 0.0))
    ]
    return surf + [area, log_radius] + concavity + normal + centroid


def _bbox_diag(meta: dict) -> float:
    size = ((meta.get("bbox") or {}).get("size")) or [1.0, 1.0, 1.0]
    diag = math.sqrt(sum(float(s) ** 2 for s in size))
    return diag or 1.0


def load_samples(root: Union[str, Path], label_vocab: Optional[list] = None) -> list:
    """Build one PyG `Data` graph per `<family>/<seed>` sample under `root`.

    Each `Data` carries: `x` [N, NUM_NODE_FEATURES], `edge_index` [2, 2E]
    (adjacency edges inserted in both directions), `edge_attr` [2E,
    NUM_EDGE_FEATURES], `y` [N] (class index per face; -1 for a label absent
    from `label_vocab`, e.g. a class seen only at eval time), plus the
    non-tensor attributes `family` and `seed` used for stratified splitting.

    `label_vocab` fixes the class-index mapping (pass the training vocab
    when loading held-out data so indices agree); when omitted it is
    derived from every sample under `root` via `build_label_vocab`.
    """
    torch, Data = _require_torch()
    vocab = label_vocab if label_vocab is not None else build_label_vocab(root)

    samples = []
    for family, seed, sample_dir in _sample_dirs(root):
        faces = json.loads((sample_dir / "faces.json").read_text()).get("matches", [])
        adjacency = json.loads((sample_dir / "adjacency.json").read_text())
        meta = json.loads((sample_dir / "meta.json").read_text())
        diag = _bbox_diag(meta)

        if not faces:
            continue

        x = torch.tensor([_node_features(m, diag) for m in faces], dtype=torch.float32)
        y = torch.tensor(
            [vocab.index(m["label"]) if m.get("label") in vocab else -1 for m in faces],
            dtype=torch.long,
        )

        src: list = []
        dst: list = []
        attrs: list = []
        for edge in adjacency.get("edges", []):
            fa, fb = edge["between"]
            convex = 1.0 if edge.get("convex") else 0.0
            length = float(edge.get("length") or 0.0) / max(diag, 1e-9)
            for a, b in ((fa, fb), (fb, fa)):
                src.append(a)
                dst.append(b)
                attrs.append([convex, length])

        if src:
            edge_index = torch.tensor([src, dst], dtype=torch.long)
            edge_attr = torch.tensor(attrs, dtype=torch.float32)
        else:
            edge_index = torch.zeros((2, 0), dtype=torch.long)
            edge_attr = torch.zeros((0, NUM_EDGE_FEATURES), dtype=torch.float32)

        data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, y=y)
        data.family = family
        data.seed = seed
        samples.append(data)

    return samples
