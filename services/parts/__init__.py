"""Unified parts library hub — `aicad.parts/1`.

Federated search across all standard-parts catalogs via the CatalogProvider protocol
(contracts/parts.md).

Re-homed from ``engine_oracle/parts_hub/`` to ``services/parts/``
(catalog orchestration — SQLite FTS5 + JSON taxonomy, no geometry — is a
service, not engine kernel). ``engine_oracle/parts_hub/__init__.py`` remains as
a thin compatibility shim re-exporting this package's public API while
``place.py`` awaits its port to ``core/parts/``.

Public API:
    register_provider(provider)   — add a CatalogProvider to the registry
    search(query, ...)            — ranked FTS search across the index
    get(part_id)                  — retrieve a single PartRecord
    rebuild_index()               — (re)build the SQLite FTS5 index from all providers
"""
from __future__ import annotations

from .registry import ProviderRegistry
from .index import PartsIndex
from .hub import PartsHub

# Module-level singleton hub (lazy-init on first call).
_hub: PartsHub | None = None


def _get_hub() -> PartsHub:
    global _hub
    if _hub is None:
        _hub = PartsHub()
        _hub.bootstrap()
    return _hub


def register_provider(provider: object) -> None:
    """Add a CatalogProvider to the default hub."""
    _get_hub().register(provider)


def search(query: str, *,
           domain: str | list[str] | None = None,
           filters: dict | None = None,
           limit: int = 20,
           sites: list[str] | None = None,
           include_live: bool = True) -> list[dict]:
    """Full-text search across all indexed providers. Returns ranked PartRecord dicts.

    ``sites`` restricts the live vendor fan-out to a category's fitting vendors
    (taxonomy routing) — see :meth:`PartsHub.search`. ``include_live=False`` returns
    only the instant local FTS layer (no browser fan-out)."""
    return _get_hub().search(query, domain=domain, filters=filters, limit=limit,
                             sites=sites, include_live=include_live)


def get(part_id: str) -> dict | None:
    """Retrieve a single PartRecord by part_id. Returns None if not found."""
    return _get_hub().get(part_id)


def rebuild_index() -> int:
    """Rebuild the FTS index from all registered providers. Returns record count."""
    return _get_hub().rebuild_index()


def browser_bootstrap_error() -> str | None:
    """Diagnostic string when browser catalog adapters failed to register, else None.

    Local FTS search keeps working either way — this is an honest status signal,
    never a silent swallow of optional-peer import failures.
    """
    return _get_hub().browser_bootstrap_error


__all__ = [
    "register_provider",
    "search",
    "get",
    "rebuild_index",
    "browser_bootstrap_error",
    "PartsHub",
    "ProviderRegistry",
    "PartsIndex",
]
