"""CatalogProvider registry — aicad.parts/1.

Maintains an ordered list of registered CatalogProvider adapters.
Thread-safe for read after initial setup; not designed for concurrent mutation.
"""
from __future__ import annotations

from typing import Iterator


class ProviderRegistry:
    """Ordered registry of CatalogProvider adapters."""

    def __init__(self) -> None:
        self._providers: list[object] = []

    def register(self, provider: object) -> None:
        """Add a provider. Later registrations win on duplicate provider_id."""
        pid = getattr(provider, "provider_id", None)
        if pid is not None:
            self._providers = [p for p in self._providers
                               if getattr(p, "provider_id", None) != pid]
        self._providers.append(provider)

    def providers(self) -> list[object]:
        """Return a snapshot of registered providers."""
        return list(self._providers)

    def get_by_domain(self, domain: str) -> list[object]:
        """Return providers whose .domain matches."""
        return [p for p in self._providers
                if getattr(p, "domain", None) == domain]

    def iter_indexable(self) -> Iterator[object]:
        """Yield providers that support iter_records() (i.e. are indexable)."""
        for p in self._providers:
            if callable(getattr(p, "iter_records", None)):
                yield p
