"""CatalogProvider adapter for the core/parts parametric families.

Wraps the `parts_catalog_list` core RPC (same call as the `list_standard_parts`
MCP tool in cad_tools.py). Domain = 'mech_family'.

No core dependency at import time — the adapter degrades gracefully (returns zero
records) when the native libchudware.so is absent (offline / CI without CHUDWARE_LIBCHUDWARE).

Provenance / license: core/parts parametric families; proprietary Chudware catalog.
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)


class CoreFamiliesAdapter:
    """CatalogProvider wrapping the C++ core/parts catalog via the parts_make RPC.

    Enumerates every family+size combination returned by `parts_catalog_list`.
    Each record's geometry_ref carries a 'core_family' recipe so the hub can call
    `parts_make` to instantiate it.
    """

    domain: str = "mech_family"
    provider_id: str = "core_families"

    def __init__(self) -> None:
        self._catalog: list[dict] | None = None

    def _load_catalog(self) -> list[dict]:
        """Load the core parts catalog. Returns [] when the core is absent."""
        if self._catalog is not None:
            return self._catalog
        try:
            # MUST be the PACKAGE module: a bare `import core_bridge` off a sys.path hack
            # leaves __package__ empty, so core_bridge.call()'s own
            # `from .daemon_bridge import DaemonError` (core_bridge.py:377, taken whenever
            # daemon mode is on — the DEFAULT) raises "attempted relative import with no known
            # parent package". The debug-only except below then swallowed it and this adapter
            # reported an EMPTY catalog, silently dropping the whole mech_family domain from
            # the Parts Library. Same defect the placer and the thumbnail renderer had.
            from services.mcp import core_bridge as cb
            result = cb.call("parts_catalog_list", {})
            families = result if isinstance(result, list) else result.get("families", [])
            self._catalog = families if isinstance(families, list) else []
        except Exception as exc:  # noqa: BLE001 — core absent is expected in CI
            log.debug("CoreFamiliesAdapter: core unavailable (%s) — returning empty catalog", exc)
            self._catalog = []
        return self._catalog

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Direct search (fallback; index is primary)."""
        norm = query.strip().lower()
        results = []
        for rec in self.iter_records():
            title = (rec.get("title") or "").lower()
            designation = (rec.get("designation") or "").lower()
            if norm in title or norm in designation:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        """Return a PartRecord by part_id."""
        if not part_id or not part_id.startswith("mech_family:"):
            return None
        rest = part_id[len("mech_family:"):]
        # rest = "<family>:<size>"
        sub = rest.split(":", 1)
        if len(sub) != 2:
            return None
        family, size = sub
        for rec in self.iter_records():
            if rec.get("designation") == size and _family_of(rec) == family:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        """Yield one PartRecord per (family, size) in the core catalog."""
        catalog = self._load_catalog()
        for fam_entry in catalog:
            if not isinstance(fam_entry, dict):
                continue
            family = fam_entry.get("key") or fam_entry.get("family") or ""
            if not family:
                continue
            description = fam_entry.get("description") or fam_entry.get("name") or family
            sizes = fam_entry.get("sizes") or []
            if not sizes:
                continue
            for size in sizes:
                yield _make_record(family, str(size), description)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _family_of(rec: dict) -> str:
    """Extract the family key from a mech_family PartRecord's geometry_ref."""
    gr = rec.get("geometry_ref") or {}
    return gr.get("family") or ""


def _make_record(family: str, size: str, description: str) -> dict:
    catalog_id = f"{family}:{size}"
    part_id = f"mech_family:{catalog_id}"
    title = f"{description} {size}".strip()
    return {
        "part_id":      part_id,
        "domain":       "mech_family",
        "title":        title,
        "standard":     None,
        "designation":  size,
        "params":       {"family": family, "size": size},
        "geometry_ref": {
            "kind":   "core_family",
            "family": family,
            "size":   size,
        },
        "sourcing":     [],
        "provenance":   "Chudware core/parts parametric catalog; proprietary.",
        "license":      "proprietary",
    }
