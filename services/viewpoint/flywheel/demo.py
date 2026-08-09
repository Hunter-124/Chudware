"""Hardware-free demo inputs for the explicit flywheel CLI."""
from __future__ import annotations

import base64
from pathlib import Path

from services.viewpoint.feedback import CameraPose, CandidatePose, CandidateSetRequest
from services.viewpoint.render import (
    EXACT_CAMERA_CAPABILITY,
    RenderProvenance,
    RenderRequest,
    RenderedImage,
)

from .loop import FlywheelJob
from .schema import AnswerKey

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class DemoRenderBackend:
    """Writes deterministic PNGs while exercising the real render-sheet assembler."""

    def render(self, request: RenderRequest) -> RenderedImage:
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_PNG_1X1)
        provenance = RenderProvenance(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            request_sha256=request.digest,
            backend_name="flywheel-demo-renderer",
            backend_version="1",
            rpc_method="doc_render_view",
            capability=EXACT_CAMERA_CAPABILITY,
            doc_id=request.doc_id,
            geometry_hash=request.geometry_hash,
            document_revision=1,
        )
        return RenderedImage(_PNG_1X1, output.resolve().as_uri(), "image/png", provenance)


def demo_job(*, seed: int = 1729, request_id: str = "flywheel-demo-1729") -> FlywheelJob:
    poses = (
        CandidatePose("view-a", CameraPose((4, 2, 3), (0, 0, 0), (0, 0, 1), 45)),
        CandidatePose("view-b", CameraPose((-3, 4, 2), (0, 0, 0), (0, 0, 1), 50)),
        CandidatePose("view-c", CameraPose((1, -5, 2), (0, 0, 0), (0, 0, 1), 40)),
    )
    request = CandidateSetRequest(
        request_id=request_id,
        geometry_hash="a" * 64,
        feature="counterbore-7",
        task_text="Is the counterbore shoulder complete? Choices: yes | no",
        candidate_poses=poses,
        randomization_seed=seed,
    )
    return FlywheelJob(
        request=request,
        doc_id="flywheel-demo-doc",
        answer_key=AnswerKey(("yes",)),
        question_provenance={
            "source": "flywheel_cli_demo",
            "question_id": "demo-counterbore-complete",
        },
        candidate_oracle={
            "view-a": {"aggregate": 0.90, "components": {"visibility": 0.95}},
            "view-b": {"aggregate": 0.55, "components": {"visibility": 0.60}},
            "view-c": {"aggregate": 0.25, "components": {"visibility": 0.30}},
        },
    )
