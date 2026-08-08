from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from services.perception.config import ModelSpec

Array = np.ndarray

WINDOW_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "aicad.perception_window/1",
    "type": "object",
    "required": [
        "window_id", "feature_spec_id", "clip_id", "split", "t0_tick",
        "cam_feat", "cam_mask", "cam_age", "proc_feat", "proc_mask",
        "proc_age", "cmd_future", "cmd_mask", "target_latent", "event_label",
    ],
    "properties": {
        "window_id": {"type": "string", "minLength": 1},
        "feature_spec_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "clip_id": {"type": "string", "minLength": 1},
        "split": {"enum": ["train", "calib", "test_locked"]},
        "t0_tick": {"type": "integer", "minimum": 0},
        "event_label": {"type": "boolean"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class WindowBatch:
    cam_feat: Array
    cam_mask: Array
    cam_age: Array
    proc_feat: Array
    proc_mask: Array
    proc_age: Array
    cmd_future: Array
    cmd_mask: Array
    target_latent: Array
    event_label: Array
    window_ids: tuple[str, ...]

    def validate(self, spec: ModelSpec) -> "WindowBatch":
        b = len(self.window_ids)
        expected = {
            "cam_feat": (b, spec.cameras, spec.window, spec.camera_features),
            "cam_mask": (b, spec.cameras, spec.window),
            "cam_age": (b, spec.cameras, spec.window),
            "proc_feat": (b, spec.window, spec.process_features),
            "proc_mask": (b, spec.window, spec.process_features),
            "proc_age": (b, spec.window, spec.process_features),
            "cmd_future": (b, spec.max_horizon, spec.command_features),
            "cmd_mask": (b, spec.max_horizon),
            "target_latent": (b, len(spec.horizons), spec.cameras, spec.latent_features),
            "event_label": (b,),
        }
        for name, shape in expected.items():
            value = np.asarray(getattr(self, name))
            if value.shape != shape:
                raise ValueError(f"{name} has shape {value.shape}, expected {shape}")
            if name not in {"cam_mask", "proc_mask", "cmd_mask", "event_label"} and not np.isfinite(value).all():
                raise ValueError(f"{name} contains non-finite values")
        for name in ("cam_mask", "proc_mask", "cmd_mask"):
            values = np.asarray(getattr(self, name))
            if not np.isin(values, (0, 1)).all():
                raise ValueError(f"{name} must be binary")
        if len(set(self.window_ids)) != b or any(not value for value in self.window_ids):
            raise ValueError("window ids must be non-empty and unique")
        return self

    def model_inputs(self) -> Mapping[str, Array]:
        return {
            "cam_feat": self.cam_feat,
            "cam_mask": self.cam_mask,
            "cam_age": self.cam_age,
            "proc_feat": self.proc_feat,
            "proc_mask": self.proc_mask,
            "proc_age": self.proc_age,
            "cmd_future": self.cmd_future,
            "cmd_mask": self.cmd_mask,
        }

    def subset(self, indices: Array) -> "WindowBatch":
        idx = np.asarray(indices)
        return WindowBatch(
            cam_feat=self.cam_feat[idx], cam_mask=self.cam_mask[idx], cam_age=self.cam_age[idx],
            proc_feat=self.proc_feat[idx], proc_mask=self.proc_mask[idx], proc_age=self.proc_age[idx],
            cmd_future=self.cmd_future[idx], cmd_mask=self.cmd_mask[idx],
            target_latent=self.target_latent[idx], event_label=self.event_label[idx],
            window_ids=tuple(self.window_ids[int(i)] for i in idx),
        )
