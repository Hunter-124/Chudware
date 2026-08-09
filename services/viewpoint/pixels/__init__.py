"""Rendered-pixel observation corpus for viewpoint encoders."""

from .agreement import RendererAgreement, compare_with_engine
from .corpus import generate_pixel_corpus, generate_visibility_audit_corpus, locked_part_indices
from .raster import RASTERIZER_VERSION, RasterFrame, rasterize
from .schema import (
    COMPONENTS,
    PIXEL_SCHEMA_VERSION,
    PixelCorpus,
    PixelCorpusConfig,
    PixelInputs,
    PixelTargets,
)

__all__ = [
    "COMPONENTS",
    "PIXEL_SCHEMA_VERSION",
    "RASTERIZER_VERSION",
    "PixelCorpus",
    "PixelCorpusConfig",
    "PixelInputs",
    "PixelTargets",
    "RasterFrame",
    "RendererAgreement",
    "compare_with_engine",
    "generate_pixel_corpus",
    "generate_visibility_audit_corpus",
    "locked_part_indices",
    "rasterize",
]
