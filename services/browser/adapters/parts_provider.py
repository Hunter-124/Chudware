"""services/browser/adapters/parts_provider.py — CatalogProvider wrapper.

Presents a :class:`services.browser.adapters.SourceAdapter` (search/
acquire) as a ``services.parts.hub`` ``CatalogProvider``
(``contracts/parts.md`` §3): live-search-only (``count``/``iter_records``
raise ``NotImplementedError``), domain
``"catalog"`` — the seam ``contracts/parts.md`` §6.3 reserved for "paid
catalog providers" (TraceParts/Werk24). This is the user-authenticated
browser-session mechanism for the same catalogs instead of a paid API; both
can coexist behind the same ``CatalogProvider`` protocol.

Zero-profile behavior: when no browser login profile exists for
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

from . import SourceAdapter


class BrowserCatalogAdapter:
    """``CatalogProvider`` wrapping one ``SourceAdapter`` instance.

    ``part_id`` scheme: ``"catalog:<site>::<url>"``. Colons inside a
    ``catalog_id`` are allowed per ``contracts/parts.md`` §0 (split on the
    FIRST colon only) so ``catalog_id`` is simply ``"<site>::<url>"``.

    Search results are cached in-memory (``part_id`` -> the adapter's result
    stub) so a subsequent :meth:`get` in the same process can re-run
    ``acquire()`` without re-searching. A ``get`` for a ``part_id`` from a
    prior process (cache miss) still works — the URL is recovered from the
    ``part_id`` itself, just with an empty ``title`` until acquired.
    """

    domain = "catalog"

    def __init__(self, source_adapter: SourceAdapter):
        self._adapter = source_adapter
        self._site = source_adapter.key
        #: Public vendor site key (e.g. "digikey", "mcmaster") — lets the hub
        #: restrict a routed search to only the vendors that fit a category
        #: without reaching into a private attribute.
        self.site = source_adapter.key
        self.provider_id = f"browser_{self._site}"
        self._cache: dict[str, dict] = {}

    def _part_id(self, result: dict) -> str:
        return f"catalog:{self._site}::{result.get('url', '')}"

    def search(self, query: str, *, domain: str | None = None,
              filters: dict | None = None) -> list[dict]:
        if domain and domain != self.domain:
            return []
        # Anonymous search is allowed (session.browser_fetch auto-creates a
        # profile); login-gated sites degrade to login_required stubs upstream.
        hits = self._adapter.search(query)
        records: list[dict] = []
        for hit in hits:
            if hit.get("ok") is False:
                records.append(hit)  # e.g. login_required — actionable, pass through as-is
                continue
            part_id = self._part_id(hit)
            self._cache[part_id] = hit
            # Carry the product thumbnail (and any sku the adapter surfaced) through
            # to the record's params so the GUI grid can show an image straight from
            # the search hit — no second acquire() round-trip. Without this the field
            # was dropped here and every live result rendered as a bare glyph.
            params: dict = {"site": self._site, "url": hit.get("url", "")}
            thumb = hit.get("thumbnail") or hit.get("image_url") or hit.get("image")
            if thumb:
                params["thumbnail"] = thumb
            if hit.get("sku"):
                params["sku"] = hit["sku"]
            records.append({
                "part_id": part_id,
                "domain": self.domain,
                "title": hit.get("title", ""),
                "standard": None,
                "designation": None,
                "params": params,
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
        result = self._cache.get(part_id)
        if result is None:
            result = {"title": "", "url": part_id[len(prefix):], "site": self._site}
        outcome = self._adapter.acquire(result)
        if not outcome.get("ok"):
            # Acquire produced no file (login_required / terms_confirmation_required /
            # distributor_only / no_download_link). Stamp identity + a link-out so the
            # downstream seam (place() / part_get / part_place) can surface an actionable
            # next step — run vendor_login(site) then retry — and the record stays usable
            # as a reference. Additive: never overwrites the adapter's reason/ask_user.
            enriched = dict(outcome)
            enriched.setdefault("part_id", part_id)
            enriched.setdefault("site", self._site)
            enriched.setdefault("url", result.get("url", ""))
            enriched.setdefault("source_url", result.get("url", ""))
            enriched.setdefault("title", result.get("title", ""))
            return enriched
        # Fold the acquire envelope's vendor-specific extras (sku / price / availability / asin /
        # lcsc / stock / ... — everything beyond the standard envelope keys) into params so
        # they land on the part record: vendor(site)/SKU/URL/fetched-at metadata for
        # BOM & sourcing. Additive — never renames the existing site/url keys.
        _ENVELOPE_KEYS = {"ok", "source_url", "fetched_ts", "session_profile_used",
                          "license_note", "artifact_path", "title"}
        params = {"site": self._site, "url": result.get("url", ""),
                  "fetched_ts": outcome.get("fetched_ts")}
        for k, v in outcome.items():
            if k not in _ENVELOPE_KEYS and v is not None:
                params[k] = v
        # geometry_ref only when a CAD file was actually downloaded (McMaster STEP, GrabCAD,
        # TraceParts). Commerce/parts vendors (Amazon, JLCPCB, PCBWay) return artifact_path=None
        # — a link-out + structured price/availability record, no importable geometry. (Using
        # outcome.get avoids a KeyError: provenance_envelope omits artifact_path when it is None.)
        artifact = outcome.get("artifact_path")
        geometry_ref = {"kind": "import_file", "path": artifact} if artifact else None
        return {
            "part_id": part_id,
            "domain": self.domain,
            "title": result.get("title", ""),
            "standard": None,
            "designation": None,
            "params": params,
            "geometry_ref": geometry_ref,
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
