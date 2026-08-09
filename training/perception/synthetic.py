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
    regimes: tuple[str, ...] = ()
    split_labels: tuple[str, ...] = ()


def development_windows_for_total(total_windows: int) -> int:
    """Return the development count produced by the fixed 60/20/20 split."""
    if total_windows < 8:
        raise ValueError("synthetic corpus requires at least eight windows")
    return int(total_windows * 0.8)


def total_windows_for_development(development_windows: int) -> int:
    """Return the smallest corpus whose fixed split has exactly this development size."""
    if development_windows < 6:
        raise ValueError("development sweep requires at least six windows")
    total_windows = (5 * development_windows + 3) // 4
    while development_windows_for_total(total_windows) < development_windows:
        total_windows += 1
    if development_windows_for_total(total_windows) != development_windows:
        raise ValueError(
            f"development size {development_windows} is not representable by the fixed split"
        )
    return total_windows


def generate_synthetic_corpus(
    *,
    spec: ModelSpec | None = None,
    windows: int = 96,
    seed: int = 7,
    anomaly_every: int = 13,
    anomaly_magnitude: float = 5.0,
) -> SyntheticCorpus:
    """Generate seeded manufacturing dynamics with non-memorisable regime instances."""
    s = spec or ModelSpec()
    if windows < 8:
        raise ValueError("synthetic corpus requires at least eight windows")
    if s.latent_features > s.camera_features:
        raise ValueError("synthetic generator requires latent_features <= camera_features")
    if anomaly_every < 2:
        raise ValueError("anomaly_every must be at least two")
    rng = np.random.default_rng(seed)

    # Mirror the public deterministic split without importing train.py (which imports
    # this module).  Samples are independent episodes.  Every split sees the same
    # physical regime families but disjoint, continuously sampled realizations.
    order = rng.permutation(windows)
    train_end = max(1, int(windows * 0.6))
    calibration_end = max(train_end + 1, int(windows * 0.8))
    split_code = np.empty(windows, np.int8)
    split_code[order[:train_end]] = 0
    split_code[order[train_end:calibration_end]] = 1
    split_code[order[calibration_end:]] = 2
    split_names = ("train", "calibration", "test_locked")
    regime_names = ("steady_cut", "material_change", "wear_ramp")
    regime_code = np.empty(windows, np.int8)
    event_label = np.zeros(windows, bool)
    for code in range(len(split_names)):
        members = np.flatnonzero(split_code == code)
        for ordinal, index in enumerate(members):
            regime_code[index] = ordinal % len(regime_names)
            event_label[index] = ordinal % anomaly_every == anomaly_every - 1

    total_ticks = s.window + s.max_horizon
    cam_feat = np.zeros(
        (windows, s.cameras, s.window, s.camera_features), np.float32
    )
    proc_feat = np.zeros((windows, s.window, s.process_features), np.float32)
    cmd_future = np.zeros(
        (windows, s.max_horizon, s.command_features), np.float32
    )
    target = np.zeros(
        (windows, len(s.horizons), s.cameras, s.latent_features), np.float32
    )
    cam_mask = np.ones((windows, s.cameras, s.window), np.uint8)
    proc_mask = np.ones((windows, s.window, s.process_features), np.uint8)
    cmd_mask = np.ones((windows, s.max_horizon), np.uint8)
    cam_age = np.zeros_like(cam_mask, dtype=np.float32)
    proc_age = np.zeros_like(proc_mask, dtype=np.float32)

    command_to_latent = rng.normal(
        0.0, 0.34, (s.command_features, s.latent_features)
    )
    camera_bias = rng.normal(
        0.0, 0.08, (s.cameras, s.latent_features)
    )
    event_amplitude = 0.22 * anomaly_magnitude
    for index in range(windows):
        regime = int(regime_code[index])
        split = int(split_code[index])
        command = np.zeros((total_ticks, s.command_features), np.float64)
        command[0] = rng.normal(0.0, 0.65, s.command_features)
        for tick in range(1, total_ticks):
            innovation = rng.normal(0.0, 0.30, s.command_features)
            if tick in (max(1, s.window - 3), s.window + 1):
                innovation += rng.normal(0.0, 0.70, s.command_features)
            command[tick] = 0.78 * command[tick - 1] + innovation
        command = np.tanh(command)

        # Split bands are deliberately disjoint but close: held-out episodes cannot
        # be identified from a training trajectory, while the underlying transition
        # law remains the same and must generalize.
        realization_band = ((-0.08, 0.0), (0.0, 0.04), (0.04, 0.12))[split]
        material = (
            0.78
            + 0.20 * regime
            + rng.uniform(*realization_band)
            + rng.normal(0.0, 0.02)
        )
        inertia = (0.55, 0.72, 0.84)[regime] + rng.normal(0.0, 0.025)
        wear = np.zeros(total_ticks, np.float64)
        if regime == 2:
            wear[:] = np.linspace(
                rng.uniform(0.05, 0.18), rng.uniform(0.55, 0.80), total_ticks
            )
        elif regime == 1:
            wear[: s.window // 2] = 0.05
            wear[s.window // 2 :] = 0.28
        else:
            wear[:] = rng.uniform(0.02, 0.12)

        precursor = np.zeros(total_ticks, np.float64)
        if event_label[index]:
            precursor[max(0, s.window - 4) : max(1, s.window - 2)] = 1.0
        latent = np.zeros(
            (total_ticks, s.cameras, s.latent_features), np.float64
        )
        latent[0] = rng.normal(
            0.0, 0.35, (s.cameras, s.latent_features)
        )
        velocity = np.zeros_like(latent[0])
        for tick in range(1, total_ticks):
            drive = material * (command[tick] @ command_to_latent)
            if regime == 1 and tick >= s.window // 2:
                drive *= 1.25
            wear_effect = wear[tick] * np.sin(drive + 0.4)
            acceleration = (
                0.20 * (drive[None, :] - latent[tick - 1])
                + 0.12 * wear_effect[None, :]
            )
            velocity = 0.62 * velocity + acceleration
            latent[tick] = (
                inertia * latent[tick - 1]
                + (1.0 - inertia) * drive[None, :]
                + 0.32 * velocity
                + camera_bias
                + rng.normal(0.0, 0.018, latent[tick].shape)
            )
            # The precursor has ended before t0.  Only its delayed consequence is
            # anomalous, so a current-frame predictor cannot see the warning.
            if event_label[index] and tick >= s.window:
                shock = np.sin((tick - s.window + 1) * 1.1) * event_amplitude
                latent[tick, :, 0] += shock
                if s.latent_features > 1:
                    latent[tick, :, 1] -= 0.55 * shock

        observed = (
            latent[: s.window]
            + rng.normal(0.0, 0.075, latent[: s.window].shape)
        )
        cam_feat[index, :, :, : s.latent_features] = np.transpose(
            observed, (1, 0, 2)
        )
        if s.camera_features > s.latent_features:
            cam_feat[index, :, :, s.latent_features :] = rng.normal(
                0.0,
                0.08,
                (
                    s.cameras,
                    s.window,
                    s.camera_features - s.latent_features,
                ),
            )
        shared = min(s.command_features, s.process_features)
        proc_feat[index, :, :shared] = (
            command[: s.window, :shared]
            + rng.normal(0.0, 0.045, (s.window, shared))
        )
        if s.process_features > shared:
            proc_feat[index, :, shared] = (
                wear[: s.window] + rng.normal(0.0, 0.025, s.window)
            )
        if s.process_features > shared + 1:
            proc_feat[index, :, shared + 1] = (
                precursor[: s.window] + rng.normal(0.0, 0.025, s.window)
            )
        cmd_future[index] = command[s.window :]
        for horizon_index, horizon in enumerate(s.horizons):
            target[index, horizon_index] = latent[s.window - 1 + horizon]

        camera_dropout = rng.random((s.cameras, s.window)) < 0.045
        if rng.random() < 0.10:
            camera_dropout[rng.integers(s.cameras), -1] = True
        cam_mask[index, camera_dropout] = 0
        cam_feat[index, camera_dropout] = 0.0
        process_dropout = rng.random((s.window, s.process_features)) < 0.035
        proc_mask[index, process_dropout] = 0
        proc_feat[index, process_dropout] = 0.0
        command_dropout = rng.random(s.max_horizon) < 0.015
        cmd_mask[index, command_dropout] = 0
        cmd_future[index, command_dropout] = 0.0
        for camera_index in range(s.cameras):
            age = 0.0
            for tick in range(s.window):
                age = 0.0 if cam_mask[index, camera_index, tick] else age + 1.0
                cam_age[index, camera_index, tick] = age
        for tick in range(s.window):
            for feature in range(s.process_features):
                previous = proc_age[index, tick - 1, feature] if tick else 0.0
                proc_age[index, tick, feature] = (
                    0.0 if proc_mask[index, tick, feature] else previous + 1.0
                )

    batch = WindowBatch(
        cam_feat=cam_feat,
        cam_mask=cam_mask,
        cam_age=cam_age,
        proc_feat=proc_feat,
        proc_mask=proc_mask,
        proc_age=proc_age,
        cmd_future=cmd_future,
        cmd_mask=cmd_mask,
        target_latent=target,
        event_label=event_label,
        window_ids=tuple(
            f"synthetic-{seed}-{split_names[int(split_code[index])]}-{index:05d}"
            for index in range(windows)
        ),
    ).validate(s)
    return SyntheticCorpus(
        spec=s,
        windows=batch,
        anomaly_ticks=tuple(int(index) for index in np.flatnonzero(event_label)),
        seed=seed,
        regimes=tuple(regime_names[int(code)] for code in regime_code),
        split_labels=tuple(split_names[int(code)] for code in split_code),
    )
