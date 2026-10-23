# tools/gsl_gnn — feature-recognition candidate ranker

A small MLP that re-orders the feature hypotheses (`candidates`) produced by the
deterministic recogniser. It only re-ranks: it never creates or drops candidates,
and with no weights it returns the deterministic order unchanged.

## Files

| File | Purpose |
|---|---|
| `ranker.py` | Feature extraction (`candidate_features`, 43-dim: mean/max of per-face features, face fraction, kind one-hot), pure-numpy inference (`numpy_forward`, `rank_candidates`), weight I/O (`export_weights`, `load_weights`), and the torch model definition (`build_ranker_model`, imported lazily). |
| `ranker_weights.json` | Exported weights (`fc1`–`fc3` weight/bias) loaded at inference time. |
| `train_ranker.py` | Retrains the weights from a generated corpus. Requires `torch`. |
| `test_ranker.py` | Unit tests; run without torch and without the engine. |

## Inference

`services/mcp/recon_tools.py` applies the ranker when `CHUDWARE_RECON_RANKER=1`
(default off). It loads `ranker_weights.json` lazily and falls back to the
deterministic order if the weights are missing or the import fails. Inference
needs only numpy.

## Retraining

1. Generate a corpus with the live engine (see `corpora/gsl_dataset/README.md` for
   the layout; the corpus is a regenerable artifact and is not committed):

   ```
   python3 -m tools.gsl_dataset_gen --count 50
   ```

2. Install `torch` for your platform (https://pytorch.org/get-started/locally/),
   then train:

   ```
   python3 tools/gsl_gnn/train_ranker.py --data corpora/gsl_dataset \
       --epochs 50 --hidden 64 --lr 1e-3 --seed 42 \
       --out tools/gsl_gnn/ranker_weights.json
   ```

Training builds positive/negative candidates from the labelled face groups, uses a
family-stratified 80/20 split, optimises binary cross-entropy, and reports
validation precision@1 against the deterministic order before exporting weights.
The engine is not needed for training.

## Tests

```
python3 -m pytest tools/gsl_gnn/test_ranker.py -v
```
