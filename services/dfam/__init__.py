"""Deterministic mesh-based Design-for-Additive analysis and orientation ranking.

All lengths are millimetres. Locations are model-frame coordinates, and build
heights are bed-relative. No engine, slicer, network, or undeclared dependencies.
"""
from __future__ import annotations
from .mesh import FaceInfo, PrintMesh, load_mesh_file
from .params import PrintParams, frame_for_build_dir
from .analyze import analyze_printability
from .checks import CHECK_NAMES
from .orient import evaluate_orientation, optimize_orientation

__all__ = ["FaceInfo", "PrintMesh", "PrintParams", "load_mesh_file", "frame_for_build_dir",
           "analyze_printability", "evaluate_orientation", "optimize_orientation", "CHECK_NAMES"]
