"""Bridge analytic viewpoint poses to engine renders and physical camera instructions."""

from .contract import (
    EXACT_CAMERA_CAPABILITY,
    INVALID_RENDER_REFUSAL,
    RENDERER_UNAVAILABLE_REFUSAL,
    UNSUPPORTED_CAMERA_REFUSAL,
    RenderBackend,
    RenderProvenance,
    RenderRefusal,
    RenderRequest,
    RenderedImage,
    WorkerRpcCall,
    WorkerRpcRenderBackend,
)
from .physical import PhysicalCameraInstruction, pose_to_physical_instruction
from .sheet import CandidateSheet, RenderSettings, render_candidate_sheet

__all__ = (
    "CandidateSheet",
    "EXACT_CAMERA_CAPABILITY",
    "INVALID_RENDER_REFUSAL",
    "PhysicalCameraInstruction",
    "RENDERER_UNAVAILABLE_REFUSAL",
    "RenderBackend",
    "RenderProvenance",
    "RenderRefusal",
    "RenderRequest",
    "RenderSettings",
    "RenderedImage",
    "UNSUPPORTED_CAMERA_REFUSAL",
    "WorkerRpcCall",
    "WorkerRpcRenderBackend",
    "pose_to_physical_instruction",
    "render_candidate_sheet",
)
