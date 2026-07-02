"""Leaf module holding the BenchDriver descriptor.

Kept separate from `runner.py` on purpose: bench modules import `BenchDriver` to expose
their `DRIVER`, and the runner imports the bench modules to auto-discover those DRIVERs. If
`BenchDriver` lived in `runner.py`, that would be a circular import — and worse, a
re-entrant one where whichever bench first triggered the runner import failed to register.
A leaf module with no back-edge to the bench package makes discovery deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# BenchResult is only referenced in the type hint of `run`; import lazily-safe from schema
# (schema.py imports nothing from this package's registration path, so no cycle).
from .schema import BenchResult


@dataclass
class BenchDriver:
    bench_id: str
    # run(smoke: bool, workdir: str) -> BenchResult
    run: Callable[[bool, str], BenchResult]
