"""Render candidate poses and seal them into the Stage-2 feedback schema."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from services.viewpoint.feedback.protocol import build_candidate_set
from services.viewpoint.feedback.schema import (
    CandidateSetRequest,
    CandidateSetResponse,
    RenderCandidate,
)
from services.viewpoint.geometry import CameraPose

from .contract import (
    RENDERER_UNAVAILABLE_REFUSAL,
    RenderBackend,
    RenderProvenance,
    RenderRefusal,
    RenderRequest,
)


@dataclass(frozen=True, slots=True)
class RenderSettings:
    image_width_px: int = 640
    image_height_px: int = 480
    near: float = 0.01
    far: float = 1000.0

    def __post_init__(self) -> None:
        if type(self.image_width_px) is not int or type(self.image_height_px) is not int:
            raise ValueError("image dimensions must be integers")
        if not (64 <= self.image_width_px <= 2048 and 64 <= self.image_height_px <= 2048):
            raise ValueError("image dimensions must be in doc_render_view's [64, 2048] range")
        if not 0.0 < float(self.near) < float(self.far):
            raise ValueError("render clipping range must satisfy 0 < near < far")


@dataclass(frozen=True, slots=True)
class CandidateSheet:
    """Feedback-ready candidate set plus a separate, non-schema provenance manifest."""

    candidate_set: CandidateSetResponse
    render_requests: tuple[RenderRequest, ...]
    render_provenance: tuple[RenderProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_set, CandidateSetResponse):
            raise ValueError("candidate_set must be CandidateSetResponse")
        object.__setattr__(self, "render_requests", tuple(self.render_requests))
        object.__setattr__(self, "render_provenance", tuple(self.render_provenance))
        ids = tuple(item.candidate_id for item in self.candidate_set.canonical_candidates)
        if tuple(item.candidate_id for item in self.render_requests) != ids:
            raise ValueError("render requests are not in canonical candidate order")
        if tuple(item.candidate_id for item in self.render_provenance) != ids:
            raise ValueError("render provenance is not in canonical candidate order")

    def provenance_dict(self) -> dict[str, Any]:
        """Out-of-band provenance; the feedback payload itself remains byte-for-schema exact."""
        return {
            "request_id": self.candidate_set.request_id,
            "geometry_hash": self.candidate_set.geometry_hash,
            "randomization_seed": self.candidate_set.randomization_seed,
            "permutation": list(self.candidate_set.permutation),
            "renders": [item.to_dict() for item in self.render_provenance],
        }


def _geometry_pose(candidate_pose: Any, settings: RenderSettings) -> CameraPose:
    pose = candidate_pose.pose
    return CameraPose(
        position=pose.eye,
        target=pose.target,
        up=pose.up,
        vertical_fov_deg=pose.fov_y_degrees,
        image_width_px=settings.image_width_px,
        image_height_px=settings.image_height_px,
        near=settings.near,
        far=settings.far,
    )


def _candidate_filename(index: int, candidate_id: str) -> str:
    digest = sha256(candidate_id.encode("utf-8")).hexdigest()[:16]
    return f"candidate-{index:04d}-{digest}.png"


def render_candidate_sheet(request: CandidateSetRequest, *, doc_id: str,
                           output_dir: str | Path, backend: RenderBackend | None,
                           settings: RenderSettings = RenderSettings()) -> CandidateSheet:
    """Render every canonical pose, then apply feedback's seeded presentation permutation.

    The operation is all-or-nothing: a missing/failed candidate raises a named refusal and no
    ``CandidateSetResponse`` is returned. Existing files may remain as diagnostic artifacts, but
    they are never represented as a complete sheet.
    """
    if not isinstance(request, CandidateSetRequest):
        raise ValueError("request must be CandidateSetRequest")
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise ValueError("doc_id must be a non-empty string")
    if backend is None:
        raise RenderRefusal(RENDERER_UNAVAILABLE_REFUSAL,
                            "candidate rendering requires an available renderer backend")
    if not isinstance(settings, RenderSettings):
        raise ValueError("settings must be RenderSettings")

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    requests: list[RenderRequest] = []
    rendered: list[RenderCandidate] = []
    provenance: list[RenderProvenance] = []
    for index, candidate in enumerate(request.candidate_poses):
        render_request = RenderRequest(
            request_id=request.request_id,
            candidate_id=candidate.candidate_id,
            doc_id=doc_id,
            geometry_hash=request.geometry_hash,
            feature=request.feature,
            pose=_geometry_pose(candidate, settings),
            output_path=str(root / _candidate_filename(index, candidate.candidate_id)),
            annotate=False,
        )
        image = backend.render(render_request)
        if image.provenance.request_id != request.request_id:
            raise RenderRefusal("viewpoint_render_provenance_mismatch",
                                "render provenance request_id does not match the candidate sheet")
        if image.provenance.candidate_id != candidate.candidate_id:
            raise RenderRefusal("viewpoint_render_provenance_mismatch",
                                "render provenance candidate_id does not match the rendered pose")
        if image.provenance.request_sha256 != render_request.digest:
            raise RenderRefusal("viewpoint_render_provenance_mismatch",
                                "render provenance request digest does not match the rendered pose")
        render_hash = sha256(image.content).hexdigest()
        requests.append(render_request)
        provenance.append(image.provenance)
        rendered.append(RenderCandidate(
            candidate_id=candidate.candidate_id,
            pose=candidate.pose,
            render_uri=image.uri,
            render_sha256=render_hash,
        ))

    candidate_set = build_candidate_set(request, rendered)
    return CandidateSheet(candidate_set, tuple(requests), tuple(provenance))
