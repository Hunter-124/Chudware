# Contract: Reverse Engineering — `aicad.reveng_*` — v1

> Version 1 (RE-00, 2026-07-07). Frozen seam for the Reverse Engineering workspace:
> photos / datasheets / scans → honest **scan → mesh → AI → parametric** pipeline with
> mesh-body fallback. Append-only per `contracts/README.md`.
>
> Producers: `engine_oracle/vision/reveng_*.py`, `services/mcp/reveng_tools.py`.
> Consumers: `host/src/ui/reveng/` GUI, DAG mission templates `reveng_*.json`,
> agent roles `re_modeler` / `re_datasheet_reader` / `re_geometry_verifier`.
>
> **Pipeline honesty:** never claim direct scan→parametric. Single-photo tiers
> (`TripoSR` / `TRELLIS.2`) are force-tagged `approximate` — reference/preview only.

---

## 0. Conventions

- **Session root:** `$CHUDWARE_REVENG_DIR/<session_id>/` where `CHUDWARE_REVENG_DIR` defaults to
  `$XDG_DATA_HOME/Chudware/reveng` (Windows: `%LOCALAPPDATA%/Chudware/reveng`).
- **Degradation envelope:** every tool returns `{"ok": false, "reason": <str>, "hint": <str>}`
  on failure — never a traceback, never silent success.
- **Fit-critical dimensions** route through the intake ledger (`confirm_dimensions`) with
  `provenance.method` in `photo_fiducial` | `datasheet` | `fitted` | `guessed` — no silent
  guessing (see `contracts/interaction.md`).
- **Honesty tiers** for photo-derived dims reuse `services/vision/measure.py`:
  `high` (±2%), `medium` (±5%), `low` (±10%, out-of-plane capped). UI shows
  `HONESTY_STRING` from measure.py wherever a photo dimension appears.
- **Long jobs** return `run_id` immediately; progress via `aicad.reveng_events/1` on the
  EventJournal; poll with `mission_status` / GUI `feedRevEngEvent` drain.

### 0.1 Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `CHUDWARE_REVENG_DIR` | no | Override session storage root |
| `OPENMVS_BIN` | for dense CPU mesh | OpenMVS `DensifyPointCloud` etc. (AGPL — subprocess ONLY) |
| `BRUSH_BIN` | for 3DGS + mesh export | Brush trainer binary (Apache, subprocess) |
| `MESHROOM_BIN` | no | Existing Meshroom path (unchanged from photogrammetry.py) |
| `CHUDWARE_TEST_CAMERA` | no | `1` enables Linux webcam ctest smoke |

---

## 1. Session directory layout

```
<CHUDWARE_REVENG_DIR>/<session_id>/
  session.json          # aicad.reveng_session/1 manifest
  images/               # user stills (JPEG/PNG)
  frames/               # video-extracted stills (blur-culled)
  sparse/               # COLMAP sparse model
  dense/                # OpenMVS / dense intermediates
  splat/                # Brush 3DGS .ply
  mesh/                 # scaled/cleaned mesh (.ply / .obj)
  datasheet/            # PDF + docling crops
  exports/              # bundles, scale-mat PDFs
```

---

## 2. `aicad.reveng_session/1`

```json
{
  "schema": "aicad.reveng_session/1",
  "session_id": "re_8f3c2a1b",
  "name": "M8 bracket from photos",
  "created_at": "2026-07-07T15:30:00Z",
  "updated_at": "2026-07-07T16:12:00Z",
  "status": "capturing",
  "part_ref": "catalog:grabcad:bracket-123",
  "stages": {
    "capture": "done",
    "scale": "pending",
    "reconstruct": "idle",
    "model": "idle",
    "review": "idle",
    "deliver": "idle"
  },
  "scale": {
    "method": "aruco_mat",
    "mm_per_unit": 1.0,
    "residual_pct": 0.4,
    "confirmed": false
  },
  "artifacts": {
    "images": 12,
    "sparse_dir": "sparse/0",
    "mesh_path": null,
    "splat_path": null,
    "doc_id": null
  },
  "honesty": {
    "pipeline": "scan_mesh_ai_parametric",
    "single_photo_tier": null,
    "approximate": false
  },
  "provenance": []
}
```

| field | type | required | meaning |
|---|---|---|---|
| `session_id` | string | yes | `re_` + 8 hex chars |
| `name` | string | yes | User label |
| `status` | enum | yes | `capturing` \| `reconstructing` \| `modeling` \| `review` \| `done` \| `error` |
| `part_ref` | string \| null | no | Parts-hub `part_id` when seeded from library |
| `stages` | object | yes | Per-stage badge for GUI rail |
| `scale` | object | yes | Scale solver state; `confirmed` gates reconstruction |
| `artifacts` | object | yes | Paths/counts for viewers |
| `honesty` | object | yes | Pipeline tier tags |
| `provenance` | array | yes | Cumulative provenance chain |

---

## 3. `aicad.reveng_fits/1`

Primitive fits from Open3D segmentation + RANSAC, fed as hints to `reverse_engineer`.

```json
{
  "schema": "aicad.reveng_fits/1",
  "session_id": "re_8f3c2a1b",
  "mesh_path": "mesh/cleaned.ply",
  "fits": [
    {
      "kind": "cylinder",
      "confidence": 0.92,
      "params_mm": {"radius": 4.0, "height": 12.0, "axis": [0, 0, 1], "origin": [0, 0, 0]},
      "source": "ransac",
      "face_ids": ["f12", "f13"]
    },
    {
      "kind": "box",
      "confidence": 0.88,
      "params_mm": {"dx": 40.0, "dy": 30.0, "dz": 10.0, "origin": [0, 0, 0]},
      "source": "ransac"
    }
  ],
  "sections": []
}
```

---

## 4. `aicad.reveng_events/1`

Progress events for GUI stepper + mission orchestration.

```json
{
  "schema": "aicad.reveng_events/1",
  "run_id": "run_a1b2c3d4",
  "session_id": "re_8f3c2a1b",
  "stage": "reconstruct",
  "pct": 45,
  "msg": "Sparse reconstruction: 18/24 images registered",
  "artifact": "sparse/0",
  "ts": "2026-07-07T15:45:00Z"
}
```

| field | type | required |
|---|---|---|
| `run_id` | string | yes |
| `session_id` | string | yes |
| `stage` | string | yes | `capture` \| `scale` \| `sparse` \| `dense` \| `splat` \| `mesh` \| `fit` \| `parametric` \| `review` \| `deliver` |
| `pct` | int 0–100 | yes |
| `msg` | string | yes |
| `artifact` | string \| null | no |
| `ts` | ISO8601 | yes |

---

## 5. Deviation schema (`get_reveng_deviation`)

```json
{
  "ok": true,
  "session_id": "re_8f3c2a1b",
  "max_mm": 0.35,
  "rms_mm": 0.12,
  "buckets": [
    {"range_mm": [0.0, 0.1], "face_count": 120, "color": "#00ff00"},
    {"range_mm": [0.1, 0.5], "face_count": 8, "color": "#ffff00"},
    {"range_mm": [0.5, null], "face_count": 2, "color": "#ff0000"}
  ],
  "mesh_path": "mesh/cleaned.ply",
  "brep_doc_id": "doc_abc123"
}
```

---

## 6. Splat `.ply` subset (Brush export)

Loader accepts Gaussian splat PLY with minimum columns:

| property | type | required |
|---|---|---|
| `x`, `y`, `z` | float | yes |
| `nx`, `ny`, `nz` | float | no (default 0,0,1) |
| `f_dc_0`, `f_dc_1`, `f_dc_2` | float | yes (SH degree-0 color) |
| `opacity` | float | yes |
| `scale_0`, `scale_1`, `scale_2` | float | yes |
| `rot_0` … `rot_3` | float | yes (quaternion w,x,y,z) |

Extra columns ignored. Coordinate system: right-handed, millimetres after scale confirm.

---

## 7. CameraBackend C++ interface

Platform abstraction for webcam capture (`host/src/platform/camera.h`).

```cpp
namespace chudware::platform {

struct CameraInfo {
    std::string device_id;
    std::string label;
};

struct CameraFrame {
    int width = 0;
    int height = 0;
    std::vector<uint8_t> rgba;  // row-major RGBA8
    double timestamp_sec = 0.0;
};

class CameraBackend {
public:
    virtual ~CameraBackend() = default;
    static std::unique_ptr<CameraBackend> create();  // V4L2 / MF / stub

    virtual std::vector<CameraInfo> listDevices() = 0;
    virtual bool open(const std::string& device_id) = 0;
    virtual void close() = 0;
    virtual bool lockExposure(double exposure_us, double gain) = 0;
    virtual bool grabFrame(CameraFrame& out) = 0;  // stills-only; no continuous record
};

}  // namespace chudware::platform
```

---

## 8. MCP tools — family `reveng`

All tools return JSON strings. Read tools use `_dumps`; errors use `_err` per MCP conventions.

### 8.1 READ

#### `list_reveng_sessions() → {ok, sessions: [session_summary]}`

#### `get_reveng_session(session_id: str) → {ok, session: aicad.reveng_session/1}`

#### `get_reveng_coverage(session_id: str) → {ok, coverage: {plan: "11_shot", captured: [int], missing: [int], blur_flags: [bool], quality: str}}`

#### `get_reveng_deviation(session_id: str) → deviation schema §5`

#### `describe_reveng_result(session_id: str) → {ok, result: {doc_id, match_pct, strategy, mesh_fallback: bool, provenance: []}}`

#### `reveng_status() → {ok, capabilities: {sparse, dense, splat, single_photo, webcam, gpu}, provisioning: {colmap, openmvs, brush, triposr, vggt, docling}, banners: [str]}`

### 8.2 WRITE

#### `reveng_start_session(name?: str, part_ref?: str) → {ok, session_id, session}`

#### `reveng_add_images(session_id: str, paths: [str] | dir: str) → {ok, added: int, session}`

#### `reveng_add_video(session_id: str, path: str, max_frames?: int = 120) → {ok, run_id, session}`

#### `reveng_ingest_datasheet(session_id: str, path: str) → {ok, tables: [object], crops: [str], session}`

#### `reveng_set_scale(session_id: str, method: "aruco_mat"|"two_point"|"known_dim", params: object) → {ok, scale, session}`

`params` shapes:
- `aruco_mat`: `{images?: [str], board?: "A4"|"Letter"}`
- `two_point`: `{image: str, p1: [x,y], p2: [x,y], distance_mm: float}`
- `known_dim`: `{feature: str, value_mm: float, source: str}`

#### `reveng_reconstruct(session_id: str, quality: "draft"|"normal"|"high", backend?: str) → {ok, run_id, session}`

#### `reveng_single_photo(session_id: str, tier: "triposr"|"trellis") → {ok, run_id, approximate: true, session}`

#### `reveng_fit_primitives(session_id: str) → {ok, fits: aicad.reveng_fits/1}`

#### `reveng_build_parametric(session_id: str, strategy?: str) → {ok, run_id, session}`

Refuses when `session.honesty.approximate` is true unless `strategy: "override_approximate"`.

#### `reveng_attach_result(session_id: str, part_ref: str) → {ok, part_ref, geometry_ref, session}`

#### `reveng_print_scale_mat(size?: "A4"|"Letter" = "A4") → {ok, path: str}`

### 8.3 EXTERNAL

#### `import_scan_mesh(session_id: str, path: str, scale_mm?: float) → {ok, mesh_path, session}`

#### `export_reveng_bundle(session_id: str, dest: str) → {ok, path: str}`

### 8.4 Reused tools (unchanged)

`start_capture_session` / `stop_capture_session`, `measure_from_photo`, `confirm_dimensions`,
`intake_status`, `fit_report`, `match_part_number`, `spec_sheet_match`, `reverse_engineer`,
`compare_parts`, `convert_face_to_sketch`, `mission_start`, `mission_status`, `part_search`,
`part_place`, browser acquisition tools.

---

## 9. DAG mission templates

| template id | file | path |
|---|---|---|
| `tmpl-reveng-scan` | `reveng_scan.json` | photos → scale gate → reconstruct → fit → parametric gate → deliver |
| `tmpl-reveng-datasheet` | `reveng_datasheet.json` | ingest → MPN fast-path → dim gate → model → deliver |
| `tmpl-reveng-single-photo` | `reveng_single_photo.json` | single-photo tier → review gate → optional mesh export |

Gate nodes use `type: "gate"`, `autonomy: "approve"`, and `inputs.gate_kind` in
`scale_confirm` | `dimension_confirm` | `final_accept`.

---

## 10. Agent routing roles

Added to `services/agent/routing.yaml`:

| role | purpose | vision |
|---|---|---|
| `re_modeler` | Feature code grounded in fitted dims | no |
| `re_datasheet_reader` | VLM reads datasheet crops/tables | yes |
| `re_geometry_verifier` | Deviation + compare_parts loop | no |

---

## 11. Embedded contract examples (for pytest validation)

See `services/mcp/test_reveng_contract.py` — examples MUST validate against §2–§5 shapes.

---

# Contract: Reverse Engineering — v2 (RE-40, 2026-07-24)

> Append-only extension of v1 per `contracts/README.md`. Everything in §0–§11 above still
> holds **except** §7, which v2 supersedes (see §12.1 — the v1 `CameraBackend` no longer
> compiles). Nothing else is renamed or removed; every addition below is additive.
>
> Motive: v1 described an honest pipeline but several seams were declared and never wired.
> v2 records the seams as they are now actually implemented, and corrects two documented
> shapes that did not match the code.

## 12.1 `CameraBackend` v2 — SUPERSEDES §7

`host/src/platform/camera.h`. The v1 interface was stills-only, negotiated no format, and
exposed a single `lockExposure`. It could not serve photogrammetry: SfM needs the sensor's
full resolution, and needs autofocus / auto-exposure / auto-white-balance **locked** so the
intrinsics and colour response stay constant across a capture set.

```cpp
namespace chudware::platform {

struct CameraInfo { std::string device_id, label, bus; };
struct CameraMode { int width, height; double fps; uint32_t fourcc;
                    std::string format_label; bool compressed; };
struct OpenRequest { int width, height; double fps; bool prefer_max_resolution; };

struct CameraFrame {
    int width, height;
    std::vector<uint8_t> rgba;   // row-major RGBA8, tightly packed
    double timestamp_sec;
    bool   synthetic;            // software-generated — MUST NOT be persisted as a capture
    double sharpness;            // variance-of-Laplacian; -1 = unscored
};

enum class GrabResult { ok, again, device_lost, decode_failed, not_open };
enum class CameraControl { Focus, Exposure, WhiteBalance, Gain, Zoom, Brightness };
struct ControlState { bool supported, auto_capable, automatic; double value; /* 0..1 */ };

class CameraBackend {
public:
    static std::unique_ptr<CameraBackend> create();      // V4L2 / MF / stub
    virtual std::vector<CameraInfo> listDevices() = 0;
    virtual std::vector<CameraMode> listModes(const std::string& device_id) = 0;
    virtual bool open(const std::string& device_id, const OpenRequest&) = 0;
    virtual void close() = 0;
    virtual bool isOpen() const = 0;
    virtual CameraMode activeMode() const = 0;
    virtual std::string lastError() const = 0;
    virtual GrabResult grabFrame(CameraFrame& out) = 0;
    virtual ControlState getControl(CameraControl) = 0;
    virtual bool setControl(CameraControl, double normalized_value) = 0;
    virtual bool setControlAuto(CameraControl, bool automatic) = 0;
    virtual bool lockForCapture() = 0;   // freeze focus+exposure+WB+gain
};

double frameSharpness(const CameraFrame&);
inline constexpr double kSharpnessBlurThreshold = 100.0;
}
```

Binding rules:

- **`GrabResult::again` is the steady state**, not an error. It means the driver has no new
  buffer queued. Callers MUST NOT reopen the device on `again`; reopen only on `device_lost`.
- **`synthetic` is a hard gate.** A frame with `synthetic == true` MUST NOT be written into a
  session, and MUST be visibly marked in any preview. Fail closed: treat a frame as synthetic
  until a backend returns `ok` with `synthetic == false`.
- `frameSharpness` is BT.601 luma on the 0–255 scale through the 3×3 Laplacian
  `[0 1 0; 1 -4 1; 0 1 0]`, population variance — the same quantity as
  `services/vision/frames.py:blur_score()`, so `kSharpnessBlurThreshold` (100.0) and
  `frames.DEFAULT_BLUR_THRESHOLD` (100.0) mean the same thing on both sides of the seam.
- Backend status: `camera_v4l2.cpp` is real (mmap streaming — `REQBUFS`/`QBUF`/`DQBUF`/
  `STREAMON` — with YUYV/UYVY/YVYU/NV12/NV21/I420/YV12/GREY/RGB24/BGR24/MJPG decode).
  `camera_mf.cpp` is real (Media Foundation source reader, `IAMCameraControl` +
  `IAMVideoProcAmp`). `camera_stub.cpp` is the fallback, always sets `synthetic = true`, and
  owns both the cross-platform `create()` factory and the `frameSharpness` definition.
- MJPEG frames from UVC cameras that omit Huffman tables get a standard Annex-K DHT spliced in
  before decode. This is load-bearing, not defensive: stb_image neither supplies default tables
  nor rejects a DHT-less JPEG — it returns success with ~75% of the pixels wrong.

## 12.2 `aicad.reveng_fits/1` additions — `fits_revision: 2`

The schema string stays `aicad.reveng_fits/1`; the payload declares `fits_revision: 2`.
Every v1 key is preserved. Geometry stays inside `params_mm`; semantics are entry-level.

| addition | where | meaning |
|---|---|---|
| `fits_revision: 2` | top level | presence of the keys below |
| `notes: [{stage, reason, hint}]` | top level | per-stage degradation (missing optional dep, unorientable normals) — never swallowed, never fatal |
| `kind: "sphere"` | fit | `params_mm{center[3], radius, origin[3]}` |
| `kind: "cone"` | fit | `params_mm{apex[3], axis[3], half_angle_rad, radius_at_base, height, origin[3]}` |
| `role` | cylinder fit | `hole` \| `boss` \| `shaft` \| `unknown` — from radial normal orientation; `unknown` when normals are absent or ambiguous, never guessed |
| `through` | cylinder fit | bool — does the bore span the local wall |
| `depth_mm` | cylinder fit | axial run; for a through bore, the full local wall thickness |
| `params_mm.base[3]` | cylinder fit | axis point at the low-`t` end of the patch (`origin` keeps its v1 meaning), so hole placement is expressible |
| `role` | plane fit | `pocket` \| `pad` \| `unknown` |
| `sections` | top level, sibling of `fits` | `[{station_mm, axis[3], polylines: [[[x,y],…]], area_mm2, closed, frame:{origin,e1,e2}}]` |
| `residual_mm` | any fit | RMS deviation of the inliers from the fitted primitive, in mm |
| `inliers` | any fit | absolute count of points supporting the fit |

`sections` replaces the v1 hardcoded `[]`. The slice plane is `{p : p·axis == station_mm}`;
lift a 2-D point back to world as `origin + x·e1 + y·e2`. Areas are shoelace sums signed by
nesting parity, so an internal bore subtracts from its enclosing loop.

Multiple cylinders are now returned (iterative RANSAC peel), where v1 fitted exactly one.

### `confidence` is a mesh SHARE, not a fit QUALITY

§3's example shows `confidence: 0.92` for a lone cylinder, which reads like a quality score. It is
not: `segment_fit` computes `confidence = inliers / total_points`, i.e. the fraction of the sampled
cloud a fit owns. It is therefore **scale-dependent** — a small bore in a large bracket legitimately
scores 0.03–0.31 — and **MUST NOT be compared against a quality threshold**. Doing exactly that
(`_HINT_MIN_CONFIDENCE = 0.5`) silently discarded every real fit hint before it reached
`reverse_engineer`, so the mesh→CAD hint path looked wired and did nothing on real data.

Consumers gate on `residual_mm` (how well the primitive actually fits) and `inliers` (whether
enough evidence supports it) instead.

### Roles are reported, never guessed

`role` is `unknown` whenever normals are absent or the orientation test is ambiguous. A consumer
MUST NOT bucket `unknown` into either additive or subtractive geometry — turning an unclassified
cylinder into a boss is how a recognised bore becomes a protrusion, which is worse than omitting
it. Sphere and cone entries carry **no** `role` at all (there is no dome-vs-socket or
countersink-vs-taper signal), so per §0 they must be surfaced to the user rather than applied.

## 12.3 Scale — `reveng_set_scale` now computes

v1 documented three `method` values but the implementation ignored `method` for all three and
simply recorded the caller's number, so "automatic ArUco scale" and "type a number in" were the
same code path. v2:

| method | behaviour |
|---|---|
| `aruco_mat` | runs `services/vision/scale_solver.py:solve_scale_aruco()` — detects the marker across registered COLMAP views, triangulates its corners, derives `mm_per_unit` with a real `residual_pct` (corner-edge coefficient of variation). Marker spec defaults match what `reveng_print_scale_mat` prints. |
| `two_point` | runs `solve_scale_two_point()` against the posed model. Refuses rather than silently picking view 0 when >1 image is registered and none is selected. |
| `known_dim` | genuinely caller-supplied. `residual_pct` is `null`; `params.source` ∈ `caliper` \| `datasheet` \| `photo_fiducial` \| `guessed`. An absent source defaults to `guessed` **and** returns a `warnings[]` entry — §0 forbids silent guessing on fit-critical dimensions. |

New failure reason: **`scale_needs_sparse`** — `aruco_mat` and `two_point` both require a
registered sparse model. Clients MUST route the user to Reconstruct (or to `known_dim`) rather
than rendering it as a generic error.

All three write `session.scale = {method, mm_per_unit, residual_pct, detail, confirmed: false}`
and append a provenance record. `detail` is a new solver passthrough.

## 12.4 Mesh scale units — a corrected seam

`session.scale.mm_per_unit` is, and always was, a **ratio**: reconstruction units × it = mm.
`services/vision/mesh_cleanup.py:cleanup_mesh()` took `scale_mm`, which it implemented as
"stretch the mesh's longest bbox dimension to equal this many mm" — a different quantity. Both
call sites fed it the wrong one (a ratio in one, a fiducial marker's edge length in the other),
so every mesh through the real pipeline was mis-scaled.

`scale_mm` is **removed** (clean cutover, no alias). Replaced by two mutually-exclusive keyword
parameters — supplying both is an error:

- `mm_per_unit` — `factor = mm_per_unit`.
- `target_longest_mm` — `factor = target_longest_mm / max(bbox_extent)` (the v1 behaviour, now
  named for what it does).

Result gains `scale_mode: "mm_per_unit" | "target_longest_mm" | null`. When neither is supplied
the mesh is left in reconstruction units and the session records
`artifacts.mesh_units: "reconstruction"` (vs `"mm"`), which also lands in provenance. **A mesh
tagged `reconstruction` is not dimensionally usable** — clients MUST say so.

## 12.5 `get_reveng_coverage` — measured vs estimated

v1 returned a count proxy with `blur_flags` hardcoded to all-`false`. v2 adds:

| key | meaning |
|---|---|
| `method` | `"pose_binned"` when camera poses were available and each registered view was binned into the nearest of the 11 shot-plan slots; `"count_proxy"` when only an image count was available |
| `images` | `[{path, blur_score, blurred, index}]` — real variance-of-Laplacian scores, threshold 100.0 |
| `mean_bin_error_deg`, per-image `bin_error_deg` | how well the plan fitted the observed views (`pose_binned` only) |
| `notes` | why a pose bin was not possible (e.g. `.bin`-only COLMAP model, <2 registered views) |

`blur_flags[i]` is true only where a still was actually measured below threshold; an unscored
image is never reported as sharp. **Clients MUST surface `method`** — an estimated ring must not
read as a measurement. `quality` is now derived from slot coverage and sharp fraction.

A COLMAP world frame has arbitrary gauge, so slot binning first recovers the orbit centre, then
fits the plan's rotation to the observed view directions before assigning slots.

## 12.6 New tools — additions to §8

These shipped without being recorded in §8. They are real, registered and user-reachable.

#### `list_reveng_dims(session_id) → {ok, session_id, dims: [dim_row], sources: [str], scale_confirmed: bool, notes?: [str], hint?}`

The Review stage's dimension ledger. `dim_row`:
`{id, key, label, value_mm, tolerance_mm, source, provenance_method, honesty_tier, confirmed, fit_critical, required, note?, fit_summary?, image_index?, image_path?, photo_value_mm?}`

- **`key`, not `id`, is the `confirm_dimensions` ledger key** to round-trip. `id` is positional.
  Keys are lane-prefixed so they cannot collide: `datasheet.<label>[_N]`, `<feature>.<dim>_mm`,
  `fit.<face_id>.diameter|length`, `fit.<a>_<b>.thickness`.
- Rows are collected from datasheet candidates (`high`), web-scraped prose (`low`),
  photo-fiducial measurements (whatever `measure.py` scored), user-typed confirmations (`high`,
  superseding the photo read while preserving it as `photo_value_mm`), and primitive fits
  (`medium` on a scaled mesh; `approximate` with `tolerance_mm: null` on an unscaled or
  single-photo session).
- A session with no dimensions returns `{ok: true, dims: []}` plus a `hint`. **Rows are never
  fabricated.**

#### `reveng_make_part_from_library(part_ref, part_number?, query?, auto_confirm_non_critical=true) → {ok, …}`

Seed → research → run the `reveng_datasheet` mission → attach.

#### `reveng_research_source(session_id, query?, part_number?, url?) → {ok, …}`

Pull a datasheet/spec bundle into the session from the web.

## 12.7 `describe_reveng_result` — `match_pct` is now written

v1 always returned `match_pct: null` and `strategy: null` because no code ever wrote them.
`reveng_build_parametric` now persists the strategy it used and the achieved `match_pct` into
the session manifest and the `reveng_result.json` sidecar; `describe_reveng_result` reads them
back and adds `scored: bool`.

Clients MUST distinguish three states: **not built yet**, **built but unscored**
(`scored: false`), and **built and scored**. A missing score is not a passing score.

## 12.8 Transport — one tool surface, two transports

`services/reveng/worker_rpc.py` was a hand-maintained 8-method subset **reimplementation**, so 13
of the tools the desktop GUI called dead-ended at `"unknown method: X"` and most of the Reverse
Engineering tab did nothing. It is now a thin adapter over the reveng MCP tool registry:
`REVENG_WORKER_METHODS` is generated from a declared tool-name tuple, and every handler bridges
to the same `@mcp.tool()` implementation the agent calls.

Consequences that are now contract, not accident:

- **Adding a reveng MCP tool exposes it to BOTH transports** via a one-line addition. There is no
  second implementation to keep in sync.
- `start_capture_session` / `stop_capture_session` share ONE session registry across the agent
  and GUI transports (previously two process-local dicts that could not see each other). The
  differing default is a pinned argument — the GUI pins `run_reconstruction=false`; the MCP tool
  keeps its historical `true` for agents.

## 12.9 Stage-event coverage — now complete

§4's `stage` enum declared `dense`, `splat` and `mesh`, but the wired stage map emitted only
`capture|scale|sparse|fit|parametric|review|deliver` — all of the dense/splat/mesh work happened
silently inside one `sparse`-stage node. The scan template's reconstruction node has since been
split (§12.10), and all three stages are now genuinely emitted.

Two rules consumers may rely on:

- **`stage` carries a BARE enum token.** Prose belongs in `msg`. A GUI sub-step track that
  substring-matches the stage field would otherwise bind the wrong step.
- **A node spanning several stages opens on the first and closes on the last**, so the observed
  sequence is monotone and never rewinds: `capture, sparse, scale, dense, mesh, fit, parametric,
  review, deliver`.

## 12.10 Scan mission — reconstruction split, and the ordering deadlock it fixes

`services/dag/templates/reveng_scan.json` ran `n_capture -> n_scale_gate -> n_reconstruct`. Once
`reveng_set_scale` began really solving (§12.3), that order became a **deadlock**: `aruco_mat` and
`two_point` both need a registered sparse model, which only `n_reconstruct` produces — downstream
of the gate that required it. The primary ArUco path could never be satisfied where it was asked.

`n_reconstruct` is split into `n_sparse` (SfM, plus the feed-forward fallback below 60%
registration) and `n_dense` (OpenMVS / Brush + mesh cleanup), with the gate between them:

```
n_capture -> n_sparse -> n_scale_gate -> n_dense -> n_fit -> n_parametric
          -> n_dim_gate -> n_verify_deviation <-> n_iterate -> n_deliver
n_sparse -> n_capture   (retry_budget 1 — too few views registered: go shoot more)
```

This is also the correct photogrammetry order: scale is solved from the sparse model and confirmed
by a human BEFORE the expensive dense pass, and `n_dense` then applies the confirmed
`mm_per_unit` so the delivered mesh is genuinely in millimetres.

- The ArUco solve runs at the TAIL of `n_sparse`, not inside the gate node: gates run their
  approval BEFORE the node body, so solving inside the gate would ask the human to approve a
  blank. It never overwrites an existing scale, and a solve failure is reported, not fatal.
- With no scale solved, the gate records `confirmed: false` and the mission continues UNSCALED
  (`artifacts.mesh_units: "reconstruction"`). It never confirms a number that does not exist.
- `n_reconstruct` is retained as an alias that composes both halves, so existing callers keep
  working.

## 12.11 `session.run` — async missions

§0 promised long jobs return `run_id` immediately; `reveng_reconstruct` did not — it invoked the
executor inline and returned only once the mission finished or hit its first gate. That is now
true rather than aspirational.

`reveng_reconstruct` returns `{ok, run_id, status: "running", async: true, session_id, template,
strategy}` immediately and runs the mission on a background thread. The `run_id`'s EventJournal is
created before the thread is spawned, so the advertised `run_id` always addresses a real journal.
`reveng_build_parametric`'s no-mesh fallback is async on the same terms.

New optional session key — the documented progress source of truth:

```json
"run": {
  "run_id": "…", "template": "reveng_scan",
  "status": "running" | "paused" | "done" | "failed" | "rejected",
  "gate": {"node_id", "node_name", "gate_kind", "autonomy", "prompt", "proposal"} | null,
  "started_at": "…", "updated_at": "…", "error": "…"
}
```

Stage names in §2 are unchanged: `n_sparse` sets `stages.reconstruct = "running"`, `n_dense` sets
it `"done"`, `n_scale_gate` sets `stages.scale = "done"`.

New refusal: **`mission_already_running`** — a second mission on a session that already has one in
flight is refused with the live `run_id`.

## 12.12 Gate tools — additions to §8.2

Approve-gates paused the mission and **nothing in the product could resume them**, so a
GUI-initiated reconstruction stopped after capture forever.

#### `reveng_gate_status(session_id) → {ok, run_id, session_id, template, status, started_at, updated_at, live, gate}`

`gate` is `{node_id, node_name, gate_kind, autonomy, prompt, proposal, pending}`. `pending` is
gate-kind specific:
- `scale_confirm`: `{mm_per_unit, residual_pct, method, confirmed, detail, mesh_units_if_approved, hint}`
- `dimension_confirm`: `{dims, n_outstanding, scale_confirmed, hint}` — the outstanding fit-critical rows

#### `reveng_resolve_gate(session_id, decision) → {ok, …}`

`decision` is REQUIRED and must be exactly `"approve"` or `"reject"`; anything else is an honest
invalid-decision envelope. **There is deliberately no default** — a scale gate defaulting to
approve would destroy the only guarantee this pipeline makes about its millimetres. `approve`
resumes in the background; `reject` leaves the checkpoint suspended (stopped but resumable).

## 12.13 Verify-node predicates

`services/dag/verify.py` `eval()`s a verify node's `end_condition` as a Python predicate.
`n_verify_deviation` carried the prose *"Deviation within tolerance or user accepts fallback mesh
body."*, which raised `SyntaxError` -> `predicate_error` -> ESCALATE on **every** run, so the scan
mission could never pass verification even on a perfect scan. It now carries a real predicate
(`result.get('max_mm', 9e9) <= result.get('tol_mm', 0.5)`) with the human sentence preserved as
`inputs.intent`.

> Repo-wide: every other DAG template still has prose in a verify node's `end_condition` and is
> escalating the same way. Out of scope here — tracked separately.
