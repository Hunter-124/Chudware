# Viewpoint rendered-pixel corpus

## Scope

`services.viewpoint.pixels` supplies the camera-limited corpus for the learned viewpoint
encoder. It consumes, but does not modify, the frozen benchmark modules
`geometry.py`, `oracle.py`, `sampling.py`, `parts.py`, and `dataset.py`. The default corpus
is deliberately aligned to the existing limited-observation experiment: seed 1729,
72 parts, six query examples per part, 16 candidates per query, 96 x 96 cameras, and the
same candidate seed tag `0x1A11`. This keeps the observation representation as the changed
variable.

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

Generating thousands of views by repeatedly importing a part and crossing the worker RPC
would spend most of the budget on document/RPC lifecycle. The corpus therefore uses the
in-process renderer `viewpoint-pixel-raster/1.0.0; engine-perspective-v1`. It consumes only
the triangle mesh and `CameraPose`, and mirrors the real `doc_render_view.camera_pose.v1`
implementation: the same perspective basis and focal scale, six frustum clipping planes,
half-pixel edge tests, reciprocal-depth z buffer, clean-shot material, world light, and
background.

This substitution is explicit and measured. `compare_with_engine` split a real generated
mechanical part into connected triangle shells, imported every shell into the real engine,
and rendered the same pose through `RenderRequest`/`doc_render_view`. At the corpus
resolution, the independent images agreed exactly on the sampled view:

```text
part_id:                       mechanical_000006c1_0000
candidate_index:               2
resolution:                    96 x 96
foreground intersection/union: 3608 / 3608 pixels
foreground IoU:                1.0
RGB MAE over foreground union: 0.0 byte levels
```

The corpus also stores the rasterizer's reciprocal-z result as normalized depth. The engine
RPC does not expose its internal z buffer, so the agreement probe measures RGB pixels and
foreground coverage rather than claiming an independently observed engine-depth result.

## Resolution, scale, and storage

The selected 96 x 96 resolution is the exact frozen limited-observation camera size. The
oracle's smallest resolvable feature is 8 pixels, so it remains eight pixels in the stored
image rather than being downsampled below the label's own resolution threshold. Moving to
160 pixels would spend 2.78 times the raw pixel storage and change neither that frozen
threshold nor the comparison protocol.

The completed corpus under `scratch/viewpoint-pixels/` occupies 310 MiB (304 MiB in the
content-addressed object store). The exact locked corpus is intentionally 6,912 images,
not an inflated tens-of-thousands tuning run: 72 parts x 6 queries x 16 candidates. A
64-candidate configuration would produce 27,648 images with the same addressing scheme,
but it would change the frozen N=16 compute contract, so it was not substituted merely to
claim a larger corpus.

| Split | Parts | Query rows | Candidate images |
|---|---:|---:|---:|
| train | 50 | 300 | 4,800 |
| validation | 11 | 66 | 1,056 |
| test | 11 | 66 | 1,056 |
| **total** | **72** | **432** | **6,912** |

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
`viewpoint-pixel-corpus/1.4.0`, renderer version, part-generator version, oracle version,
and the observed oracle backend. Images, depth, input records, and target arrays are
separate SHA-256-addressed objects. A manifest entry is published atomically after every
row. The measured run was interrupted once by a 600-second command deadline and resumed
without regenerating the 259 published rows, directly exercising resumability.

## Frozen comparison and visibility audit

A direct three-part alignment check against `LimitedCorpusConfig(level="depth_only")`
reported identical part order, zero component-label error, and identical random/fixed/
greedy choices. Candidate forward-vector storage differed only by float32 conversion
(`5.960464477539063e-08` maximum absolute error):

```text
part_ids True
camera_forward_max_abs 5.960464477539063e-08
components_max_abs 0.0
pixel {'seeded_random': [6, 12, 0], 'fixed_isometric': [7, 4, 2], 'greedy_geometric': [4, 7, 4]}
frozen {'seeded_random': [6, 12, 0], 'fixed_isometric': [7, 4, 2], 'greedy_geometric': [4, 7, 4]}
```

This makes future pixel-policy results directly comparable to the checked-in depth-only
locked-test whole-part regret `0.300661` and its unchanged candidate labels. It does not
claim that pixels have beaten that number before the encoder training/evaluation slice
runs.

`generate_visibility_audit_corpus()` separately reproduces the frozen pooled information
protocol: part indices 0..7, one target `i % feature_count`, eight cameras seeded
`1729 + 1009*i`, and 160 x 120 images. It produced 8 evaluation-only rows / 64 images on
CUDA in 9.614980 seconds. Those rows align with the checked-in one-look depth-observation
visibility correlation `0.583859` (reported as `.584`); a learned probe can therefore be
measured against the same truth rather than a newly sampled easier set.

## Reproduction evidence

Full CUDA generation (RTX 3080 Ti), with the required resource bounds:

```text
OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m services.viewpoint.pixels \
  --parts 72 --examples-per-part 6 --candidates 16 \
  --width 96 --height 96 --workers 6 --device cuda \
  --cache-dir scratch/viewpoint-pixels
```

The first invocation reached its 600.23-second command deadline; the identical resume
completed in 388.35 seconds. Total observed shell wall clock was **988.58 seconds
(16m 28.58s)**. The manifest's row-generation timer was **983.9291236869176 seconds** and
recorded `oracle_backends: ["cuda"]`, `cache_hits: 259`, 432 rows, and 6,912 images.

Renderer agreement:

```text
OMP_NUM_THREADS=6 MKL_NUM_THREADS=6 nice -n 15 \
  python3 -X utf8 -m services.viewpoint.pixels --agreement

foreground_iou: 1.0
rgb_mae_union: 0.0
matching_foreground_pixels: 3608
union_foreground_pixels: 3608
```

No-leakage test:

```text
python3 -X utf8 -m pytest \
  services/viewpoint/pixels/test_pixels.py::test_inference_sample_has_no_oracle_or_exact_occlusion_capability -q
.                                                                        [100%]
1 passed in 1.11s
```

Acceptance suite with normal optional dependencies:

```text
python3 -X utf8 -m pytest services/viewpoint -q
.............................................................            [100%]
61 passed in 27.75s
```

Acceptance suite with a `torch` import shim that raises `ImportError`:

```text
PYTHONPATH=scratch/no-torch python3 -X utf8 -m pytest services/viewpoint -q
ssss.........................................................            [100%]
57 passed, 4 skipped in 21.92s
```

## Honest remaining uncertainty

What remains unproven is the actual learned-policy gain: one generated-part RGB sample
matches the engine exactly and the corpus is locked/aligned, but only training and
part-grouped evaluation can establish whether learned pixels beat the depth-only
`0.300661` regret or `.583859` one-look visibility-information baseline on held-out parts.
