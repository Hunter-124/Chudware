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

## Measured deterministic failure

The reproducible CPU run was:

```bash
python3 -X utf8 -m training.viewpoint.train --parts 48 --examples-per-part 2 --candidates 24 --epochs 500 --image-size 256
```

Seed was 1729. Whole `part_id` values were shuffled into 34 train, 7 validation, and 7 test
parts; overlap was zero. The locked test contains 14 feature/task examples and was not used for
checkpoint selection. Values below are arithmetic means over selected poses. “Aggregate” is the
oracle's weighted geometric mean, with canonical weights `(1, 1.25, 1, 1, .75, 1.25, .5)`.

| chooser | aggregate | coverage | visibility | foreshortening | scale | silhouette | depth | uniqueness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| learned policy | **0.928693** | 1.000000 | 1.000000 | 0.844227 | 1.000000 | 0.688615 | 1.000000 | 1.000000 |
| fixed isometric | 0.425314 | 1.000000 | 0.500000 | 0.580917 | 1.000000 | 0.567262 | 1.000000 | 1.000000 |
| best-of-8 random | 0.916260 | 1.000000 | 1.000000 | 0.825909 | 1.000000 | 0.637530 | 1.000000 | 1.000000 |
| greedy geometric | 0.484824 | 1.000000 | 0.500000 | 0.981252 | 1.000000 | 0.512969 | 1.000000 | 1.000000 |

The status-quo isometric view loses 0.503380 aggregate and exposes only 50% mean visibility in this
corpus. The learned policy's **strict** per-example win rates are 85.7143% against isometric,
28.5714% against best-of-8 random, and 50.0000% against greedy. Thus the learned policy has the
highest mean aggregate here, but it does **not** beat best-of-8 on most individual examples and it
only ties the greedy baseline's win/loss rate. Those facts are not hidden by the aggregate.
Four components saturate on these simple cuboids; this run does not establish broad CAD quality.

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

Each geometry token is 16 `float32` values:

| slice | meaning |
|---|---|
| `0:3` | feature center relative to part center, divided by part bounding radius |
| `3:6` | feature half-extent divided by bounding radius |
| `6:9` | unit feature normal/axis, or zero when not applicable |
| `9:13` | face/edge/hole/boss one-hot |
| `13` | `log1p(feature_area / radius²)` |
| `14` | normalized radial position |
| `15` | normalized triangle support |

`geometry_mask[B,G]` removes padding and `feature_mask[B,G]` identifies the feature being asked
about. Shared token weights plus radius normalization are the generalization mechanism across parts;
no fixed mesh size is required. Production extraction must preserve the same normalization and may
supply more tokens than the procedural corpus.

### Task encoding

`task[B,12]` contains a four-way feature kind, six-way question kind
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
| task | `[B,12]` | float32 | finite task encoding |
| geometry | `[B,G,16]` | float32 | finite normalized tokens |
| geometry_mask | `[B,G]` | bool | at least one valid token |
| feature_mask | `[B,G]` | bool | at least one marked valid feature token |
| candidates | `[B,N,12]` | float32 | continuous valid-pose encodings |
| candidate_mask | `[B,N]` | bool | at least one valid candidate |
| component target/output | `[B,N,7]` | float32 | canonical order, each in `[0,1]` |
| selected index | `[B]` | int64 | index of maximum aggregate among valid candidates |

Canonical component order is `coverage, visibility, foreshortening, scale, silhouette, depth,
uniqueness`, exactly matching `InspectabilityLabel.quality_components()`.

## Training objective and split discipline

`generate_oracle_corpus` constructs seeded procedural parts, obtains diverse cameras from
`generate_candidate_set`, and obtains every target exclusively from
`services.viewpoint.oracle.evaluate`. Training minimizes:

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

- **Fixed isometric:** candidate whose forward direction is closest to looking from `(+1,+1,+1)`.
- **Best-of-8 random:** stable seeded random subset of eight offered poses, with the geometric oracle
  allowed to choose the best. This is intentionally strong and spends eight free oracle calls.
- **Greedy geometric:** no labels; one step combining feature-axis alignment, target centering,
  distance, and FOV proxies.

Every chooser reports the seven selected-pose components and aggregate. Policy win-rate is the
fraction with strictly greater oracle aggregate; ties are not wins. The benchmark does not compare
predicted component calibration, LLM answer correctness, render latency, or camera transition cost.

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
python3 -X utf8 -m training.viewpoint.train --parts 12 --examples-per-part 1 --candidates 8 --epochs 40 --image-size 128
```

The final reproduced 48-part run completed in 30.64 seconds on the development workstation CPU, including
oracle labeling and 500 epochs. A larger 3080 Ti run is:

```bash
python3 -X utf8 -m training.viewpoint.train \
  --device cuda --parts 512 --examples-per-part 4 --candidates 64 \
  --epochs 1200 --image-size 640 \
  --output-weights training/viewpoint/viewpoint_stage1.npz \
  --output-report training/viewpoint/viewpoint_stage1_report.json
```

Oracle label generation remains CPU work; CUDA accelerates only gradient training. Memory is roughly
linear in `B*G*N*hidden` for attention plus the component targets. This full-batch reference path is
appropriate for the stated run; substantially larger corpora need a part-grouped mini-batch loader,
not silent example-level shuffling.

## Honest limitations

- The benchmark has only 48 procedural cuboids and 14 locked test examples; it is an executable
  architecture benchmark, not evidence of production generalization.
- Coverage, scale, depth, and uniqueness saturate in the reported corpus. More varied assemblies,
  tiny features, clipping, repeated geometry, and real occluders are required.
- Candidate ranking cannot recover a useful view absent from the candidate set.
- The structured encoder currently covers face/edge/hole/boss; production pocket/region token
  semantics are not yet specified.
- Stage 1 cannot label whether an LLM answered correctly and does not prove question-conditioned
  usefulness. Stage 2 is only a seam.
- No collision-free camera path, temporal smoothness, multi-view set selection, uncertainty
  calibration, or real-image encoder is implemented.
- The benchmark reports one fixed seed and no confidence interval. The strict win rates explicitly
  show that higher mean aggregate does not mean per-example dominance.
