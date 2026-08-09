# Learned viewpoint policy — Stage 1

## Problem and decision

A deterministic perspective screenshot answers the wrong question: “what is a familiar camera
angle?” rather than “can this feature be inspected for this task?” A hole wants an axial view, an
edge wants a side-on view, a surface defect needs pixels and low occlusion, and an identity question
may need a silhouette that disambiguates repeated features. The same part therefore has several
valid modes and no globally correct isometric camera.

The policy **ranks a bounded candidate set** instead of directly regressing a pose or iteratively
refining one.

- Direct pose regression turns multiple equally useful views into their often-useless Euclidean
  average, requires a separate validity projection, and makes supervision depend on an arbitrary
  oracle tie-break.
- Iterative refinement can eventually outperform a discrete set, but makes latency and stopping
  behavior part of the safety boundary and is awkward to supervise when the inspectability surface
  has occlusion discontinuities.
- Candidate scoring keeps every output a renderer-validated `CameraPose`, permits a fixed latency
  budget, exposes the score of every alternative, and learns from all oracle evaluations rather than
  one selected pose. Candidate diversity is supplied by `services.viewpoint.sampling`.

This is a real neural policy. Every projection, cross-attention query, fusion matrix, and component
head is learned jointly with AdamW and back-propagation. There is no ridge fit and candidate choice
is not the greedy baseline in disguise.

## Compute-equivalence result on the mechanical-part corpus

The defensible claim is compute equivalence, not mean aggregate in isolation. The policy uses
structured geometry to select one camera, then the expensive downstream consumer receives one
render. Random search renders `N` seeded poses and receives an oracle choice of the best. The
reproducible CPU run was:

```bash
python3 -X utf8 -m training.viewpoint.train \
  --parts 72 --examples-per-part 6 --candidates 32 --epochs 350 \
  --seed 1729 --image-size 320 --device cpu --bootstrap-samples 5000 \
  --output-weights training/viewpoint/stage1_hard_weights.npz \
  --output-report training/viewpoint/stage1_hard_report.json
```

Whole `part_id` values split into 50 train, 11 validation, and 11 locked-test parts with zero
overlap. The test set has **66 feature/task examples**, up from 14, and was not used for checkpoint
selection. Confidence intervals resample whole parts, not rows, for 5,000 seeded bootstrap samples.
Aggregate is the oracle's weighted geometric mean with canonical weights
`(1, 1.25, 1, 1, .75, 1.25, .5)`.

| chooser | policy@1 aggregate | random@N aggregate | policy - random, mean | 95% part-bootstrap CI | strict win | strict loss | tie |
|---|---:|---:|---:|---:|---:|---:|---:|
| random@1 | 0.264853 | 0.164971 | +0.099882 | [-0.002967, +0.212689] | 27.27% | 16.67% | 56.06% |
| random@2 | 0.264853 | 0.217183 | +0.047670 | [-0.052209, +0.164570] | 27.27% | 21.21% | 51.52% |
| random@4 | 0.264853 | 0.349344 | -0.084491 | [-0.209388, +0.032274] | 21.21% | 34.85% | 43.94% |
| random@8 | 0.264853 | 0.463487 | -0.198635 | [-0.342727, -0.052367] | 12.12% | 48.48% | 39.39% |
| random@16 | 0.264853 | 0.603040 | -0.338187 | [-0.430545, -0.246754] | 4.55% | 65.15% | 30.30% |
| random@32 | 0.264853 | 0.678135 | -0.413282 | [-0.494798, -0.320218] | 0.00% | 78.79% | 21.21% |

The discrete point crossover is **N=4**: random search first matches/exceeds the policy mean after
four renders. Its 95% part-bootstrap interval is **[1, 8]**. Thus “one policy render equals four
random renders” is only a point estimate; the interval includes one, so this run does not establish
a compute saving.

The distribution explains the mean/win-rate pathology. Values below are per-example
`policy aggregate - baseline aggregate`.

| baseline | mean win magnitude | mean loss magnitude | p10 | p25 | median | p75 | p90 |
|---|---:|---:|---:|---:|---:|---:|---:|
| random@1 | 0.636045 | 0.441509 | -0.323012 | 0.000000 | 0.000000 | 0.035058 | 0.819765 |
| random@2 | 0.520216 | 0.444121 | -0.539078 | 0.000000 | 0.000000 | 0.029235 | 0.724240 |
| random@4 | 0.444089 | 0.512768 | -0.727171 | -0.147935 | 0.000000 | 0.000000 | 0.455845 |
| random@8 | 0.430680 | 0.517354 | -0.815380 | -0.558598 | 0.000000 | 0.000000 | 0.058661 |
| random@16 | 0.062971 | 0.523471 | -0.849907 | -0.742944 | -0.164289 | 0.000000 | 0.000000 |
| random@32 | 0.000000 | 0.524550 | -0.853211 | -0.786654 | -0.495248 | -0.018056 | 0.000000 |

Against random@1 and random@2, the policy wins infrequently but by more when it wins, while more
than half the examples tie at zero. That produces a higher point mean with only 27.27% strict wins.
At random@8 and above the pathology disappears in the unfavorable direction: losses are both more
frequent and larger. The other one-render baselines show the same rare-large-win pattern:

| baseline | baseline mean | policy mean | strict win/loss/tie | mean win/loss magnitude | delta p10/median/p90 |
|---|---:|---:|---:|---:|---:|
| fixed isometric | 0.175942 | 0.264853 | 31.82% / 15.15% / 53.03% | 0.558412 / 0.585856 | -0.551487 / 0 / 0.796919 |
| greedy geometric | 0.191288 | 0.264853 | 28.79% / 13.64% / 57.58% | 0.524008 / 0.566768 | -0.135236 / 0 / 0.753001 |

### Difficulty slices

| difficulty | examples (parts) | policy@1 | random@1 mean / strict win | random@2 | random@4 | random@8 | random@16 | random@32 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| easy | 17 (11) | 0.565518 | 0.293389 / 58.82% | 0.425008 / 58.82% | 0.590748 / 41.18% | 0.703681 / 23.53% | 0.836911 / 11.76% | 0.864685 / 0.00% |
| medium | 24 (10) | 0.096644 | 0.156194 / 8.33% | 0.206547 / 8.33% | 0.264318 / 8.33% | 0.303138 / 4.17% | 0.431864 / 0.00% | 0.545750 / 0.00% |
| hard | 25 (11) | 0.221881 | 0.086072 / 24.00% | 0.086072 / 24.00% | 0.266814 / 20.00% | 0.454091 / 12.00% | 0.608335 / 4.00% | 0.678370 / 0.00% |

The policy's point crossover is N=4 on easy and hard rows, but N=1 on medium rows. This is not a
policy that uniquely unlocks hard features: it has its strongest absolute score and only
majority-strict wins on easy rows, while medium rows lose even to one random render.

The old cuboid run remains useful as a failure audit: policy mean 0.928693 exceeded random@8
0.916260 while strict wins were only 28.57%, and coverage, scale, depth, and uniqueness were exactly
1.0. On the new held-out corpus the policy means are respectively 0.946970, 0.779359, 0.960606, and
0.893939, with exact-1 saturation rates 93.94%, 42.42%, 95.45%, and 89.39%. Scale now measures
meaningful variation; coverage, depth, and uniqueness remain too often saturated and must not be
treated as four independent pieces of evidence.

**Decision:** the current policy does **not** earn its existence over brute-force search. The N=4
point crossover is too uncertain, random@1 parity is inside the confidence interval, strict wins are
low, and the result collapses on medium difficulty.

## Architecture: Feature-Conditioned Set Ranker

The project-level architectural contribution is a **candidate-conditioned, feature-biased set
attention ranker** trained with the two-stage curriculum below. It is not claimed as a new academic
attention primitive without a literature study.

1. A task MLP maps feature kind, question kind, and requested detail/scale to a query context.
2. Shared geometry MLPs map an unordered set of normalized feature/body tokens to keys and values.
   There are no vertex indices, absolute units, or part IDs to memorize.
3. A candidate MLP maps each continuous camera representation to an embedding.
4. Each candidate and the task context form a separate attention query. The query attends over all
   geometry tokens; a learned bias marks the exact feature of interest. This lets a pose ask a
   different occlusion/layout question of the same part.
5. The fusion vector is `[candidate, task, attended geometry, candidate*geometry,
   abs(candidate-geometry)]`. A SiLU MLP predicts the seven inspectability components. Their
   canonical weighted geometric mean ranks valid candidates.

Attention is invariant to geometry-token order and output scores are equivariant to candidate order.
The multiplicative and absolute relation channels make the head compare camera and geometry rather
than merely concatenate two summaries. Hidden width is 32, intentionally small enough for a NumPy
runtime. No pretrained image encoder is used: Stage 1 receives exact structured geometry, where a
raster encoder would discard information and introduce a training/runtime dependency. A permissive
visual encoder may be evaluated during Stage 2, where rendered appearance actually matters; its
license and model-card provenance must be recorded before adoption.

### Geometry and feature conditioning

Each geometry token is 18 `float32` values:

| slice | meaning |
|---|---|
| `0:3` | feature center relative to part center, divided by part bounding radius |
| `3:6` | feature half-extent divided by bounding radius |
| `6:9` | unit feature normal/axis, or zero when not applicable |
| `9:15` | face/edge/hole/boss/pocket/region one-hot |
| `15` | `log1p(feature_area / radius²)` |
| `16` | normalized radial position |
| `17` | normalized triangle support |

`geometry_mask[B,G]` removes padding and `feature_mask[B,G]` identifies the feature being asked
about. Shared token weights plus radius normalization are the generalization mechanism across parts;
no fixed mesh size is required. These summary tokens do not encode adjacency, concavity, or explicit
line-of-sight blockers.

### Task encoding

`task[B,14]` contains a six-way feature kind, six-way question kind
(`presence`, `dimension`, `surface`, `clearance`, `orientation`, `identity`), minimum useful scale,
and requested detail. Stage 1 supplies all fields, but its geometric oracle is intentionally blind
to natural-language usefulness. Consequently the question-kind dimensions get no trustworthy
semantic supervision until Stage 2. This is a curriculum property, not evidence that Stage 1 learned
question meaning.

### Camera encoding and rotations

`candidates[B,N,12]` is:

- unit forward vector (3),
- unit up vector projected orthogonal to forward (3),
- normalized target offset (3),
- log normalized camera distance (1),
- vertical FOV divided by 180 (1), and
- log aspect ratio (1).

Forward plus orthogonal up is a continuous 6D rotation representation; it avoids the wraparound and
gimbal discontinuities of Euler angles. Because the policy ranks already valid cameras, inference
does not need to reconstruct a rotation matrix or repair a predicted pose.

### Tensor contract

| tensor | shape | dtype | contract |
|---|---|---|---|
| task | `[B,14]` | float32 | finite task encoding |
| geometry | `[B,G,18]` | float32 | finite normalized tokens |
| geometry_mask | `[B,G]` | bool | at least one valid token |
| feature_mask | `[B,G]` | bool | at least one marked valid feature token |
| candidates | `[B,N,12]` | float32 | continuous valid-pose encodings |
| candidate_mask | `[B,N]` | bool | at least one valid candidate |
| component target/output | `[B,N,7]` | float32 | canonical order, each in `[0,1]` |
| selected index | `[B]` | int64 | index of maximum aggregate among valid candidates |

Canonical component order is `coverage, visibility, foreshortening, scale, silhouette, depth,
uniqueness`, exactly matching `InspectabilityLabel.quality_components()`.

## Training objective and split discipline

`generate_oracle_corpus` consumes seeded occluding mechanical parts and their `easy`/`medium`/`hard`
feature labels from `services.viewpoint.parts`. It calls `generate_benchmark_candidate_set` once at
the maximum render budget and uses nested prefixes for the random-search curve. Every target comes
exclusively from `services.viewpoint.oracle.evaluate`. Training minimizes:

1. Smooth-L1 loss on all seven component predictions; and
2. listwise cross-entropy from the oracle aggregate distribution to the predicted aggregate
   distribution (temperature 0.12).

The second term teaches ordering over every offered camera while the first preserves diagnostic
component meaning. All weight matrices are optimized end-to-end. Gradient norm is clipped to 5 and
AdamW uses learning rate `3e-3`, weight decay `1e-5`. Seeds cover NumPy candidate/corpus generation,
part splitting, and Torch initialization. The reported benchmark is CPU deterministic. CUDA runs use
the same seed but bit-identical results across GPU/driver versions are not promised.

The split unit is **part identity**, never camera pose or feature row. Every example and every pose
from a part stays in one of train, validation, or locked test. Validation chooses the checkpoint;
test is opened only for the final policy/baseline report.

## Baselines and metrics

- **Fixed isometric:** one render at the offered camera nearest the historical `(+1,+1,+1)` view.
- **Random@N:** `N` nested, seeded random render attempts for `N in {1,2,4,8,16,32}`, with the
  geometric oracle allowed to choose the best. This deliberately gives search its real compute cost.
- **Greedy geometric:** one render chosen without labels by feature-axis alignment, target
  centering, distance, and FOV proxies.

Every baseline reports its mean selected-pose aggregate, strict policy win/loss/tie rates, positive
and negative mean magnitudes, and delta quantiles. The headline crossover is the first tested `N`
whose random-search mean reaches policy@1. Its 95% interval comes from resampling whole test parts.
Difficulty labels are reported separately. The benchmark still does not measure LLM answer
correctness, wall-clock renderer differences, camera transition cost, or predicted-component
calibration.

## Two-stage curriculum

### Stage 1 — free geometric oracle (implemented)

The kernel/renderer can provide unlimited, exact labels for framing, occlusion, foreshortening,
scale, silhouette, depth validity, and ambiguity. This stage teaches the physical visibility prior
without paying an LLM and produces the NumPy-exportable candidate ranker in this package.

### Stage 2 — scarce consumer preference (specified seam, not implemented here)

`ViewpointFeedback` owns collection and aggregation. Its final API is
`services.viewpoint.feedback.aggregate_preferences(records) -> AggregationResult`; each
`PreferenceExample` carries `geometry_hash`, `feature`, `task_text`, winner/loser
`RenderCandidate`, `rubric_margin`, `mean_confidence`, `pairwise_agreement`, and source record IDs.
A future adapter must:

1. resolve `geometry_hash` to the exact geometry tokens and encode the task text/feature;
2. normalize feedback camera `eye/target/up/fov_y_degrees` into the 12-value camera contract;
3. initialize from the Stage-1 weights;
4. optimize a pairwise logistic preference loss, weighting each pair by
   `rubric_margin * mean_confidence * pairwise_agreement`; and
5. retain a Stage-1 replay term so scarce preferences do not erase geometric validity.

That adapter and preference fine-tuning are deliberately absent. Geometry is learnable from physics;
which image let a consuming model answer is not. Neither label source substitutes for the other.

## Runtime and resources

NumPy inference is `NumpyViewpointPolicy.forward/select`; weights are plain compressed NPZ arrays.
Torch is imported only inside `build_torch_policy` and `fit_policy`. Dataset generation, baselines,
metrics, and reference inference remain network-free, GPU-free, and Torch-free.

Quick CPU validation:

```bash
python3 -X utf8 -m pytest training/viewpoint -q
python3 -X utf8 -m training.viewpoint.train \
  --parts 6 --examples-per-part 1 --candidates 32 --epochs 3 \
  --image-size 72 --bootstrap-samples 64
```

The reproduced 72-part CPU run took 483.22 seconds, including mechanical-part generation, 13,824
oracle pose labels, 350 full-batch epochs, and 5,000 bootstrap samples. Oracle label generation
remains CPU work; CUDA accelerates only gradient training. Memory is roughly linear in
`B*G*N*hidden` for attention plus component targets. Substantially larger corpora need a
part-grouped mini-batch loader, not silent example-level shuffling.

## Honest limitations and next experiment

- The locked result has 66 examples but only 11 independent test parts. The part-bootstrap interval
  correctly remains broad; this is not production-generalization evidence.
- Three components still saturate on most selected examples: coverage 93.94%, depth 95.45%, and
  uniqueness 89.39%. The aggregate therefore contains less independent signal than seven names
  imply.
- Candidate ranking cannot recover a useful view absent from the candidate set.
- Stage 1 cannot label whether an LLM answered correctly. Question-kind conditioning is
  unidentifiable under a task-blind geometric oracle; Stage 2 remains only a seam.
- No collision-free camera path, temporal smoothness, multi-view set selection, uncertainty
  calibration, or real-image encoder is implemented.

The next experiment is an ablation, not a promised win. The observed median-zero policy and weak
medium slice are consistent with four concrete architectural limits, but this run does not identify
which is causal:

1. **Capacity:** the single 32-wide attention/fusion block may be too small for target/occluder
   interactions. Compare width/depth under the same locked split.
2. **Geometry encoding:** center, extent, axis, kind, area, radius, and triangle count omit topology,
   concavity, adjacency, and explicit target-to-camera blocker clearance. Add target-centered
   occlusion/adjacency channels before adding a raster encoder.
3. **Conditioning:** remove question-kind dimensions during geometric pretraining or supply a
   genuinely task-dependent label; do not ask a task-blind oracle to supervise them.
4. **Loss:** many candidates have zero aggregate and many selections tie. Compare the current
   smooth-L1 plus soft listwise loss with top-k regret or pairwise margins using stratified nonzero
   hard negatives.

Run those as predeclared ablations on the same part split. A harder benchmark that the policy loses
is the useful result here; changing the generator or tuning against the locked test would erase it.
