from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Mapping

import numpy as np

from .config import ModelSpec

Array = np.ndarray


def _sigmoid(value: Array) -> Array:
    value = np.clip(value, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-value))


def _softmax_masked(logits: Array, usable: Array) -> Array:
    masked = np.where(usable, logits, -1.0e9)
    maximum = np.max(masked, axis=-1, keepdims=True)
    exp = np.exp(masked - maximum) * usable
    total = exp.sum(axis=-1, keepdims=True)
    return np.divide(exp, total, out=np.zeros_like(exp), where=total > 0)


def surprise_score(
    observed: Array,
    predicted_mean: Array,
    predicted_logvar: Array,
    valid: Array | None = None,
) -> Array:
    """Return heteroscedastic Gaussian NLL, averaged over valid values.

    Inputs have shape ``[B,K,C,Dz]``. A missing item yields NaN instead of a
    reassuring normal score.
    """

    observed = np.asarray(observed, dtype=np.float32)
    mean = np.asarray(predicted_mean, dtype=np.float32)
    logvar = np.asarray(predicted_logvar, dtype=np.float32)
    if observed.shape != mean.shape or logvar.shape != mean.shape:
        raise ValueError("observed, mean, and logvar must have identical shapes")
    terms = 0.5 * ((observed - mean) ** 2 / np.exp(logvar) + logvar)
    if valid is None:
        return terms.mean(axis=(1, 2, 3), dtype=np.float64).astype(np.float32)
    mask = np.broadcast_to(np.asarray(valid, dtype=bool), terms.shape)
    count = mask.sum(axis=(1, 2, 3))
    total = np.where(mask, terms, 0.0).sum(axis=(1, 2, 3), dtype=np.float64)
    return np.divide(
        total,
        count,
        out=np.full(total.shape, np.nan, dtype=np.float64),
        where=count > 0,
    ).astype(np.float32)


def anomaly_probability(surprise: Array, center: float, scale: float) -> Array:
    if scale <= 0:
        raise ValueError("scale must be positive")
    return _sigmoid((np.asarray(surprise, dtype=np.float32) - center) / scale).astype(np.float32)


class NumpyMLDP:
    """Pure-numpy causal temporal encoder and multi-horizon prediction heads."""

    def __init__(
        self,
        spec: ModelSpec | None = None,
        *,
        seed: int = 0,
        parameters: Mapping[str, Array] | None = None,
    ) -> None:
        self.spec = spec or ModelSpec()
        self.seed = int(seed)
        self.parameters = (
            {key: np.asarray(value, dtype=np.float32).copy() for key, value in parameters.items()}
            if parameters is not None
            else self._initialize(self.seed)
        )
        self._validate_parameters()

    def _initialize(self, seed: int) -> dict[str, Array]:
        s = self.spec
        rng = np.random.default_rng(seed)

        def normal(shape: tuple[int, ...], fan_in: int) -> Array:
            return rng.normal(0.0, 1.0 / np.sqrt(fan_in), shape).astype(np.float32)

        k = len(s.horizons)
        return {
            "cam_w": normal((s.camera_features, s.hidden), s.camera_features),
            "cam_b": np.zeros(s.hidden, np.float32),
            "proc_w": normal((s.process_features, s.hidden), s.process_features),
            "proc_b": np.zeros(s.hidden, np.float32),
            "depth_w": normal((len(s.dilations), s.hidden), s.hidden),
            "channel_w": normal((len(s.dilations), s.hidden, s.hidden), s.hidden),
            "channel_b": np.zeros((len(s.dilations), s.hidden), np.float32),
            "cmd_w": normal((s.command_features, s.hidden), s.command_features),
            "horizon_emb": normal((k, s.hidden), s.hidden),
            "forecast_w": normal((k, s.hidden, s.latent_features), s.hidden),
            "forecast_b": np.zeros((k, s.latent_features), np.float32),
            "logvar_w": normal((k, s.hidden, s.latent_features), s.hidden) * np.float32(0.1),
            "logvar_b": np.full((k, s.latent_features), -1.0, np.float32),
            "sensor_w": normal((k, s.hidden, s.sensor_features), s.hidden),
            "sensor_b": np.zeros((k, s.sensor_features), np.float32),
            "sensor_logvar_w": normal((k, s.hidden, s.sensor_features), s.hidden) * np.float32(0.1),
            "sensor_logvar_b": np.full((k, s.sensor_features), -1.0, np.float32),
            "state_w": normal((k, s.hidden, s.state_labels), s.hidden),
            "state_b": np.zeros((k, s.state_labels), np.float32),
            "event_w": normal((s.hidden, 3), s.hidden),
            "event_b": np.zeros(3, np.float32),
            "importance_w": normal((s.hidden,), s.hidden),
            "importance_b": np.zeros((), np.float32),
            "relevance_w": normal((s.hidden,), s.hidden),
            "retain_w": normal((s.hidden,), s.hidden),
            "retain_b": np.zeros((), np.float32),
            "anomaly_w": normal((s.hidden,), s.hidden),
            "anomaly_b": np.zeros((), np.float32),
        }

    def _expected_shapes(self) -> dict[str, tuple[int, ...]]:
        s = self.spec
        k, layers = len(s.horizons), len(s.dilations)
        return {
            "cam_w": (s.camera_features, s.hidden), "cam_b": (s.hidden,),
            "proc_w": (s.process_features, s.hidden), "proc_b": (s.hidden,),
            "depth_w": (layers, s.hidden), "channel_w": (layers, s.hidden, s.hidden),
            "channel_b": (layers, s.hidden), "cmd_w": (s.command_features, s.hidden),
            "horizon_emb": (k, s.hidden), "forecast_w": (k, s.hidden, s.latent_features),
            "forecast_b": (k, s.latent_features), "logvar_w": (k, s.hidden, s.latent_features),
            "logvar_b": (k, s.latent_features), "sensor_w": (k, s.hidden, s.sensor_features),
            "sensor_b": (k, s.sensor_features),
            "sensor_logvar_w": (k, s.hidden, s.sensor_features),
            "sensor_logvar_b": (k, s.sensor_features),
            "state_w": (k, s.hidden, s.state_labels), "state_b": (k, s.state_labels),
            "event_w": (s.hidden, 3), "event_b": (3,), "importance_w": (s.hidden,),
            "importance_b": (), "relevance_w": (s.hidden,), "retain_w": (s.hidden,),
            "retain_b": (), "anomaly_w": (s.hidden,), "anomaly_b": (),
        }

    def _validate_parameters(self) -> None:
        expected = self._expected_shapes()
        if set(self.parameters) != set(expected):
            missing = sorted(set(expected) - set(self.parameters))
            extra = sorted(set(self.parameters) - set(expected))
            raise ValueError(f"parameter keys mismatch; missing={missing}, extra={extra}")
        for name, shape in expected.items():
            if self.parameters[name].shape != shape:
                raise ValueError(f"{name} has shape {self.parameters[name].shape}, expected {shape}")

    def validate_inputs(self, inputs: Mapping[str, Array]) -> int:
        s = self.spec
        required = {
            "cam_feat": (None, s.cameras, s.window, s.camera_features),
            "cam_mask": (None, s.cameras, s.window),
            "cam_age": (None, s.cameras, s.window),
            "proc_feat": (None, s.window, s.process_features),
            "proc_mask": (None, s.window, s.process_features),
            "proc_age": (None, s.window, s.process_features),
            "cmd_future": (None, s.max_horizon, s.command_features),
            "cmd_mask": (None, s.max_horizon),
        }
        batch: int | None = None
        for name, shape in required.items():
            if name not in inputs:
                raise ValueError(f"missing model input {name}")
            actual = np.asarray(inputs[name]).shape
            if len(actual) != len(shape) or actual[1:] != shape[1:]:
                raise ValueError(f"{name} has shape {actual}, expected [B,{','.join(map(str, shape[1:]))}]")
            batch = actual[0] if batch is None else batch
            if actual[0] != batch:
                raise ValueError("all inputs must have the same batch size")
        return int(batch or 0)

    def encode_sequence(self, inputs: Mapping[str, Array]) -> tuple[Array, Array, Array]:
        """Encode every context tick; output at t depends only on input ticks <= t."""
        self.validate_inputs(inputs)
        p = self.parameters
        cam_feat = np.asarray(inputs["cam_feat"], np.float32)
        cam_mask = np.asarray(inputs["cam_mask"], bool)
        proc_feat = np.asarray(inputs["proc_feat"], np.float32)
        proc_mask = np.asarray(inputs["proc_mask"], np.float32)

        cam_emb = np.tanh(cam_feat @ p["cam_w"] + p["cam_b"])
        cam_emb *= cam_mask[..., None]
        counts = cam_mask.sum(axis=1)[..., None]
        cam_fused = np.divide(
            cam_emb.sum(axis=1), counts, out=np.zeros_like(cam_emb[:, 0]), where=counts > 0
        )
        proc_emb = np.tanh((proc_feat * proc_mask) @ p["proc_w"] + p["proc_b"])
        proc_valid = np.any(proc_mask > 0, axis=-1)
        proc_emb *= proc_valid[..., None]
        tokens = np.tanh(cam_fused + proc_emb).astype(np.float32)

        for layer, dilation in enumerate(self.spec.dilations):
            past = np.zeros_like(tokens)
            if dilation < self.spec.window:
                past[:, dilation:] = tokens[:, :-dilation]
            mixed = np.tanh(tokens + past * p["depth_w"][layer])
            tokens = np.tanh(
                mixed + mixed @ p["channel_w"][layer] + p["channel_b"][layer]
            ).astype(np.float32)
        return tokens, cam_emb.astype(np.float32), proc_valid

    def _prediction_contexts_from_encoded(
        self,
        inputs: Mapping[str, Array],
        tokens: Array,
        cam_emb: Array,
        proc_valid: Array,
    ) -> tuple[Array, Array, Array]:
        cam_mask = np.asarray(inputs["cam_mask"], bool)
        valid_tick = np.any(cam_mask, axis=1) | proc_valid
        count = valid_tick.sum(axis=1, keepdims=True)
        pooled = np.divide(
            (tokens * valid_tick[..., None]).sum(axis=1),
            count,
            out=np.zeros_like(tokens[:, 0]),
            where=count > 0,
        )
        context = np.tanh(tokens[:, -1] + pooled).astype(np.float32)

        cmd = np.asarray(inputs["cmd_future"], np.float32)
        cmd_mask = np.asarray(inputs["cmd_mask"], bool)
        horizon_contexts = []
        for index, horizon in enumerate(self.spec.horizons):
            command = cmd[:, horizon - 1] * cmd_mask[:, horizon - 1, None]
            horizon_contexts.append(
                np.tanh(
                    context
                    + command @ self.parameters["cmd_w"]
                    + self.parameters["horizon_emb"][index]
                )
            )
        horizon_context = np.stack(horizon_contexts, axis=1).astype(np.float32)
        camera_last = cam_emb[:, :, -1]
        camera_context = np.tanh(
            horizon_context[:, :, None, :] + camera_last[:, None, :, :]
        )
        usable = cam_mask[:, :, -1]
        return horizon_context, camera_context.astype(np.float32), usable

    def prediction_contexts(self, inputs: Mapping[str, Array]) -> tuple[Array, Array, Array]:
        encoded = self.encode_sequence(inputs)
        return self._prediction_contexts_from_encoded(inputs, *encoded)

    def forward(self, inputs: Mapping[str, Array]) -> dict[str, Array]:
        p = self.parameters
        tokens, cam_emb, proc_valid = self.encode_sequence(inputs)
        horizon_ctx, camera_ctx, usable = self._prediction_contexts_from_encoded(
            inputs, tokens, cam_emb, proc_valid
        )
        z_hat = np.einsum("bkch,khd->bkcd", camera_ctx, p["forecast_w"]) + p["forecast_b"][None, :, None, :]
        z_logvar = np.einsum("bkch,khd->bkcd", camera_ctx, p["logvar_w"]) + p["logvar_b"][None, :, None, :]
        z_logvar = np.clip(z_logvar, np.log(1.0e-4), 8.0)
        snr_hat = np.einsum("bkh,khs->bks", horizon_ctx, p["sensor_w"]) + p["sensor_b"]
        snr_logvar = np.einsum("bkh,khs->bks", horizon_ctx, p["sensor_logvar_w"]) + p["sensor_logvar_b"]
        state_logit = np.einsum("bkh,khn->bkn", horizon_ctx, p["state_w"]) + p["state_b"]
        rel_logits = np.einsum("bch,h->bc", cam_emb[:, :, -1], p["relevance_w"])
        result = {
            "z_hat": z_hat.astype(np.float32),
            "z_logvar": z_logvar.astype(np.float32),
            "snr_hat": snr_hat.astype(np.float32),
            "snr_logvar": np.clip(snr_logvar, np.log(1.0e-4), 8.0).astype(np.float32),
            "state_logit": state_logit.astype(np.float32),
            "anomaly": _sigmoid(tokens[:, -1] @ p["anomaly_w"] + p["anomaly_b"]).astype(np.float32),
            "event_bnd": (tokens @ p["event_w"] + p["event_b"]).astype(np.float32),
            "importance": _sigmoid(tokens[:, -1] @ p["importance_w"] + p["importance_b"]).astype(np.float32),
            "cam_rel": _softmax_masked(rel_logits, usable).astype(np.float32),
            "retain": (tokens @ p["retain_w"] + p["retain_b"]).astype(np.float32),
        }
        return result

    def observe(self, inputs: Mapping[str, Array], observed_latent: Array, *, center: float, scale: float) -> dict[str, Array]:
        result = self.forward(inputs)
        result["surprise"] = surprise_score(observed_latent, result["z_hat"], result["z_logvar"])
        result["anomaly"] = anomaly_probability(result["surprise"], center, scale)
        return result

    def save(self, path: str | Path) -> None:
        metadata = json.dumps({"spec": asdict(self.spec), "seed": self.seed}, sort_keys=True)
        np.savez_compressed(path, __metadata__=np.array(metadata), **self.parameters)

    @classmethod
    def load(cls, path: str | Path) -> "NumpyMLDP":
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["__metadata__"]))
            spec_data = metadata["spec"]
            spec_data["horizons"] = tuple(spec_data["horizons"])
            spec_data["dilations"] = tuple(spec_data["dilations"])
            parameters = {key: archive[key] for key in archive.files if key != "__metadata__"}
        return cls(ModelSpec(**spec_data), seed=int(metadata["seed"]), parameters=parameters)
