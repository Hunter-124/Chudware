"""Advisory-only manufacturing perception primitives.

This package never owns machine offsets, gates, or safety decisions.
"""

from .config import ModelSpec
from .packet import build_evidence_packet, validate_evidence_packet
from .reference import NumpyMLDP, surprise_score

__all__ = [
    "ModelSpec",
    "NumpyMLDP",
    "build_evidence_packet",
    "surprise_score",
    "validate_evidence_packet",
]
