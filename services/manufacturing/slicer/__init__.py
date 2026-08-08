"""Out-of-process additive slicer contract.

This package deliberately communicates with slicers only through files and a CLI.
It does not import or link slicer implementation libraries.
"""

from .contract import (
    Artifact,
    Determinism,
    ModelRef,
    PerObjectStats,
    ProfileRef,
    SliceProvenance,
    SliceRefusal,
    SliceRequest,
    SliceResult,
)
from .sidecar import invoke_sidecar

__all__ = [
    "Artifact",
    "Determinism",
    "ModelRef",
    "PerObjectStats",
    "ProfileRef",
    "SliceProvenance",
    "SliceRefusal",
    "SliceRequest",
    "SliceResult",
    "invoke_sidecar",
]
