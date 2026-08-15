# tools/gsl_gnn — GNN face-classification adapter (SCAFFOLD, gated)

Per-face graph classifier over the synthetic GSL corpus
(`corpora/gsl_dataset`): nodes are B-rep faces (`doc_query` matches), edges
are face adjacency (`doc_adjacency`), and the label is the feature TYPE that
created the face (free synthetic supervision — no hand-labeling). This is
the "learned model" branch that
`docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` §3.4/§3.6 permits ONLY after a
measured residual proves deterministic recognition (`tools/gsl_eval.py` →
`docs/review/RECOGNITION_RESIDUAL.md`) actually fails for a given part class.

**No training run is part of this change. This directory is scaffolding
only.**

## Hard gate (binding)

Train ONLY for part classes that `docs/review/RECOGNITION_RESIDUAL.md` flags
(median match_pct < 90% or faces-claimed < 70%). The model output feeds a
TEMPLATED caption head only — never free-form numbers; it never suppresses
or replaces the raw face list; no soft-token/vision adapter
(`docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` §3.6). Wiring inference into
the engine is a separate, later decision.

## Files

- `dataset.py` — `load_samples(root)` / `build_label_vocab(root)`: walks
  `<root>/<family>/<seed>/{faces.json,adjacency.json,meta.json}` and builds
  one PyTorch-Geometric `Data` graph per sample. Imports `torch` /
  `torch_geometric` lazily inside functions, so the module stays importable
  and `py_compile`-clean on a machine with neither installed.
- `model.py` — `build_model(in_channels, num_classes, hidden_channels=128,
  dropout=0.2)`: 3-layer GraphSAGE + ReLU + dropout, linear per-node
  classification head. Also lazy-imports torch.
- `train.py` — CLI: family-stratified 80/20 split, cross-entropy loss,
  per-epoch train/val loss+accuracy, best checkpoint (`best.pt`) + confusion
  matrix (`confusion_matrix.json` and a pretty `confusion_matrix.txt`,
  no plotting dependency).
- `requirements.txt` — `torch>=2.2`, `torch-geometric>=2.5` (majors pinned;
  CUDA wheel choice is user-side).

## Setup

Install `torch` first for your CUDA version
(https://pytorch.org/get-started/locally/), then:

```
pip install -r tools/gsl_gnn/requirements.txt
```

## Corpus

The corpus is a regenerable artifact, not committed. Regenerate before
training:

```
python3 tools/gsl_dataset_gen.py --count 50
```

See `corpora/gsl_dataset/README.md` for the full layout and family list, and
`tools/gsl_eval.py` / `docs/review/RECOGNITION_RESIDUAL.md` for the measured
residual that decides WHICH families/classes, if any, clear the hard gate
above.

## Run

```
python3 tools/gsl_gnn/train.py \
  --data corpora/gsl_dataset \
  --epochs 50 --lr 1e-3 \
  --device cuda-if-available \
  --out runs/gsl_gnn
```

Useful flags: `--hidden` (default 128), `--dropout` (default 0.2),
`--batch-size` (default 8), `--val-frac` (default 0.2, per-family holdout),
`--seed`.

Outputs under `--out`:
- `best.pt` — `state_dict` + `label_vocab` + hyperparameters of the
  highest-val-accuracy epoch.
- `confusion_matrix.json` — `{"labels": [...], "matrix": [[...]]}`.
- `confusion_matrix.txt` — the same matrix as an aligned text table.

## Node / edge features

- Node (18-dim, see `dataset.NUM_NODE_FEATURES`): `surf` one-hot
  (plane/cylinder/cone/sphere/torus/bspline/other), `area_or_len / diag²`,
  `log1p(radius or 0)`, `concavity` one-hot (bore/boss/none), unit `normal`
  xyz, `centroid / diag` xyz. `diag` is the sample's bbox diagonal from
  `meta.json["bbox"]["size"]`.
- Edge (2-dim, undirected — each adjacency edge inserted in both
  directions): `[convex flag, length / diag]`.
- Label vocabulary is derived from every `faces.json["matches"][*].label`
  found under the corpus root (`build_label_vocab`), sorted for
  determinism; pass a fixed vocab into `load_samples` when scoring held-out
  data so class indices agree with a trained checkpoint.
