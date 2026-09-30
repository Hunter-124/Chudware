# ViewNet Observer — a from-scratch vision model as the usefulness judge (no language models)

How useful is a view set to the agent? The Observer answers that without any LLM/VLM: it is
our own vision network, trained from scratch (no pretrained weights), that reads labelled
geometry facts off EXACTLY the pixels the agent receives (`shot_planner.render_shot_grid`
grids). Its cross-fitted P(correct answer) for a (view set, query) pair is the soft target the
utility model (`training/viewpoint/utility/`) learns to predict from face tokens.

```
geomlabel JSONL ─► queries.py (structured queries, answer-balanced) ─► store/observer/queries.jsonl
                ─► render.py workers (own engine each) ─► store/images + observer/sets.jsonl
utility trials ─► store/sets.jsonl (strategy sets, rendered through render.py too)
        both ──► train.py: tile cache ─► ObserverNet, 5-fold part cross-fit ─► observer/<judge>/fold*.pt
                 judge.label(): P(correct) from the fold that never saw the part ─► utility observe
```

## Queries (`queries.py`, schema `viewnet.query/2`)

Derived only from `chudware.geomlabel/1` records; no text. Kinds and answer spaces (one
shared `A_MAX = 21` logit vector, invalid options masked):

| kind | modes | answer | chance |
|---|---|---|---|
| count | part | instances of a family / variant over the whole part, bins 0,1,2,3,4,5+ | 1/6 |
| presence_side | target | opening side ±X ±Y ±Z | 1/6 |
| through_blind | target | through / blind | 1/2 |
| type | target | CANON class (all but stock/other) | 1/19 |

`vec` (QD = 27) contains only kind one-hot, mode one-hot, and subject class multi-hot.
It has **no** bbox-normalised centroid, has-location flag, opening axis, or instance ID:
those were answer-correlated even after answer balancing. Part mode asks only whole-part
count questions with no location. Target mode identifies the feature through the render's
emphasis overlay; presence_side and through_blind name its family (both through/blind
variants), while type leaves the subject empty. Answers are balanced within every
(mode, kind, subject) stratum — each value capped at 1.5× the rarest supported value —
and a stratum with one supported answer is dropped.

`queries.qvec_probe(bank, split_fn)` fits a numpy ridge multiclass predictor to the **entire
query vector** on training parts and scores unseen parts once per query, alongside the
training majority per (mode, kind, subject). This catches leaks hidden by marginal balance;
the real bank must pass `probe accuracy <= majority + 0.03` for **every** kind. `observer
report` includes this control, in addition to the more demanding pixel-blind Observer.
qids include the schema and are content hashes; the balancing seed chooses surviving queries.
The 5399-part manifest generated 76,372 balanced v2 queries.

On Windows, run the ViewNet campaign in WSL with a Linux `libchudware.so`; native
Windows is supported for production rendering but not for campaign training.

## Rendering (`render.py`)

`render_sets(record, [ViewSet], store=...)` / `render_many(jobs, store=..., workers=...)` /
`RenderPool(n)`: spawned worker processes, each with its OWN in-process engine
(`CHUDWARE_ENGINE_DAEMON=0`, `CHUDWARE_LIBCHUDWARE`, default `core/build-linux/bin`), never the
user's hostd and never the caller's engine. A target set is rendered with the planner's own
roles (`shot_planner._roles`: target + context ring) and emphasis overlay, exactly as
`plan_shots` grids are. Grids go to the store's content-addressed `images/<sha>.png`; the
render index `render_index/<key>` (key = part, cameras, sections, target, tile and a digest of
shot_planner.py / cad_common.py / cad_viewpoint.py / the engine library) skips re-renders and
re-renders when the renderer changes. Worker outputs stay in a private temp dir (the durable
render cache the GUI shows is not used). Failed sets are logged to
`observer/render_failures.jsonl`; a crashed worker rebuilds the pool and retries once.

`observer render` draws the Observer's own training sets per part: 3 random k=1 + 3 random
k=4 whole-part sets, and for 2 target features 2 + 2 random sets each (section candidates
join exactly when `plan_shots` would add them), plus the hand-greedy k=1/k=4 pick per universe
so good views are represented too (≈ 18 sets per part).

Measured throughput (12-thread Ryzen 5 7600X3D, 10 workers, planning with candidates +
`view_metrics` + hand-greedy included): **10.2 grids/s** over 60 parts (1143 grids in 112 s;
7.5 grids/s on a 20-part run where the slowest part's tail dominates). The utility `trials`
path measured 12 sets/s with 8 workers through `render_sets`.

## Model (`model.py`, ObserverNet, 3.0 M parameters)

Input = the grid cut back into its tiles (k = 1 or 4; `shot_planner._compose` layout, 4 px
gutters), each box-filtered to 192×256 (a k=4 grid is read at 384×512 effective), cached once
as uint8 npy (`observer/tiles/192x256/`, ≈ 150 grids/s on 12 CPUs). Shared ResNet-ish tile
encoder with GroupNorm (stride 32 → 6×8 tokens per tile, learned position + tile-slot
embeddings); variant **a** (crossfit labeller): one self-attention layer over all tiles'
tokens, then two query-conditioned cross-attention blocks in which each query attends to the
image on its own (queries never see each other); variant **b** (independent judge): wider,
shallower encoder, no token self-attention, one 8-head cross-attention block + masked mean
pool. Query MLP → token; head over [attended, query] → 21 logits, invalid options at −1e9.

Training: CE with 0.05 label smoothing over valid options, AdamW (wd 0.05, lr 1e-3, warmup +
cosine), bf16 autocast on CUDA, channels-last. Augmentation that cannot change an answer:
brightness / contrast / per-channel colour gain, zoom 0.9–1.06 and ±4 % shift; never flips or
rotations (sides, the XYZ triad and tile letters must keep their meaning). Batches are
bucketed by tile count (~64 tiles); every epoch each set carries a fresh draw of up to 4 of
its queries. Early stopping on a 10 % part slice of the fold's training parts.
Measured on the RTX 3080 Ti: model step 1800–1900 train tiles/s, ≈ 5000 inference tiles/s;
end to end with 6 loader processes ≈ 2050 tiles/s (≈ 820 sets/s on the smoke mix), GPU-bound.

## Cross-fitting and judges (`train.py`, `judge.py`)

`fold_of(part, K=5, salt)` assigns every part (all splits) a fold; fold model f trains on the
other folds' parts and its checkpoint records those exact part ids. `judge.label(store,
set_keys, qids, judge=...)` (zipped pairs) answers each pair with the model of the part's own
fold and raises on any part found in that model's training list; rows are
`{set_key, qid, part_id, fold, p_correct, correct, observer_digest, judge}`.

- `crossfit` (variant a, seed 0): the utility model's training labels.
- `independent` (variant b, seed 1, its own fold salt): a second, equally out-of-sample
  cross-fit used ONLY for the final A/B and the label-noise floor — never training labels.

## Report (`observer report`)

On fold 0 of the crossfit split (held-out parts never trained on): per-kind accuracy /
mean P / log-loss vs train-part fraction (1/8 … 1), seed spread at full data, chance per kind,
the majority-per-stratum baseline, `qvec_probe` (fit only on training-part vectors), the
shuffled-label control (answers permuted within each training stratum), and the blind
control (pixels zeroed; the gap to it measures what the pixels contribute). Written to
`observer/report.json`, printed as a table. Query-only probe scores on the 5399-part v2
bank, fold 0 (once per query, **not** weighted by the number of grids):

| kind | qvec probe | majority | held-out queries |
|---|---:|---:|---:|
| count | 0.242 | 0.236 | 3833 |
| presence_side | 0.174 | 0.174 | 4915 |
| through_blind | 0.536 | 0.536 | 4093 |
| type | 0.080 | 0.080 | 3477 |

## Recipe

```bash
export CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so
python3 -m training.viewpoint.observer render                       # own training sets (engine)
python3 -m training.viewpoint.utility trials                        # strategy sets (see its README)
python3 -m training.viewpoint.observer requery                     # after query-schema changes; no render
python3 -m training.viewpoint.observer train --judge crossfit       # 5 folds
python3 -m training.viewpoint.observer train --judge independent    # 5 folds
python3 -m training.viewpoint.observer report                       # convergence evidence
python3 -m training.viewpoint.observer label --namespace observer   # Observer-set labels (diagnostics)
```

`requery` rebuilds `observer/queries.jsonl` and remaps qids in existing
`observer/sets.jsonl`, `sets.jsonl`, and `sets_ab.jsonl` when present. Images/tokens and
set keys remain unchanged; it moves stale query rows, labels, report and fold checkpoints
into `query_archive/<timestamp>/` instead of allowing stale labels to be reused. Judge
checkpoint loading checks `query_schema` and rejects old versions. `train` reads the store's
training namespace (`sets.jsonl`, never A/B) plus `observer/sets.jsonl`; `--folds 0,1`
trains a subset (`label --trained-folds-only` then labels what those folds may). The
utility pipeline labels its pairs with `utility observe`. `render` skips parts already
present in `observer/sets.jsonl`.

## Query-v2 three-epoch GPU sanity (measured, RTX 3080 Ti)

Fold 0 held-out parts, one run with pixels and one with all pixels zeroed, each from
scratch for three epochs. The 750-part deterministic subset used 21,711 train sets and
6,787 held-out sets (17,262 held-out pairs); full/blind accuracy: overall
0.2810/0.2813, count 0.2574/0.2626, presence_side 0.2494/0.2631,
through_blind 0.6254/0.6070, type 0.0989/0.0734. A second target-only 2000-part
subset (33,103 train, 8,666 held-out sets; lr 0.002) reached overall
0.2919/0.2906, presence_side 0.2102/0.2004, through_blind 0.5970/0.5970,
type 0.0751/0.0852. **Three epochs did not establish an aggregate visual gain.**
Low query-only leakage does not by itself prove that this from-scratch visual reader
learns the pixel task in three epochs; cross-fit labels must be evaluated with the
full report's blind control before treating them as view usefulness.

## Historical pre-v2 smoke run (measured; not evidence for current schema)

80 parts (`render --limit-parts 80`): 1498 grids; 1071 balanced queries. `train --folds 0
--epochs 8` (56 train parts, 1038 sets): 25 s wall (first epoch 11.5 s incl. loader spawn and
cuDNN autotune, then 1.3 s per epoch); held-out fold 0 (18 parts, 1747 pairs) accuracy 0.43
vs chance 0.24 — count 0.32 (1/6), presence_side 0.57 (1/6), through_blind 0.56 (1/2), type
0.16 (1/19). `label --namespace observer --trained-folds-only`: 1747 pairs in 0.97 s (≈ 1800
pairs/s including the checkpoint load), identical per-kind accuracy to the fold's
out-of-fold report. `report --fractions 0.25,0.5,1 --seeds 0,1 --epochs 4`: 7 fits in 71 s
(4 epochs is too few at this size: the curve is flat and the blind control scores the same as
the full runs — exactly the failure the controls exist to expose).

The following pre-v2 scale estimate does not account for the expanded 5399-part manifest.
The actual run size and pixel gap must be re-measured under schema v2.

## Tests

```bash
python3 -m pytest training/viewpoint/observer -q -p no:cacheprovider
```

Answer balance per stratum and dropped single-answer strata, determinism by seed,
option masks, geometry-free query vectors, a synthetic injected-field leak caught by
`qvec_probe`, idempotent rerender-free set migration/label invalidation, query upsert;
model shapes for k = 1/4 and both
variants, masked options at probability 0, queries not influencing each other, padded tiles
ignored; the grid split matching the shot-grid layout; cross-fitting never labelling a part
with a model that saw it (and refusing a checkpoint that claims to); a tiny run learning a
planted pixel signal far above chance while the blind control stays near chance.

This torch build aborts ("double free or corruption") in multi-threaded CPU backward through
ObserverNet; the tests pin one intra-op thread. Training is meant for CUDA.
