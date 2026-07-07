"""Download-on-demand provisioning descriptors + readiness probe for the
Reverse Engineering workspace (contracts/reveng.md §8.1 ``reveng_status``).

The heavy RE toolchain (COLMAP / OpenMVS / Brush binaries, TripoSR / TRELLIS /
VGGT weights) is **never bundled** in the base runtime — it is fetched on demand
into the user data dir, or discovered from a user-provided binary via env var.
This module holds the SHA-pinned descriptor table and a pure, offline-safe
``reveng_status()`` probe that reports per-capability availability without ever
touching the network.

License discipline (CONTRIBUTING.md guardrails):
- **OpenMVS is AGPL** -> ``mode: "env"`` (discovery-only via ``OPENMVS_BIN``),
  subprocess ONLY, NEVER bundled/linked and NEVER auto-downloaded by us. Its
  descriptor carries no fetch URL; the user installs it themselves.
- COLMAP (BSD) and Brush (Apache-2.0) are subprocess binaries we may fetch.
- TripoSR (MIT) / TRELLIS (MIT) / VGGT weights are fetched model payloads;
  see ``license`` on each descriptor and ``licenses.md`` for the register.

SHA pinning honesty: hashes that could not be independently verified offline are
recorded as the sentinel ``"PENDING"``. The schema is still well-formed (the
``sha256`` key is always present), the readiness probe tolerates the sentinel,
and the *download path* (RE-later) MUST refuse to install a ``PENDING`` artifact
until a real hash is filled in. We never fabricate a SHA256 value.
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

# Sentinel for an as-yet-unverified artifact hash. A real download implementation
# must refuse to fetch/accept an artifact whose descriptor still carries this.
SHA_PENDING = "PENDING"

# ---------------------------------------------------------------------------
# Provisioning descriptor table
# ---------------------------------------------------------------------------
# mode:
#   "download" -> we may fetch the artifact from `url` and verify `sha256`.
#   "env"      -> discovery-only; user supplies the binary via `env_var`. We
#                 NEVER auto-download (license or upstream policy). No `url`.
# kind: "binary" (subprocess tool) | "weights" (model checkpoint).
# capability: which reveng_status capability this descriptor feeds.
REVENG_DESCRIPTORS: dict[str, dict] = {
    # --- Sparse reconstruction: COLMAP (BSD-3-Clause, subprocess) -----------
    "colmap": {
        "kind": "binary",
        "capability": "sparse",
        "license": "BSD-3-Clause",
        "mode": "env",  # prefer a system/user COLMAP; download variant may be added later
        "env_var": "COLMAP_BIN",
        "url": None,
        "sha256": SHA_PENDING,
        "note": "Sparse SfM. Set COLMAP_BIN or install colmap on PATH.",
    },
    # --- Dense mesh: OpenMVS (AGPL-3.0 -> subprocess ONLY, never bundled) ---
    "openmvs": {
        "kind": "binary",
        "capability": "dense",
        "license": "AGPL-3.0",
        # AGPL: discovery-only. We must NOT ship a fetch URL for this.
        "mode": "env",
        "env_var": "OPENMVS_BIN",
        "url": None,
        "sha256": None,  # N/A: we never fetch/verify an AGPL binary
        "note": (
            "AGPL-3.0 -> subprocess ONLY, never bundled or linked. "
            "Install OpenMVS yourself and set OPENMVS_BIN (DensifyPointCloud etc.)."
        ),
    },
    # --- 3D Gaussian Splatting trainer: Brush (Apache-2.0, subprocess) ------
    "brush": {
        "kind": "binary",
        "capability": "splat",
        "license": "Apache-2.0",
        "mode": "download",
        "env_var": "BRUSH_BIN",
        # SHA-pinned release URL placeholder; hash unverified offline -> PENDING.
        "url": "https://github.com/ArthurBrussee/brush/releases/download/v0.2.0/brush-linux-x86_64.tar.gz",
        "sha256": SHA_PENDING,
        "note": "3DGS trainer + mesh export. Set BRUSH_BIN to override the fetched binary.",
    },
    # --- Single-photo weights: TripoSR (MIT) -------------------------------
    "triposr": {
        "kind": "weights",
        "capability": "single_photo",
        "license": "MIT",
        "mode": "download",
        "env_var": "TRIPOSR_WEIGHTS",
        "url": "https://huggingface.co/stabilityai/TripoSR/resolve/main/model.ckpt",
        "sha256": SHA_PENDING,
        "note": "Single-photo -> mesh (approximate tier). MIT.",
    },
    # --- Single-photo weights: TRELLIS (MIT) -------------------------------
    # NOTE: the v1 contract's `provisioning` dict has no `trellis` key
    # (colmap/openmvs/brush/triposr/vggt/docling). TRELLIS availability folds
    # into the `single_photo` capability alongside TripoSR; the descriptor is
    # tracked here but is intentionally NOT surfaced as provisioning.trellis.
    "trellis": {
        "kind": "weights",
        "capability": "single_photo",
        "license": "MIT",
        "mode": "download",
        "env_var": "TRELLIS_WEIGHTS",
        "url": "https://huggingface.co/microsoft/TRELLIS-image-large/resolve/main/model.safetensors",
        "sha256": SHA_PENDING,
        "note": "Single-photo -> mesh (approximate tier). MIT.",
    },
    # --- Multi-view geometry weights: VGGT ---------------------------------
    # License UNVERIFIED offline: some Meta model releases are CC-BY-NC
    # (non-commercial), which would bar bundling/commercial fetch. Descriptor
    # is discovery-only (mode="env") pending a confirmed SPDX + licenses.md
    # entry. See work/questions/RE-06.md.
    "vggt": {
        "kind": "weights",
        "capability": "sparse",
        "license": "UNVERIFIED",
        "mode": "env",
        "env_var": "VGGT_WEIGHTS",
        "url": None,
        "sha256": SHA_PENDING,
        "note": (
            "Feed-forward multi-view geometry weights. License unverified offline "
            "(possible CC-BY-NC) -> discovery-only until confirmed; set VGGT_WEIGHTS."
        ),
    },
    # --- Datasheet OCR/layout: docling (MIT, pip dep) ----------------------
    "docling": {
        "kind": "python",
        "capability": "datasheet",
        "license": "MIT",
        "mode": "pip",
        "env_var": None,
        "url": None,
        "sha256": None,
        "note": "Datasheet table/layout extraction. Installed as a Python dependency.",
    },
}


def _artifact_available(name: str) -> bool:
    """True if the descriptor's artifact appears present, without any network I/O.

    - ``env`` mode: the env var is set AND points at an existing path (or, for a
      PATH tool like colmap, is resolvable via ``shutil.which``).
    - ``pip`` mode: the module imports.
    - ``download`` mode: env override path exists, else the (future) cached
      artifact path exists. Offline with nothing provisioned -> False.
    """
    d = REVENG_DESCRIPTORS.get(name)
    if not d:
        return False
    env_var = d.get("env_var")
    if env_var:
        val = os.environ.get(env_var)
        if val:
            if os.path.exists(val):
                return True
            # bare command name on PATH (e.g. COLMAP_BIN="colmap")
            if shutil.which(val):
                return True
    mode = d.get("mode")
    if mode == "pip":
        try:
            __import__(name)
            return True
        except Exception:  # noqa: BLE001
            return False
    if mode == "env":
        # env var not set / not resolvable above -> also try the bare tool name on PATH
        return bool(shutil.which(name))
    # download mode with no env override and no cached artifact discoverable offline
    return False


def _gpu_available() -> bool:
    """Best-effort, offline GPU check. Never imports heavy ML frameworks."""
    if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() not in ("", "-1"):
        return True
    if os.path.exists("/dev/nvidia0") or os.path.exists("/dev/dri"):
        return True
    return bool(shutil.which("nvidia-smi"))


def _webcam_available() -> bool:
    """Offline webcam check: opt-in test flag or a Linux video device node."""
    if os.environ.get("AICAD_TEST_CAMERA", "").strip() == "1":
        return True
    return os.path.exists("/dev/video0")


# Capabilities exposed by the frozen §8.1 contract, in order.
_CAPABILITY_KEYS = ("sparse", "dense", "splat", "single_photo", "webcam", "gpu")
# Provisioning keys exposed by the frozen §8.1 contract (NB: no "trellis").
_PROVISIONING_KEYS = ("colmap", "openmvs", "brush", "triposr", "vggt", "docling")


def provisioning_status() -> dict[str, dict]:
    """Per-descriptor availability + descriptor metadata (all contract keys)."""
    out: dict[str, dict] = {}
    for name in _PROVISIONING_KEYS:
        d = REVENG_DESCRIPTORS[name]
        out[name] = {
            "available": _artifact_available(name),
            "mode": d.get("mode"),
            "license": d.get("license"),
            "env_var": d.get("env_var"),
            "sha256": d.get("sha256"),
            "note": d.get("note"),
        }
    return out


def reveng_status() -> dict:
    """Readiness probe for the Reverse Engineering workspace (contract §8.1).

    Returns the frozen shape::

        {ok, capabilities: {sparse, dense, splat, single_photo, webcam, gpu},
         provisioning: {colmap, openmvs, brush, triposr, vggt, docling},
         banners: [str], missing: [str]}

    Pure and offline-safe: reads env vars + filesystem only, never the network.
    ``missing`` is the structured list of provisioning keys whose artifact is
    absent, so callers (RE-02 / GUI) can render a fix-it list without re-deriving.
    """
    prov = provisioning_status()

    colmap = prov["colmap"]["available"]
    openmvs = prov["openmvs"]["available"]
    brush = prov["brush"]["available"]
    triposr = prov["triposr"]["available"]
    # TRELLIS is not a contract provisioning key; fold it into single_photo.
    trellis = _artifact_available("trellis")

    capabilities = {
        "sparse": colmap or prov["vggt"]["available"],
        "dense": openmvs and colmap,  # dense needs a sparse model first
        "splat": brush,
        "single_photo": triposr or trellis,
        "webcam": _webcam_available(),
        "gpu": _gpu_available(),
    }

    missing = [name for name in _PROVISIONING_KEYS if not prov[name]["available"]]

    banners: list[str] = []
    if not capabilities["sparse"]:
        banners.append("Sparse reconstruction unavailable: install COLMAP (set COLMAP_BIN).")
    if openmvs is False:
        banners.append(
            "Dense mesh (OpenMVS) not configured. OpenMVS is AGPL-3.0 -> "
            "install it yourself and set OPENMVS_BIN (subprocess only, never bundled)."
        )
    if not capabilities["splat"]:
        banners.append("3D Gaussian Splatting unavailable: Brush binary not provisioned.")
    if not capabilities["single_photo"]:
        banners.append("Single-photo tier unavailable: TripoSR/TRELLIS weights not provisioned.")

    return {
        "ok": True,
        "capabilities": capabilities,
        "provisioning": {k: prov[k]["available"] for k in _PROVISIONING_KEYS},
        "provisioning_detail": prov,
        "banners": banners,
        "missing": missing,
    }
