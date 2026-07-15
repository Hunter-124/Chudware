"""Single-image → 3D mesh tier (TripoSR low / TRELLIS.2 high).

Implements the ``single_photo`` capability described in contracts/reveng.md §0.
Any mesh produced is FORCE-TAGGED ``honesty: "approximate"`` in the session
manifest.  Callers that want to feed the mesh into parametric fitting MUST pass
``allow_approximate=True`` explicitly; without the flag this module raises
``ApproximateMeshGuardError`` to prevent silent injection of unreliable geometry
into the parametric pipeline.

Models are download-on-demand and NOT bundled.  When weights are absent the
module returns the degradation envelope:
    {"ok": False, "reason": "backend_not_provisioned", "hint": "<message>"}

Honesty tiers (contracts/reveng.md):
  - Output mesh ``honesty`` field is always ``"approximate"``.
  - No field is ever set to ``"fitted"`` or ``"high"`` by this module.

License: TripoSR is MIT, TRELLIS is MIT.  Neither is linked; they are invoked
as subprocess / optional import only, conditioned on weight availability.
"""
from __future__ import annotations

import os
import importlib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public sentinel / error
# ---------------------------------------------------------------------------

class ApproximateMeshGuardError(RuntimeError):
    """Raised when an approximate mesh is passed to the parametric path without
    the explicit ``allow_approximate=True`` override flag.
    """


# The honesty tag that MUST appear on every mesh produced by this module.
APPROXIMATE_TAG = "approximate"

# Reason code returned when backend weights are absent.
BACKEND_NOT_PROVISIONED = "backend_not_provisioned"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _backend_available() -> tuple[bool, str]:
    """Return ``(available, backend_name)`` without any network I/O.

    Checks via reveng_provision._artifact_available which reads env vars +
    filesystem only.  Never imports the heavy ML frameworks.
    """
    from ..vision.reveng_provision import _artifact_available  # type: ignore[import]

    if _artifact_available("trellis"):
        return True, "trellis"
    if _artifact_available("triposr"):
        return True, "triposr"
    return False, ""


def _not_provisioned_envelope(backend_name: str = "") -> dict[str, Any]:
    hint = (
        "Set TRELLIS_WEIGHTS or TRIPOSR_WEIGHTS to the path of the downloaded "
        "model checkpoint and retry.  Use reveng_status() to check full "
        "provisioning state."
    )
    return {
        "ok": False,
        "reason": BACKEND_NOT_PROVISIONED,
        "hint": hint,
        "backend_attempted": backend_name or None,
    }


def _tag_approximate(result: dict[str, Any]) -> dict[str, Any]:
    """Ensure ``honesty: "approximate"`` is set on *result* (mutates in place,
    also returns it so callers can use the return value).
    """
    result["honesty"] = APPROXIMATE_TAG
    return result


# ---------------------------------------------------------------------------
# Guard: refuse to feed approximate mesh into parametric path
# ---------------------------------------------------------------------------

def assert_not_approximate(
    mesh_result: dict[str, Any],
    *,
    allow_approximate: bool = False,
    context: str = "parametric fitting",
) -> None:
    """Raise ``ApproximateMeshGuardError`` if *mesh_result* is approximate and
    ``allow_approximate`` is not ``True``.

    Usage::

        mesh = reconstruct_single_photo(image_path, session_id)
        assert_not_approximate(mesh, allow_approximate=False)  # raises
        assert_not_approximate(mesh, allow_approximate=True)   # ok, user explicit

    Args:
        mesh_result: The dict returned by :func:`reconstruct_single_photo`.
        allow_approximate: Must be ``True`` to permit an approximate mesh into
            the guarded path.  Default ``False`` — the safe default that
            enforces the contract.
        context: Human-readable description of the guarded path (for the error
            message).
    """
    if mesh_result.get("honesty") == APPROXIMATE_TAG and not allow_approximate:
        raise ApproximateMeshGuardError(
            f"Mesh is tagged honesty='{APPROXIMATE_TAG}' and cannot be fed into "
            f"{context} without an explicit allow_approximate=True override.  "
            "Review the mesh visually before enabling the override."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reconstruct_single_photo(
    image_path: str | os.PathLike,
    session_id: str,
    *,
    quality: str = "low",
    output_dir: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Reconstruct a 3-D mesh from a single photograph.

    Args:
        image_path: Path to the input JPEG or PNG image.
        session_id: RE session identifier (``re_`` + 8 hex chars).
        quality: ``"low"`` → TripoSR (fast, ~5 s on GPU);
                 ``"high"`` → TRELLIS.2 (slower, higher fidelity).
                 Falls back to whichever backend is provisioned.
        output_dir: Directory to write the output mesh.  Defaults to
            ``$AICAD_REVENG_DIR/<session_id>/mesh/``.

    Returns:
        On success::

            {
                "ok": True,
                "session_id": "<id>",
                "mesh_path": "<path>",
                "backend": "triposr" | "trellis",
                "honesty": "approximate",   # ALWAYS
                "quality": "low" | "high",
            }

        On missing weights::

            {
                "ok": False,
                "reason": "backend_not_provisioned",
                "hint": "<human-readable fix>",
                "backend_attempted": None,
            }

        On other errors::

            {
                "ok": False,
                "reason": "<error-class>",
                "hint": "<message>",
                "backend_attempted": "<backend>",
                "honesty": "approximate",   # still tagged even on failure
            }

    Raises:
        Nothing — all errors are returned as the degradation envelope.
    """
    available, backend_name = _backend_available()
    if not available:
        return _not_provisioned_envelope()

    # Resolve output directory
    if output_dir is None:
        reveng_dir = os.environ.get(
            "AICAD_REVENG_DIR",
            os.path.join(
                os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                "AI-CAD", "reveng",
            ),
        )
        out_dir = Path(reveng_dir) / session_id / "mesh"
    else:
        out_dir = Path(output_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = Path(image_path)

    # Select backend: prefer the quality hint but fall back gracefully.
    from ..vision.reveng_provision import _artifact_available  # noqa: PLC0415

    if quality == "high" and _artifact_available("trellis"):
        selected = "trellis"
    elif quality == "low" and _artifact_available("triposr"):
        selected = "triposr"
    else:
        # Fall back to whatever is available.
        selected = backend_name

    try:
        mesh_path = _run_backend(selected, image_path, out_dir, session_id)
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "reason": type(exc).__name__,
            "hint": str(exc),
            "backend_attempted": selected,
        }
        return _tag_approximate(result)

    result = {
        "ok": True,
        "session_id": session_id,
        "mesh_path": str(mesh_path),
        "backend": selected,
        "quality": quality,
    }
    return _tag_approximate(result)


# ---------------------------------------------------------------------------
# Backend dispatch (lazy import — heavy ML libs optional)
# ---------------------------------------------------------------------------

def _run_backend(
    backend: str,
    image_path: Path,
    out_dir: Path,
    session_id: str,
) -> Path:
    """Invoke the selected backend and return the path to the output mesh.

    Raises on failure so the caller can wrap in the degradation envelope.
    """
    if backend == "triposr":
        return _run_triposr(image_path, out_dir, session_id)
    if backend == "trellis":
        return _run_trellis(image_path, out_dir, session_id)
    raise ValueError(f"Unknown backend: {backend!r}")


def _run_triposr(image_path: Path, out_dir: Path, session_id: str) -> Path:
    """Invoke TripoSR weights to produce a mesh.

    We attempt a lazy import of the ``tsr`` package (installed alongside the
    weights). If the package is absent we fall back to a subprocess call using
    the weights file directly (future integration point).
    """
    weights_path = os.environ.get("TRIPOSR_WEIGHTS", "")

    # Attempt lazy package import (optional heavy dep)
    try:
        tsr = importlib.import_module("tsr")
    except ImportError:
        tsr = None

    if tsr is not None:
        # Package available: use its API
        output_mesh = out_dir / f"{session_id}_triposr.obj"
        model = tsr.models.TSR.from_pretrained(
            weights_path or "stabilityai/TripoSR",
            config_name="config.yaml",
            weight_name="model.ckpt",
        )
        model.renderer.set_chunk_size(8192)
        import PIL.Image  # type: ignore[import]
        image = PIL.Image.open(image_path).convert("RGB")
        scene_codes = model([image], device="cpu")
        meshes = model.extract_mesh(scene_codes, resolution=128)
        meshes[0].export(str(output_mesh))
        return output_mesh

    # No package: raise so caller surfaces as error envelope (weights present
    # but tsr package not installed — user needs pip install tsr).
    raise RuntimeError(
        "TripoSR weights are provisioned but the 'tsr' Python package is not "
        "installed.  Install it alongside the weights: pip install tsr"
    )


def _run_trellis(image_path: Path, out_dir: Path, session_id: str) -> Path:
    """Invoke TRELLIS weights to produce a mesh."""
    weights_path = os.environ.get("TRELLIS_WEIGHTS", "")

    try:
        trellis = importlib.import_module("trellis")
    except ImportError:
        trellis = None

    if trellis is not None:
        output_mesh = out_dir / f"{session_id}_trellis.glb"
        pipeline = trellis.pipelines.TrellisImageTo3DPipeline.from_pretrained(
            weights_path or "microsoft/TRELLIS-image-large"
        )
        import PIL.Image  # type: ignore[import]
        image = PIL.Image.open(image_path).convert("RGB")
        outputs = pipeline.run(image, seed=42)
        glb = outputs["mesh"][0].export_glb()
        output_mesh.write_bytes(glb)
        return output_mesh

    raise RuntimeError(
        "TRELLIS weights are provisioned but the 'trellis' Python package is not "
        "installed.  Install it alongside the weights."
    )
