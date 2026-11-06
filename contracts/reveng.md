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
| `OPENMVS_BIN` | no | Directory of OpenMVS tools (`DensifyPointCloud` etc.); default is `PATH`, then the managed install fetched by `reveng_install_tools` / the app (AGPL — subprocess ONLY) |
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

#### `reveng_status() → {ok, capabilities: {sparse, dense, splat, single_photo, webcam, gpu}, provisioning: {colmap, openmvs, brush, triposr, vggt, docling}, banners: [str], install: {running, tool, message, done, total, installed: [str], errors: {tool: str}}}`

#### `reveng_install_tools(tools?: ["brush", "openmvs"]) → {ok, install}`

Starts a background download of the missing SHA-256-pinned Brush / OpenMVS releases into the
managed tools dir (about 210 MB, Linux x86_64; other platforms return `unsupported_platform`
and use `BRUSH_BIN` / `OPENMVS_BIN`). Idempotent while running and a no-op when nothing is
missing; progress is `reveng_status().install`. The GUI fetches the tools automatically the
first time it sees them missing (once per run) and offers Install / Retry in the Session panel;
`scripts/provision_reveng.py` is only a developer CLI over `services/vision/reveng_install.py`.

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

---

## 13. Native Gaussian reconstruction — environment and stationary object capture

This extension supersedes the Brush-as-mesh claims in §12. Gaussian reconstruction
and measured CAD reconstruction are separate workflows. Brush produces a Gaussian
PLY reference, **not a mesh or dimensionally verified CAD model**. The measured CAD
mission uses OpenMVS and retains its sparse → scale confirmation → dense →
dimension-review gates. Gaussian capture does not require or silently approve those
gates; its units remain `"reconstruction"`.

### 13.1 Local backend and inputs

`services/vision/gaussian_pipeline.py` runs real COLMAP registration and image
undistortion followed by headless Brush training. The maintained adapter is
`services/vision/dense_backends.py`; it invokes Brush's positional dataset CLI and
omits `--with-viewer`, so no external application window is required.

Brush v0.3.0 is Apache-2.0, pinned to source commit
`3edecbb2fe79d3e2c87eeab85b15e0b1dd10d486`. The Linux x86-64 release archive is
SHA-256 `4f0f9a8785d1951c62df26aae247c02c5bba32b00f40b06df4e1c9b867399e20`.
`scripts/provision_reveng.py` verifies and installs it; `--source-checkout PATH`
checks out that exact source for local development. `BRUSH_BIN` selects a compatible
locally modified build. Upstream: <https://github.com/ArthurBrussee/brush>.

Photo ingest validates, copies and content-addresses accepted images within the
session. Video ingest publishes bounded, decoded keyframes through the same
canonical image store, reports truncation, and does not manufacture a mission
`run_id`. Source-file removal does not invalidate already ingested frames.
The native camera rejects synthetic, stale, repeated and blurred frames before
ingest; continuous capture has one ingest request in flight and a session/generation
binding, so changing rail selection cannot redirect frames into another session.

### 13.2 Gaussian tools

| Tool | Arguments and effect |
|---|---|
| `reveng_start_splat` | `session_id`, `mode="environment"`, `live=false`, `video_path=""`, `quality="normal"`, `iterations=2000`, `crop_bounds=null`. Starts an owned asynchronous reconstruction. `mode` is `environment` or `object`. |
| `reveng_splat_status` | `session_id`. Returns current persisted state and the last validated artifact. |
| `reveng_finish_splat` | `session_id`. Stops waiting for new views and requests a final pass over accepted frames. |
| `reveng_cancel_splat` | `session_id`. Cancels the verified owned supervisor and trainer descendants; retains the last valid preview. |
| `reveng_crop_splat` | `session_id`, `bounds=null`. Applies six reconstruction-coordinate bounds to the source Gaussian cloud; null restores the original artifact. |

`object` means a stationary subject observed from multiple viewpoints plus explicit
3D cropping. It does not promise automatic segmentation, a rotating-background
turntable solution, moving-object reconstruction or 4D splatting.

The public state includes `ok`, `session_id`, `run_id`, `status`, `stage`, `msg`,
`mode`, `live`, `quality`, `frames`, `registered`, `iteration`, `total_iterations`,
`pct`, `finish_requested`, `splat_path`, `source_splat_path`, `artifact_revision`,
`bounds`, `crop_bounds`, `view_camera`, `units`, `backend`, and structured failure
details (`error`, `reason`, `hint`). Not every field is populated before the first
valid export. Process identities and ownership tokens are private.

### 13.3 Progressive capture, publication and recovery

Live capture begins reconstruction with at least six accepted images. At least
three new images trigger another frozen snapshot; preview passes use at most
250 iterations. Capture continues while these snapshots register and train.
Finalize uses the requested full iteration budget on the accepted capture set.
This is progressive batch reconstruction, **not video-rate SLAM, continuous pose
tracking or resumed optimizer state**.

States are `waiting`, `sparse`, `training`, `watching`, `finalizing`, `done`,
`cancelled`, `failed`, or `interrupted`. A non-live final pass may remain
`finalizing` while `stage` reports sparse registration or training. Missing
dependencies, insufficient registration and invalid outputs are explicit failures,
not fixture/model fallbacks.

`services/reveng/gaussian_jobs.py` owns atomic `<session>/splat_job.json` state,
immutable batch exports, a cross-process GPU reservation, and verified Linux
process groups. PID, start identity and boot identity prevent cancellation from
signalling reused/unrelated processes. Worker shutdown terminates and reaps owned
descendants without reacquiring a lock from its signal handler. CAD missions and
Gaussian jobs cannot concurrently mutate the same session.

Each published PLY is validated before advancing `artifact_revision`. Training
and loading share a **2,000,000-Gaussian limit**; the native loader also caps files
at 2 GiB. Cancel, interrupted/restarted work and failed later batches retain the
last complete artifact rather than exposing partially written files. Session
status projects the Gaussian sidecar without rewriting capture-owned metadata.
The rail's optional `gaussian` summary is independent of measured-CAD stage status.

### 13.4 Native viewing and calibrated cameras

The native renderer reads ASCII or little-endian binary PLY, log scales, wxyz
quaternions, opacity logits and degree 0–3 spherical harmonics. It projects full
anisotropic covariance, depth-sorts, and alpha-composites in the embedded workspace.
It matches Brush's 15%-expanded frustum clamp in the projection Jacobian; offscreen
depth-elongated Gaussians must not smear across the image.

Each immutable SfM batch can publish a calibrated camera:

```json
{
  "id": "immutable batch identity",
  "eye": [0, 0, 0],
  "forward": [0, 0, 1],
  "up": [0, -1, 0],
  "focus_distance": 1,
  "intrinsics": [500, 500, 320, 240, 640, 480]
}
```

These numbers illustrate the shape only. Production values come from a supported
registered, undistorted capture camera and its visible positive-depth points.
Camera metadata is published atomically with its matching validated PLY; an early
or failed new batch cannot move the old cloud into a different SfM coordinate frame.
Asynchronous UI loads are fenced by session, artifact and revision.

The first view uses that capture calibration. Orbit, pan and wheel zoom remain
interactive; **Capture view** restores it and **Frame splat bounds** explicitly
fits the entire cloud. Wheel ownership prevents parent-panel scrolling. The GL
pass keeps projection on the full preview rectangle and intersects its scissor
with the visible ImGui clip rectangle. New-session and rail-refresh selection use
session identity, never an unstable list index.

### 13.5 Runtime provisioning

Use the application's Python environment with the pinned `pycolmap==3.13.0`
dependency, video decoding support, the provisioned Brush executable and a working
Vulkan GPU driver. For a development checkout:

```bash
.venv-mcp/bin/python scripts/provision_reveng.py
CHUDWARE_PYTHON="$PWD/.venv-mcp/bin/python" ./host/build-linux/bin/chudware
```

The compiled service dispatcher accepts `services.reveng.gaussian_jobs` and
`services.vision.gaussian_pipeline` child commands; Linux packaging includes
pycolmap. Process supervision is Linux-only. A source-dispatch smoke is not evidence
that a Nuitka/AppImage release artifact has been built or tested.

## 14. Attached model authority — v3 amendment (2026-10-30)

`artifacts.doc_id` is a live document handle, never a file path. When an RE result
has a parametric document, `reveng_attach_result` and the library orchestrator
save a new session-owned `.step+` snapshot with `preserve_path: true`. It retains
the editable feature history and native build-state cache without rebinding the
live document, clearing its dirty state, or overwriting its source. The result
sidecar records `doc_id` as provenance, `native_path` as the saved native
artifact, and an explicit `geometry_ref` and `geometry_source` for placement.

An incomplete or failed native rebuild is refused with its diagnostics before a
new attachment is published. It is never replaced by a scan mesh as an implicit
fallback; an earlier published attachment is left unchanged. When no parametric
document exists, a generated STEP or a mesh-only result can be attached and sent
to the native importer, which must explicitly reject unusable geometry. Legacy
mesh-only sidecars remain usable when their paths meet the current placement
boundary; a legacy live `doc_id` is not interpreted as a filename or silently
replaced by its accompanying mesh.

Cell session metadata and the native snapshot remain in actor-owned state.
Placement references are workspace-relative: a separately and exclusively
created workspace artifact carries the selected source without overwriting an
existing user file. The result adapter only enumerates the requesting actor's
session state; unusable absolute or escaping cell references are not advertised
as models.

## 15. Shared video, splat and metric reconstruction — v4 amendment (2026-10-30)

`reveng_add_video(session_id, path, max_frames=120)` extracts frames only. It returns
an asynchronous `run_id`; the run's session status and `aicad.reveng_events/1`
capture events report progress and errors. `wait=true` runs extraction inline for a
short clip. Video and stills form one submitted image set. The session persists the
shared undistorted `sfm_dataset` (images, COLMAP model, source-image mapping and
input signature); Brush and OpenMVS consume this dataset without an independent
solve. Changed input invalidates the previous solve's arbitrary gauge and scale.
COLMAP binary and text models are both supported.
The extraction manifest `frames/clip_<sha256>_manifest.json` records each selected
frame's path, source path and clip ID, frame index, relative presentation time in
seconds, sharpness and clockwise orientation. Selection is sharpness-aware within
temporal bins and bounded in sample count and memory. User-installed `ffmpeg` and
`ffprobe` on PATH are optional subprocesses, not bundled dependencies.

`recon_transform` is a persisted similarity, with `mm_per_unit`, proper 3×3
row-major `rotation`, `translation_mm`, `source`, nullable `residual_mm`, and
`confirmed`. It maps each native point `x` to
`mm_per_unit * rotation @ x + translation_mm`. Raw gauge artifacts remain
distinguishable from transformed artifacts through their paths, units and
`*_transform_applied` flags. A camera's position uses this same transform; its
directions use its rotation and its focus distance uses its scale. A known
dimension requires its real `value_mm` and either a positive `recon_length` or
two distinct 3D points in the native gauge. Its scale is
`mm_per_unit = value_mm / recon_length`, never the physical dimension alone.
`import_scan_mesh(..., scale_mm=...)` treats `scale_mm` as millimetres per source
unit and records this transform; fitting applies it once. A dimension-critical
fit, CAD build or millimetre deviation requires a confirmed scale.
Two-point calibration on a shared dataset requires `image_space="undistorted"`;
the supplied pixels must come from its undistorted image, not the distorted source.
The source-image mapping identifies the paired registered model image.
When there is no scan mission gate, `reveng_confirm_scale(session_id)` records a
person's explicit approval of a solved, internally consistent scale. Active or
paused missions must use their own `reveng_resolve_gate` instead.

`reveng_import_splat` accepts Gaussian PLY, `.splat`, `.spz` and `.ksplat` and
reports source format, count and conversion losses. `reveng_convert_splat` exports
supported Gaussian encodings and reports losses. Imported clouds have their own
gauge and cannot inherit unrelated session cameras or scale. `reveng_splat_to_mesh`
extracts a surface from Gaussian centers and explicitly reports `approximate=true`;
it is not an exact Gaussian isosurface or a metrology guarantee. OpenMVS is an
optional subprocess (provisioned by `scripts/provision_reveng.py`); when it is unavailable, this approximate
extraction can serve as the measurement-surface fallback. Brush exports visual
Gaussians, not a mesh.

`reveng_sparse_preview` returns bounded points, index-aligned `points_native`,
camera frusta and an optional binary STL preview. Fitting revision 3 adds the
unit/approximation and applied-transform state, and per-fit inlier RMS, standard
error, calibration residual and explicit uncertainty caveats; inlier RMS is not a
complete dimensional uncertainty. Explicit `alignment="icp"` reports a rigid
surface alignment for deviation; world-frame boolean comparisons remain unchanged.
Critical dimensions must be independently measured and confirmed.

## 16. Browser artifact transport — v5 amendment (2026-10-31)

Cell worker responses may include `artifact_refs` keyed by `splat`, `gaussian_splat`,
`mesh`, `mesh_preview`, or `sparse_points`; each reference contains an opaque `id`,
byte `size`, and `kind`. References are scoped to the authenticated workspace and
actor and resolve only to owned session artifacts. The gateway streams the pinned,
no-follow file at `/reveng/artifact/{id}`, rejects changed, unsafe or larger than
2 GiB artifacts, and does not accept a filesystem path from the browser. Existing
native paths remain available to desktop callers. A browser PLY open requests
`reveng_import_splat` with `classify_ply` to inspect the server-side header before
dispatching Gaussian versus mesh/point input. The worker bridge also accepts
`session_id` instead of `path` when converting an owned session, keeping its handle
distinct from a workspace path at the cell admission boundary.
Cell bundle exports likewise confine `dest` to the workspace before creating
directories or writing the archive; desktop destinations retain their native behavior.

The browser asynchronously downloads Gaussian and binary-STL preview artifacts and
parses them in memory; sparse preview points and camera frusta remain in the worker
response. Cell responses sample at most 10,000 index-aligned sparse preview points
to fit the WebSocket frame limit and report that sampling as a warning; the complete
sparse model remains in the session. GPU devices are not exposed to browser cells,
so Gaussian training is disabled there. Import, conversion and viewing of existing
splats remain available.

## 17. Rebuild integrity, alignment and bounded evidence — v6 amendment (2026-10-31)

This amendment supersedes the duplicated `points_native` and response-sampling
details in §§15–16. `reveng_sparse_preview` returns at most 5,000 RGB `points`,
`total_points`, `sparse_points_path`, and bounded camera frusta. All returned points
and frusta use the same frame: reconstruction units while scale is unconfirmed,
millimetres after confirmation. A confirmed `recon_transform` is returned so a
client can recover a native point as `rotation.T @ (point_mm - translation_mm) /
mm_per_unit`. The full sparse model remains the authoritative artifact.

`reveng_align_axes(session_id, method="planes", rotation=null)` writes an absolute
proper native-to-world rotation to the persisted similarity transform without
changing its scale or confirmation. `planes` uses dominant orthogonal fitted planes
or mesh face normals and reports a PCA fallback; `pca` uses mesh points; `manual`
requires a proper 3×3 rotation. The returned method, source and fallback reason
describe the actual calculation. Estimated axes are not verified part datums.
Mesh replacement, recalibration and alignment detach prior fits, document and
result state and reset model/review/deliver stages without deleting previously
published attachment snapshots. Fits carry the measurement mesh SHA-256, native
mesh SHA-256 and full similarity transform; consumers must reject missing or
mismatched provenance, including legacy unattached `fits.json` files.

Pose-binned coverage accepts a slot only when a registered view's nearest-slot
angular error is at most 25 degrees. It exposes each slot's nearest registered-view
error, including missing slots. `count_proxy` remains an explicitly estimated
image-count coverage when usable poses are unavailable.

`reverse_engineer` and deterministic `reveng_build_parametric` return
`ok:false`, `reason:"no_features_rebuilt"` for zero applied features or a bodyless
result. No such result is built or scored, and no synthetic zero match is reported.
`mesh_fallback` is true only with an actual mesh body. `recognition.unclaimed`
contains at most 100 examples; `unclaimed_count` and `unclaimed_path` identify the
complete actor-owned JSON artifact.

`reveng_splat_to_mesh` defaults to `method="auto"`: it uses approximate Gaussian
depth TSDF only with a proven same-gauge undistorted COLMAP model, and otherwise
reports a Poisson fallback and `fallback_reason`. An imported splat or standalone
file never inherits unrelated poses. Explicit `poisson` and `ball_pivoting` remain
available; explicit TSDF with usable poses reports fusion failures rather than
concealing them. Neither TSDF nor center-based extraction is a metrology guarantee:
independently measure critical features and inspect missing or invented surfaces.

`reveng_splat_to_mesh` prunes the Gaussian scene to its subject before any surface
extraction (`services/formats/splat_prune.py`): adaptive opacity cut (`min_opacity`
omitted; a fixed 0.5 keeps only 1-8% of a Brush splat), the sphere the registered
cameras orbit (percentile bounds without poses), oversized and needle-like splats, the
dominant floor plane (`remove_floor`; flat splats and floor-colored splats inside the
band, so a subject lying on the floor survives) and k-NN floaters plus small islands.
The result adds `prune` (per-stage counts), `points_path` (binary PLY of colored
centers) and `pruned_splat_path` (Gaussian PLY) beside the mesh, in reconstruction units.

Imported scans are stored as immutable session artifacts so a later same-named
import cannot overwrite an earlier published attachment. In an exported bundle,
the latest imported scan uses `mesh/<original filename>`; the archive's
`session.json` identifies its current mesh artifacts using archive-relative
paths. If an older session file already occupies that stable name, the bundle
retains it under a distinct archive-history path.

## 18. Video culling, per-clip cameras, snapshot retention and the ChArUco scale mat — v7 amendment (2026-11-04)

This amendment refines §15's sharpness-aware selection. `reveng_add_video` and the
Gaussian video path no longer apply the absolute 100.0 variance-of-Laplacian floor to
video. That score scales with resolution, compression and scene texture; a 1080p HEVC
phone orbit of a matte object scores a median near 15 and lost 593 of 600 sampled
frames to the fixed floor. By default `services/vision/frames.py:extract_frames()`
keeps the sharpest frame in each temporal bin and drops a bin only when that frame is
below `RELATIVE_BLUR_FRACTION` (0.5) of the median score of every frame sampled from the
same clip. The manifest's `blur_threshold` is the floor actually applied and
`rejected_blurry` counts sampled frames below it. A caller-supplied `blur_threshold`
remains an absolute floor. §12.1's `kSharpnessBlurThreshold` and
`frames.DEFAULT_BLUR_THRESHOLD` (both 100.0) still define the live-capture and still
coverage audit.

Video frames from one extracted clip share one COLMAP camera (one lens and zoom per
clip); stills and frames of other clips keep independent intrinsics. On a 113-frame
iPhone orbit this registered 88 views instead of 74 with per-frame cameras.

Brush snapshots: a training pass exports at most about 50 previews
(`export_every = min(steps, max(100, steps // 50))`). Raw Brush exports are deleted once
copied or superseded, and only the newest two published snapshots are kept; the
previous one remains for a reader still loading it. Clients must load the
`splat_path` of the current record rather than retain older preview paths.

Scale mat: `reveng_print_scale_mat(size)` now writes the standard ChArUco mat
(`scale_mat.MAT_BOARD`: 7×10 squares of 25 mm, `DICT_5X5_100`, 18 mm markers, the same
175 × 250 mm board on A4 and Letter) and returns `{ok, path, preview_path, kind:"charuco",
spec, size, dpi, page_mm, square_len_mm, print_note}`; `path` is the PDF, `preview_path` a PNG
of the page. The board is rasterized at its exact size; the page margin is the quiet zone.
(Previously the tool imported a nonexistent function and always answered
`backend_not_provisioned`.) `reveng_set_scale(method="aruco_mat")` first solves the ChArUco
board (`params.charuco` overrides the spec): every corner resolved in at least three registered
views is triangulated, observations reprojecting worse than 2 px are dropped, and the board's
planar layout is fitted with a similarity transform. `mm_per_unit` is the inverse fitted scale;
`residual_pct` is the RMS fit residual relative to the RMS corner radius, and `scale.detail`
carries `n_corners`, `rms_residual_mm`, `max_residual_mm`, `corner_span_mm` and
`mat: {kind, ...spec}`. When no board resolves, a legacy single-marker mat (§12.3) is solved
from its four corners, and a failure reports both causes.

## 19. Existing CAD model discovery

Before researching dimensions or building a replacement, start a reverse-engineering
session and search for existing CAD with
`reveng_find_models(session_id, query, limit=8)`. Inspect candidates for suitability
and provenance; when a suitable model is available, select it with
`reveng_use_model(session_id, url=...)` or `reveng_use_model(session_id, path=...)`.
If no suitable model is available, proceed with research or reconstruction. A found
model is not by itself proof of fit or accuracy; verify critical dimensions.

`reveng_find_models` returns `{ok, session_id, query, candidates, standard_part,
coverage, hint}` and persists the search in `session.reference` without discarding
an already selected model. A direct URL is returned as a candidate rather than
automatically attached; standard hardware is represented separately.

`reveng_use_model` accepts exactly one of `url` or `path`. It obtains a URL's CAD
file under the session project root without importing it into a document; a local
path identifies an existing CAD file. It stores the selected model in
`session.reference.model` (`path`, `format`, `source_url`, `site`, `sha256`,
`license`, `ts`) and its path in `artifacts.reference_model_path`, completes
capture and model stages, sets status to `done` and lane to `reference`, and
records `use_reference_model` provenance. Success returns
`{ok, session_id, path, format, source_url, site}`.

Background search: `reveng_find_models(..., wait=false)` returns
`{ok, session_id, query, pending: true}` at once and runs the fan-out off the request thread
(the CAD-library sites render through a single-request browser daemon, about a minute and a
half cold, and worker stdio dispatch is serialized). `session.reference.status` is
`searching` until the result is stored (`done`, with `candidates`, `standard_part`,
`coverage`, `hint`, `searched_at`) or the search fails (`failed`, with `error`). A later
search for a different `query` supersedes a running one; its late result is dropped. The GUI
transport pins `wait=false`; the MCP default stays `wait=true` (blocking, full result).

## 20. Working-folder scoping and intake lane

- `session.project_dir` (desktop only; absent in a cell): the realpath of the working folder
  that owns the session — the explicit `project_dir` argument of `reveng_start_session`,
  `reveng_import_splat`, `reveng_make_part_from_library` and `reveng_start_from_product`, else
  the bound agent project root (`CHUDWARE_PROJECT_DIR`), else `null`.
- `list_reveng_sessions(project_dir?)` returns only sessions whose `project_dir` equals the
  given folder (else the bound project root); sessions without one are excluded from a
  filtered list. With neither known the list is unfiltered. A cell never filters.
- `session.lane`: `"scan"` (default) or `"reference"` (set by a research seed, a model search
  or a chosen model). Summaries carry `project_dir` and `lane`.
- `reveng_start_session(research=...)` and `reveng_make_part_from_library` search for an
  existing model first; research (and, for the library flow, the build mission) runs only
  when no `exact` / `strong` candidate or standard part was found. The library flow then
  returns `{ok, session_id, part_ref, model_present: false, existing_models, standard_part,
  hint}`; `force_build=true` skips the search.

## 21. Gaussian median-depth fusion — v8 amendment (2026-11-05)

This amendment supersedes the TSDF details of §17 for `reveng_splat_to_mesh` and the
reconstruct pipeline's splat fallback; method selection, pruning and the `prune`,
`points_path` and `pruned_splat_path` results are unchanged.

- Region: the TSDF renders every Gaussian of the raw splat inside the subject bounds, the
  pruned subject's 0.5-99.5 percentile box grown by 15 % of its largest side. Pruning
  only fixes that region; no splat the optimizer used to form a surface is dropped.
- Depth: up to 120 registered views (greedy coverage of viewing direction and distance)
  are rendered at 960 px on the long side in parallel worker processes. A pixel's depth
  is where transmittance crosses 1/2, using each footprint's ray-conditional depth; it is
  located with two levels of per-pixel optical-depth histograms (2048 depth cells), so it
  does not depend on splat order. Transmittance that never reaches 1/2 is no depth.
- Subject: a pixel is fused when its surface lies inside the bounds, more than three times
  the plane noise above the support plane (when `remove_floor` is true), is seen within 80°
  of its normal, is not next to a depth jump, a hole or the silhouette, and is reproduced
  by at least two of its six nearest similar-direction views. The support plane is the most
  populated of up to five RANSAC planes that the cameras see from one side, with more
  material on that side than behind it, and at least a fifth of whose inliers have no
  material above them; otherwise there is none and `support_plane` is `null`.
- Fusion: Open3D `VoxelBlockGrid` (CUDA when available), 4-voxel truncation, colors from
  the dataset's undistorted photographs. The voxel defaults to 1.5 times the median pixel
  footprint on the subject. Vertices that at least three views, and 30 % of the views that
  see them, look through are removed. Kept components are the largest and those within
  2 % of its extent, each at least 1 % of its size; with a support plane, components that
  never rise ten noise levels (or eight voxels) above it are dropped.
- Results add `support_plane` (`{normal, offset, noise}` or `null`), `selected_views`,
  `render_workers`, `fragments`, `render_seconds`, `subject_pixels`,
  `inconsistent_pixels`, `pixel_footprint`, `voxel_size`, `carved_vertices`, and
  component counts. The wall-clock budget is 1500 s; the MCP call timeout is 1800 s.
- Background meshing: `reveng_splat_to_mesh(path_or_session, ..., wait=true)`. For a
  session, `wait=false` holds the session claim, returns `{ok, session_id, pending: true}`
  at once and records `session.mesh_job` = `{status: "running", started_at}`, then
  `"done"` (adding `finished_at`, `mesh_path`, `raw_mesh_path`, `points_path`,
  `pruned_splat_path`, `method`, `units`, `vertices`, `triangles`, `voxel_size`,
  `support_plane`, and `fallback_reason` / `warning` when present) or `"failed"` (with
  `error`), and only then releases the claim. A file path with `wait=false` is refused
  (`reason: "wait_false_requires_session"`). The GUI worker transport pins `wait=false`
  because worker stdio dispatch is serialized. `get_reveng_session` reports a `running`
  job whose session claim is gone as `failed` with an `interrupted` error.
