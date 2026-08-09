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

This is a real neural policy. The task, candidate, attention, fusion, and scalar scoring matrices
are learned jointly with AdamW and back-propagation. There is no ridge fit and candidate choice is
not the greedy baseline in disguise.

## Locked-split result: underfit fixed, generalization still inadequate

The redesign was run once on the **same seed-1729 locked split** and unchanged frozen oracle/candidate
pool used by `efccbb77`:

```bash
python3 -X utf8 -m training.viewpoint.train \
  --parts 72 --examples-per-part 6 --candidates 32 --epochs 500 \
  --seed 1729 --image-size 320 --device cpu --bootstrap-samples 2000 \
  --output-weights training/viewpoint/candidate_ranker_weights.npz \
  --output-report training/viewpoint/candidate_ranker_report.json
```

Whole parts split 50 train / 11 validation / 11 locked test, with 300 / 66 / 66 feature
examples and zero part overlap. The run completed in 539.42 seconds on CPU.

### Fit check before opening the test result

| split | scorer-selected aggregate | best-of-32 oracle | regret | fraction of upper bound |
|---|---:|---:|---:|---:|
| train | 0.545020 | 0.577007 | 0.031986 | 94.46% |
| validation | 0.440888 | 0.654164 | 0.213275 | 67.40% |

The selected-aggregate train/validation gap is **0.104132**; the checkpoint was epoch 222. The old
run's best validation aggregate was 0.325. The new head fits the training ordering closely, so the
failure was architectural—not a shortage of learnable signal: a multimodal pose-regression
objective and candidate-blind occlusion representation were the identified bugs. This intervention
also raises hidden width from 32 to 96, so it does not isolate how much of the train-fit gain came
from objective, evidence, or width. The much larger validation regret makes cross-part
generalization the active failure. No auxiliary regression term was used.

### Deployment-matched comparison

`efccbb77` called its curve random@N, but it oracle-selected the best candidate; those unchanged
values are correctly labeled **best-of-N oracle** below. `random-from-N` is now a seeded uniform
choice from the same prefix. The old direct-regression policy used one predicted pose and therefore
has one constant `policy@1 = 0.264853` line.

| N | old policy@1 | new policy-selected-from-N | random-from-N | best-of-N oracle | mean regret, 95% part CI | policy-random mean delta, 95% part CI | strict W/L/T vs random |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.264853 | 0.164971 | 0.164971 | 0.164971 | 0.000000 [0.000000, 0.000000] | 0.000000 [0.000000, 0.000000] | 0.00% / 0.00% / 100.00% |
| 2 | 0.264853 | 0.206353 | 0.159435 | 0.217183 | 0.010830 [0.000000, 0.027111] | +0.046918 [+0.016619, +0.077853] | 10.61% / 0.00% / 89.39% |
| 4 | 0.264853 | 0.266027 | 0.115089 | 0.349344 | 0.083317 [0.028994, 0.149688] | +0.150938 [+0.077242, +0.220352] | 27.27% / 1.52% / 71.21% |
| 8 | 0.264853 | 0.267895 | 0.123209 | 0.463487 | 0.195592 [0.115286, 0.276406] | +0.144686 [+0.060511, +0.229439] | 28.79% / 9.09% / 62.12% |
| 16 | 0.264853 | 0.281798 | 0.148775 | 0.603040 | 0.321241 [0.264234, 0.376659] | +0.133023 [+0.041925, +0.223511] | 33.33% / 12.12% / 54.55% |
| 32 | 0.264853 | 0.341177 | 0.172378 | 0.678135 | 0.336958 [0.284297, 0.385492] | +0.168799 [+0.081225, +0.263209] | 42.42% / 16.67% / 40.91% |

The scorer is significantly better in mean than seeded uniform choice for every nontrivial budget,
but this is still driven by relatively infrequent large wins. Per-example
`policy-selected-from-N - random-from-N` is:

| N | mean win / loss magnitude | p10 | p25 | median | p75 | p90 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.000000 / 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| 2 | 0.442369 / 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.058008 |
| 4 | 0.595055 / 0.749101 | 0.000000 | 0.000000 | 0.000000 | 0.069367 | 0.717963 |
| 8 | 0.651504 / 0.471552 | 0.000000 | 0.000000 | 0.000000 | 0.403960 | 0.727339 |
| 16 | 0.599165 / 0.550263 | -0.087161 | 0.000000 | 0.000000 | 0.551175 | 0.708575 |
| 32 | 0.616382 / 0.556178 | -0.587620 | 0.000000 | 0.000000 | 0.647084 | 0.796378 |

### Difficulty slices

Each cell is `policy / random / oracle upper bound` on that difficulty.

| difficulty (examples, parts) | N=1 | N=2 | N=4 | N=8 | N=16 | N=32 |
|---|---:|---:|---:|---:|---:|---:|
| easy (17, 11) | .293389 / .293389 / .293389 | .385218 / .310788 / .425008 | .465314 / .190278 / .590748 | .415202 / .299047 / .703681 | .503116 / .317197 / .836911 | .582608 / .319098 / .864685 |
| medium (24, 10) | .156194 / .156194 / .156194 | .204948 / .154501 / .206547 | .219797 / .099877 / .264318 | .183550 / .035971 / .303138 | .182720 / .029443 / .431864 | .239463 / .106436 / .545750 |
| hard (25, 11) | .086072 / .086072 / .086072 | .086072 / .061250 / .086072 | .174893 / .078565 / .266814 | .248698 / .087388 / .454091 | .226417 / .148808 / .608335 | .274650 / .135913 / .678370 |

Medium no longer loses to random at N=1/2, and hard improves materially at N=4/8, but performance is
not monotone as more candidates are offered and regret grows sharply with the oracle opportunity
set. At N=32 the scorer captures only 50.31% of the oracle upper-bound aggregate.

**Decision:** the redesigned scorer fixes the measured training underfit and is useful relative to
blind random choice, but it still does **not** earn its existence over brute-force oracle search.
Best-of-4 oracle (0.349344) already exceeds policy-selected-from-32 (0.341177), and N=32 regret is
0.336958 with a wholly positive part-bootstrap interval. What remains is a generalization/ranking
problem, not an inability to fit the training corpus.

## Architecture: candidate-conditioned inspectability scorer

The policy no longer imagines a pose. It receives a feature/task encoding, an unordered set of
geometry tokens, and each already-valid candidate pose, then emits one unconstrained scalar ranking
logit per candidate. This matches deployment: the harness renders or otherwise offers `N` seeded
candidates and the policy orders those same candidates.

1. A task MLP maps feature kind, question kind, requested detail, and scale to a query context.
2. Shared geometry MLPs map normalized feature/body tokens to attention keys and values. A learned
   bias marks the target token.
3. A candidate MLP maps the continuous pose plus view-dependent geometric evidence to an embedding.
4. Each candidate/task pair independently attends over the same geometry set.
5. The fusion vector `[candidate, task, context, candidate*context, abs(candidate-context)]` passes
   through a 96-wide SiLU layer and a scalar scoring head.

Geometry-token permutation leaves the result unchanged; candidate permutation permutes scores the
same way. The NumPy and Torch forward paths are numerically checked against each other. Unlike direct
pose regression, the head never averages several good modes into a bad camera: all modes remain in
the offered set, and only their ordering is learned.

### Geometry and feature conditioning

Each geometry token remains 18 `float32` values:

| slice | meaning |
|---|---|
| `0:3` | feature center relative to part center, divided by part bounding radius |
| `3:6` | feature half-extent divided by bounding radius |
| `6:9` | unit feature normal/axis, or zero when not applicable |
| `9:15` | face/edge/hole/boss/pocket/region one-hot |
| `15` | `log1p(feature_area / radius²)` |
| `16` | normalized radial position |
| `17` | normalized triangle support |

`geometry_mask[B,G]` removes padding and `feature_mask[B,G]` marks the requested feature. These
view-independent summaries alone could not express “a boss lies between this camera and the hole”:
the same target and blocker tokens were presented for every candidate, leaving a shallow network to
infer a discontinuous line-of-sight test from centers and extents.

### Continuous pose and view-dependent evidence

`candidates[B,N,48]` contains:

| slice | values |
|---|---|
| `0:3`, `3:6` | unit forward and orthogonalized unit up |
| `6:9` | target offset divided by part radius |
| `9:12` | log normalized distance, FOV/180, log aspect ratio |
| `12:15` | feature-to-camera direction in a canonical target-local frame |
| `15:18` | feature axis in the camera right/up/forward frame |
| `18:20` | log target distance and log apparent scale |
| `20:26` | projected target center, extent, in-frame fraction, and in-front fraction |
| `26:35` | visibility bits for a center-plus-eight-extrema ray pattern |
| `35:44` | normalized first-hit depth for those nine rays |
| `44:48` | visible fraction and depth mean/minimum/standard deviation |

The encoder uses only public geometry data and helpers: `Scene.feature_points/feature_center`, mesh
triangles, `CameraPose.forward/right/true_up`, `unit`, and `project_points`. The coarse segment
raycast ignores target-surface triangles and never reads an oracle label. It is cheap and deliberately
lower fidelity than the frozen oracle, but it changes with the candidate and directly exposes the
blocker relation the hard corpus requires. The problem is now learnable by a small scorer because
visibility, relative axis direction, distance, and apparent scale are explicit inputs instead of
latent geometry it would have to reconstruct.

Forward plus orthogonal up remains a continuous 6D rotation representation. There are no Euler
angles, wraparound discontinuities, gimbal singularities, or predicted rotations to repair.

### Tensor contract

| tensor | shape | dtype | contract |
|---|---|---|---|
| task | `[B,14]` | float32 | finite task encoding |
| geometry | `[B,G,18]` | float32 | finite normalized tokens |
| geometry_mask | `[B,G]` | bool | at least one valid token |
| feature_mask | `[B,G]` | bool | at least one marked valid feature token |
| candidates | `[B,N,48]` | float32 | valid pose plus view-dependent evidence |
| candidate_mask | `[B,N]` | bool | at least one valid candidate |
| component labels | `[B,N,7]` | float32 | frozen oracle evidence in canonical order |
| score output | `[B,N]` | float32 | unconstrained ranking logits |
| selected index | `[B]` | int64 | maximum valid-candidate logit |

Canonical label order remains `coverage, visibility, foreshortening, scale, silhouette, depth,
uniqueness`; labels are used to derive training utility and benchmark outcomes, not regressed by the
new head.

## Training objective and split discipline

For candidate utility $q_i$ (the frozen oracle's weighted geometric mean), training forms every
non-tied valid pair in one feature's candidate set. The consequence-weighted RankNet loss is

$$
\mathcal{L} =
\frac{\sum_{i<j}|q_i-q_j|\,\operatorname{softplus}
\left(-\operatorname{sign}(q_i-q_j)(s_i-s_j)/0.10\right)}
{\sum_{i<j}|q_i-q_j|}.
$$

Thus the supervision says which candidate is better and weights mistakes by their inspectability
consequence. It does not regress a pose or an absolute scalar. Exact ties are omitted. The redesign
uses **no auxiliary regression term** (`auxiliary_regression_weight = 0.0`); preserving calibrated
component outputs did not justify mixing the failed absolute objective into the ranking experiment.

All matrices train end-to-end with AdamW, learning rate `2e-3`, weight decay `1e-5`, and gradient
norm clipped to 5. Seeds cover corpus/candidate generation, part splitting, model initialization,
random-from-N selection, and bootstrap resampling. CPU is deterministic.

The split unit is **part identity**, never pose, feature row, or pair. The unchanged seed-1729 locked
split remains 50 train, 11 validation, and 11 test parts (300/66/66 feature examples respectively).
Validation chooses a checkpoint; the locked test is used only after the training-fit check.

## Deployment-matched baselines and metrics

For each nested candidate budget `N in {1,2,4,8,16,32}` the report now separates three choosers:

- **Policy-selected-from-N:** the scorer's maximum logit among the first N offered candidates.
- **Random-from-N:** one seeded uniform choice from the same N candidates.
- **Best-of-N oracle:** the maximum frozen-oracle utility in that set. This is an upper bound, not a
  realizable policy and not mislabeled as random selection.

The honest headline is policy regret, `best-of-N oracle - policy-selected-from-N`. Each budget also
reports policy-versus-random strict win/loss/tie rate, positive/negative magnitudes, delta quantiles,
and 95% confidence intervals from resampling whole parts. Difficulty is sliced at every budget.
Fixed isometric and the old greedy heuristic remain in the machine-readable legacy comparison, but
they are not the deployment upper bound.

## Two-stage curriculum

### Stage 1 — free geometric ranking (implemented)

The kernel/renderer supplies exact component labels for every candidate. Stage 1 converts those
components to utility only for pair ordering and produces the NumPy-exportable scorer in this
package.

### Stage 2 — scarce consumer preference (specified seam, not implemented here)

`services.viewpoint.feedback` already stores winner/loser candidate preferences. A future adapter
must resolve each geometry hash, reconstruct the same 48-value candidate evidence from the recorded
raw camera and geometry, initialize from Stage-1 weights, and optimize pairwise logistic preference
loss weighted by rubric margin, confidence, and agreement. Stage-1 replay remains necessary so
scarce consumer preferences do not erase geometric validity.

That adapter is deliberately absent. Geometry can supervise physical visibility; only downstream
consumer feedback can supervise whether a rendered view answered the user's actual question.

## Runtime and resources

NumPy inference is `NumpyViewpointPolicy.forward/select`; weights are compressed NPZ arrays. Torch
is imported only inside `build_torch_policy` and `fit_policy`. Dataset generation, metrics, and
reference inference remain network-free, GPU-free, and Torch-free. Both dependency modes are in the
test contract.

Quick CPU validation:

```bash
python3 -X utf8 -m pytest training/viewpoint -q
python3 -X utf8 -m training.viewpoint.train \
  --parts 6 --examples-per-part 1 --candidates 32 --epochs 3 \
  --image-size 72 --bootstrap-samples 64
```

The locked 72-part run took 539.42 seconds, including mechanical-part generation, 13,824 frozen
oracle labels, candidate ray features, 500 full-batch epochs, and 2,000 whole-part bootstraps.
Oracle generation and candidate evidence remain CPU work; CUDA accelerates only gradient training.
Attention memory is roughly linear in `B*G*N*hidden`; pairwise-loss memory is `B*N²`. Larger corpora
need part-grouped mini-batches, never example-level shuffling that leaks part identity.

## Honest limitations and next diagnosis

- The locked result has 66 examples but only 11 independent test parts. Whole-part intervals are
  appropriate but do not establish production generalization.
- At policy-selected-from-32, coverage is exactly one on 100% of examples, depth on 95.45%, and
  uniqueness on 92.42%. The seven named components are not seven independent signals.
- Nine target rays expose gross blockers but not topology, concavity, adjacency, translucency, or
  image-space appearance. A useful view absent from the candidate set remains unrecoverable.
- The geometric oracle is task-blind, so question-kind semantics and downstream answer quality are
  still unidentifiable until Stage 2.
- No collision-free camera path, temporal smoothness, multi-view set selection, uncertainty
  calibration, or real-image encoder is implemented.

The train regret of 0.031986 rules out “the scorer cannot represent the training ordering” as the
next diagnosis. Validation regret is 0.213275 and locked-test regret grows to 0.336958 at N=32, while
the selected outcome is non-monotone with candidate budget. The next predeclared experiments should
therefore use training/validation only: part-diverse data scaling, topology-aware target/blocker
features, and ranking calibration or hard-negative sampling. The locked test must not become a
tuning set, and the oracle must not change.

What remains unproven is whether any such scorer generalizes to genuinely unseen mechanical parts
or improves downstream inspection answers on real renders. This Stage-1 run proves neither.
