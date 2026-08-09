"""Optional batched accelerators for the frozen viewpoint geometry oracle.

Importing this package never imports Torch.  :func:`evaluate_many` selects CUDA, then
Torch CPU, then the frozen NumPy implementation at call time.
"""

from .torch_oracle import BackendInfo, evaluate_grid, evaluate_many

__all__ = ("BackendInfo", "evaluate_grid", "evaluate_many")
