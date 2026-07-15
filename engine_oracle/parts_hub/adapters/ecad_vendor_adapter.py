"""CatalogProvider adapter for live ECAD vendor search (T-P2).

Live-only provider (no iter_records — raises NotImplementedError so the hub
skips indexing). Wraps the B1b ProviderChain (Nexar → Digi-Key → Mouser)
from engine_oracle/parts_providers.py for live MPN lookups.

Domain: "ecad". provider_id: "ecad_vendor".

Raises→chain-None contract (work/questions/T-B1b.md ruling):
  - each provider raises ProviderError on missing creds / no results
  - ProviderChain catches, falls through; returns None when all decline
  - this adapter maps None → [] (graceful, CI-safe)

Live results are marked source:"live" per contracts/parts.md §6.1 and the T-P2 DoD.
Results are NEVER indexed (no iter_records).
No scraping, no McMaster (licensing — see contracts/licenses.md).
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)


class EcadVendorAdapter:
    """CatalogProvider for live ECAD vendor search via Nexar/DK/Mouser ProviderChain.

    search() queries the live chain and wraps results as PartRecords with
    source:"live". iter_records() raises NotImplementedError — the index skips
    this provider and falls back to live search at query time.

    Degrades gracefully (returns []) when:
      - engine_oracle.parts_providers is unavailable
      - all vendor providers lack credentials / are offline
      - the provider chain returns None
    """

    domain: str = "ecad"
    provider_id: str = "ecad_vendor"

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Live MPN/keyword search via the Nexar/DK/Mouser ProviderChain."""
        if domain and domain != "ecad":
            return []
        norm = query.strip()
        if not norm:
            return []
        chain = _get_chain()
        if chain is None:
            return []
        try:
            result = chain.fetch(norm)
        except Exception as exc:  # noqa: BLE001
            log.debug("EcadVendorAdapter: chain.fetch failed: %s", exc)
            return []
        if result is None:
            return []
        # result is a VendorPart-like dict (aicad.sourcing/1)
        return [_vendor_part_to_record(result)]

    def get(self, part_id: str) -> dict | None:
        """Look up a live vendor part by MPN encoded in the part_id."""
        if not part_id or not part_id.startswith("ecad:vendor:"):
            return None
        mpn = part_id[len("ecad:vendor:"):]
        results = self.search(mpn)
        for rec in results:
            if rec.get("part_id") == part_id:
                return rec
        return results[0] if results else None

    def count(self) -> int:
        """Live-search-only providers cannot enumerate; the index skips them."""
        raise NotImplementedError("ecad_vendor is a live-search-only provider")

    def iter_records(self) -> Iterator[dict]:
        """Live-search-only — not indexable."""
        raise NotImplementedError("ecad_vendor is a live-search-only provider")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_chain():
    """Return a ProviderChain instance, or None if unavailable."""
    try:
        from ...parts_providers import default_pricing_chain
        return default_pricing_chain()
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadVendorAdapter: ProviderChain unavailable: %s", exc)
        return None


def _vendor_part_to_record(vp: dict) -> dict:
    """Convert a VendorPart dict (aicad.sourcing/1) to an ecad PartRecord.

    Marks source:"live" per T-P2 DoD. Params carry the VendorPart fields
    verbatim so downstream tools have full sourcing context.
    """
    mpn = vp.get("mpn") or vp.get("part_number") or ""
    manufacturer = vp.get("manufacturer") or ""
    description = vp.get("description") or ""
    designation = mpn

    title_parts = [manufacturer, mpn, description]
    title = " ".join(p for p in title_parts if p).strip()
    if not title:
        title = f"Electronic component {mpn}"

    params: dict = {
        "nominal_size": mpn,
        "standard":     None,
        "kind":         "symbol",  # ecad domain default
    }
    # Carry VendorPart fields verbatim
    for k, v in vp.items():
        params[k] = v

    return {
        "part_id":      f"ecad:vendor:{mpn}",
        "domain":       "ecad",
        "title":        title,
        "standard":     None,
        "designation":  designation,
        "params":       params,
        "geometry_ref": None,   # no local geometry for live vendor results
        "sourcing":     [vp],   # raw VendorPart as sourcing entry
        "provenance":   f"Live vendor search (Nexar/Digi-Key/Mouser); MPN={mpn!r}.",
        "license":      "user-supplied",
        "source":       "live",  # additive optional field — T-P2 DoD marking
    }
