# Information-limited viewpoint observations

## Why this boundary exists

The analytic oracle and a camera policy solve different problems. The oracle receives the complete
triangle mesh, the exact target feature record, and unlimited pose evaluations. On synthetic CAD it
is both stronger and cheaper than a learned policy. A policy cannot earn its existence by losing to
that teacher under equal information.

The deployable question is instead whether a policy with **camera-obtainable information** approaches
a full-information oracle, and whether successive looks improve that result. The package
`services.viewpoint.observation` makes that comparison executable. A `Scene` and target feature ID
enter only the synthetic sensor boundary. The returned object is a frozen, slotted, level-specific
record: it cannot retain the scene or feature object and it contains no generic payload through which
labels can leak.

The exact-feature input currently emulates an upstream detector or user-provided target prompt. It is
used to synthesize a deliberately quantized image-space hypothesis. A real detector that produces
that hypothesis is **not** implemented here.

## Public contract

```python
from services.viewpoint.observation import (
    CorruptionConfig,
    ObservationConfig,
    SequentialObserver,
    observe,
)

one = observe(scene, feature_id, camera, level="depth_only", seed=1729)
sequence = SequentialObserver(
    scene, feature_id, level="depth_plus_prior", seed=1729
)
looks = sequence.looks(candidate_cameras[:K])
```

Every level carries only:

- `level` and the requested `camera` pose;
- `workspace_bounds.minimum` and `.maximum`, each a world-space three-vector;
- `feature_region.bounds_normalized = [x0, y0, x1, y1]`, a clipped, quantized 2D region;
- `feature_region.depth_interval`, a coarse depth hypothesis;
- `feature_region.uncertainty_px` and `.confidence`;
- `visits`, the poses already observed and their camera-derived visibility, valid-return, and
  feature-confidence outcomes;
- `visibility_estimate` and `valid_return_fraction`, both camera-derived scalars in `[0, 1]`.

`WorkspaceBounds.as_vector()` has shape `(6,)` and `FeatureRegion2D.as_vector()` shape `(8,)`, with
the fields in the order above. Depth maps use `float64`; a missing return is `NaN`. Point and normal
arrays are `float64`, triangle indices are `int64`, and numeric payload arrays owned by the
observation dataclasses are read-only.

### Graded information levels

| level | additional fields | intended comparison |
|---|---|---|
| `full` | complete `mesh_vertices`, `mesh_triangles`, plus coarse `depth_map` and `normal_map` | the current mesh cheat; an upper-information comparator, not camera-deployable |
| `depth_only` | coarse `depth_map`, `normal_map` | one RGB-D-like look without world history |
| `depth_plus_prior` | current maps plus `accumulated_points`, `accumulated_normals` | current look fused with surfaces actually observed on earlier looks |
| `sparse_points` | bounded attempted-ray `points`, `normals` sample; missing returns remain `NaN` | a low-bandwidth depth sensor or sparse reconstruction without valid-return selection bias |

The normal channel is a quantized visible-triangle estimate. It has no target ownership or component
identity. `depth_plus_prior` voxel-deduplicates only points returned by cameras already visited; it
does not reveal unseen triangles. Sparse sampling chooses uniformly from all attempted rays rather
than only successful returns, so reflective/grazing failures cannot silently bias the point sample.

The dataclasses deliberately do **not** expose `Scene`, `Feature`, `feature_id`, feature triangle
indices, exact feature points or orientation, exact occlusion sets, ambiguous feature IDs,
`InspectabilityLabel`, aggregate utility, or any of the oracle's seven component labels. `full`
contains the mesh because it is the explicit cheat comparator, but still receives only the same
coarse target-region hypothesis as the camera levels.

## Synthetic camera and corruption model

The reference sensor casts a `24 x 32` perspective ray grid by default and retains the nearest
surface return. Resolution, sparse sample count, target-region quantization, target uncertainty, and
prior voxel size are explicit in `ObservationConfig`.

Visibility is a geometric line-of-sight readout, not target-region occupancy. The synthetic target
hypothesis is projected into the sensor grid, and each target ray compares its expected **range**
against the observed range at that ray. A closer return is an occluder. `depth_plus_prior` carries the
finite-only voxel union as geometry, but prior points are not allowed to prove current line of sight:
a stale point behind a newly observed boss is not a valid visibility return. `sparse_points` uses the nearest attempted ray
within a support radius derived from its sampling density. `full` casts those target rays directly
against the complete mesh; this intentionally reconstructs the full-information visibility ceiling
without importing or attaching an `InspectabilityLabel`. Confusing forward-axis projection depth
with ray range was the original low-signal defect and is explicitly avoided.

`CorruptionConfig` has three independently disabled-by-zero corruption families:

1. `depth_noise_std` adds zero-mean Gaussian range noise to finite returns.
2. `grazing_dropout_probability` removes returns increasingly as the absolute ray/normal cosine
   falls below `grazing_cosine_threshold`.
3. `calibration_position_std` and `calibration_angle_std_deg` perturb the actual extrinsic used to
   capture the frame while points are reconstructed through the requested nominal pose. This
   creates the expected world-frame misregistration rather than merely changing metadata.

A `SeedSequence` gives calibration, dropout, range noise, and sparse sampling separate child random
streams. Enabling depth noise therefore cannot change which returns the grazing process dropped.
Repeated calls with the same scene, camera, configuration, and seed are byte-deterministic.

This is a coarse geometric sensor, not a claim of photorealism. It does not model RGB texture,
exposure, multipath, translucent material, rolling shutter, lens distortion, learned normal error,
or a real target detector.

## Sequential K-look semantics

`SequentialObserver.look(camera)` captures one new frame and returns an observation whose `visits`
includes that current look. Thus calls 1 through K return histories of lengths 1 through K. Each
`VisitRecord.visibility_estimate` is the outcome of that frame; the observation-level
`visibility_estimate` is the running maximum over the visits. For `depth_plus_prior`,
`accumulated_points` is the voxelized union of finite visible returns from all looks so far. No
oracle value is used to update either state.

This supports the deployment-realistic loop:

1. choose a first pose from the prior target region and workspace bounds;
2. capture depth/normal evidence;
3. update visible geometry and uncertainty;
4. choose the next pose conditioned on all observed outcomes;
5. stop at K looks or an explicit confidence criterion.

Whether a learned sequential policy uses this evidence well is a separate evaluation. The sensor
only makes the experiment possible and prevents the policy input from silently regaining the
teacher's mesh or labels.

## Measured information audit

The deterministic audit below kept the eight locked parts and used eight target features per part,
giving 64 sequence samples grouped by `part_id`. Each target had a seeded 16-camera pool. A random
sequence is a permutation fixed before observing any return. The `oracle_best_next` sequence orders
that pool by the frozen oracle's true visibility; because the measured objective is best visibility
seen so far, it establishes the best-camera ceiling at its first look. At K=1, 2, 4 and 8, the audit
compares the running maximum observation estimate with the running maximum
`1 - occlusion_fraction`. The oracle appears only after capture on this evaluation side.

```bash
OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m services.viewpoint.observation.benchmark \
  --parts 8 --looks-per-part 8 --candidate-pool 16 --targets-per-part 8 --seed 1729
```

The unchanged pooled one-look check remains:

| information level | samples | Pearson correlation | MI (bits) | estimate mean | oracle mean |
|---|---:|---:|---:|---:|---:|
| `full` | 64 | 1.000000 | 1.799199 | 0.261719 | 0.261719 |
| `depth_only` | 64 | 0.583859 | 0.756250 | 0.167643 | 0.261719 |
| `depth_plus_prior` | 64 | 0.583859 | 0.756250 | 0.167643 | 0.261719 |
| `sparse_points` | 64 | 0.139067 | 0.461442 | 0.245768 | 0.261719 |

The K-look results are correlation / empirical eight-bin MI in bits:

| random sequence level | K=1 | K=2 | K=4 | K=8 |
|---|---:|---:|---:|---:|
| `full` | 1.000000 / 1.545656 | 1.000000 / 1.916232 | 1.000000 / 2.281081 | 1.000000 / 1.990990 |
| `depth_only` | 0.339616 / 0.401623 | 0.480747 / 0.509220 | 0.437611 / 0.762341 | 0.414709 / 0.601681 |
| `depth_plus_prior` | 0.339616 / 0.401623 | 0.480747 / 0.509220 | 0.437611 / 0.762341 | 0.414709 / 0.601681 |
| `sparse_points` | 0.087567 / 0.236962 | 0.043274 / 0.211427 | 0.124012 / 0.260194 | -0.044104 / 0.250068 |

| oracle-best-next sequence level | K=1 | K=2 | K=4 | K=8 |
|---|---:|---:|---:|---:|
| `full` | 1.000000 / 1.892095 | 1.000000 / 1.892095 | 1.000000 / 1.892095 | 1.000000 / 1.892095 |
| `depth_only` | 0.324439 / 0.396737 | 0.548835 / 0.587884 | 0.646541 / 0.684142 | 0.557667 / 0.623922 |
| `depth_plus_prior` | 0.324439 / 0.396737 | 0.548835 / 0.587884 | 0.646541 / 0.684142 | 0.557667 / 0.623922 |
| `sparse_points` | -0.136968 / 0.288306 | -0.152799 / 0.292308 | -0.005349 / 0.368864 | -0.010955 / 0.212573 |

K=1 in the sequence table is one random row per target and therefore is not the same sampling
distribution as the pooled 0.584 one-look check. Small MI differences remain descriptive: 64
whole-part-grouped rows do not make them population estimates.

### What the fusion diagnosis found

- **A real scalar fusion defect was fixed.** The point cloud accumulated, but the observation-level
  scalar described only the current camera, so the policy's scalar channel discarded all earlier
  outcomes. It now reports the running best while `VisitRecord` remains per-frame. The old
  reproject-and-OR path was also removed: a prior point cannot establish that a target is visible
  through an occluder in the current frame.
- **Later looks are not the same capture.** Every eight-look sequence used eight unique camera
  positions. Consecutive forward-axis separation averaged 90.046 degrees for random order and 77.664
  degrees for oracle order (minimum 0.800 and 3.943 degrees respectively).
- **Evidence is combined, not overwritten.** Mean finite voxel counts grew
  67.25 -> 127.78 -> 251.75 -> 524.27 for random order and
  65.05 -> 133.62 -> 262.05 -> 514.67 for oracle order at K=1/2/4/8.
- **NaNs do not poison fusion.** Invalid returns are filtered before voxel union; every accumulated
  point in all 512 audited sequence/checkpoint observations was finite.
- **Feature-neighbourhood coverage does increase.** Within one coarse sensor-pixel footprint, mean
  weighted support grew 0.420 -> 0.734 -> 0.911 -> 0.996 for random order and
  0.606 -> 0.896 -> 0.990 -> 1.000 for oracle order.

Thus motion adds some measurable information through K=4 under the best-next order, but not
monotonically. The strongest depth result is only correlation 0.647 / MI 0.684 at K=4; it falls to
0.558 / 0.624 at K=8 even though physical neighbourhood coverage reaches 1.0. Random ordering peaks
at correlation 0.481 at K=2 (MI peaks at 0.762 at K=4) and ends at 0.415 / 0.602. The identical
`depth_only` and `depth_plus_prior` scalar rows are intentional after the fix: accumulated points are
extra geometry for a policy, not permission to manufacture line-of-sight evidence. A 24 x 32 depth
camera cannot identify a surface hidden behind a boss from that look; moving exposes the
neighbourhood, but the coarse returns still do not reliably associate the best visible surface with
the target. Raising these figures with the frozen oracle's sample mask would violate the information
boundary.

### Ceiling and honest policy-gap attribution

The best measured observation ceiling is therefore the oracle-ordered K=4 depth correlation of
**0.647**, not 1.0 and not a monotonically improving K=8 claim. For one explicitly labeled diagnostic
attribution, linearly attenuating the 0.638 full-information ceiling from the 0.123 random baseline by
that correlation gives `0.123 + 0.647 * (0.638 - 0.123) = 0.456`. On that approximation, observation
quality accounts for **0.182 of the 0.337 policy-to-oracle gap, or 54.0%**; the remaining 0.155
(46.0%) is selection/policy headroom. The deployment-realistic random K=8 correlation of 0.415 gives
a lower estimated ceiling of 0.337 and attributes 0.301, or 89.4%, of the gap to observation quality.
These are diagnostic estimates, not causal bounds: correlation of the visibility component cannot
identify aggregate policy quality.

Both comparison bars remain necessary. The limited policy's 0.301 is 2.45x random (0.123) and 1.75x
the fixed isometric view (0.172), so it is useful under deployable information. In the same breath, it
recovers only 47.2% of the 0.638 full-information oracle ceiling, limited-information data scaling
saturates at validation regret 0.346 / 0.344 / 0.351 for 200 / 1,000 / 5,000 parts, and sequential
K=4 adds nothing within whole-part uncertainty (depth 0.439 vs random 0.439, prior 0.446 vs 0.439,
sparse 0.431 vs 0.439; every delta interval crosses zero).

## What remains unproven

It remains unproven that a learned one-shot or K-look policy can turn these limited observations into
near-oracle pose choices on unseen parts, that the coarse feature hypothesis can be produced reliably
from a real webcam, or that improved geometric visibility improves downstream model answers. The
sensor boundary and its leakage tests make those experiments honest; they do not supply their result.
