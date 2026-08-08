from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class ModelSpec:
    """Static MLDP tensor contract.

    Defaults are intentionally tiny enough for CPU tests. ``production()`` carries the
    dimensions from the manufacturing design without making those dimensions the unit-test cost.
    """

    cameras: int = 2
    window: int = 8
    camera_features: int = 6
    process_features: int = 5
    command_features: int = 3
    latent_features: int = 4
    sensor_features: int = 3
    state_labels: int = 4
    hidden: int = 12
    horizons: tuple[int, ...] = (1, 2, 4)
    dilations: tuple[int, ...] = (1, 2, 4)

    def __post_init__(self) -> None:
        positive = (
            self.cameras,
            self.window,
            self.camera_features,
            self.process_features,
            self.command_features,
            self.latent_features,
            self.sensor_features,
            self.state_labels,
            self.hidden,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("all model dimensions must be positive")
        if not self.horizons or any(h <= 0 for h in self.horizons):
            raise ValueError("horizons must be positive")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be sorted and unique")
        if any(d <= 0 for d in self.dilations):
            raise ValueError("dilations must be positive")

    @property
    def max_horizon(self) -> int:
        return self.horizons[-1]

    @property
    def feature_spec_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def production(cls) -> "ModelSpec":
        return cls(
            cameras=4,
            window=64,
            camera_features=200,
            process_features=64,
            command_features=16,
            latent_features=128,
            sensor_features=32,
            state_labels=24,
            hidden=256,
            horizons=(1, 4, 20, 120),
            dilations=(1, 2, 4, 8, 16, 32),
        )
