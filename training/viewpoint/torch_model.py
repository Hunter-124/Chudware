from __future__ import annotations

from typing import Any

import numpy as np

from .model import ModelSpec, NumpyViewpointPolicy, PolicyWeights


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def build_torch_policy(
    *,
    spec: ModelSpec | None = None,
    reference: NumpyViewpointPolicy | None = None,
    seed: int = 0,
) -> Any:
    """Build the trainable mirror; Torch remains absent from NumPy inference."""
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("Torch is optional; install it only for viewpoint training") from exc

    actual = (reference.spec if reference is not None else (spec or ModelSpec())).validate()
    initial = reference.weights if reference is not None else PolicyWeights.random(actual, seed)

    class TorchViewpointPolicy(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            for name, value in initial.arrays().items():
                setattr(self, name, torch.nn.Parameter(torch.as_tensor(value.copy(), dtype=torch.float32)))

        @staticmethod
        def silu(value: Any) -> Any:
            return torch.nn.functional.silu(value)

        def forward(
            self,
            task: Any,
            geometry: Any,
            geometry_mask: Any,
            feature_mask: Any,
            candidates: Any,
            candidate_mask: Any | None = None,
        ) -> Any:
            task_h = self.silu(task @ self.task_w + self.task_b)
            keys = self.silu(geometry @ self.geom_k_w + self.geom_k_b)
            values = self.silu(geometry @ self.geom_v_w + self.geom_v_b)
            cand_h = self.silu(candidates @ self.cand_w + self.cand_b)
            task_h = task_h[:, None, :].expand_as(cand_h)
            query = self.silu(torch.cat((cand_h, task_h), dim=-1) @ self.query_w + self.query_b)
            logits = torch.einsum("bnh,bgh->bng", query, keys) / np.sqrt(actual.hidden_dim)
            logits = logits + feature_mask[:, None, :].float() * self.feature_bias
            logits = logits.masked_fill(~geometry_mask[:, None, :].bool(), -1.0e9)
            attention = torch.softmax(logits, dim=-1)
            context = torch.einsum("bng,bgh->bnh", attention, values)
            fusion = torch.cat((cand_h, task_h, context, cand_h * context, torch.abs(cand_h - context)), dim=-1)
            hidden = self.silu(fusion @ self.fuse_w + self.fuse_b)
            output = torch.sigmoid(hidden @ self.out_w + self.out_b)
            if candidate_mask is not None:
                output = output.masked_fill(~candidate_mask[..., None].bool(), 0.0)
            return output

        def to_numpy_reference(self) -> NumpyViewpointPolicy:
            arrays = {
                name: value.detach().cpu().numpy().astype(np.float32)
                for name, value in self.named_parameters()
            }
            return NumpyViewpointPolicy(PolicyWeights(**arrays), actual)

    return TorchViewpointPolicy()
