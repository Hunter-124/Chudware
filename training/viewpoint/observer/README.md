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
        both ──► dense.py: cached RGB cameras + mesh raster ─► observer/dense/192x256/*.npz
                train.py: tile cache + dense class/edge supervision ─► ObserverNet
                5-fold part cross-fit ─► observer/<judge>/fold*.pt
                judge.label(): held-out P(correct) ─► utility observe
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

## Dense geometry supervision (`dense.py`)

`observer dense --workers 8` resumes raster labels **without rendering the RGB again**.
For every existing grid, its source camera is reconstructed from the part/target
candidates (trial-set `view_indices`, or Observer-set `view_labels` and sections).
Trials and Observer grids have separate camera pools: fixed-camera indices in trials
determine whether section candidates were inserted *even when no section was picked*.
Conflating those pools once produced misregistered labels for fixed views. Each
engine worker imports a part once and reuses the annotation mesh. The same
`cad_common._mesh_depth_buffer` that places visible face tags assigns each tile pixel
its manifest face's CANON class; ID 0 = background, ID 22 = section cap. A second
uint8 plane marks feature-instance, silhouette, and depth-discontinuity edges.
Explicit x-ray views use the renderer's second, target-only depth pass, so the
opaque target is labelled in front of its translucent occluder. Existing training
grids use uncut and section cameras; x-ray alignment was checked by an actual
`plan_shots`/`render_shot_grid` view. `observer/dense/192x256/<image-sha>.npz`
stores losslessly compressed uint8 `[k,H,W,2]` masks atomically (temporary
file + `os.replace`); reruns skip done files. The `dense` stage migrates prior
raw `.npy` masks without rerasterizing and rebuilds malformed/truncated ones.
Camera-to-RGB alignment is checked against the original grid's foreground
colour. Run dense **after** both `render` and `utility trials`, before
`train`/`report`. The 192×256 raster is at model input resolution; RGB uses BOX
downsampling, so antialiased borders can differ by up to a downsample pixel.
Overlays of part, target, section, and x-ray tiles were visually checked.

## Model (`model.py`, from scratch)

Shared GroupNorm tile encoder produces stride-32 visual tokens and stride-8
features. A light class/edge decoder reads stride-8 features; its per-class
probabilities and edge map are pooled to a 2×2 semantic map per tile and appended
to the query-attention tokens. The semantic map comes **only from pixels**, never
the query answer or part metadata. Variant **a** uses a self-attention layer plus
two query-conditioned cross-attention blocks; **b** uses wider, shallower stages
with one cross block and mean pooling. The grid's tiles are BOX-downsampled to
192×256, retaining the precise 1/4-tile locations and render emphasis colours.

Training jointly optimizes query CE and class-balanced dense CE with edge BCE
(dense weight 1.5 at start, decaying to 0.525); every epoch keeps query CE active.
Dense loss ignores padded tiles. Photometric augmentations keep the labels
registered; geometric zoom and shift are disabled while training dense labels.
On a synthetic dataset with no dense files the model still trains its query head
without auxiliary loss. For real training, a partially built dense cache raises
an error instead of silently mixing labelled and unlabelled sets. The held-out
report includes class mIoU alongside accuracy, blind and shuffled controls.

Fold training writes an atomic `foldN.<config-and-set-hash>.progress.pt` at every
completed epoch (network, optimizer, LR schedule, best validation model, RNG and
sampler epoch); `train` automatically resumes matching progress after a reboot.
Final fold checkpoints, `folds.json`, and `report.json` are also atomic.

## Cross-fitting and judges (`train.py`, `judge.py`)

`fold_of(part, K=5, salt)` assigns every part (all splits) a fold; fold model f trains on the
other folds' parts and its checkpoint records those exact part ids. `judge.label(store,
set_keys, qids, judge=...)` (zipped pairs) answers each pair with the model of the part's own
fold and raises on any part found in that model's training list; rows are
`{set_key, qid, part_id, fold, p_correct, correct, observer_digest, judge}`.

The dense-reader checkpoint schema is `viewnet.observer/3`; older query-only
checkpoints are rejected rather than silently reused.

- `crossfit` (variant a, seed 0): the utility model's training labels.
- `independent` (variant b, seed 1, its own fold salt): a second, equally out-of-sample
  cross-fit used ONLY for the final A/B and the label-noise floor — never training labels.

## Report (`observer report`)

On fold 0 of the crossfit split (held-out parts never trained on): per-kind accuracy /
mean P / log-loss vs train-part fraction (1/8 … 1), seed spread at full data, chance per kind,
the majority-per-stratum baseline, `qvec_probe` (fit only on training-part vectors), the
shuffled-label control (answers permuted within each training stratum), and the blind
control (pixels zeroed; the gap to it measures what the pixels contribute), plus
held-out per-pixel class mIoU at stride 8 (classes absent in ground truth excluded).
Written atomically to `observer/report.json`, printed as a table. Each
fraction/seed/control run also has an atomic result under `observer/report_runs/`;
interrupted runs resume from per-epoch model/optimizer/RNG checkpoints, so a
reboot does not discard the completed parts of a multi-hour report. Query-only
probe scores on the 5399-part v2 bank, fold 0 (once per query, **not** weighted
by the number of grids):

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
python3 -m training.viewpoint.observer dense --workers 8            # existing RGB → pixel labels
python3 -m training.viewpoint.observer requery                     # after query-schema changes; no render
python3 -m training.viewpoint.observer train --judge crossfit       # 5 folds
python3 -m training.viewpoint.observer train --judge independent    # 5 folds
python3 -m training.viewpoint.observer report                       # convergence evidence
python3 -m training.viewpoint.observer label --namespace observer   # Observer-set labels (diagnostics)
```

For the full reboot-resumable campaign (render → trials → dense → both
cross-fits → report → utility → A/B), run
`VIEWNET_RUN=dense-geometry training/viewpoint/campaign.sh service`.
The service resumes interrupted dense files and training epochs rather than
starting those jobs from scratch.

`requery` rebuilds `observer/queries.jsonl` and remaps qids in existing
`observer/sets.jsonl`, `sets.jsonl`, and `sets_ab.jsonl` when present. Images/tokens and
set keys remain unchanged; it moves stale query rows, labels, report and fold checkpoints
into `query_archive/<timestamp>/` instead of allowing stale labels to be reused. Judge
checkpoint loading checks `query_schema` and rejects old versions. `train` reads the store's
training namespace (`sets.jsonl`, never A/B) plus `observer/sets.jsonl`; `--folds 0,1`
trains a subset (`label --trained-folds-only` then labels what those folds may). The
utility pipeline labels its pairs with `utility observe`. `render` skips parts already
present in `observer/sets.jsonl`.

## Dense-reader held-out probe (measured, query-v2, RTX 3080 Ti)

One deterministic, frozen 790-part complete-label subset (788 parts with matching
queries), fold 0 held out: 20,558 training grids, 2,236 early-stop grids, 6,651
held-out grids from 176 parts (14,438 query/grid pairs). Variant a trained for
eight epochs from scratch at 192×256, 32 tiles/batch; the blind control used
identical parts, schedule and seed, but zero-valued RGB pixels.

| Query kind | Full | Blind | Difference |
|---|---:|---:|---:|
| All | 0.5242 | 0.3854 | +0.1388 |
| Count | 0.5077 | 0.4118 | +0.0959 |
| Presence side | 0.3844 | 0.2986 | +0.0858 |
| Through/blind | 0.7939 | 0.6696 | +0.1243 |
| Type | 0.5288 | 0.1327 | +0.3961 (3.99×) |

Held-out class mIoU at stride 8: **0.4189** on 512 frozen held-out grids
(classes absent from ground truth excluded). Full validation accuracy by epoch
0–7: 0.414, 0.444, 0.488, 0.528, 0.509, 0.533, 0.543, 0.545; blind:
0.400, 0.400, 0.395, 0.395, 0.403, 0.405, 0.403, 0.403.
Unique-query qvec probe vs training majority: count 0.437/0.439,
presence 0.204/0.184, through/blind 0.610/0.610, type 0.146/0.146.
The probe stays at majority (all differences ≤ 0.020), while vision wins.
This bounded subset establishes pixel use, **not** the eventual full-corpus
cross-fit accuracy; the campaign report evaluates that separately.

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
