# Viewpoint oracle acceleration

## Scope and dispatch

`services.viewpoint.accel` is an optional accelerator for the frozen NumPy oracle. It does
not change `geometry.py`, `oracle.py`, `sampling.py`, `parts.py`, or `dataset.py`, and it
does not redefine their numerical policy. Callers batch one scene at a time:

```python
from services.viewpoint.accel import evaluate_many

labels = evaluate_many(
    scene,
    [(feature_id, pose) for pose in candidate_poses],
    device="auto",
)
```

`device="auto"` selects CUDA, then Torch CPU, then `oracle.evaluate`. Torch is imported
lazily, so merely importing `services.viewpoint.accel` has no Torch dependency. Explicit
`cuda` is also fail-soft: unavailable CUDA selects Torch CPU, and CUDA allocation failure
retries on Torch CPU. `return_info=True` returns a `BackendInfo` beside the labels so a
caller can record the device that actually ran. No hook in frozen `oracle.py` is required;
corpus and sequential-search callers pass their whole candidate list directly to
`evaluate_many`.

The Torch kernel uses float64 Möller--Trumbore arithmetic over a ray-by-triangle tensor.
Occlusion rays for all candidate poses and every scene feature (ambiguity needs all of
them), plus both rays for every requested silhouette segment, enter the same batch. A
numerical boundary band detects exact/grazing comparisons and resolves only those rare
rays with the frozen NumPy predicate. This prevents a device rounding difference from
turning a hit into a miss.

## Profile before optimization

Command (Ryzen 5 7600X3D, `nice -n 15`, six OpenMP/MKL threads):

```text
nice -n 15 python3 -X utf8 -m services.viewpoint.accel.benchmark profile
```

The workload was 8 generated mechanical parts, all 12 target features, and 12 randomized
benchmark poses per target: 1,152 labels. It made 77,857,478 calls in 59.832 s.
Cumulative rows overlap: ambiguity contains visual signatures, visual signatures contain
occlusion, and silhouette also calls the ray predicate.

| Region | Calls | Cumulative seconds | Share of 59.832 s | Interpretation |
|---|---:|---:|---:|---|
| `_ray_hits` | 265,400 | 42.508 | 71.0% | dominant ray-triangle intersection kernel |
| `_occlusion_fraction` | 14,976 | 43.232 | 72.3% | sample loop including its ray intersections |
| `_silhouette_informativeness` | 1,152 | 8.211 | 13.7% | hull/segment probes including their ray intersections |
| `_ambiguity` | 1,152 | 46.473 | 77.7% | repeated signatures; almost all time is nested occlusion |
| `_visual_signature` excluding occlusion | 13,824 | about 3.080 | 5.1% | projection, sorting, and signature assembly |
| ambiguity comparison excluding signatures | 1,152 | about 0.161 | 0.3% | length checks and tolerance comparison |
| part and candidate generation outside `evaluate` | 8 parts | 0.369 | 0.6% | not the bottleneck in this profile |

This evidence is why the accelerator batches ray-triangle work rather than replacing the
cheap label arithmetic or part generator. Convex hull and viewport polygon clipping stay
on CPU to preserve the reference implementation.

## Parity

The parity gate uses two seeded randomized mechanical parts, all 12 target feature kinds
per part, and three independently randomized benchmark poses per target. Each of the 72
reference labels is compared with Torch CPU and CUDA (144 backend-label comparisons).
Additional cases cover a fully occluded point feature, a one-point/zero-area projection,
near-tangent grazing cameras, and a NaN ray miss. Required tolerance is `5e-12` absolute
for the seven normalized components, with exact ambiguous-feature IDs.
The numeric report is reproducible with
`nice -n 15 python3 -X utf8 -m services.viewpoint.accel.benchmark parity`.


Measured maximum absolute error on Torch CPU and RTX 3080 Ti CUDA was:

| Component | Maximum absolute error |
|---|---:|
| coverage | 0.0 |
| visibility / occlusion fraction | 0.0 |
| foreshortening | 0.0 |
| scale | 0.0 |
| silhouette | 0.0 |
| depth | 0.0 |
| uniqueness / ambiguity | 0.0 |
| aggregate (additional check) | 0.0 |

There were zero ambiguous-ID mismatches. The tolerance is a rejection threshold, not a
claim that near-threshold changes are acceptable: the observed corpus was bit-identical
as Python floats.

## VRAM management

The kernel never tensors an entire multi-part corpus. It works per scene and chunks the
flattened candidate-by-feature ray dimension. Chunk size is recomputed from current
`torch.cuda.mem_get_info()` (or available host pages for Torch CPU), triangle count,
float/bool element sizes, and the live Möller--Trumbore temporary count. Only one quarter
of currently free memory is budgeted, leaving allocator and application headroom. There
is no fixed ray-count constant. A transient CUDA OOM clears the CUDA cache and continues
on Torch CPU rather than failing the corpus.

## Throughput and 5,000-part wall clock

Command (same machine and resource limits):

```text
nice -n 15 python3 -X utf8 -m services.viewpoint.accel.benchmark throughput
```

The measured scale matches the training expansion contract: one target feature and eight
candidate poses per part. The timed sample was 24 fresh parts / 192 labels at 320 x 240;
Torch paths were warmed before timing. Part generation itself measured 261.562 parts/s.
Here “feature/s” means one complete seven-component feature-pose label per second.

| Backend | Oracle seconds (192 labels) | Features/s | 5,000 parts x 8 poses, including part generation |
|---|---:|---:|---:|
| frozen NumPy CPU | 8.970278 | 21.404 | 1,887.924 s (31m 27.9s) |
| Torch CPU | 2.920461 | 65.743 | 627.545 s (10m 27.5s) |
| RTX 3080 Ti CUDA | 1.026377 | 187.066 | 232.944 s (3m 52.9s) |

The 5,000-part values are projections from the measured per-part wall time plus measured
part generation, not claims that a 5,000-part run was completed during this benchmark.
At this workload CUDA is 8.74x the frozen NumPy label throughput and reduces projected
wall time by 8.10x.

## What remains CPU-bound

Part generation, camera sampling, feature-point extraction, projection, viewport polygon
clipping, convex-hull construction, visual-signature sorting/comparison, label assembly,
and dataset serialization remain CPU-bound. The hull and clipping work are intentionally
left as the frozen NumPy reference; the profiled dominant occlusion and silhouette
ray-triangle predicates run on the selected Torch device in batches.
