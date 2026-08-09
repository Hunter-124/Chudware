"""Learned, task-conditioned viewpoint ranking.

The runtime reference path depends only on NumPy.  Torch is imported lazily by
``training.viewpoint.torch_model`` and the training entry point.
"""
from . import runtime as _runtime  # Establish native thread/scratch defaults before NumPy imports.


from .data import ViewpointBatch, encode_candidate, split_by_part
from .model import ModelSpec, NumpyViewpointPolicy, PolicyWeights

__all__ = [
    "ModelSpec",
    "NumpyViewpointPolicy",
    "PolicyWeights",
    "ViewpointBatch",
    "encode_candidate",
    "split_by_part",
]
