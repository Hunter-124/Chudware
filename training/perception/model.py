from __future__ import annotations

from typing import Any

import numpy as np

from services.perception.config import ModelSpec
from services.perception.reference import NumpyMLDP


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


def build_torch_model(reference: NumpyMLDP | None = None, *, spec: ModelSpec | None = None, seed: int = 0) -> Any:
    """Create a torch Module mirroring :class:`NumpyMLDP` exactly.

    Torch is imported only when this optional path is requested.
    """
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("torch is optional; install it to build the torch MLDP") from exc

    ref = reference or NumpyMLDP(spec, seed=seed)
    s = ref.spec

    class TorchMLDP(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.spec = s
            self.reference_seed = ref.seed
            self.parameter_names = tuple(ref.parameters)
            for name, value in ref.parameters.items():
                self.register_parameter(
                    name,
                    torch.nn.Parameter(
                        torch.from_numpy(np.asarray(value, np.float32).copy())
                    ),
                )

        def _encode(self, inputs: dict[str, Any]) -> tuple[Any, Any, Any]:
            cam_feat = inputs["cam_feat"].float()
            cam_mask = inputs["cam_mask"].bool()
            proc_feat = inputs["proc_feat"].float()
            proc_mask = inputs["proc_mask"].float()
            cam_emb = torch.tanh(torch.matmul(cam_feat, self.cam_w) + self.cam_b)
            cam_emb = cam_emb * cam_mask.unsqueeze(-1)
            counts = cam_mask.sum(dim=1).unsqueeze(-1)
            cam_fused = cam_emb.sum(dim=1) / counts.clamp_min(1)
            cam_fused = torch.where(counts > 0, cam_fused, torch.zeros_like(cam_fused))
            proc_emb = torch.tanh(torch.matmul(proc_feat * proc_mask, self.proc_w) + self.proc_b)
            proc_valid = torch.any(proc_mask > 0, dim=-1)
            proc_emb = proc_emb * proc_valid.unsqueeze(-1)
            tokens = torch.tanh(cam_fused + proc_emb)
            for layer, dilation in enumerate(s.dilations):
                past = torch.zeros_like(tokens)
                if dilation < s.window:
                    past[:, dilation:] = tokens[:, :-dilation]
                mixed = torch.tanh(tokens + past * self.depth_w[layer])
                tokens = torch.tanh(
                    mixed + torch.matmul(mixed, self.channel_w[layer]) + self.channel_b[layer]
                )
            return tokens, cam_emb, proc_valid

        def forward(
            self,
            cam_feat: Any,
            cam_mask: Any,
            cam_age: Any,
            proc_feat: Any,
            proc_mask: Any,
            proc_age: Any,
            cmd_future: Any,
            cmd_mask: Any,
        ) -> tuple[Any, ...]:
            del cam_age, proc_age
            inputs = {
                "cam_feat": cam_feat, "cam_mask": cam_mask, "proc_feat": proc_feat,
                "proc_mask": proc_mask,
            }
            tokens, cam_emb, proc_valid = self._encode(inputs)
            valid_tick = torch.any(cam_mask.bool(), dim=1) | proc_valid
            count = valid_tick.sum(dim=1, keepdim=True)
            pooled = (tokens * valid_tick.unsqueeze(-1)).sum(dim=1) / count.clamp_min(1)
            pooled = torch.where(count > 0, pooled, torch.zeros_like(pooled))
            context = torch.tanh(tokens[:, -1] + pooled)
            horizon_values = []
            for index, horizon in enumerate(s.horizons):
                command = cmd_future[:, horizon - 1].float() * cmd_mask[:, horizon - 1].float().unsqueeze(-1)
                horizon_values.append(
                    torch.tanh(context + torch.matmul(command, self.cmd_w) + self.horizon_emb[index])
                )
            horizon_ctx = torch.stack(horizon_values, dim=1)
            camera_ctx = torch.tanh(horizon_ctx[:, :, None, :] + cam_emb[:, None, :, -1, :])
            z_hat = torch.einsum("bkch,khd->bkcd", camera_ctx, self.forecast_w) + self.forecast_b[None, :, None, :]
            z_logvar = torch.einsum("bkch,khd->bkcd", camera_ctx, self.logvar_w) + self.logvar_b[None, :, None, :]
            z_logvar = torch.clamp(z_logvar, min=float(np.log(1.0e-4)), max=8.0)
            snr_hat = torch.einsum("bkh,khs->bks", horizon_ctx, self.sensor_w) + self.sensor_b
            snr_logvar = torch.einsum("bkh,khs->bks", horizon_ctx, self.sensor_logvar_w) + self.sensor_logvar_b
            snr_logvar = torch.clamp(snr_logvar, min=float(np.log(1.0e-4)), max=8.0)
            state_logit = torch.einsum("bkh,khn->bkn", horizon_ctx, self.state_w) + self.state_b
            anomaly = torch.sigmoid(torch.matmul(tokens[:, -1], self.anomaly_w) + self.anomaly_b)
            event_bnd = torch.matmul(tokens, self.event_w) + self.event_b
            importance = torch.sigmoid(torch.matmul(tokens[:, -1], self.importance_w) + self.importance_b)
            rel_logits = torch.einsum("bch,h->bc", cam_emb[:, :, -1], self.relevance_w)
            usable = cam_mask[:, :, -1].bool()
            masked = torch.where(usable, rel_logits, torch.full_like(rel_logits, -1.0e9))
            rel_exp = torch.exp(masked - masked.max(dim=-1, keepdim=True).values) * usable
            cam_rel = rel_exp / rel_exp.sum(dim=-1, keepdim=True).clamp_min(1.0)
            retain = torch.matmul(tokens, self.retain_w) + self.retain_b
            return (
                z_hat, z_logvar, snr_hat, snr_logvar, state_logit,
                anomaly, event_bnd, importance, cam_rel, retain,
            )

        def to_numpy_reference(self) -> NumpyMLDP:
            """Export the current learned parameters into the frozen numpy format."""
            parameters = {
                name: getattr(self, name).detach().cpu().numpy().astype(
                    np.float32, copy=True
                )
                for name in self.parameter_names
            }
            return NumpyMLDP(
                self.spec,
                seed=self.reference_seed,
                parameters=parameters,
            )

    return TorchMLDP()


OUTPUT_NAMES = (
    "z_hat", "z_logvar", "snr_hat", "snr_logvar", "state_logit",
    "anomaly", "event_bnd", "importance", "cam_rel", "retain",
)
