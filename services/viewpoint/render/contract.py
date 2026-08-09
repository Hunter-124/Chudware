"""Exact-pose request contract over the existing ``doc_render_view`` RPC seam."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

import numpy as np

from services.viewpoint.geometry import CameraPose, camera_from_dict

REQUEST_SCHEMA = "chudware.viewpoint.render_request/1"
RPC_METHOD = "doc_render_view"
EXACT_CAMERA_CAPABILITY = "doc_render_view.camera_pose.v1"
RENDERER_UNAVAILABLE_REFUSAL = "viewpoint_renderer_unavailable"
UNSUPPORTED_CAMERA_REFUSAL = "viewpoint_exact_camera_unsupported"
INVALID_RENDER_REFUSAL = "viewpoint_render_invalid"
_RENDER_FAILED_REFUSAL = "viewpoint_render_failed"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _geometry_hash(value: object) -> str:
    text = _nonempty(value, "geometry_hash")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("geometry_hash must be a lowercase SHA-256 digest")
    return text


class RenderRefusal(RuntimeError):
    """A named, machine-checkable refusal; never substitute pixels for this failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = _nonempty(code, "code")


@dataclass(frozen=True, slots=True)
class WorkerRpcCall:
    method: str
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        _nonempty(self.method, "method")
        object.__setattr__(self, "params", dict(self.params))


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """One exact perspective camera request, plus preference-data provenance."""

    request_id: str
    candidate_id: str
    doc_id: str
    geometry_hash: str
    feature: str
    pose: CameraPose
    output_path: str
    annotate: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.request_id, "request_id")
        _nonempty(self.candidate_id, "candidate_id")
        _nonempty(self.doc_id, "doc_id")
        _geometry_hash(self.geometry_hash)
        _nonempty(self.feature, "feature")
        if not isinstance(self.pose, CameraPose):
            raise ValueError("pose must be services.viewpoint.geometry.CameraPose")
        if not (64 <= self.pose.image_width_px <= 2048
                and 64 <= self.pose.image_height_px <= 2048):
            raise ValueError("image dimensions must be in doc_render_view's [64, 2048] range")
        _nonempty(self.output_path, "output_path")
        if type(self.annotate) is not bool:
            raise ValueError("annotate must be a bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "doc_id": self.doc_id,
            "geometry_hash": self.geometry_hash,
            "feature": self.feature,
            "pose": self.pose.as_dict(),
            "output_path": self.output_path,
            "annotate": self.annotate,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RenderRequest":
        expected = {
            "schema", "request_id", "candidate_id", "doc_id", "geometry_hash", "feature",
            "pose", "output_path", "annotate",
        }
        if set(value) != expected:
            missing = sorted(expected - set(value))
            unknown = sorted(set(value) - expected)
            raise ValueError(f"invalid RenderRequest fields (missing={missing}, unknown={unknown})")
        if value["schema"] != REQUEST_SCHEMA:
            raise ValueError(f"unsupported render request schema {value['schema']!r}")
        pose_value = value["pose"]
        if not isinstance(pose_value, dict):
            raise ValueError("pose must be an object")
        return cls(
            request_id=value["request_id"],
            candidate_id=value["candidate_id"],
            doc_id=value["doc_id"],
            geometry_hash=value["geometry_hash"],
            feature=value["feature"],
            pose=camera_from_dict(pose_value),
            output_path=value["output_path"],
            annotate=value["annotate"],
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_worker_rpc(self) -> WorkerRpcCall:
        """Use ``doc_render_view``; the nested camera object is the required v1 extension."""
        camera = self.pose.as_dict()
        width = camera.pop("image_width_px")
        height = camera.pop("image_height_px")
        return WorkerRpcCall(
            RPC_METHOD,
            {
                "doc_id": self.doc_id,
                "out_path": self.output_path,
                "annotate": self.annotate,
                "view": {
                    "camera": {"projection": "perspective", **camera},
                    "width": width,
                    "height": height,
                },
                "viewpoint": {
                    "schema": REQUEST_SCHEMA,
                    "request_id": self.request_id,
                    "candidate_id": self.candidate_id,
                    "geometry_hash": self.geometry_hash,
                    "feature": self.feature,
                },
            },
        )

    @classmethod
    def from_worker_rpc(cls, call: WorkerRpcCall) -> "RenderRequest":
        if call.method != RPC_METHOD:
            raise ValueError(f"expected {RPC_METHOD!r}, got {call.method!r}")
        params = call.params
        view = params.get("view")
        provenance = params.get("viewpoint")
        if not isinstance(view, Mapping) or not isinstance(provenance, Mapping):
            raise ValueError("view and viewpoint RPC params must be objects")
        camera = view.get("camera")
        if not isinstance(camera, Mapping) or camera.get("projection") != "perspective":
            raise ValueError("view.camera must request perspective projection")
        pose = dict(camera)
        pose.pop("projection")
        pose["image_width_px"] = view.get("width")
        pose["image_height_px"] = view.get("height")
        if provenance.get("schema") != REQUEST_SCHEMA:
            raise ValueError("unsupported viewpoint RPC provenance schema")
        return cls(
            request_id=provenance.get("request_id"),
            candidate_id=provenance.get("candidate_id"),
            doc_id=params.get("doc_id"),
            geometry_hash=provenance.get("geometry_hash"),
            feature=provenance.get("feature"),
            pose=camera_from_dict(pose),
            output_path=params.get("out_path"),
            annotate=params.get("annotate", False),
        )


@dataclass(frozen=True, slots=True)
class RenderProvenance:
    request_id: str
    candidate_id: str
    request_sha256: str
    backend_name: str
    backend_version: str
    rpc_method: str
    capability: str
    doc_id: str
    geometry_hash: str
    document_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "candidate_id": self.candidate_id,
            "request_sha256": self.request_sha256,
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "rpc_method": self.rpc_method,
            "capability": self.capability,
            "doc_id": self.doc_id,
            "geometry_hash": self.geometry_hash,
            "document_revision": self.document_revision,
        }


@dataclass(frozen=True, slots=True)
class RenderedImage:
    content: bytes
    uri: str
    media_type: str
    provenance: RenderProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content.startswith(_PNG_SIGNATURE):
            raise RenderRefusal(INVALID_RENDER_REFUSAL, "renderer did not return a PNG image")
        _nonempty(self.uri, "uri")
        if self.media_type != "image/png":
            raise RenderRefusal(INVALID_RENDER_REFUSAL, "renderer media type must be image/png")
        if not isinstance(self.provenance, RenderProvenance):
            raise ValueError("provenance must be RenderProvenance")


@runtime_checkable
class RenderBackend(Protocol):
    def render(self, request: RenderRequest) -> RenderedImage: ...


RpcCaller = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class WorkerRpcRenderBackend:
    """Adapter for the existing worker call shape after exact-camera capability is advertised."""

    def __init__(self, call: RpcCaller, *, capabilities: tuple[str, ...] = (),
                 backend_name: str = "chudware-worker", backend_version: str = "unknown") -> None:
        if not callable(call):
            raise ValueError("call must be callable")
        self._call = call
        self._capabilities = frozenset(capabilities)
        self._backend_name = _nonempty(backend_name, "backend_name")
        self._backend_version = _nonempty(backend_version, "backend_version")

    @staticmethod
    def _same_pose(echo: object, pose: CameraPose) -> bool:
        if not isinstance(echo, Mapping):
            return False
        try:
            actual = camera_from_dict(dict(echo))
        except (KeyError, TypeError, ValueError):
            return False
        return (
            np.array_equal(actual.position, pose.position)
            and np.array_equal(actual.target, pose.target)
            and np.array_equal(actual.up, pose.up)
            and math.isclose(actual.vertical_fov_deg, pose.vertical_fov_deg, rel_tol=0.0, abs_tol=0.0)
            and actual.image_width_px == pose.image_width_px
            and actual.image_height_px == pose.image_height_px
            and math.isclose(actual.near, pose.near, rel_tol=0.0, abs_tol=0.0)
            and math.isclose(actual.far, pose.far, rel_tol=0.0, abs_tol=0.0)
        )

    def render(self, request: RenderRequest) -> RenderedImage:
        if EXACT_CAMERA_CAPABILITY not in self._capabilities:
            raise RenderRefusal(
                UNSUPPORTED_CAMERA_REFUSAL,
                f"{RPC_METHOD} does not advertise {EXACT_CAMERA_CAPABILITY}",
            )
        call = request.to_worker_rpc()
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self._call(call.method, call.params)
        except Exception as exc:
            raise RenderRefusal(RENDERER_UNAVAILABLE_REFUSAL,
                                f"renderer call failed: {exc}") from exc
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            reason = response.get("reason") if isinstance(response, Mapping) else "invalid response"
            raise RenderRefusal(_RENDER_FAILED_REFUSAL, f"renderer refused request: {reason}")
        if response.get("capability") != EXACT_CAMERA_CAPABILITY:
            raise RenderRefusal(UNSUPPORTED_CAMERA_REFUSAL,
                                "renderer did not confirm the exact-camera capability")
        if not self._same_pose(response.get("camera"), request.pose):
            raise RenderRefusal(INVALID_RENDER_REFUSAL,
                                "renderer camera echo differs from the requested pose")
        if response.get("path") != request.output_path:
            raise RenderRefusal(INVALID_RENDER_REFUSAL,
                                "renderer path echo differs from the requested output path")
        try:
            content = output.read_bytes()
        except OSError as exc:
            raise RenderRefusal(INVALID_RENDER_REFUSAL,
                                f"renderer output is unreadable: {exc}") from exc
        revision = response.get("revision")
        if revision is not None and type(revision) is not int:
            raise RenderRefusal(INVALID_RENDER_REFUSAL, "renderer revision must be an integer or null")
        provenance = RenderProvenance(
            request_id=request.request_id,
            candidate_id=request.candidate_id,
            request_sha256=request.digest,
            backend_name=self._backend_name,
            backend_version=self._backend_version,
            rpc_method=RPC_METHOD,
            capability=EXACT_CAMERA_CAPABILITY,
            doc_id=request.doc_id,
            geometry_hash=request.geometry_hash,
            document_revision=revision,
        )
        return RenderedImage(content, output.resolve().as_uri(), "image/png", provenance)
