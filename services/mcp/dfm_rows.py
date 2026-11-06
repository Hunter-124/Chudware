"""Dispatcher for the geometry-derived DfM row producers used by ``dfm_check``.

Each producer module exposes::

    PROCESSES: frozenset[str]          # canonical process ids the module covers
    CHECKS: tuple[str, ...]            # row names it can emit (stable ids)
    def rows(ctx: DfmContext, names: list[str]) -> list[dict]

``names`` is the subset of ``CHECKS`` the caller asked for. ``rows`` returns rows built with
``services.mcp.dfm_common.make_row``; it may raise ``DfmEvidenceError`` / ``MissingRule`` and
this dispatcher then emits a ``not_evaluated`` row per requested name carrying the reason,
so a missing measurement can never read as a pass.

Module registry (a module that is missing from disk is a bug, not a skip: the dispatcher
reports it as not_evaluated instead of silently dropping the rows).
"""
from __future__ import annotations

import importlib
from typing import Iterable

from services.mcp.dfm_common import DfmContext, not_evaluated

_PRODUCER_MODULES = (
    "services.mcp.dfm_rows_additive",     # FDM/SLA/SLS/MJF corner + anisotropy rows
    "services.mcp.dfm_rows_machining",    # CNC mill/lathe rows, hole standards, tolerance feasibility
    "services.mcp.dfm_rows_sheetmetal",   # sheet-metal bend rules
    "services.mcp.dfm_rows_molding",      # injection/cast draft, undercut, rib, uniformity
)


def _load(name: str):
    return importlib.import_module(name)


def producer_checks() -> dict[str, tuple[str, ...]]:
    """{module: CHECKS} for the registered producers that import cleanly."""
    out: dict[str, tuple[str, ...]] = {}
    for mod_name in _PRODUCER_MODULES:
        try:
            out[mod_name] = tuple(_load(mod_name).CHECKS)
        except Exception:  # noqa: BLE001 - listing must not fail because one producer broke
            continue
    return out


def collect(ctx: DfmContext, only: Iterable[str] | None = None) -> list[dict]:
    """All requested rows from every producer that covers ``ctx.process``."""
    selected = None if only is None else set(only)
    rows: list[dict] = []
    for mod_name in _PRODUCER_MODULES:
        try:
            mod = _load(mod_name)
        except Exception as exc:  # noqa: BLE001
            # Unknown which checks the broken module owns: only report when the caller
            # asked for exact names we cannot place, otherwise say so once.
            rows.append(not_evaluated(
                mod_name.rsplit(".", 1)[-1], f"DfM row module {mod_name} failed to import: {exc}"))
            continue
        if ctx.process not in mod.PROCESSES:
            continue
        names = [c for c in mod.CHECKS
                 if ctx.want(c) and (selected is None or c in selected)]
        if not names:
            continue
        try:
            produced = mod.rows(ctx, names)
        except Exception as exc:  # noqa: BLE001 - includes DfmEvidenceError / MissingRule
            produced = [not_evaluated(n, str(exc) or exc.__class__.__name__) for n in names]
        rows.extend(produced)
    return rows
