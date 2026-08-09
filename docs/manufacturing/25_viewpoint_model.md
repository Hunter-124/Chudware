# Learned viewpoint policy — limited-information and sequential evaluation

## Decision and corrected framing

The policy ranks a bounded camera candidate set for a feature/question pair. It does **not** regress a
pose. That architectural decision remains correct: inspectability is multimodal, and ranking avoids
averaging several good views into one bad pose.

The evaluation question has changed. Comparing a learned policy with the analytic oracle while both
receive exact CAD geometry is a student-versus-teacher check on the teacher's home turf. The oracle
has the exact mesh, exact feature, exact occlusion geometry, and every candidate evaluation. It is
cheap and unbeatable in that setting. That comparison is useful for debugging fit, but it is **not**
the product claim.

The product-relevant bar is:

1. how close a policy with camera-limited information gets to the unchanged full-information oracle;
2. whether an adaptive K-look policy uses accumulated observations to beat a non-adaptive one-shot
   search schedule at the same render budget; and
3. eventually, whether selected views improve downstream task answers, for which the geometric
   oracle cannot supply labels.

**Verdict from this run: the policy does not yet earn its existence for the product setting.** It
beats blind one-view random and usually beats fixed isometric, so it learns signal. It remains far
from the full-information oracle, adding parts does not reduce limited-information validation regret,
and at K=2 or K=4 no sequential policy beats one-shot random search with a whole-part confidence
interval excluding zero. This is an honest negative result, not a reason to weaken the oracle.

## Reproducible protocol

All randomness is seed 1729. Splits are by whole `part_id`; no pose, target, or candidate row crosses
a split. The original 72-part split remains embedded exactly: 50 original training parts, 11
validation parts, and the same 11 locked test parts. Procedural additions start at part index 72 and
belong only to training.

The two reported runs were:

```bash
python3 -X utf8 -m training.viewpoint.scale \
  --ladder 50 200 1000 5000 --epochs 120 --workers 6 --device cpu \
  --output-weights training/viewpoint/full_information_scale_weights.npz \
  --output-report training/viewpoint/full_information_scale_report.json

python3 -X utf8 -m training.viewpoint.information \
  --train-parts 5000 --part-ladder 200 1000 5000 --epochs 120 \
  --weight-decay 0.001 --workers 6 --device cpu --bootstrap-samples 2000 \
  --output-report training/viewpoint/information_asymmetry_report.json
```

The full-information expansion keeps the original 50 × 6 × 32 training rows unchanged and adds one
8-candidate target row per new part. Validation and locked test remain 11 × 6 × 32 at 320 px. The
limited-information run uses one initial observation and eight candidates per training part; the
validation and locked test sets have six targets per part and 16 candidates at 96 px. The sequential
benchmark uses eight candidates. Its K value counts **additional** renders after the same common
fixed calibrated initial camera for both methods.

Every interval below is a 95% percentile interval from 2,000 seeded resamples of whole parts. The
oracle remains the maximum measured analytic quality over the entire relevant candidate pool; it is
never budget-matched downward to help the policy.

## Full-information reference: student versus teacher

### Existing 72-part result, retained verbatim as the reference row

The previous candidate-ranker result used exact geometry and candidate-conditioned ray evidence.
Its fit check was:

| split | policy aggregate | full-information best-of-32 oracle | regret | oracle fraction |
|---|---:|---:|---:|---:|
| train | 0.545020 | 0.577007 | 0.031986 | 94.46% |
| validation | 0.440888 | 0.654164 | 0.213275 | 67.40% |

On the locked test at N=32 it selected 0.341177, versus random-from-32 0.172378 and the
full-information oracle 0.678135, for regret 0.336958. Best-of-4 oracle was already 0.349344. These
numbers prove that the ranking head can fit and that it learns more than random. They do not justify
a learned policy on CAD geometry.

### Full-information validation regret versus training parts

| training parts | training rows | train aggregate / upper bound | validation aggregate | validation oracle | validation regret |
|---:|---:|---:|---:|---:|---:|
| 50 | 300 | 0.393425 / 0.577007 | 0.438044 | 0.654164 | 0.216120 |
| 200 | 450 | 0.363103 / 0.521029 | 0.443063 | 0.654164 | 0.211101 |
| 1,000 | 1,250 | 0.358332 / 0.470152 | 0.439341 | 0.654164 | 0.214823 |
| 5,000 | 5,250 | 0.320059 / 0.438591 | 0.468744 | 0.654164 | 0.185420 |

The 50-part endpoint is close to the previous 500-epoch validation regret of 0.213275 despite using
only 120 epochs. At 200 and 1,000 parts the curve is effectively flat and non-monotone. At 5,000 it
finally improves by 0.027855 versus the old result, a 13.1% regret reduction. The curve has **not**
shown a clean saturation point; it shows a long plateau followed by a modest gain. Because expansion
rows have eight candidates while the retained base has 32, training aggregates and upper bounds are
not comparable across rows; validation regret is the stable comparison.

The selected 5,000-part full-information policy reached 0.403032 on the unchanged locked test. Its
oracle was still 0.678135, so regret was 0.275103 [0.213635, 0.349631]. This improves the previous
policy by 0.061855 and lowers regret by 18.4%, but still loses decisively to its teacher. Difficulty
was:

| difficulty (examples, parts) | policy | random | fixed isometric | oracle | regret |
|---|---:|---:|---:|---:|---:|
| easy (17, 11) | 0.735192 | 0.392743 | 0.290402 | 0.864685 | 0.129493 |
| medium (24, 10) | 0.220056 | 0.029908 | 0.087055 | 0.545750 | 0.325694 |
| hard (25, 11) | 0.352820 | 0.051168 | 0.183441 | 0.678370 | 0.325549 |

### Regularization and augmentation trials

Regularization was selected on validation at 200 parts before opening the locked test:

| AdamW weight decay | validation aggregate | validation regret | result |
|---:|---:|---:|---|
| 0 | 0.438285 | 0.215879 | did not help |
| 1e-5 | 0.438285 | 0.215879 | did not help |
| 1e-4 | 0.439957 | 0.214207 | small improvement, not selected |
| 1e-3 | 0.443063 | 0.211101 | selected |

Candidate-order permutation was also tried as an augmentation check. The maximum absolute score
change after permuting all 32 candidates was exactly 0.0 and every selected physical candidate was
identical. This did not help because the architecture is already exactly candidate-permutation
equivariant; no redundant shuffle augmentation was added. No test result was used to select either
regularization or augmentation.

## Information-asymmetric headline evaluation

### What the policy can see

The synthetic sensor boundary returns no `Scene`, mesh, analytic feature, occlusion set, oracle
component, or label. The policy receives one of:

- `depth_only`: a 12 × 16 depth/normal image with NaN missing returns;
- `depth_plus_prior`: the current depth/normal image plus voxel-deduplicated world points and normals
  accumulated only from prior finite camera returns; or
- `sparse_points`: 48 uniformly attempted pixel returns, with missed points/normals represented by
  NaNs rather than silently sampling only successful hits.

All levels also expose calibrated workspace bounds, the camera, visit summaries, and a quantized 2D
feature-region hypothesis. The sensor audit against true visibility, grouped by part, reported
correlations of 0.584 for depth-only, 0.596 for sequential depth-plus-prior, and 0.139 for sparse
points; the full-mesh control was 1.000. Thus the levels are no longer identical readouts, but sparse
visibility is weak.

The encoder converts the public object only. It uses one metadata token, a 4 × 4 grid of observed
depth/normal tokens, and up to eight workspace-octant point tokens for prior or sparse returns.
Candidate vectors contain calibrated pose, current-camera-relative pose, the quantized 2D region,
visit count, and observed visibility/return fractions. Exact target geometry and the old nine-ray
CAD occlusion evidence are absent.

### Per-observation-level locked-test result

The policy makes one additional camera choice after the common initial observation. `random` is one
seeded uniform candidate; `fixed` is the nearest offered isometric camera. Both have the same initial
observation available. The full oracle sees analytic geometry and all 16 labels.

| observation | policy | random | fixed | full oracle | oracle regret, 95% part CI | policy−random, 95% part CI | policy−fixed, 95% part CI |
|---|---:|---:|---:|---:|---:|---:|---:|
| depth_only | 0.300661 | 0.122591 | 0.171608 | 0.638062 | 0.337401 [0.280749, 0.396614] | +0.178070 [+0.102354, +0.254252] | +0.129053 [+0.020345, +0.236038] |
| depth_plus_prior | 0.242777 | 0.122591 | 0.171608 | 0.638062 | 0.395285 [0.313919, 0.474466] | +0.120186 [+0.040783, +0.210681] | +0.071169 [-0.071806, +0.208212] |
| sparse_points | 0.265603 | 0.122591 | 0.171608 | 0.638062 | 0.372459 [0.300526, 0.436077] | +0.143013 [+0.073043, +0.215206] | +0.093996 [-0.025317, +0.206880] |

Depth-only is the strongest one-look policy and significantly beats both blind baselines, but it
captures only 47.1% of the full-oracle aggregate. Depth-plus-prior has no useful prior on the first
decision beyond the initial view and does worse. Sparse points beat random but not fixed isometric
with a confidence interval excluding zero.

### Limited-information validation regret versus training parts

This is the predeclared depth-plus-prior curve, evaluated on the unchanged 11 validation parts:

| training parts | train aggregate / upper bound | validation aggregate | validation oracle | validation regret |
|---:|---:|---:|---:|---:|
| 200 | 0.363884 / 0.478272 | 0.271861 | 0.618252 | 0.346391 |
| 1,000 | 0.266110 / 0.492213 | 0.273796 | 0.618252 | 0.344456 |
| 5,000 | 0.221639 / 0.483340 | 0.267272 | 0.618252 | 0.350980 |

The curve saturates by 200 parts and slightly worsens at 5,000. The 200-part train→validation gap of
0.092023 disappears by 1,000 parts, yet validation regret does not improve. More procedural parts
fix the measured overfit gap but do not make this observation encoder recover the teacher's missing
information. That is a representation/information result, not permission to tune on the locked test.

### One-look difficulty slices

Each cell is `policy / random / fixed / full oracle (regret)`.

| observation | easy (17 examples, 11 parts) | medium (24, 10) | hard (25, 11) |
|---|---:|---:|---:|
| depth_only | .509367 / .191630 / .341720 / .814856 (.305490) | .234171 / .105990 / .130393 / .643002 (.408831) | .222572 / .091581 / .095498 / .513100 (.290528) |
| depth_plus_prior | .534654 / .191630 / .341720 / .814856 (.280202) | .174205 / .105990 / .130393 / .643002 (.468797) | .110129 / .091581 / .095498 / .513100 (.402971) |
| sparse_points | .553294 / .191630 / .341720 / .814856 (.261562) | .200552 / .105990 / .130393 / .643002 (.442450) | .132423 / .091581 / .095498 / .513100 (.380677) |

Easy features drive most wins. Medium and hard regret remain large under every observation level.

## Sequential K-look diagnosis

The budget is matched explicitly. Both methods receive and are charged for one common fixed
calibrated initial render. They then spend exactly K additional renders: the learned policy chooses
adaptively from the accumulated observation, while the baseline commits to one seeded random
permutation before seeing any outcome. Thus the total-render curve is 2, 3, 5, and 9 renders at
K = 1, 2, 4, and 8. Neither method receives a render that the other is charged for.

### Does history reach the scorer?

Yes. The direct regression test holds the physical candidate set and final camera fixed, constructs
two different prior-look histories, and then freezes the encoded candidate tensor as well. The
encoded history tokens differ and the checked-in depth-plus-prior policy's score vector differs.
History reaches the network; the sequential null was not caused by a disconnected history input.

The audit did find a perception-side fusion bug: `SequentialObserver` accumulated points but exposed
the current frame's scalar visibility estimate as if it described the accumulation. It now exposes
running-max visibility while retaining per-frame values in `VisitRecord`. No oracle value enters an
observation.

### Information gained per look after the fusion fix

The observation audit used 64 targets (eight locked parts × eight targets), a 16-pose pool, and seed
1729. Cells are Pearson correlation with true visibility / mutual information. `random` uses a seeded
random pose order; `oracle-best-next` is a diagnostic upper schedule, not a deployable policy.

```bash
OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m services.viewpoint.observation.benchmark \
  --parts 8 --looks-per-part 8 --candidate-pool 16 --targets-per-part 8 --seed 1729
```

| schedule | observation | K=1 | K=2 | K=4 | K=8 |
|---|---|---:|---:|---:|---:|
| random | full control | 1.000 / 1.545656 | 1.000 / 1.916232 | 1.000 / 2.281081 | 1.000 / 1.990990 |
| random | depth | .339616 / .401623 | .480747 / .509220 | .437611 / .762341 | .414709 / .601681 |
| random | depth plus prior | .339616 / .401623 | .480747 / .509220 | .437611 / .762341 | .414709 / .601681 |
| random | sparse points | .087567 / .236962 | .043274 / .211427 | .124012 / .260194 | -.044104 / .250068 |
| oracle-best-next | full control | 1.000 / 1.892095 | 1.000 / 1.892095 | 1.000 / 1.892095 | 1.000 / 1.892095 |
| oracle-best-next | depth | .324439 / .396737 | .548835 / .587884 | .646541 / .684142 | .557667 / .623922 |
| oracle-best-next | depth plus prior | .324439 / .396737 | .548835 / .587884 | .646541 / .684142 | .557667 / .623922 |
| oracle-best-next | sparse points | -.136968 / .288306 | -.152799 / .292308 | -.005349 / .368864 | -.010955 / .212573 |

Depth accumulation contains real information: random ordering improves from r=.340 at K=1 to .481
at K=2, and the diagnostic best-next schedule reaches .647 at K=4. It is not monotonic, however;
random depth falls to .438/.415 at K=4/K=8 and best-next falls to .558 at K=8. Depth plus prior is
numerically identical to depth, so accumulated points add no visibility information under this
estimator. Sparse points never establish useful correlation. The fusion has signal, but perception
still caps and sometimes degrades that signal.

### Why the original eight-candidate null was not a valid product finding

After the fusion fix, the original eight-pose experiment is:

| observation | K | adaptive policy | random best | policy−random, 95% whole-part CI |
|---|---:|---:|---:|---:|
| depth | 1 | .285545 | .181710 | +.103835 [+.021234, +.204482] |
| depth | 2 | .336239 | .300704 | +.035535 [-.052179, +.105814] |
| depth | 4 | .459549 | .439146 | +.020403 [-.039133, +.075502] |
| depth | 8 | .536164 | .536164 | 0 [0, 0] |
| depth plus prior | 1 | .207592 | .181710 | +.025882 [-.057621, +.130828] |
| depth plus prior | 2 | .336958 | .300704 | +.036254 [-.031785, +.095292] |
| depth plus prior | 4 | .457934 | .439146 | +.018788 [-.043495, +.078175] |
| depth plus prior | 8 | .536164 | .536164 | 0 [0, 0] |
| sparse points | 1 | .234119 | .181710 | +.052409 [-.032985, +.153750] |
| sparse points | 2 | .322605 | .300704 | +.021901 [-.050152, +.084085] |
| sparse points | 4 | .434640 | .439146 | -.004506 [-.069119, +.046721] |
| sparse points | 8 | .536164 | .536164 | 0 [0, 0] |

At K=4 the random schedule has already sampled half the pool. At K=8 every method exhausts the pool
and mechanically equals its .536164 oracle. That endpoint proves accounting, not learning, and the
small pool leaves too little selection problem to support a sequential-product verdict.

## Model and objective

The neural scorer is unchanged in kind. A task MLP embeds feature/question semantics. Geometry or
observation tokens become attention keys and values. Every candidate pose becomes a query, and the
fusion MLP emits one unconstrained scalar rank logit. Geometry-token permutation leaves scores
unchanged; candidate permutation permutes scores identically. The NumPy inference implementation is
the deployment reference and Torch remains an optional, lazily imported training dependency.

For oracle utility $q_i$ and score $s_i$, consequence-weighted RankNet minimizes

$$
\mathcal{L} =
\frac{\sum_{i<j}|q_i-q_j|\,\operatorname{softplus}
\left(-\operatorname{sign}(q_i-q_j)(s_i-s_j)/0.10\right)}
{\sum_{i<j}|q_i-q_j|}.
$$

Exact ties and padded candidates are excluded. Training intentionally keeps padded logits finite
before pairwise subtraction; otherwise masked `-inf - -inf` pairs produce NaN gradients even when a
later pair mask excludes them. The NumPy path never imports Torch.

## What remains unproven

- The test has 66 examples but only 11 independent parts. Whole-part intervals are necessary but do
  not establish broad production generalization.
- `FeatureRegion2D` is a deliberately quantized synthetic hypothesis produced from the known target.
  A real detector that finds the user's intended feature under occlusion has not been built or
  evaluated. Workspace bounds are also supplied by the synthetic sensor.
- The generator varies parameters and rotations but keeps one mechanical topology family. Five
  thousand procedural identities are not five thousand real manufacturing distributions.
- Sparse visibility has only 0.139 correlation with the oracle visibility signal. More points,
  learned image features, uncertainty calibration, or a different sequential state may be needed.
- The analytic oracle is task-blind. Stage 2 must measure whether a view lets a downstream model or
  person answer the actual question; by construction no geometric oracle can certify that.
- Collision-free camera motion, reachability, temporal smoothness, lighting, material appearance,
  real depth noise, and camera calibration drift are not in this benchmark.

The next justified work is not another oracle concession. It is a representation experiment on
training/validation only that makes accumulated observations change the ranking usefully, followed
by a predeclared locked-test rerun, and then real-camera/answer-quality evaluation. Until either the
limited-information regret closes materially or adaptive K-look selection beats matched one-shot
search, the learned policy remains an informative research result rather than a product dependency.
