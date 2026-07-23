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
- **Honesty tiers** for photo-derived dims reuse `engine_oracle/vision/measure.py`:
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

Added to `services/agent/routing.yaml` (and symlink `agent/routing.yaml`):

| role | purpose | vision |
|---|---|---|
| `re_modeler` | Feature code grounded in fitted dims | no |
| `re_datasheet_reader` | VLM reads datasheet crops/tables | yes |
| `re_geometry_verifier` | Deviation + compare_parts loop | no |

---

## 11. Embedded contract examples (for pytest validation)

See `services/mcp/test_reveng_contract.py` — examples MUST validate against §2–§5 shapes.
