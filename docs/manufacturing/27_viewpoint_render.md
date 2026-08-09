# Viewpoint pose-to-image and physical-camera bridge

## Scope and safety boundary

`services.viewpoint.render` closes the data-shape loop between an analytic candidate pose, a real
render request, the Stage-2 preference schema, and an advisory physical camera placement. It does
**not** claim that the current engine can yet render the full analytic `CameraPose`, and it does not
claim that a webcam can be placed or aimed automatically. A missing renderer, missing exact-camera
capability, camera-echo mismatch, unreadable output, or non-PNG output is a named refusal. No code in
this bridge synthesizes a placeholder image.

The bridge deliberately reuses the existing document renderer rather than creating another
rasterizer. The target worker seam is:

```text
worker protocol 2 request -> core/api/libchudware.cpp
                          -> chudware::doc::doc_render_view
                          -> core/doc/session.cpp software render_view_to_rgb
                          -> PNG at out_path
```

This is the same `doc_render_view` seam used by `services/mcp/cad_render_tools.py`. The CAM lane's
`cam_simulate` RPC returns stock/toolpath field data for the host to mesh and draw; it is not an
arbitrary camera-to-PNG RPC. The interactive viewport owns a local OpenGL `Renderer` and likewise
has no worker request that accepts an exact external camera pose. Therefore neither CAM simulation
nor a GUI screenshot is used as a substitute.

## Pose to render request

`RenderRequest` is strict and round-trippable. It records the preference request id, candidate id,
document id, geometry hash, feature, exact `services.viewpoint.geometry.CameraPose`, output path,
and annotation setting. Its worker call is the existing method `doc_render_view`:

```json
{
  "method": "doc_render_view",
  "params": {
    "doc_id": "doc-...",
    "out_path": "/absolute/path/candidate.png",
    "annotate": false,
    "view": {
      "camera": {
        "projection": "perspective",
        "position": [40.0, 20.0, 30.0],
        "target": [0.0, 0.0, 5.0],
        "up": [0.0, 0.0, 1.0],
        "vertical_fov_deg": 45.0,
        "near": 0.1,
        "far": 1000.0
      },
      "width": 640,
      "height": 480
    },
    "viewpoint": {
      "schema": "chudware.viewpoint.render_request/1",
      "request_id": "offer-...",
      "candidate_id": "view-a",
      "geometry_hash": "<64 lowercase hex>",
      "feature": "counterbore-7"
    }
  }
}
```

The width and height are constrained to the existing `doc_render_view` range 64 through 2048.
`annotate=false` prevents labels, callouts, and an axis triad from becoming accidental policy
features. The `viewpoint` object is optional provenance under worker protocol 2's unknown-optional-
parameter rule; the renderer need not consume it to draw the frame.

### Exact integrator addition required

The method exists, but its exact-pose input does **not**. At present `doc_render_view` resolves a
named view, azimuth/elevation, or a single scene-to-camera `dir`; it then auto-frames an
**orthographic** camera. That loses camera position, target offset, roll, perspective FOV, and
near/far clipping. Sending the payload above to the current implementation would silently render a
different camera, so `WorkerRpcRenderBackend` refuses before calling it unless the backend
advertises `doc_render_view.camera_pose.v1`.

The integrator must extend the existing method, not add a parallel renderer:

1. `ping` (or the caller's equivalent capability negotiation) must include the exact string
   `doc_render_view.camera_pose.v1` only when the running implementation supports the following
   semantics.
2. When `view.camera.projection == "perspective"`, `doc_render_view` must use `position`, `target`,
   normalized image-up, vertical FOV, near, far, width, and height exactly in document/WCS
   coordinates. It must not auto-fit, replace the target with the body center, choose another up
   vector, or switch to orthographic projection. Existing named/direction view behavior remains
   unchanged when `view.camera` is absent.
3. A successful response must retain existing fields and add:

   ```json
   {
     "ok": true,
     "path": "/absolute/path/candidate.png",
     "capability": "doc_render_view.camera_pose.v1",
     "camera": {
       "position": [40.0, 20.0, 30.0],
       "target": [0.0, 0.0, 5.0],
       "up": [0.0, 0.0, 1.0],
       "vertical_fov_deg": 45.0,
       "image_width_px": 640,
       "image_height_px": 480,
       "near": 0.1,
       "far": 1000.0
     },
     "revision": 17
   }
   ```

   `camera` is the exact applied camera after validation and `revision` is the rendered document
   revision. A renderer that clamps, reframes, or otherwise changes the camera must echo the changed
   values; this bridge will refuse the mismatch rather than attach incorrect pose provenance to the
   pixels.
4. Failure remains an ordinary `ok:false` render response. It must not write a stock image, stale
   previous frame, checkerboard, or synthetic success PNG.

`WorkerRpcRenderBackend` validates capability, camera echo, path echo, integer revision, readable
PNG signature, and media type. The adapter computes a SHA-256 over the request and the candidate
assembler independently computes a SHA-256 over the returned PNG bytes.

## Candidate sheet and preference flywheel

`render_candidate_sheet` accepts the existing
`services.viewpoint.feedback.schema.CandidateSetRequest`, a CAD document id, an output directory,
render settings, and a `RenderBackend`. It processes candidates in the request's canonical order:

1. Convert feedback's `eye/target/up/fov_y_degrees` to the analytic `CameraPose`, adding explicit
   image dimensions and clip planes.
2. Emit one `RenderRequest` per candidate and require a real PNG result with matching request id,
   candidate id, and request digest.
3. Construct each feedback `RenderCandidate` with the original feedback pose, renderer URI, and
   SHA-256 of the actual bytes.
4. Call feedback's existing `build_candidate_set`, which records `randomization_seed` and the
   deterministic permutation while preserving canonical candidate identity.

The model-facing value is `CandidateSheet.candidate_set`, an unmodified
`CandidateSetResponse`. Its serialized keys are exactly `schema_version`, `request_id`,
`geometry_hash`, `feature`, `task_text`, `canonical_candidates`, `permutation`, and
`randomization_seed`; no bridge-only field is injected into the feedback schema. This means the
result can go directly to `score_candidate_set`, and a resulting `PreferenceRecord` retains the
render URI/hash, canonical poses, shown order, task, geometry identity, scorer/model provenance,
and answer outcome.

Render-engine provenance that does not fit feedback schema v1 is held separately in
`CandidateSheet.render_provenance` and `provenance_dict()`: request digest, backend name/version,
RPC method, capability, document id/revision, geometry hash, request id, and candidate id. The
manifest repeats seed and permutation so an archived image set can be audited without inferring
presentation order from filenames. Persistence of that out-of-band manifest is an integrator
responsibility; the bridge does not mutate the frozen feedback schema.

Assembly is all-or-nothing. If any candidate refuses or returns mismatched provenance, no complete
`CandidateSetResponse` is returned. Earlier PNG files may remain as diagnostic artifacts but cannot
silently become a partial preference set.

Named refusals include:

| code | meaning |
|---|---|
| `viewpoint_renderer_unavailable` | no backend was supplied or the worker call could not be made |
| `viewpoint_exact_camera_unsupported` | the worker did not advertise/confirm exact camera v1 |
| `viewpoint_render_failed` | the renderer returned `ok:false` |
| `viewpoint_render_invalid` | path/camera/revision/media/PNG validation failed |
| `viewpoint_render_provenance_mismatch` | result identity or request digest did not match the candidate |

## Physical-camera counterpart

`pose_to_physical_instruction` resolves the supplied fixture frame to the requested machine frame
through `services.manufacturing.metrology.TransformGraph`. It therefore uses the same
`TransformEdge`, `ResolvedTransform`, and `Pose` types as stock, fixture, station, and probe
registration rather than defining a second transform system.

The caller must provide `units_to_mm`. Stage-1 analytic geometry is scale-normalized and does not
prove that one corpus coordinate is one millimetre. Native CAD/WCS coordinates pass `1.0`; any other
scale must come from the CAD-to-fixture registration. The function scales eye and target, applies
the resolved fixture-to-machine transform, rotates the camera basis, and returns:

- position and target in machine millimetres;
- unit aim and image-up directions in the machine frame;
- working distance in millimetres;
- FOV and requested image dimensions;
- metrology transform edge ids and whether transform uncertainty is unknown; and
- a metrology `Pose` for the physical camera frame.

The camera-frame convention is right-handed: local +X is image-right, +Y is image-up, and local -Z
is the optical/aim axis. This makes `camera_pose_machine` composable with the existing metrology
frame graph while keeping the familiar camera-forward convention explicit.

This instruction is advisory geometry only. No collision-free camera path, fixture keep-out check,
tripod/robot inverse kinematics, lens distortion model, autofocus/exposure lock, device actuation,
or evidence-tier promotion occurs here. A webcam frame remains advisory under the metrology rules.

## Verification and what remains unproven

The package test uses an in-process fake backend and no GPU or engine process. It covers exact
pose/request round trips, the worker adapter's capability and camera echo, feedback-schema candidate
assembly, real-byte hashing, reproducible recorded permutation, named missing-renderer refusal, and
a hand-computed fixture-to-machine rigid transform including the camera optical axis.

This does **not** prove that current engine pixels correspond to an analytic candidate pose: the
required `doc_render_view.camera_pose.v1` extension is not implemented in this write set. It also
does not prove physical camera placement, calibration, visibility, or safety without registered
hardware and a measured fixture/CAD transform.
