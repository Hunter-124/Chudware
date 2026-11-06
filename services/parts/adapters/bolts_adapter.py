"""CatalogProvider adapter for the BOLTS-format fastener/bearing tables.

Wraps services/parts/bolts_loader.py + the clean-room ISO/DIN data under
services/parts/data/bolts/. Covers domain=fastener and domain=bearing.

No rewrite, no data move — pure adapter by design.

Provenance / license: clean-room ISO/DIN dimensional data; see services/parts/data/bolts/README.md
and contracts/licenses.md. NOT BOLTS GPL data.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from services.parts import bolts_loader
from services.parts.bolts_loader import Standard


# Family-hint text to include in the search index alongside the family key.
_FAMILY_SYNONYMS: dict[str, str] = {
    "shcs":       "socket head cap screw socket head shcs",
    "bhcs":       "button head cap screw button head bhcs",
    "fhcs":       "flat head cap screw flat head fhcs countersunk",
    "hex_bolt":   "hex head bolt hex bolt hhcs hex head",
    "hex_nut":    "hex nut nut",
    "washer_flat": "flat washer washer",
    "bearing":    "deep groove ball bearing bearing",
}


class BoltsAdapter:
    """CatalogProvider for fasteners and bearings from the BOLTS/ISO tables.

    Covers two domains: 'fastener' and 'bearing'. The provider_id is 'bolts'.
    Both domains share the same underlying tables; domain assignment follows
    Standard.kind ('fastener' | 'bearing').
    """

    domain: str = "fastener"        # primary; also emits "bearing" records
    provider_id: str = "bolts"

    def __init__(self, data_dir: str | None = None) -> None:
        self._data_dir = data_dir
        self._tables: dict[str, Standard] | None = None

    def _get_tables(self) -> dict[str, Standard]:
        if self._tables is None:
            self._tables = bolts_loader.load_bolts_tables(self._data_dir)
        return self._tables

    def invalidate_cache(self) -> None:
        """Reload the dimensional tables before rebuilding the index."""
        self._tables = None

    def fingerprint_paths(self) -> list[Path]:
        root = Path(self._data_dir).expanduser() if self._data_dir else bolts_loader.DATA_DIR
        return [root, *bolts_loader._iter_blt_files(root)]

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Direct search (used by PartsHub fallback; index is primary path)."""
        norm = query.strip().lower()
        results = []
        for rec in self.iter_records():
            if domain and rec.get("domain") != domain:
                continue
            text = " ".join([str(rec.get("title") or ""),
                             str(rec.get("designation") or ""),
                             str(rec.get("standard") or ""),
                             *rec.get("aliases", [])]).lower()
            if norm in text:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        """Return a PartRecord by part_id, or None."""
        if not part_id:
            return None
        # part_id = "<domain>:<catalog_id>" where catalog_id = "<std_id>:<size>"
        # Split on first colon only for the domain prefix, then again for std:size.
        parts = part_id.split(":", 1)
        if len(parts) != 2:
            return None
        domain_prefix, rest = parts
        if domain_prefix not in ("fastener", "bearing"):
            return None
        # rest = "ISO4762:M8" or "ISO15:6205"
        sub = rest.split(":", 1)
        if len(sub) != 2:
            return None
        std_id, size = sub
        tables = self._get_tables()
        # Prefer standard IDs over aliases, and never pick an arbitrary standard
        # when a normalized identifier or alias names more than one table.
        norm = lambda value: re.sub(r"[\s_-]+", "", value).casefold()
        candidates = [std for std in tables.values()
                      if std.kind == domain_prefix and norm(std.id) == norm(std_id)]
        if not candidates:
            candidates = [std for std in tables.values()
                          if std.kind == domain_prefix
                          and any(norm(alias) == norm(std_id) for alias in std.aliases)]
        if len(candidates) != 1:
            return None
        std = candidates[0]
        for canonical_size, params in std.sizes.items():
            if canonical_size.strip().upper().replace(" ", "") == size.strip().upper().replace(" ", ""):
                return _make_record(std, canonical_size, params)
        return None

    def count(self) -> int:
        """Return the number of (standard, size) pairs."""
        return sum(len(std.sizes) for std in self._get_tables().values())

    def iter_records(self) -> Iterator[dict]:
        """Yield one PartRecord per (standard, size) pair."""
        tables = self._get_tables()
        for std in tables.values():
            for size, params in std.sizes.items():
                yield _make_record(std, size, params)


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def _make_record(std: Standard, size: str, params: dict) -> dict:
    domain = std.kind  # "fastener" | "bearing"
    catalog_id = f"{std.id}:{size}"
    part_id = f"{domain}:{catalog_id}"

    synonyms = _FAMILY_SYNONYMS.get(std.family, "")
    title = f"{std.name} {size}".strip()

    # geometry_ref: all bolts/bearings go via core_family (parts_make RPC).
    geometry_ref: dict = {
        "kind":   "core_family",
        "family": std.family,
        "size":   size,
    }
    if std.needs_length:
        geometry_ref["needs_length"] = True

    return {
        "part_id":      part_id,
        "domain":       domain,
        "title":        title,
        "standard":     std.id,
        # The loader already carries the standard's alternate designations
        # ("DIN 912" / "SHCS" for ISO 4762) — publish them so they are searchable.
        # index._record_to_row folds `aliases` into the FTS text; without it a
        # buyer's "DIN 912 M3" found no fastener in the library at all.
        "aliases":      [*std.aliases, *([synonyms] if synonyms else [])],
        "designation":  size,
        "family":       std.name,
        "params":       dict(params),
        "geometry_ref": geometry_ref,
        "sourcing":     [],
        "provenance":   (
            f"ISO/DIN clean-room dimensional data; standard {std.id!r}. "
            "Source: services/parts/data/bolts/ (not BOLTS GPL data). "
            "See contracts/licenses.md."
        ),
        "license":      "clean-room",
    }
