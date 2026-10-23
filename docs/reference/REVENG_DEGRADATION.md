# Reverse Engineering workspace — degradation matrix

`reveng_status()` in `services/vision/reveng_provision.py` is an offline-safe
readiness probe (env/filesystem/PATH checks and optional Python imports, never the
network) that reports per-capability availability (`contracts/reveng.md` §8.1).
The Reverse Engineering tab's capability panel (`loadCapabilities` in
`host/src/ui/reveng/reveng_workspace.cpp`)
reads `capabilities`, `provisioning`/`provisioning_detail` and `banners` to gate the
stage controls and show fix-it hints; agents call the same probe as the
`reveng_status` tool. This page catalogues the degradation scenarios.

## Result shape

```
{ok, capabilities: {sparse, dense, splat, single_photo, webcam, gpu},
 provisioning: {colmap, openmvs, brush, triposr, vggt, docling},
 provisioning_detail: {<key>: {available, mode, license, env_var, sha256, note}},
 cad_recode: {available, model, env_var, license},
 banners: [str], missing: [str]}
```

`capabilities` and `provisioning` keys are the frozen §8.1 set. `cad_recode` is an
additive discovery block (not a frozen provisioning key), like TRELLIS, which folds
into `single_photo`. `missing` lists the provisioning keys whose artifact is absent.

## Provisioning keys

| Key | Kind / mode | Available when |
|-----|-------------|----------------|
| `colmap` | Python (`pip`) | `pycolmap` imports in the services Python runtime (no `COLMAP_BIN` lookup) |
| `openmvs` | binary (`env`, AGPL) | `OPENMVS_BIN` names an existing path or a command on `PATH`, else `openmvs` is on `PATH` |
| `brush` | binary (`download`) | `BRUSH_BIN` resolves via `PATH` lookup (an explicit override is authoritative even when invalid), else the managed install `$XDG_DATA_HOME/Chudware/tools/brush/v0.3.0/brush_app` (default `~/.local/share/...`) is executable |
| `triposr` | weights (`download`) | `TRIPOSR_WEIGHTS` names an existing path or a command on `PATH` (no cache probe) |
| `vggt` | weights (`env`) | `VGGT_WEIGHTS` names an existing path or a command on `PATH`, else `vggt` on `PATH` |
| `docling` | Python (`pip`) | `docling` imports |

Not provisioning keys: `trellis` (`TRELLIS_WEIGHTS`, same rule as `triposr`) and
`cad_recode` (`CAD_RECODE_WEIGHTS` path, or one of
`~/.cache/huggingface/hub/models--filapro--cad-recode-v1.5`, `~/.cache/cad_recode`,
`/opt/models/cad-recode-v1.5` exists).

## Capability keys

| Key | Meaning | Derived from |
|-----|---------|--------------|
| `sparse` | Sparse SfM | `colmap` (pycolmap). VGGT does not feed this flag. |
| `dense` | Dense mesh (OpenMVS) | `openmvs` **and** `colmap` (dense needs a sparse model first) |
| `splat` | 3DGS trainer (Brush) | `brush` **and** `colmap` |
| `single_photo` | Single-image → mesh (TripoSR / TRELLIS) | `triposr` or `trellis` |
| `webcam` | Live capture on the worker | `CHUDWARE_TEST_CAMERA=1`, or `/dev/video0` exists |
| `gpu` | GPU detected | `CUDA_VISIBLE_DEVICES` set and not `-1`, or `/dev/nvidia0` or `/dev/dri` exists, or `nvidia-smi` on `PATH` |

## Banners

Emitted verbatim by `reveng_status()`:

| Condition | Banner |
|-----------|--------|
| `sparse` false | `Sparse reconstruction unavailable: install pycolmap in the services Python runtime.` |
| `openmvs` absent | `Dense mesh (OpenMVS) not configured. OpenMVS is AGPL-3.0 -> install it yourself and set OPENMVS_BIN (subprocess only, never bundled).` |
| `splat` false | `3D Gaussian Splatting needs pycolmap and Brush: python scripts/provision_reveng.py.` |
| `single_photo` false | `Single-photo tier unavailable: TripoSR/TRELLIS weights not provisioned.` |
| `cad_recode` absent | `ML parametric (CAD-Recode) unavailable: set CAD_RECODE_WEIGHTS or snapshot_download('filapro/cad-recode-v1.5') (~4GB VRAM bf16).` |

There is no banner for `gpu`, `webcam`, `vggt` or `docling`; read the flags and
`missing` for those.

## Degradation scenarios

### No GPU

`capabilities.gpu = false`. The probe does not gate any other capability on the
GPU flag; reconstruction capabilities depend only on their artifacts.

### No webcam

`capabilities.webcam = false`. Live capture on the worker is unavailable;
reconstruction from existing photos/video is unaffected.

### No pycolmap

`sparse`, `dense` and `splat` are all `false` even when OpenMVS or Brush is
present, because both depend on a sparse model. `colmap` is in `missing`.

### Nothing provisioned

All capability flags except possibly `gpu`/`webcam` are `false`, every
provisioning key is in `missing`, and all five banners are emitted.

### VLM availability

Not reported by `reveng_status()`; VLM/provider availability belongs to the
agent provider layer.

## License notes (summary — see `contracts/license_debt.md` items 10, 12, 13 and 19)

| Backend | License (descriptor) | Policy in code |
|---------|----------------------|----------------|
| pycolmap | BSD-3-Clause | optional Python import in the services runtime; never bundles the COLMAP binary |
| OpenMVS | **AGPL-3.0** | `mode: "env"`, no fetch URL — **subprocess only, never linked/bundled/auto-fetched**; user installs and sets `OPENMVS_BIN` |
| Brush | Apache-2.0 | subprocess; `scripts/provision_reveng.py` installs the v0.3.0 Linux x86_64 release verified against the pinned SHA256 (or clones the pinned source with `--source-checkout`) |
| TripoSR | MIT | weights descriptor, SHA256 `PENDING` — a download path must refuse until a real hash is pinned |
| TRELLIS | MIT | as TripoSR |
| VGGT | **UNVERIFIED** (possible CC-BY-NC) | discovery-only (`VGGT_WEIGHTS`) until the license is confirmed |
| docling | MIT | Python dependency |
| CAD-Recode v1.5 | **CC-BY-NC-4.0** | `strategy="ml"` rebuild backend (`services/vision/cad_recode.py`); open compliance item 19 in `contracts/license_debt.md`; weights are download-on-demand / user-provisioned, never bundled |

These are descriptor and ledger summaries, not a new licensing determination.
`contracts/licenses.md` and `contracts/license_debt.md` remain the authorities;
availability does not by itself establish deployment or redistribution permission.

## Extending the matrix

To add a new capability:
1. Add a descriptor to `REVENG_DESCRIPTORS` in `reveng_provision.py`.
2. Add the key to `_CAPABILITY_KEYS` and (if a provisioning artifact) `_PROVISIONING_KEYS`
   — both are the frozen §8.1 set, so a contract change comes first; otherwise surface it
   additively like `cad_recode`.
3. Update the `capabilities` dict in `reveng_status()`.
4. Add a banner for the missing case.
5. Add a row to `contracts/license_debt.md` if the license is non-permissive.
6. Add a test scenario to `services/vision/test_reveng_degradation.py`.
