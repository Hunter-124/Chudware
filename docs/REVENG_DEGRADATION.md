# Reverse Engineering workspace — degradation matrix

`reveng_status()` in `engine_oracle/vision/reveng_provision.py` returns a dict
describing per-capability availability.  The GUI banner layer (RE-02) reads
`banners` and `missing` to surface fix-it prompts.  This page catalogues the
degradation scenarios.

## Capability keys

| Key | Meaning | Requirements |
|-----|---------|--------------|
| `gpu` | CUDA/GPU detected | `/dev/nvidia0` present, or `nvidia-smi` on PATH, or `CUDA_VISIBLE_DEVICES` set non-empty |
| `webcam` | Live capture possible | `/dev/video0` present, or `AICAD_TEST_CAMERA=1` |
| `sparse` | Sparse SfM (COLMAP / VGGT) | `COLMAP_BIN` set + path exists, or `colmap` on PATH, or `VGGT_WEIGHTS` set |
| `dense` | Dense mesh (OpenMVS) | `sparse` **and** `OPENMVS_BIN` set + path exists |
| `splat` | 3DGS trainer (Brush) | `BRUSH_BIN` set, or Brush binary downloaded |
| `single_photo` | Single-image → mesh (TripoSR / TRELLIS) | `TRIPOSR_WEIGHTS` or `TRELLIS_WEIGHTS` set, or weights downloaded |

## Degradation scenarios

### No GPU

`capabilities.gpu = false`.  Reconstruction backends remain available if their
binaries are present (COLMAP, OpenMVS, Brush all run CPU-only, slower).
Single-photo models require a GPU for practical speed; the banner layer should
surface a warning but not block the workflow.

### No webcam

`capabilities.webcam = false`.  Live-capture workflows are unavailable.
Photogrammetry from existing photos/video is unaffected.

### No reconstruction binaries

All of `sparse`, `dense`, `splat` become `false`.  `reveng_status()` emits
banners for each missing capability.  The `missing` list names each absent
provisioning key so the UI can render a focused install checklist.

Example banners emitted:
- `"Sparse reconstruction unavailable: install COLMAP (set COLMAP_BIN)."`
- `"Dense mesh (OpenMVS) not configured. OpenMVS is AGPL-3.0 -> install it yourself and set OPENMVS_BIN (subprocess only, never bundled)."`
- `"3D Gaussian Splatting unavailable: Brush binary not provisioned."`
- `"Single-photo tier unavailable: TripoSR/TRELLIS weights not provisioned."`

### No local VLM

VLM availability is not surfaced by `reveng_status()` directly — it is managed
by the AI copilot provider system.  When the local VLM is absent the RE workspace
falls back to cloud inference; the copilot layer emits its own banner.

### Fully offline / fully degraded

No GPU, no webcam, no binaries.  All capability flags are `false`.  The `missing`
list contains all provisioning keys.  The GUI should render a first-run setup
wizard rather than individual banners.

## License notes (summary — see contracts/license_debt.md for full audit)

| Backend | License | Bundling policy |
|---------|---------|-----------------|
| COLMAP | BSD-3-Clause | subprocess; may fetch binary (SHA-pinned) |
| OpenMVS | **AGPL-3.0** | **subprocess ONLY, never linked/bundled/auto-fetched** — user installs, sets `OPENMVS_BIN` |
| Brush | Apache-2.0 | subprocess; may fetch binary (SHA-pinned, pending) |
| TripoSR | MIT | weights download (SHA pending) |
| TRELLIS | MIT | weights download (SHA pending) |
| VGGT | **UNVERIFIED** (possible CC-BY-NC) | discovery-only (`VGGT_WEIGHTS`) until license confirmed |
| pycolmap | BSD-3-Clause | optional Python import |
| docling | MIT | pip dependency |
| CAD-Recode | **CC-BY-NC-4.0** | **NOT shipped — inspiration only** |

OpenMVS's AGPL-3.0 means any network-service / SaaS deployment that invokes it
must open-source the service.  The descriptor enforces `mode: "env"` with no
fetch URL — we never auto-download it.

CAD-Recode (CC-BY-NC) is not shipped in any form; it is recorded in
`contracts/license_debt.md` so future contributors know not to add it without a
license review or confirmed relicensing.

## Extending the matrix

To add a new capability:
1. Add a descriptor to `REVENG_DESCRIPTORS` in `reveng_provision.py`.
2. Add the key to `_CAPABILITY_KEYS` and (if a provisioning artifact) `_PROVISIONING_KEYS`.
3. Update the `capabilities` dict in `reveng_status()`.
4. Add a banner for the missing case.
5. Add a row to `contracts/license_debt.md` if the license is non-permissive.
6. Add a test scenario to `engine_oracle/vision/test_reveng_degradation.py`.
