from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from services.perception.config import ModelSpec
from .data import WindowBatch


@dataclass(frozen=True)
class SyntheticCorpus:
    spec: ModelSpec
    windows: WindowBatch
    anomaly_ticks: tuple[int, ...]
    seed: int


def generate_synthetic_corpus(
    *,
    spec: ModelSpec | None = None,
    windows: int = 96,
    seed: int = 7,
    anomaly_every: int = 13,
    anomaly_magnitude: float = 5.0,
) -> SyntheticCorpus:
    """Generate smooth command-conditioned dynamics with deterministic deviations."""
    s = spec or ModelSpec()
    if windows < 8:
        raise ValueError("synthetic corpus requires at least eight windows")
    if s.latent_features > s.camera_features:
        raise ValueError("synthetic generator requires latent_features <= camera_features")
    if anomaly_every < 2:
        raise ValueError("anomaly_every must be at least two")
    rng = np.random.default_rng(seed)
    total = windows + s.window + s.max_horizon
    command = rng.normal(0.0, 0.35, (total, s.command_features)).astype(np.float32)
    for tick in range(1, total):
        command[tick] = 0.92 * command[tick - 1] + 0.08 * command[tick]
    command_to_latent = rng.normal(
        0.0, 0.25, (s.command_features, s.latent_features)
    ).astype(np.float32)
    latent = np.zeros((total, s.cameras, s.latent_features), np.float32)
    camera_bias = rng.normal(0.0, 0.1, (s.cameras, s.latent_features)).astype(np.float32)
    for tick in range(1, total):
        drive = command[tick] @ command_to_latent
        latent[tick] = (
            0.86 * latent[tick - 1]
            + 0.14 * drive[None, :]
            + 0.02 * rng.normal(size=(s.cameras, s.latent_features))
            + 0.01 * camera_bias
        )

    first_t0 = s.window - 1
    anomaly_ticks = tuple(
        first_t0 + index + s.horizons[0]
        for index in range(anomaly_every - 1, windows, anomaly_every)
    )
    for ordinal, tick in enumerate(anomaly_ticks):
        direction = 1.0 if ordinal % 2 == 0 else -1.0
        latent[tick, :, 0] += np.float32(direction * anomaly_magnitude)
        if s.latent_features > 1:
            latent[tick, :, 1] -= np.float32(direction * anomaly_magnitude * 0.5)

    camera_features = rng.normal(
        0.0, 0.03, (total, s.cameras, s.camera_features)
    ).astype(np.float32)
    camera_features[..., : s.latent_features] += latent
    process = rng.normal(0.0, 0.03, (total, s.process_features)).astype(np.float32)
    shared = min(s.process_features, s.command_features)
    process[:, :shared] += command[:, :shared]

    cam_feat = np.empty((windows, s.cameras, s.window, s.camera_features), np.float32)
    proc_feat = np.empty((windows, s.window, s.process_features), np.float32)
    cmd_future = np.empty((windows, s.max_horizon, s.command_features), np.float32)
    target = np.empty(
        (windows, len(s.horizons), s.cameras, s.latent_features), np.float32
    )
    labels = np.zeros(windows, bool)
    for index in range(windows):
        t0 = first_t0 + index
        cam_feat[index] = np.transpose(camera_features[t0 - s.window + 1 : t0 + 1], (1, 0, 2))
        proc_feat[index] = process[t0 - s.window + 1 : t0 + 1]
        cmd_future[index] = command[t0 + 1 : t0 + s.max_horizon + 1]
        target_ticks = tuple(t0 + horizon for horizon in s.horizons)
        target[index] = np.stack([latent[tick] for tick in target_ticks], axis=0)
        labels[index] = any(tick in anomaly_ticks for tick in target_ticks)

    cam_mask = np.ones((windows, s.cameras, s.window), np.uint8)
    proc_mask = np.ones((windows, s.window, s.process_features), np.uint8)
    cmd_mask = np.ones((windows, s.max_horizon), np.uint8)
    batch = WindowBatch(
        cam_feat=cam_feat,
        cam_mask=cam_mask,
        cam_age=np.zeros_like(cam_mask, dtype=np.float32),
        proc_feat=proc_feat,
        proc_mask=proc_mask,
        proc_age=np.zeros_like(proc_mask, dtype=np.float32),
        cmd_future=cmd_future,
        cmd_mask=cmd_mask,
        target_latent=target,
        event_label=labels,
        window_ids=tuple(f"synthetic-{seed}-{index:05d}" for index in range(windows)),
    ).validate(s)
    return SyntheticCorpus(spec=s, windows=batch, anomaly_ticks=anomaly_ticks, seed=seed)
