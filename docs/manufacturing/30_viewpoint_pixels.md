# Viewpoint rendered-pixel corpus

## Scope

`services.viewpoint.pixels` supplies the camera-limited corpus for the learned viewpoint
encoder. It consumes, but does not modify, the frozen benchmark modules
`geometry.py`, `oracle.py`, `sampling.py`, `parts.py`, and `dataset.py`. The production
configuration keeps seed 1729, the original 11 validation parts, the original 11 locked
test parts, six query examples per part, 16 candidates per query, and 96 x 96 cameras.
It grows only the training split: the original 50 training parts plus deterministic part
indices 72 through 521 give 500 training parts and 3,000 training rows.

```python
from services.viewpoint.pixels import PixelCorpusConfig, generate_pixel_corpus

corpus = generate_pixel_corpus(PixelCorpusConfig(), workers=6, device="cuda")
model_inputs = corpus.model_inputs()       # observations plus query metadata only
training_targets = corpus.targets          # training/evaluation side only
```

## Inference capability boundary

`PixelInputs` has exactly these fields:

| Field | Shape / dtype | Meaning |
|---|---|---|
| `rgb` | `uint8[B,N,H,W,3]` | clean rendered sensor pixels |
| `depth` | `float32[B,N,H,W]` | finite optical-axis depth normalized to the camera near/far interval; background is zero |
| `depth_mask` | `bool[B,N,H,W]` | valid rendered depth returns |
| `cameras` | `float32[B,N,16]` | position, forward, true-up, right, vertical FOV in radians, aspect, near, far |
| `coarse_prior` | `float32[B,N,8]` | the existing uncertainty-bearing `FeatureRegion2D.as_vector()` only |
| `task` | `float32[B,14]` | structurally separate query metadata: feature-class one-hot, question-class one-hot, fixed min-scale/detail constants |
| `candidate_mask` | `bool[B,N]` | offered candidate rows |

The task says what the user asked to inspect, but contains no position, axis, normal,
extent, target ID, or other exact feature geometry. The sensor channels contain no mesh,
feature object, exact visibility/occlusion set, ambiguous-feature IDs, oracle aggregate,
or oracle component. The seven labels live in a separate `PixelTargets.components`
array in canonical order: coverage, visibility, foreshortening, scale, silhouette, depth,
and uniqueness.

Every input JSON object is content-addressed by its own SHA-256 and contains no row ID or
target-object reference. The training manifest pairs an input-object address with a
separate opaque target-object address; `model_inputs()` never returns that pairing or a
label key. This prevents an inference sample from looking up its own training target.
Evaluation-only random, fixed-isometric, and limited-information greedy indices also stay
in `PixelTargets`, never in model inputs.

## Renderer path and engine agreement

Generating tens of thousands of views by repeatedly importing a part and crossing the
worker RPC would spend most of the budget on document/RPC lifecycle. The corpus therefore
uses `viewpoint-pixel-raster/1.2.0; engine-perspective-v1; cuda-batch-v1`. CPU projection
and six-plane clipping retain the exact scalar camera math, while the dominant
reciprocal-depth test is batched over all 16 candidates in float64 on CUDA. Torch remains
a lazy optional import: `auto` falls back to the exact scalar NumPy renderer when Torch or
CUDA is unavailable.

The renderer mirrors `doc_render_view.camera_pose.v1`: the same perspective basis and
focal scale, six clipping planes, half-pixel edge tests, reciprocal-depth z buffer,
clean-shot material, world light, and background. Shading is Gouraud, like the engine's
`render_view_to_rgb`: the diffuse term is computed per vertex, carried through clipping,
and interpolated per pixel. The shading normals reproduce the mesh-import lane's
`compute_mesh_smooth_normals` exactly — positions welded into 1e-6 mm buckets, an
area-weighted sum over the incident triangles within a 40 degree crease angle, stored at
float32 — because that is the body the engine actually renders for a corpus part. A
12-part / 192-view scalar-versus-CUDA check found 192/192 byte-identical RGB frames,
minimum foreground IoU 1.0, maximum RGB MAE 0.0, and maximum absolute depth error 0.0.

`compare_with_engine` splits a generated mechanical part into connected triangle shells,
imports every shell into the real engine, and renders the same pose through
`RenderRequest`/`doc_render_view`. The native probe runs in an exec-created worker process:
five reproducible in-suite crashes placed the fault in OCCT's TBB parallel mesher
(`NCollection_IncAllocator` through `BRepMesh_FaceDiscret`) after CUDA/TBB initialization.
Process isolation keeps that native allocator state out of the Torch-loaded test process;
a worker signal is reported as a mandatory parity failure rather than killing pytest. The
post-change 160 x 120 agreement result was:

```text
part_id:                       mechanical_000006c1_0000
candidate_index:               2
resolution:                    160 x 120
foreground intersection/union: 6648 / 6648 pixels
foreground IoU:                1.0
RGB MAE over foreground union: 0.0 byte levels
```

The corpus stores the reciprocal-z result as normalized depth. The engine RPC does not
expose its internal z buffer, so the engine probe measures RGB pixels and foreground
coverage. The independently measured scalar-versus-CUDA check above covers stored depth.

## Resolution, scale, and storage

The selected 96 x 96 resolution is the exact frozen limited-observation camera size. The
oracle's smallest resolvable feature is 8 pixels, so it remains eight pixels in the stored
image rather than being downsampled below the label's own resolution threshold. Moving to
160 pixels would spend 2.78 times the raw pixel storage and change neither that frozen
threshold nor the comparison protocol.

The completed corpus under `scratch/viewpoint-pixels-large/` contains 1,982,613,133
logical bytes (1.847 GiB); `du -sh` reports 2.2 GiB allocated. The candidate count remains
the frozen N=16 contract. Volume comes only from 450 additional deterministic training
parts, not from multiplying looks or changing validation/test.

| Split | Parts | Query rows | Candidate images |
|---|---:|---:|---:|
| train | 500 | 3,000 | 48,000 |
| validation | 11 | 66 | 1,056 |
| test | 11 | 66 | 1,056 |
| **total** | **522** | **3,132** | **50,112** |

The locked test IDs are present and occur only in `test`:

```text
mechanical_000006c1_0005  mechanical_000006c1_0006
mechanical_000006c1_0022  mechanical_000006c1_0035
mechanical_000006c1_0036  mechanical_000006c1_0041
mechanical_000006c1_0043  mechanical_000006c1_0046
mechanical_000006c1_0058  mechanical_000006c1_0061
mechanical_000006c1_0071
```

The manifest records corpus ID, part ID/index, split, base and candidate seeds, resolution,
`viewpoint-pixel-corpus/1.5.0`, renderer version, part-generator version, oracle version,
and the observed oracle backend. Images, depth, input records, and target arrays are
separate SHA-256-addressed objects. Every row is published atomically. A 600.23-second
wall-clock kill left 1,951 complete rows; the identical command resumed with
`cache_hits: 1951` and completed without losing or regenerating them.

## Frozen comparison and visibility audit

A re-run of the direct three-part alignment check against
`LimitedCorpusConfig(level="depth_only")` reported identical part order, zero
component-label error, and identical fixed/greedy choices. Candidate forward-vector
storage differed only by float32 conversion (`5.960464477539063e-08` maximum absolute
error):

```text
part_ids True
camera_forward_max_abs 5.960464477539063e-08
components_max_abs 0.0
pixel {'seeded_random': [6, 12, 0], 'fixed_isometric': [7, 4, 2], 'greedy_geometric': [4, 7, 4]}
frozen {'fixed_isometric': [7, 4, 2], 'greedy_geometric': [4, 7, 4]}
```

The 132 locked validation/test rows were also compared object-for-object with the original
72-part manifest. Split, part ID, input-object hash, target-object hash, all 11 validation
part IDs, and all 11 test part IDs were identical.

`generate_visibility_audit_corpus()` separately reproduces the frozen pooled information
protocol: part indices 0..7, one target `i % feature_count`, eight cameras seeded
`1729 + 1009*i`, and 160 x 120 images. Those 8 rows / 64 images align with the checked-in
one-look depth-observation visibility correlation `0.583859` (reported as `.584`).

## Reproduction evidence

### Generation profile and throughput

The before column is a profiling-instrumented cold three-row run (14.566 seconds total);
the after column is the final manifest's cumulative stage timers across 3,132 rows. The
cold profile attributes Torch import to oracle labelling, so stage shares are more useful
than comparing its absolute seconds directly to the full-corpus wall clock.

| Stage | Before seconds / share | After seconds | After ms/row | After share |
|---|---:|---:|---:|---:|
| rasterisation | 12.357 / 84.84% | 608.806 | 194.38 | 60.46% |
| oracle labelling | 1.835 / 12.60% | 288.555 | 92.13 | 28.65% |
| coarse prior | 0.098 / 0.67% | 60.512 | 19.32 | 6.01% |
| encoding | 0.012 / 0.08% | 7.990 | 2.55 | 0.79% |
| object IO | 0.011 / 0.08% | 11.345 | 3.62 | 1.13% |
| manifest writes | 0.001 / 0.01% | 15.025 | 4.80 | 1.49% |
| sampling | below profile cutoff | 5.747 | 1.83 | 0.57% |

The original 432-row manifest took 983.9291236869176 generation seconds:
**0.439056 rows/s**. The enlarged manifest took 1007.0024167049123 generation seconds:
**3.110221 rows/s**, a **7.084x** throughput increase. The observed shell wall clock was
600.23 + 426.11 = **1,026.34 seconds (17m 6.34s)** including the killed invocation,
resume startup, final materialisation, and JSON output.

```text
OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m services.viewpoint.pixels \
  --parts 522 --examples-per-part 6 --candidates 16 \
  --width 96 --height 96 --workers 6 --device cuda \
  --cache-dir scratch/viewpoint-pixels-large
```

The manifest reports `oracle_backends: ["cuda"]`, `cache_hits: 1951`, 3,132 rows, and
50,112 images. The scalar/CUDA raster parity command covered 12 parts and 192 views with
foreground IoU 1.0, RGB MAE 0.0, and depth error 0.0; the independent engine command is
reported above.

### Required checks

```text
python3 -X utf8 -m pytest \
  services/viewpoint/pixels/test_pixels.py::test_inference_sample_has_no_oracle_or_exact_occlusion_capability -q
.                                                                        [100%]
1 passed in 0.45s

OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m pytest services/viewpoint -q
.............................................................            [100%]
61 passed in 18.62s
# Two immediate repetitions: 61 passed in 18.35s; 61 passed in 17.53s.

OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  env PYTHONPATH=scratch/no-torch python3 -X utf8 -m pytest services/viewpoint -q
ssss.........................................................            [100%]
57 passed, 4 skipped in 12.02s
```

## Honest remaining uncertainty

The scale confound is removed, but this corpus slice alone does not prove that a learned
pixel encoder generalizes or beats the frozen depth-only `0.300661` regret and `.583859`
one-look visibility correlation. That conclusion requires the separate locked-split
retraining/evaluation to complete without tuning to the test result.
