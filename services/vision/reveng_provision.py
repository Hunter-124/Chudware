"""Download-on-demand provisioning descriptors + readiness probe for the
Reverse Engineering workspace (contracts/reveng.md §8.1 ``reveng_status``).

The heavy RE toolchain (COLMAP / OpenMVS / Brush binaries, TripoSR / TRELLIS /
VGGT / CAD-Recode weights) is **never bundled** in the base runtime — it is
fetched on demand into the user data dir, or discovered from a user-provided
binary via env var. This module holds the SHA-pinned descriptor table and a
pure, offline-safe ``reveng_status()`` probe that reports per-capability
availability without ever touching the network.

License discipline (CONTRIBUTING.md guardrails):
- **OpenMVS is AGPL** -> ``mode: "env"`` (discovery-only via ``OPENMVS_BIN``),
  subprocess ONLY, NEVER bundled/linked and NEVER auto-downloaded by us. Its
  descriptor carries no fetch URL; the user installs it themselves.
- COLMAP's Python bindings (BSD) provide SfM; Brush (Apache-2.0) runs as a subprocess.
- TripoSR (MIT) / TRELLIS (MIT) / VGGT weights are fetched model payloads;
  see ``license`` on each descriptor and ``licenses.md`` for the register.
- CAD-Recode v1.5 weights are CC-BY-NC-4.0 (licensing consciously deferred —
  see ``contracts/license_debt.md``); download-on-demand only.

SHA pinning honesty: hashes that could not be independently verified offline are
recorded as the sentinel ``"PENDING"``. The schema is still well-formed (the
``sha256`` key is always present), the readiness probe tolerates the sentinel,
and the *download path* (RE-later) MUST refuse to install a ``PENDING`` artifact
until a real hash is filled in. We never fabricate a SHA256 value.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# Sentinel for an as-yet-unverified artifact hash. A real download implementation
# must refuse to fetch/accept an artifact whose descriptor still carries this.
SHA_PENDING = "PENDING"

BRUSH_VERSION = "v0.3.0"
BRUSH_SOURCE = "3edecbb2fe79d3e2c87eeab85b15e0b1dd10d486"
BRUSH_URL = "https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/brush-app-x86_64-unknown-linux-gnu.tar.xz"
BRUSH_SHA256 = "4f0f9a8785d1951c62df26aae247c02c5bba32b00f40b06df4e1c9b867399e20"


def brush_install_dir() -> Path:
    """Managed tool location, shared by the installer and offline discovery."""
    data = os.environ.get("XDG_DATA_HOME", "")
    root = Path(data) if data and Path(data).is_absolute() else Path.home() / ".local/share"
    return root / "Chudware/tools/brush" / BRUSH_VERSION


def brush_executable() -> str | None:
    """An explicit override is authoritative, including when it is invalid."""
    override = os.environ.get("BRUSH_BIN")
    if override:
        return shutil.which(os.path.expanduser(override))
    path = brush_install_dir() / "brush_app"
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None

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
    # Sparse reconstruction uses pycolmap in the actual pipeline, not COLMAP_BIN.
    "colmap": {
        "kind": "python",
        "capability": "sparse",
        "license": "BSD-3-Clause",
        "mode": "pip",
        "env_var": None,
        "url": None,
        "sha256": None,
        "note": "Sparse SfM requires importable pycolmap in the services Python runtime.",
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
        "url": BRUSH_URL,
        "sha256": BRUSH_SHA256,
        "version": BRUSH_VERSION,
        "source_commit": BRUSH_SOURCE,
        "note": "Gaussian trainer (no mesh export). Run python scripts/provision_reveng.py; BRUSH_BIN overrides.",
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
    # entry.
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
    # --- ML parametric rebuild: CAD-Recode v1.5 (CC-BY-NC-4.0, deferred) ---
    # Not a frozen §8.1 provisioning key (colmap/openmvs/brush/triposr/vggt/
    # docling). Tracked here for discovery + status banners; availability is
    # also exposed via capabilities["ml_parametric"] (additive, non-contract).
    # Weights: filapro/cad-recode-v1.5 on HuggingFace. Set CAD_RECODE_WEIGHTS
    # or let huggingface_hub cache under ~/.cache/huggingface/hub/.
    "cad_recode": {
        "kind": "weights",
        "capability": "ml_parametric",
        "license": "CC-BY-NC-4.0",
        "mode": "download",
        "env_var": "CAD_RECODE_WEIGHTS",
        "url": "https://huggingface.co/filapro/cad-recode-v1.5",
        "sha256": SHA_PENDING,
        "note": (
            "ML mesh→CadQuery parametric rebuild (filapro/cad-recode-v1.5). "
            "CC-BY-NC-4.0 (non-commercial) — documented exception in THIRD_PARTY_NOTICES.md. "
            "Provision: python -c \"from huggingface_hub import snapshot_download; "
            "snapshot_download('filapro/cad-recode-v1.5')\" "
            "or set CAD_RECODE_WEIGHTS. Needs torch+transformers; ~4GB VRAM bf16."
        ),
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
    if name == "brush":
        return brush_executable() is not None
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
            __import__("pycolmap" if name == "colmap" else name)
            return True
        except Exception:  # noqa: BLE001
            return False
    if mode == "env":
        # env var not set / not resolvable above -> also try the bare tool name on PATH
        return bool(shutil.which(name))
    # download mode: env override handled above. For HF weight caches, probe the
    # well-known hub layout offline (no network).
    if name == "cad_recode":
        return _cad_recode_weights_present()
    return False


def _cad_recode_weights_present() -> bool:
    """Offline check for CAD-Recode weights (env or HF hub cache)."""
    env = os.environ.get("CAD_RECODE_WEIGHTS", "").strip()
    if env and os.path.exists(env):
        return True
    home = os.path.expanduser("~")
    candidates = (
        os.path.join(home, ".cache", "huggingface", "hub", "models--filapro--cad-recode-v1.5"),
        os.path.join(home, ".cache", "cad_recode"),
        "/opt/models/cad-recode-v1.5",
    )
    return any(os.path.exists(p) for p in candidates)


def _gpu_available() -> bool:
    """Best-effort, offline GPU check. Never imports heavy ML frameworks."""
    if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() not in ("", "-1"):
        return True
    if os.path.exists("/dev/nvidia0") or os.path.exists("/dev/dri"):
        return True
    return bool(shutil.which("nvidia-smi"))


def _webcam_available() -> bool:
    """Offline webcam check: opt-in test flag or a Linux video device node."""
    if os.environ.get("CHUDWARE_TEST_CAMERA", "").strip() == "1":
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

    # CAD-Recode is discovery-only (like trellis): not a frozen §8.1 key.
    # Availability is via REVENG_DESCRIPTORS["cad_recode"] / _artifact_available.
    cad_recode = _artifact_available("cad_recode")

    capabilities = {
        "sparse": colmap,
        "dense": openmvs and colmap,  # dense needs a sparse model first
        "splat": brush and colmap,
        "single_photo": triposr or trellis,
        "webcam": _webcam_available(),
        "gpu": _gpu_available(),
    }

    missing = [name for name in _PROVISIONING_KEYS if not prov[name]["available"]]

    banners: list[str] = []
    if not capabilities["sparse"]:
        banners.append("Sparse reconstruction unavailable: install pycolmap in the services Python runtime.")
    if openmvs is False:
        banners.append(
            "Dense mesh (OpenMVS) not configured. OpenMVS is AGPL-3.0 -> "
            "install it yourself and set OPENMVS_BIN (subprocess only, never bundled)."
        )
    if not capabilities["splat"]:
        banners.append("3D Gaussian Splatting needs pycolmap and Brush: python scripts/provision_reveng.py.")
    if not capabilities["single_photo"]:
        banners.append("Single-photo tier unavailable: TripoSR/TRELLIS weights not provisioned.")
    if not cad_recode:
        banners.append(
            "ML parametric (CAD-Recode) unavailable: set CAD_RECODE_WEIGHTS or "
            "snapshot_download('filapro/cad-recode-v1.5') (~4GB VRAM bf16)."
        )

    return {
        "ok": True,
        "capabilities": capabilities,
        "provisioning": {k: prov[k]["available"] for k in _PROVISIONING_KEYS},
        "provisioning_detail": prov,
        # Additive discovery (not a frozen §8.1 provisioning key — like trellis).
        "cad_recode": {
            "available": cad_recode,
            "model": "filapro/cad-recode-v1.5",
            "env_var": "CAD_RECODE_WEIGHTS",
            "license": "CC-BY-NC-4.0",
        },
        "banners": banners,
        "missing": missing,
    }
