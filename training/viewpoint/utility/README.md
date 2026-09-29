# ViewNet utility model — training & evaluation (no language models)

Trains `services/viewpoint/utility/model.py` (contract §4) to predict how useful a view SET
is, and decides with measured evidence whether it ships. Supervision comes from our own
from-scratch vision network, the **Observer** (`training/viewpoint/observer/`): usefulness of
a set = the probability that an Observer that never trained on the part recovers a labelled
geometry fact from exactly the pixels the agent would receive. No language or vision-language
model is called anywhere in labelling, training or evaluation.

```
geomlabel JSONL ──► trials: per part, candidates + native metrics (in-process engine),
                    view sets per strategy, set tokens ──► observer render workers ──► store
                                                                                     │
      observer train (5-fold part cross-fit, + independent replicate) ◄──────────────┤
                                                                                     │
      observe: judge.label(crossfit) ──► p_correct per (set, query) ─────────────────┤
                                                                                     ▼
      train (regularisation sweep, 5 seeds, export NPZ, parity)
            │
      evaluate (learning curve, controls, noise floor, calibration, baselines) ──► report
            │
      ab (fresh sets on held-out TEST parts, scored by the INDEPENDENT observer) ──► --ship
```

On Windows, run the ViewNet campaign in WSL with a Linux `libchudware.so`; native
Windows is supported for production inference but not for campaign training.

## Prerequisites

- Labels: `chudware.geomlabel/1` manifest from `training.viewpoint.datasets.build_manifest`
  (default `$CHUDWARE_VIEWNET_DATA/manifests/geomlabel.jsonl`, data root default
  `~/.cache/chudware/viewnet`).
- Engine: an in-process `libchudware`, never the user's hostd:
  `export CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so`.
  `trials` measures in the calling process; grids are rendered by the observer's spawned
  worker processes, each with its own engine.
- torch (system `python3`, CUDA used when available) for `observe`, `train`, `evaluate`, `ab`
  and the observer.

## Recipe

```bash
export CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so

# 0. labels (see training/viewpoint/datasets/DATASETS.md)
python3 -m training.viewpoint.datasets.build_manifest --help

# 1. view sets for every part and split: strategies x k in {1,4}, tokens + rendered grids
python3 -m training.viewpoint.utility trials --render-workers 8

# 2. train the observers on the store (see training/viewpoint/observer/README.md):
#    the cross-fitted labeller and the independent replicate (A/B + noise floor only)
python3 -m training.viewpoint.observer train --judge crossfit
python3 -m training.viewpoint.observer train --judge independent

# 3. soft labels: P(correct) of every (set, query) pair from the fold that never saw the part;
#    the independent replicate labels val pairs for the label-noise floor
python3 -m training.viewpoint.utility observe --judge crossfit
python3 -m training.viewpoint.utility observe --judge independent --splits val

# 4. regularisation sweep on val, 5 seeds, export utility_v1.npz, numpy/torch parity gate
python3 -m training.viewpoint.utility train

# 5. convergence report (refits with train's chosen config); stamps report_sha into the NPZ
python3 -m training.viewpoint.utility evaluate
#    -> $CHUDWARE_VIEWNET_DATA/utility/run/report.md

# 6. (optional) a second round with model-greedy sets from the new weights
python3 -m training.viewpoint.utility trials --weights ~/.cache/chudware/viewnet/utility/run/utility_v1.npz \
    --splits train,val
python3 -m training.viewpoint.utility observe && python3 -m training.viewpoint.utility train \
    && python3 -m training.viewpoint.utility evaluate

# 7. held-out A/B on TEST parts, scored by the independent observer; installs only on pass
python3 -m training.viewpoint.utility ab --ship
```

Every stage resumes: `trials` skips parts already complete under the same config digest
(strategies, k, seed, tile, targets, weights sha) and the observer's render index skips grids
already rendered; `observe` labels only pairs without a label (`--relabel` after retraining
the observer; the newest row wins). `--budget-minutes` / `--limit-parts` cap a run.

## Wall clock (this box: 12 threads, RTX 3080 Ti)

Measured on the real manifest (2400 parts: 1906 train / 246 val / 248 test): `trials` with
`--render-workers 8` produced 736 view sets for 37 parts in 61 s — 12 sets/s, ≈ 20 sets and
≈ 80 (set, query) pairs per part, ≈ 1.6 s per part including the native metrics. A utility
fit runs ≈ 33k pair-epochs/s on the GPU (val scoring included), i.e. ≈ 5 s per epoch on
≈ 150k train pairs; early stopping typically lands between 30 and 80 epochs.

| stage | work | estimate |
|---|---|---|
| trials (all splits) | 2400 parts → ≈ 48k sets, ≈ 190k pairs | ≈ 65 min |
| observer train ×2 | 5 folds each, ≈ 15-30 min per fold (Observer's estimate from its measured ≈ 2050 tiles/s end to end) | ≈ 1.5-2.5 h per judge |
| observe crossfit / independent-val | ≈ 190k / ≈ 20k pairs; one-off tile cache ≈ 150 grids/s, then ≈ 1800 pairs/s incl. model load | ≈ 7-10 min / ≈ 1 min |
| train | 6-config sweep + 5 seeds = 11 fits | ≈ 30-70 min |
| evaluate | 4 fractions × 5 seeds + 3 shuffled controls ≈ 12 full-fit equivalents | ≈ 30-75 min |
| ab (test parts) | 248 parts × 5 arms ≈ 2.5k sets + independent labels | ≈ 10 min |

## Strategies (`trials.py`)

Per part (and per picked target feature) the planner's candidate pool is built exactly as
`shot_planner.plan_shots` builds it — `candidate_cameras` (target lattice, section cuts when
internal or unseen), `view_metrics`, the planner's role rules — then the fixed
front/right/top/iso cameras are measured after it. `random`, `iso`, `fixed4`, `hand_greedy`,
`adversarial_low` (greedy on −hand_utility) and `model_greedy` (when weights exist) pick sets
from that pool; greedy strategies only see the planner's own candidates. Identical sets
chosen by several strategies are stored once. A set's key is the hash of its part, target,
exact cameras/sections, tile and feature schema.

Queries come from `training.viewpoint.observer.queries` (count / presence_side /
through_blind / type, part and target mode), answer-balanced over the whole manifest; each
set record carries the qids of its part's queries that match its mode and target.

## Store (`store.py`)

`$CHUDWARE_VIEWNET_DATA/utility/store/`: `sets.jsonl` / `sets_ab.jsonl` (view-set records per
namespace: part, split, strategies, k, views, hand_utility, image, tokens path, qids),
`labels_crossfit.jsonl` / `labels_independent.jsonl` (p_correct, correct, fold, observer
digest per (set_key, qid)), `tokens/<set>.npz` (`features.build_set_tokens`),
`images/<sha>.png`, `parts_<namespace>.jsonl` (resume markers), and the observer's own files
under `observer/` (queries, observer-only sets, checkpoints).

## Model & verdicts

- `train.py`: the exact §4 architecture plus per-query-kind intercepts (`kind_bias`), BCE on
  the SOFT target p_correct, AdamW, early stopping on val log-loss, 5 seeds, normalisation
  from train sets only. Regularisation (a set's token fingerprint is memorised long before
  the signal is found): dropout on the face-token embeddings + weight decay, the pair chosen
  on val log-loss from `REG_GRID`. Dropping raw token columns or whole faces was measured to
  hurt — it can erase the one target face a label hinges on. Baselines on the same pairs:
  logistic regression on mean/max-pooled tokens, and calibrated hand_utility (1-D logistic on
  the hand score); both with per-kind intercepts.
- `export.py`: NPZ with the contract keys (float32 weights, `kind_bias`, 0-d string
  `feature_schema`/`trained_on`/`report_sha`); fails unless numpy `UtilityModel` reproduces the
  trained torch logits within 1e-5.
- Report: learning curve over train-part fractions × seeds, seed spread, shuffled-label
  control, label-noise floor (cross-fit vs independent observer on the same val pairs: Brier
  floor = E[(p_a − p_b)²]/2, hard agreement, and the soft log-loss floor = mean target
  entropy), calibration (ECE + reliability table), per-fold and per-kind metrics, observer
  accuracy per strategy, baselines with whole-part bootstrap CIs.
- **Converged** iff the last doubling of train parts improves mean val log-loss by < 1 %
  relative, seed std at full data < total improvement over the curve, and the shuffled-label
  control is not below the prior by > 1 % (leakage).
- **Ship** iff the model beats calibrated hand_utility on held-out (test) log-loss AND, on
  held-out test parts, the independent observer's P(correct) for model-greedy@4 minus
  hand-greedy@4 has a 95 % whole-part bootstrap CI excluding 0 (iso@1, fixed4, random@4 are
  reported alongside). Otherwise the reports say so and the planner stays on hand_utility.

## Tests

```bash
python3 -m pytest training/viewpoint/utility -q -p no:cacheprovider
```

The trainer recovering a planted signal from soft targets with the shipped regularisation
while the shuffled control stays at the prior (small CPU run), train-only normalisation,
metric edges, the part bootstrap, and numpy/torch export parity.
