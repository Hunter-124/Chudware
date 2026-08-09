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
against the observed range at that ray. A closer return is an occluder. `depth_plus_prior` also
reprojects the accumulated world points into the candidate camera and takes visibility supported by
either the current frame or the prior reconstruction. `sparse_points` uses the nearest attempted ray
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
`VisitRecord` contains only the requested pose and outcomes inferred from that frame. For
`depth_plus_prior`, `accumulated_points` is the voxelized union of finite visible returns from all
looks so far. No oracle value is used to update state.

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

The deterministic audit below used eight generated parts, eight looks per part, seed 1729, and no
corruption. Samples remain grouped by `part_id`; no pose or feature row is treated as an independent
split unit. The oracle appears only on the evaluation side as
`1 - occlusion_fraction`. The observation-side readout is the declared
`visibility_estimate`, independently reconstructed per level by the line-of-sight computations
above; the analytic value is never passed into a camera-limited observation.

```bash
python3 -X utf8 -m services.viewpoint.observation.benchmark \
  --parts 8 --looks-per-part 8 --seed 1729
```

| information level | samples | Pearson correlation with oracle visibility | empirical MI (bits) | estimate mean | oracle mean |
|---|---:|---:|---:|---:|---:|
| `full` | 64 | 1.000000 | 1.799199 | 0.261719 | 0.261719 |
| `depth_only` | 64 | 0.583859 | 0.756250 | 0.167643 | 0.261719 |
| `depth_plus_prior` | 64 | 0.595711 | 0.771570 | 0.171875 | 0.261719 |
| `sparse_points` | 64 | 0.139067 | 0.461442 | 0.245768 | 0.261719 |

Mutual information is empirical after fixed eight-bin discretization on `[0, 1]`; 64 samples are too
few to interpret small differences as population effects. The `full` equality is the required
sanity check: mesh-ray occlusion reconstructs the unchanged oracle visibility ceiling. The two depth
levels recover materially more of that signal than the original proxy (correlation 0.584/0.596
rather than 0.153), while prior accumulation gives a small measured increase. Sparse sampling is
properly ordered as the weakest signal. Its estimate mean is now 0.246 against oracle mean 0.262,
instead of the selection-biased 0.674 produced when only valid returns were sampled. Its MI remains
nonzero, but the low correlation is an explicit limitation rather than a favorable sparse-policy
claim. No threshold or metric was selected to make a policy win.

These numbers are intentionally not compared as if they were policy aggregate quality. The current
full-information candidate ranker reports train selected aggregate 0.545 versus oracle 0.577,
validation regret 0.213, and policy-from-32 test aggregate 0.341 versus best-of-4 oracle 0.349. This
observation audit measures a different axis: how much oracle visibility is recoverable from a
camera-limited signal. The next valid policy evaluation is **limited-information policy versus the
unchanged full-information oracle**, at matched pose/look budgets, with results grouped and split by
whole part.

## What remains unproven

It remains unproven that a learned one-shot or K-look policy can turn these limited observations into
near-oracle pose choices on unseen parts, that the coarse feature hypothesis can be produced reliably
from a real webcam, or that improved geometric visibility improves downstream model answers. The
sensor boundary and its leakage tests make those experiments honest; they do not supply their result.
