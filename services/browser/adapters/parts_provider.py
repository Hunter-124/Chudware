"""services/browser/adapters/parts_provider.py — CatalogProvider wrapper (T-X3).

Presents a T-X2 :class:`services.browser.adapters.SourceAdapter` (search/
acquire) as an ``engine_oracle.parts_hub`` ``CatalogProvider``
(``contracts/parts.md`` §3): live-search-only (``count``/``iter_records``
raise ``NotImplementedError``, same as ``EcadVendorAdapter``), domain
``"catalog"`` — the seam ``contracts/parts.md`` §6.3 reserved for "paid
catalog providers" (TraceParts/Werk24). This is the user-authenticated
browser-session mechanism for the same catalogs instead of a paid API; both
can coexist behind the same ``CatalogProvider`` protocol.

Zero-profile behavior (T-X3 DoD): when no browser login profile exists for
this adapter's site, :meth:`search`/:meth:`get` return empty/``None``
SILENTLY — the provider contributes nothing, exactly like a live vendor
provider with no configured API key (``contracts/parts.md`` §3, "degrade
gracefully... never a traceback"). A profile that exists but hits a login
wall or a terms click-through mid-session still surfaces the ``ask_user``
shape via :meth:`search`/:meth:`get` — that IS new, actionable information
for the caller, not silent degradation.
"""
from __future__ import annotations

from typing import Iterator

from ..profiles import has_profile
from . import SourceAdapter


class BrowserCatalogAdapter:
    """``CatalogProvider`` wrapping one T-X2 ``SourceAdapter`` instance.

    ``part_id`` scheme: ``"catalog:<site>::<url>"``. Colons inside a
    ``catalog_id`` are allowed per ``contracts/parts.md`` §0 (split on the
    FIRST colon only) so ``catalog_id`` is simply ``"<site>::<url>"``.

    Search results are cached in-memory (``part_id`` -> the T-X2 result
    stub) so a subsequent :meth:`get` in the same process can re-run
    ``acquire()`` without re-searching. A ``get`` for a ``part_id`` from a
    prior process (cache miss) still works — the URL is recovered from the
    ``part_id`` itself, just with an empty ``title`` until acquired.
    """

    domain = "catalog"

    def __init__(self, source_adapter: SourceAdapter):
        self._adapter = source_adapter
        self._site = source_adapter.key
        self.provider_id = f"browser_{self._site}"
        self._cache: dict[str, dict] = {}

    def _part_id(self, result: dict) -> str:
        return f"catalog:{self._site}::{result.get('url', '')}"

    def search(self, query: str, *, domain: str | None = None,
              filters: dict | None = None) -> list[dict]:
        if domain and domain != self.domain:
            return []
        if not has_profile(self._site):
            return []  # silent — same posture as a live vendor provider with no API key
        hits = self._adapter.search(query)
        records: list[dict] = []
        for hit in hits:
            if hit.get("ok") is False:
                records.append(hit)  # e.g. login_required — actionable, pass through as-is
                continue
            part_id = self._part_id(hit)
            self._cache[part_id] = hit
            records.append({
                "part_id": part_id,
                "domain": self.domain,
                "title": hit.get("title", ""),
                "standard": None,
                "designation": None,
                "params": {"site": self._site, "url": hit.get("url", "")},
                "geometry_ref": None,  # not yet downloaded — part_get triggers acquisition
                "sourcing": [],
                "provenance": f"{self._site} search result via the user's own browser "
                              f"session (not yet downloaded — part_get acquires it)",
                "license": "user-supplied",
                "source": "live",
            })
        return records

    def get(self, part_id: str) -> dict | None:
        prefix = f"catalog:{self._site}::"
        if not part_id.startswith(prefix):
            return None
        if not has_profile(self._site):
            return None  # silent — no session, nothing this provider can do
        result = self._cache.get(part_id)
        if result is None:
            result = {"title": "", "url": part_id[len(prefix):], "site": self._site}
        outcome = self._adapter.acquire(result)
        if not outcome.get("ok"):
            return outcome  # login_required / terms_confirmation_required / etc — actionable
        return {
            "part_id": part_id,
            "domain": self.domain,
            "title": result.get("title", ""),
            "standard": None,
            "designation": None,
            "params": {"site": self._site, "url": result.get("url", "")},
            "geometry_ref": {"kind": "import_file", "path": outcome["artifact_path"]},
            "sourcing": [],
            "provenance": f"Acquired via {self._site} under the user's own browser session "
                          f"({outcome['source_url']}, fetched {outcome['fetched_ts']})",
            "license": "user-supplied",
            "license_note": outcome["license_note"],
            "source": "live",
        }

    def count(self) -> int:
        raise NotImplementedError(f"{self.provider_id} is a live-search-only provider")

    def iter_records(self) -> Iterator[dict]:
        raise NotImplementedError(f"{self.provider_id} is a live-search-only provider")
