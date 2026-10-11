"""Task registry: every artifact-graded task, keyed by id.

``REGISTRY`` is built lazily on first access so ``tasks.schema`` (imported by
``evidence``) never drags the task modules — and their check imports — into a cycle.
A task module that does not exist yet is skipped; any other import error propagates.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmarks.artifact_validity.tasks.schema import Task

TASK_MODULES = (
    "benchmarks.artifact_validity.tasks.facilities",
    "benchmarks.artifact_validity.tasks.core",
    "benchmarks.artifact_validity.tasks.solids",
    "benchmarks.artifact_validity.tasks.modeling",
    "benchmarks.artifact_validity.tasks.hard",
)

_REGISTRY: dict[str, "Task"] | None = None


def _build() -> dict[str, "Task"]:
    out: dict[str, Task] = {}
    for name in TASK_MODULES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise
        for task in getattr(mod, "TASKS", ()):
            if task.id in out:
                raise ValueError(f"duplicate task id {task.id!r} ({name})")
            out[task.id] = task
    return out


def __getattr__(attr: str):
    global _REGISTRY
    if attr == "REGISTRY":
        if _REGISTRY is None:
            _REGISTRY = _build()
        return _REGISTRY
    raise AttributeError(attr)
