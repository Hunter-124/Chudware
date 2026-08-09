from __future__ import annotations

import numpy as np

from services.perception.config import ModelSpec
from services.vision import frames as vision_frames


class FrameFeatureExtractor:
    """Deterministic BGR frame features matching ``ModelSpec.camera_features``."""

    def __init__(self, spec: ModelSpec) -> None:
        self._width = spec.camera_features

    def extract(self, frame: np.ndarray) -> tuple[float, ...]:
        pixels = np.asarray(frame)
        if pixels.ndim not in (2, 3) or pixels.size == 0:
            raise ValueError("frame must be a non-empty grayscale or BGR array")
        if pixels.ndim == 3 and pixels.shape[2] != 3:
            raise ValueError("colour frames must have exactly three BGR channels")
        if not np.issubdtype(pixels.dtype, np.number) or not np.isfinite(pixels).all():
            raise ValueError("frame pixels must be finite numbers")

        image = np.clip(pixels.astype(np.float32, copy=False), 0.0, 255.0) / 255.0
        if image.ndim == 3:
            gray = image[..., 0] * 0.114 + image[..., 1] * 0.587 + image[..., 2] * 0.299
            channels = [image[..., index] for index in range(3)]
        else:
            gray = image
            channels = [image, image, image]

        # Reuse the repository's variance-of-Laplacian primitive rather than carrying a fork.
        sharpness = vision_frames.blur_score(pixels)
        gx = np.diff(gray, axis=1) if gray.shape[1] > 1 else np.zeros_like(gray)
        gy = np.diff(gray, axis=0) if gray.shape[0] > 1 else np.zeros_like(gray)

        values: list[float] = [
            float(gray.mean()),
            float(gray.std()),
            float(np.quantile(gray, 0.10)),
            float(np.quantile(gray, 0.50)),
            float(np.quantile(gray, 0.90)),
            float(np.log1p(max(0.0, sharpness)) / 16.0),
        ]
        for channel in channels:
            values.extend((float(channel.mean()), float(channel.std())))
        for gradient in (gx, gy):
            absolute = np.abs(gradient)
            values.extend((float(absolute.mean()), float(absolute.std()), float(absolute.max(initial=0.0))))

        values.extend(self._histogram(gray, bins=32))
        for channel in channels:
            values.extend(self._histogram(channel, bins=16))
        values.extend(self._grid_means(gray, rows=8, columns=8))
        values.extend(self._grid_means(np.abs(gx), rows=4, columns=4))
        values.extend(self._grid_means(np.abs(gy), rows=4, columns=4))

        if len(values) < self._width:
            values.extend(0.0 for _ in range(self._width - len(values)))
        return tuple(float(value) for value in values[: self._width])

    @staticmethod
    def _histogram(image: np.ndarray, *, bins: int) -> list[float]:
        counts, _ = np.histogram(image, bins=bins, range=(0.0, 1.0))
        denominator = max(1, int(image.size))
        return [float(value) / denominator for value in counts]

    @staticmethod
    def _grid_means(image: np.ndarray, *, rows: int, columns: int) -> list[float]:
        row_groups = np.array_split(image, rows, axis=0)
        values: list[float] = []
        for row_group in row_groups:
            column_groups = np.array_split(row_group, columns, axis=1)
            values.extend(float(cell.mean()) if cell.size else 0.0 for cell in column_groups)
        return values
