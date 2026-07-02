"""Leaf module holding the BenchDriver descriptor.

Kept separate from `runner.py` on purpose: bench modules import `BenchDriver` to expose
their `DRIVER`, and the runner imports the bench modules to auto-discover those DRIVERs. If
`BenchDriver` lived in `runner.py`, that would be a circular import — and worse, a
re-entrant one where whichever bench first triggered the runner import failed to register.
A leaf module with no back-edge to the bench package makes discovery deterministic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

# BenchResult is only referenced in the type hint of `run`; import lazily-safe from schema
# (schema.py imports nothing from this package's registration path, so no cycle).
from .schema import BenchResult

# Committed frozen datasets + reports live here. Kept on the leaf module so bench modules
# can resolve their dataset paths without importing `runner` (which would re-form the cycle).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BENCHES_DIR = os.path.join(_REPO_ROOT, "training", "benches")


@dataclass
class BenchDriver:
    bench_id: str
    # run(smoke: bool, workdir: str) -> BenchResult
    run: Callable[[bool, str], BenchResult]
