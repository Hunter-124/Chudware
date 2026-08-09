from __future__ import annotations

import base64
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest

from services.manufacturing.metrology.transform import EdgeSource, Pose, TransformEdge, TransformGraph
from services.viewpoint.feedback.schema import (
    CameraPose as FeedbackCameraPose,
    CandidatePose,
    CandidateSetRequest,
    CandidateSetResponse,
)
from services.viewpoint.geometry import CameraPose
from services.viewpoint.render import (
    EXACT_CAMERA_CAPABILITY,
    RENDERER_UNAVAILABLE_REFUSAL,
    UNSUPPORTED_CAMERA_REFUSAL,
    RenderProvenance,
    RenderRefusal,
    RenderRequest,
    RenderedImage,
    WorkerRpcRenderBackend,
    pose_to_physical_instruction,
    render_candidate_sheet,
)

GEOMETRY_HASH = "a" * 64
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def geometry_pose() -> CameraPose:
    return CameraPose(
        position=(4.0, 2.0, 3.0),
        target=(0.0, 0.0, 0.5),
        up=(0.0, 0.0, 1.0),
        vertical_fov_deg=52.0,
        image_width_px=800,
        image_height_px=600,
        near=0.25,
        far=250.0,
    )


def candidate_request(seed: int = 1729, request_id: str = "request-one") -> CandidateSetRequest:
    poses = (
        CandidatePose("view-a", FeedbackCameraPose((4, 2, 3), (0, 0, 0), (0, 0, 1), 45)),
        CandidatePose("view-b", FeedbackCameraPose((-3, 4, 2), (0, 0, 0), (0, 0, 1), 50)),
        CandidatePose("view-c", FeedbackCameraPose((1, -5, 2), (0, 0, 0), (0, 0, 1), 40)),
    )
    return CandidateSetRequest(
        request_id=request_id,
        geometry_hash=GEOMETRY_HASH,
        feature="counterbore-7",
        task_text="Determine whether the counterbore shoulder is complete.",
        candidate_poses=poses,
        randomization_seed=seed,
    )


class FakeRenderBackend:
    def render(self, request: RenderRequest) -> RenderedImage:
        provenance = RenderProvenance(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            request_sha256=request.digest,
            backend_name="fake-renderer",
            backend_version="test-1",
            rpc_method="doc_render_view",
            capability=EXACT_CAMERA_CAPABILITY,
            doc_id=request.doc_id,
            geometry_hash=request.geometry_hash,
            document_revision=7,
        )
        return RenderedImage(
            PNG_1X1,
            f"artifact://candidate/{request.request_id}/{request.candidate_id}",
            "image/png",
            provenance,
        )


def test_pose_to_request_round_trip_preserves_exact_camera(tmp_path: Path) -> None:
    request = RenderRequest(
        request_id="offer-7",
        candidate_id="pose-b",
        doc_id="doc-4",
        geometry_hash=GEOMETRY_HASH,
        feature="hole-2",
        pose=geometry_pose(),
        output_path=str(tmp_path / "pose-b.png"),
    )
    restored = RenderRequest.from_dict(request.to_dict())
    rpc_restored = RenderRequest.from_worker_rpc(request.to_worker_rpc())
    assert restored.to_dict() == request.to_dict()
    assert rpc_restored.to_dict() == request.to_dict()
    call = request.to_worker_rpc()
    assert call.method == "doc_render_view"
    assert call.params["view"]["camera"]["projection"] == "perspective"
    assert call.params["view"]["camera"]["position"] == [4.0, 2.0, 3.0]
    assert call.params["view"]["width"] == 800
    assert call.params["annotate"] is False


def test_worker_rpc_adapter_requires_exact_capability(tmp_path: Path) -> None:
    backend = WorkerRpcRenderBackend(lambda _method, _params: {})
    request = RenderRequest("r", "c", "doc", GEOMETRY_HASH, "hole", geometry_pose(),
                            str(tmp_path / "candidate.png"))
    with pytest.raises(RenderRefusal) as refused:
        backend.render(request)
    assert refused.value.code == UNSUPPORTED_CAMERA_REFUSAL


def test_worker_rpc_adapter_accepts_fake_exact_camera_backend(tmp_path: Path) -> None:
    def fake_call(method, params):
        assert method == "doc_render_view"
        Path(params["out_path"]).write_bytes(PNG_1X1)
        camera = dict(params["view"]["camera"])
        camera.pop("projection")
        camera["image_width_px"] = params["view"]["width"]
        camera["image_height_px"] = params["view"]["height"]
        return {
            "ok": True,
            "path": params["out_path"],
            "camera": camera,
            "capability": EXACT_CAMERA_CAPABILITY,
            "revision": 12,
        }

    backend = WorkerRpcRenderBackend(
        fake_call,
        capabilities=(EXACT_CAMERA_CAPABILITY,),
        backend_name="fake-worker",
        backend_version="test-2",
    )
    request = RenderRequest("r", "c", "doc", GEOMETRY_HASH, "hole", geometry_pose(),
                            str(tmp_path / "candidate.png"))
    result = backend.render(request)
    assert result.content == PNG_1X1
    assert result.provenance.document_revision == 12
    assert result.provenance.request_sha256 == request.digest


def test_candidate_sheet_matches_feedback_schema_and_records_provenance(tmp_path: Path) -> None:
    request = candidate_request()
    sheet = render_candidate_sheet(
        request,
        doc_id="doc-live-1",
        output_dir=tmp_path,
        backend=FakeRenderBackend(),
    )
    payload = sheet.candidate_set.to_dict()
    assert set(payload) == {
        "schema_version", "request_id", "geometry_hash", "feature", "task_text",
        "canonical_candidates", "permutation", "randomization_seed",
    }
    assert CandidateSetResponse.from_dict(payload) == sheet.candidate_set
    assert tuple(item.candidate_id for item in sheet.candidate_set.canonical_candidates) == (
        "view-a", "view-b", "view-c",
    )
    assert all(item.render_sha256 == sha256(PNG_1X1).hexdigest()
               for item in sheet.candidate_set.canonical_candidates)
    provenance = sheet.provenance_dict()
    assert provenance["permutation"] == list(sheet.candidate_set.permutation)
    assert [item["candidate_id"] for item in provenance["renders"]] == [
        "view-a", "view-b", "view-c",
    ]
    assert all(item["capability"] == EXACT_CAMERA_CAPABILITY for item in provenance["renders"])


def test_candidate_permutation_is_recorded_and_reproducible(tmp_path: Path) -> None:
    first = render_candidate_sheet(candidate_request(909, "first"), doc_id="doc",
                                   output_dir=tmp_path / "first", backend=FakeRenderBackend())
    second = render_candidate_sheet(candidate_request(909, "second"), doc_id="doc",
                                    output_dir=tmp_path / "second", backend=FakeRenderBackend())
    assert first.candidate_set.randomization_seed == 909
    assert first.candidate_set.permutation == second.candidate_set.permutation
    assert tuple(item.candidate_id for item in first.candidate_set.ordered_candidates) == tuple(
        first.candidate_set.canonical_candidates[index].candidate_id
        for index in first.candidate_set.permutation
    )


def test_missing_renderer_is_named_refusal(tmp_path: Path) -> None:
    with pytest.raises(RenderRefusal) as refused:
        render_candidate_sheet(candidate_request(), doc_id="doc", output_dir=tmp_path, backend=None)
    assert refused.value.code == RENDERER_UNAVAILABLE_REFUSAL


def test_pose_to_physical_instruction_matches_hand_computed_transform() -> None:
    # Fixture -> machine: rotate +90 degrees about Z, then translate by (100, 200, 300) mm.
    graph = TransformGraph()
    graph.add_edge(TransformEdge(
        id="fixture-registration-9",
        source_frame="fixture:vise-1",
        target_frame="machine",
        pose=Pose((100.0, 200.0, 300.0), (0.0, 0.0, 2**-0.5, 2**-0.5)),
        covariance=tuple(tuple(0.0 for _ in range(6)) for _ in range(6)),
        source=EdgeSource.PROBED,
        revision=9,
    ))
    camera = CameraPose(
        position=(10.0, 0.0, 5.0),
        target=(0.0, 0.0, 5.0),
        up=(0.0, 0.0, 1.0),
        vertical_fov_deg=45.0,
        image_width_px=640,
        image_height_px=480,
        near=0.1,
        far=100.0,
    )
    instruction = pose_to_physical_instruction(
        camera,
        transforms=graph,
        fixture_frame="fixture:vise-1",
        machine_frame="machine",
        units_to_mm=1.0,
    )
    assert instruction.position_machine_mm == pytest.approx((100.0, 210.0, 305.0))
    assert instruction.target_machine_mm == pytest.approx((100.0, 200.0, 305.0))
    assert instruction.aim_direction_machine == pytest.approx((0.0, -1.0, 0.0))
    assert instruction.up_direction_machine == pytest.approx((0.0, 0.0, 1.0))
    assert instruction.working_distance_mm == pytest.approx(10.0)
    assert instruction.transform_edge_ids == ("fixture-registration-9",)
    # Camera local -Z is the documented optical axis; its transformed direction must be the aim.
    local_optical_tip = instruction.camera_pose_machine.apply((0.0, 0.0, -1.0))
    optical_direction = np.asarray(local_optical_tip) - np.asarray(instruction.position_machine_mm)
    assert optical_direction == pytest.approx(instruction.aim_direction_machine)
