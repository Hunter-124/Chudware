"""Optional-Torch pixel encoder feeding the existing candidate-ranking head."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import ModelSpec


@dataclass(frozen=True)
class PixelModelSpec:
    """Static contract for camera-visible inputs to the pixel policy."""

    rgb_channels: int = 3
    use_depth: bool = True
    camera_dim: int = 16
    prior_dim: int = 8
    stem_channels: int = 32
    stage_channels: tuple[int, int, int] = (32, 64, 96)
    dropout: float = 0.10
    ranker: ModelSpec = ModelSpec()

    @property
    def input_channels(self) -> int:
        return self.rgb_channels + (2 if self.use_depth else 0)

    def validate(self) -> "PixelModelSpec":
        if self.rgb_channels != 3:
            raise ValueError("rendered RGB must have exactly three channels")
        if self.camera_dim <= 0 or self.prior_dim <= 0 or self.stem_channels <= 0:
            raise ValueError("pixel model dimensions must be positive")
        if len(self.stage_channels) != 3 or any(value <= 0 for value in self.stage_channels):
            raise ValueError("stage_channels must contain three positive widths")
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.ranker.validate()
        return self


ENCODER_PROVENANCE = {
    "architecture": "repository-original compact residual convolutional trunk",
    "pretrained_weights": None,
    "external_encoder_code": None,
    "torch_runtime_license": "BSD-3-Clause",
    "torch_license_evidence": "PyTorch LICENSE (BSD-3-Clause), https://github.com/pytorch/pytorch/blob/main/LICENSE",
}


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def build_pixel_policy(*, spec: PixelModelSpec | None = None, seed: int = 1729) -> Any:
    """Build a compact residual image trunk wrapped around the existing ranker.

    Each offered camera's rendered pixels are encoded independently. Spatial image
    tokens and one coarse FeatureRegion2D-prior token replace the hand-computed
    geometry tokens. The camera-only descriptor is projected to the ranker's
    existing 48-D candidate contract; the task/query/attention/fusion/ranking head
    itself is unchanged.
    """
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("pixel viewpoint training requires optional Torch") from exc

    from .torch_model import build_torch_policy

    actual = (spec or PixelModelSpec()).validate()
    torch.manual_seed(seed)

    class ResidualBlock(torch.nn.Module):
        def __init__(self, channels: int) -> None:
            super().__init__()
            groups = min(8, channels)
            while channels % groups:
                groups -= 1
            self.conv1 = torch.nn.Conv2d(channels, channels, 3, padding=1, bias=False)
            self.norm1 = torch.nn.GroupNorm(groups, channels)
            self.conv2 = torch.nn.Conv2d(channels, channels, 3, padding=1, bias=False)
            self.norm2 = torch.nn.GroupNorm(groups, channels)

        def forward(self, value: Any) -> Any:
            residual = value
            value = torch.nn.functional.silu(self.norm1(self.conv1(value)))
            value = self.norm2(self.conv2(value))
            return torch.nn.functional.silu(value + residual)

    class PixelViewpointPolicy(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            widths = actual.stage_channels
            self.stem = torch.nn.Sequential(
                torch.nn.Conv2d(actual.input_channels, actual.stem_channels, 5, stride=2, padding=2, bias=False),
                torch.nn.GroupNorm(8, actual.stem_channels),
                torch.nn.SiLU(),
            )
            stages: list[Any] = []
            previous = actual.stem_channels
            for width in widths:
                if width != previous:
                    stages.extend((
                        torch.nn.Conv2d(previous, width, 3, stride=2, padding=1, bias=False),
                        torch.nn.GroupNorm(8, width),
                        torch.nn.SiLU(),
                    ))
                stages.extend((ResidualBlock(width), ResidualBlock(width)))
                previous = width
            self.trunk = torch.nn.Sequential(*stages)
            self.token_projection = torch.nn.Conv2d(previous, actual.ranker.geometry_dim, 1)
            self.spatial_dropout = torch.nn.Dropout2d(actual.dropout)
            self.prior_projection = torch.nn.Sequential(
                torch.nn.Linear(actual.prior_dim, 32),
                torch.nn.SiLU(),
                torch.nn.Linear(32, actual.ranker.geometry_dim),
            )
            self.camera_projection = torch.nn.Sequential(
                torch.nn.Linear(actual.camera_dim, actual.ranker.candidate_dim),
                torch.nn.SiLU(),
                torch.nn.Linear(actual.ranker.candidate_dim, actual.ranker.candidate_dim),
            )
            self.ranker = build_torch_policy(spec=actual.ranker, seed=seed)

        @staticmethod
        def _image_channels(rgb: Any, depth: Any | None, depth_mask: Any | None) -> Any:
            if rgb.ndim != 5 or rgb.shape[-1] != 3:
                raise ValueError("rgb must have shape [batch,candidate,height,width,3]")
            image = rgb.float().permute(0, 1, 4, 2, 3)
            if rgb.dtype == torch.uint8:
                image = image / 255.0
            elif not torch.is_floating_point(rgb):
                raise ValueError("rgb must be uint8 or normalized floating point")
            image = image * 2.0 - 1.0
            if not actual.use_depth:
                if depth is not None or depth_mask is not None:
                    raise ValueError("depth inputs supplied to an RGB-only policy")
                return image
            if depth is None or depth_mask is None:
                raise ValueError("depth and depth_mask are required by this policy")
            if depth.shape != rgb.shape[:2] + rgb.shape[2:4] or depth_mask.shape != depth.shape:
                raise ValueError("depth tensors must match the RGB batch/candidate/spatial axes")
            mask = depth_mask.bool()
            finite_depth = torch.where(mask, depth.float(), torch.zeros_like(depth, dtype=torch.float32))
            if not bool(torch.isfinite(finite_depth).all()):
                raise ValueError("finite depth values are required where depth_mask is true")
            return torch.cat((image, finite_depth[:, :, None], mask.float()[:, :, None]), dim=2)

        def encode_observation(
            self,
            rgb: Any,
            coarse_prior: Any,
            *,
            depth: Any | None = None,
            depth_mask: Any | None = None,
        ) -> tuple[Any, Any, Any, Any]:
            image = self._image_channels(rgb, depth, depth_mask)
            batch, candidates = image.shape[:2]
            if coarse_prior.shape != (batch, candidates, actual.prior_dim):
                raise ValueError("coarse_prior violates PixelModelSpec")
            encoded = self.spatial_dropout(
                self.token_projection(self.trunk(self.stem(image.reshape(-1, *image.shape[2:]))))
            )
            spatial = encoded.flatten(2).transpose(1, 2)
            prior = self.prior_projection(coarse_prior.float().reshape(-1, actual.prior_dim))[:, None, :]
            geometry = torch.cat((spatial, prior), dim=1)
            geometry_mask = torch.ones(geometry.shape[:2], dtype=torch.bool, device=geometry.device)
            feature_mask = torch.zeros_like(geometry_mask)
            feature_mask[:, -1] = True
            pooled = torch.cat((spatial.mean(dim=1), spatial.amax(dim=1), prior[:, 0]), dim=-1)
            return geometry, geometry_mask, feature_mask, pooled.reshape(batch, candidates, -1)

        def forward(
            self,
            task: Any,
            rgb: Any,
            cameras: Any,
            coarse_prior: Any,
            candidate_mask: Any,
            *,
            depth: Any | None = None,
            depth_mask: Any | None = None,
        ) -> Any:
            geometry, geometry_mask, feature_mask, _ = self.encode_observation(
                rgb, coarse_prior, depth=depth, depth_mask=depth_mask
            )
            batch, candidates = rgb.shape[:2]
            if task.shape != (batch, actual.ranker.task_dim):
                raise ValueError("task violates ranker ModelSpec")
            if cameras.shape != (batch, candidates, actual.camera_dim):
                raise ValueError("cameras violates PixelModelSpec")
            if candidate_mask.shape != (batch, candidates):
                raise ValueError("candidate_mask must match batch and candidate axes")
            flat_task = task[:, None, :].expand(-1, candidates, -1).reshape(-1, actual.ranker.task_dim)
            flat_candidate = self.camera_projection(cameras.float()).reshape(-1, 1, actual.ranker.candidate_dim)
            flat_valid = torch.ones((batch * candidates, 1), dtype=torch.bool, device=rgb.device)
            scores = self.ranker(
                task=flat_task,
                geometry=geometry,
                geometry_mask=geometry_mask,
                feature_mask=feature_mask,
                candidates=flat_candidate,
                candidate_mask=flat_valid,
            ).reshape(batch, candidates)
            return scores.masked_fill(~candidate_mask.bool(), -torch.inf)

        def probe_features(
            self,
            rgb: Any,
            coarse_prior: Any,
            *,
            depth: Any | None = None,
            depth_mask: Any | None = None,
        ) -> Any:
            """Return frozen pixel/prior features for a label-side visibility probe."""
            return self.encode_observation(
                rgb, coarse_prior, depth=depth, depth_mask=depth_mask
            )[3]

    return PixelViewpointPolicy()
