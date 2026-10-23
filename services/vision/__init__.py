"""Chudware vision — image ingestion + fiducial metrology (Pillar 10 v1).

Architecture rule: **VLMs pick pixels,
deterministic code measures.** Frontier VLMs read physical scales at only 10–32% accuracy,
so this module never lets a model guess a dimension — a fiducial (ArUco/ChArUco) gives a
calibrated pixel→mm homography and the code does the arithmetic. Every extracted dimension
still flows through the existing intake confidence tiers (``services.intake.intake``); vision
never writes geometry.

Two layers, split by dependency so the MCP server always imports cleanly:

* :mod:`~services.vision.ingest` — pure Pillow (already a runtime dep). Photo-ID registry +
  the downscale pipeline that enforces the agent-facing cap (≤1024 px / <1 MB) and
  keeps a ``resource_link`` to the full-res original.
* :mod:`~services.vision.fiducial` / :mod:`~services.vision.measure` /
  :mod:`~services.vision.calib_sheet` — need OpenCV (``cv2``, Apache-2.0; ArUco lives in core
  ``objdetect``, no contrib/GPU). OpenCV is an **optional** runtime dep: these import without
  it and callers gate on :data:`HAVE_CV2` / catch :class:`VisionUnavailable`, degrading to a
  contract-shaped ``{ok: False, reason, hint}`` — never a traceback (CONTRIBUTING CI rule).
"""
from __future__ import annotations

from .ingest import (
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_PX,
    PhotoRecord,
    PhotoRegistry,
    ingest_image,
)
from .fiducial import HAVE_CV2, VisionUnavailable, require_cv2
from .measure import HONESTY_STRING, MeasureResult, measure_from_photo
from .calib_sheet import DEFAULT_BOARD, BoardSpec, generate_aruco_marker, generate_charuco_sheet

__all__ = [
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_PX",
    "PhotoRecord",
    "PhotoRegistry",
    "ingest_image",
    "HAVE_CV2",
    "VisionUnavailable",
    "require_cv2",
    "HONESTY_STRING",
    "MeasureResult",
    "measure_from_photo",
    "DEFAULT_BOARD",
    "BoardSpec",
    "generate_aruco_marker",
    "generate_charuco_sheet",
]
