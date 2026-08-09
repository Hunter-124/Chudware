"""Learned, task-conditioned viewpoint ranking.

The runtime reference path depends only on NumPy.  Torch is imported lazily by
``training.viewpoint.torch_model`` and the training entry point.
"""

from .data import ViewpointBatch, split_by_part
from .model import ModelSpec, NumpyViewpointPolicy, PolicyWeights

__all__ = [
    "ModelSpec",
    "NumpyViewpointPolicy",
    "PolicyWeights",
    "ViewpointBatch",
    "split_by_part",
]
