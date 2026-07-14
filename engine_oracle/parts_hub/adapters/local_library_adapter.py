"""CatalogProvider for the watched local parts library (vendor_parts index).

Federates STEP/STEP+/IGES/BREP files registered via
``engine_oracle.parts_library.scan_library_folder`` /
``engine_oracle.vendor_parts.register_local_cad`` into the PartsHub FTS index
so ``part_search`` finds them by title/part-number and ``part_place`` imports
them as editable docs via ``geometry_ref.kind = "import_file"``.

Indexable (``iter_records`` yields every registered CAD file that still exists
on disk). Removal/replace of a file drops it from the next rebuild; callers that
mutate the vendor_parts index should call
``engine_oracle.parts_hub.rebuild_index()`` afterwards.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator


class LocalLibraryAdapter:
    """Indexable CatalogProvider wrapping the vendor_parts on-disk index."""

    domain: str = "library"
    provider_id: str = "local_library"

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Direct search fallback (index is the primary path)."""
        if domain and domain not in (self.domain, "catalog", "library"):
            return []
        norm = (query or "").strip().lower()
        if not norm:
            return list(self.iter_records())
        hits: list[dict] = []
        for rec in self.iter_records():
            hay = " ".join([
                str(rec.get("title", "")),
                str(rec.get("designation", "")),
                str(rec.get("standard", "")),
                " ".join(str(v) for v in (rec.get("params") or {}).values()),
            ]).lower()
            if norm in hay:
                hits.append(rec)
        return hits

    def get(self, part_id: str) -> dict | None:
        if not part_id or not str(part_id).startswith("library:"):
            return None
        for rec in self.iter_records():
            if rec.get("part_id") == part_id:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        """Yield one PartRecord per vendor_parts entry with an on-disk CAD file."""
        # Prefer the same module identity the scanner/register path mutates
        # (``engine.vendor_parts`` — engine_oracle is a parallel import path that
        # shares INDEX on disk but not the in-process module object tests patch).
        try:
            from engine import vendor_parts  # type: ignore  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            try:
                from engine_oracle import vendor_parts  # noqa: PLC0415
            except Exception:
                return
        try:
            rows = vendor_parts.list_parts()
        except Exception:  # noqa: BLE001
            return
        for row in rows:
            rec = _vendor_row_to_record(row)
            if rec is not None:
                yield rec


def _vendor_row_to_record(row: dict) -> dict | None:
    """Map a vendor_parts index row onto a PartRecord (import_file geometry)."""
    if not isinstance(row, dict):
        return None
    cad_path = str(row.get("cad_path") or "").strip()
    if not cad_path:
        return None
    try:
        if not Path(cad_path).is_file():
            return None  # removed/replaced → drop from indexable set
    except OSError:
        return None

    supplier = str(row.get("supplier") or "library")
    part_number = str(row.get("part_number") or Path(cad_path).stem)
    description = str(row.get("description") or part_number)
    key = str(row.get("key") or f"{supplier}::{part_number}")
    # Prefer the registry key so part_id stays stable across re-scans.
    part_id = f"library:{key}"

    params = dict(row.get("specs") or {})
    if row.get("unit_price") is not None:
        params.setdefault("price_usd", row["unit_price"])
    if row.get("currency"):
        params.setdefault("currency", row["currency"])
    params.setdefault("supplier", supplier)
    params.setdefault("part_number", part_number)
    if row.get("product_url"):
        params.setdefault("product_url", row["product_url"])
    if row.get("cad_format"):
        params.setdefault("cad_format", row["cad_format"])

    sourcing: list[dict] = []
    if row.get("unit_price") is not None or row.get("stock") is not None:
        src_row: dict = {"vendor": supplier}
        if row.get("unit_price") is not None:
            src_row["price"] = row["unit_price"]
            src_row["currency"] = row.get("currency") or "USD"
        if row.get("stock") is not None:
            src_row["stock"] = row["stock"]
        sourcing.append(src_row)

    title = f"{supplier} {part_number} {description}".strip()
    return {
        "part_id": part_id,
        "domain": "library",
        "title": title,
        "standard": supplier,
        "designation": part_number,
        "params": params,
        "geometry_ref": {"kind": "import_file", "path": cad_path},
        "sourcing": sourcing,
        "provenance": (
            f"Watched local parts library ({supplier} {part_number}); "
            f"source={row.get('source') or 'library'}; cad_path={cad_path}"
        ),
        "license": "user",
        "source": "local",
    }


__all__ = ["LocalLibraryAdapter"]
