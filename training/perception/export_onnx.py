from __future__ import annotations

from pathlib import Path
from typing import Mapping

import numpy as np

from services.perception.reference import NumpyMLDP
from .model import OUTPUT_NAMES, build_torch_model

INPUT_NAMES = (
    "cam_feat", "cam_mask", "cam_age", "proc_feat", "proc_mask", "proc_age",
    "cmd_future", "cmd_mask",
)


def onnx_available() -> bool:
    try:
        import onnx  # noqa: F401
    except ImportError:
        return False
    return True


def onnxruntime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def export_onnx(reference: NumpyMLDP, example: Mapping[str, np.ndarray], path: str | Path) -> Path:
    """Export the static MLDP graph. Torch and ONNX remain optional dependencies."""
    try:
        import torch
        import onnx  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("torch and onnx are optional and required only for export") from exc
    model = build_torch_model(reference).eval()
    tensors = tuple(torch.from_numpy(np.asarray(example[name])) for name in INPUT_NAMES)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            model,
            tensors,
            destination,
            input_names=list(INPUT_NAMES),
            output_names=list(OUTPUT_NAMES),
            opset_version=18,
            do_constant_folding=True,
            dynamic_axes=None,
        )
    return destination


def run_onnx(path: str | Path, inputs: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError("onnxruntime is optional and required only for ONNX inference") from exc
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    outputs = session.run(list(OUTPUT_NAMES), {name: np.asarray(inputs[name]) for name in INPUT_NAMES})
    return dict(zip(OUTPUT_NAMES, outputs))
