from __future__ import annotations

from typing import Any, Mapping, Protocol

import numpy as np


class Redactor(Protocol):
    """Site-owned policy hook; capture calls it before every persistent write."""

    def redact_frame(self, camera_id: str, t_mono_ns: int, frame: np.ndarray) -> np.ndarray: ...

    def redact_record(self, kind: str, record: Mapping[str, Any]) -> Mapping[str, Any]: ...


class PassThroughRedactor:
    """Explicit policy for scenes certified to need no pixel or field masking."""

    def redact_frame(self, camera_id: str, t_mono_ns: int, frame: np.ndarray) -> np.ndarray:
        del camera_id, t_mono_ns
        return np.array(frame, copy=True, order="C")

    def redact_record(self, kind: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
        del kind
        return dict(record)
