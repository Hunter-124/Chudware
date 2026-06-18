"""Deterministic AI orchestrator (M9) — role subagents over the doc-op vocabulary.

A Python state machine (INTAKE -> PLAN -> MODEL -> REVIEW -> FIX -> MEMORY) decides every
transition; the model only fills in each stage's bounded prompt. Built for 4-8k-context local
models: every agent call gets a FRESH, size-bounded context assembled from a shared DesignState.
Full transparency via the events.md §3 hierarchy (streamed + persisted to events.jsonl).

The legacy ``agent/loop.py`` (``agent_design``) is untouched and remains the supported path.

Public entry point: :func:`agent.orchestrator.run.run_design`.
"""
from __future__ import annotations

from .run import RunResult, run_design  # noqa: F401
