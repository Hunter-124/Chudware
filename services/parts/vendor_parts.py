"""Supplier/vendor part cache and import helpers.

The first supported workflow is intentionally compliance-friendly:

* register a CAD file the user downloaded manually or through an approved supplier API;
* cache supplier metadata next to the CAD file under %APPDATA%/Chudware/vendor_parts;
* expose compact records that a small local model can reason over without re-reading catalogs;
* insert cached CAD into a document as normal `import` features.

McMaster-Carr publishes CAD downloads and an approved Product Information API for authorized
customers. This module does not scrape their website. API adapters can populate the same records
once credentials/certificates are configured.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from services import paths


CAD_EXTS = {".step": "step", ".stp": "step", ".igs": "iges", ".iges": "iges", ".brep": "brep",
            ".stl": "stl"}
INDEX = paths.VENDOR_PARTS / "index.json"


@dataclass
class VendorPart:
    supplier: str
    part_number: str
    description: str = ""
    cad_path: str = ""
    cad_format: str = ""
    product_url: str = ""
    specs: dict[str, Any] = field(default_factory=dict)
    source: str = "manual"
    added_ts: float = field(default_factory=time.time)
    # Optional: mtime of the on-disk source file when last registered. Used by the watched
    # parts-library scanner to skip unchanged files (idempotent re-scan). C++ core ignores it.
    source_mtime: float = 0.0
    # --- Sourcing/pricing fields (additive; see contracts/sourcing_schema.md). ---
    # All optional so records registered without a price feed remain valid and unchanged. A
    # provider that returns pricing populates these; consumers must treat None as "unknown".
    unit_price: float | None = None          # price for a single unit at qty 1, in `currency`
    currency: str = "USD"                    # ISO-4217 code the prices are quoted in
    price_breaks: list[dict] = field(default_factory=list)  # [{"qty": int, "unit_price": float}]
    stock: int | None = None                 # units available now; None = unknown, 0 = out of stock
    lead_time_days: int | None = None        # business days to ship if not in stock
    moq: int | None = None                   # minimum order quantity
    fetched_ts: float | None = None          # epoch seconds when pricing/stock was last fetched

    @property
    def key(self) -> str:
        return f"{slug(self.supplier)}::{slug(self.part_number)}"


def slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value).strip()).strip("-.").lower()
    return s or "part"


def _load_index() -> dict[str, dict]:
    if not INDEX.exists():
        return {}
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(data: dict[str, dict]) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=INDEX.parent,
            prefix=f".{INDEX.name}.", suffix=".tmp", delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, INDEX)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _detect_format(path: Path) -> str:
    fmt = CAD_EXTS.get(path.suffix.lower())
    if not fmt:
        raise ValueError(f"unsupported CAD extension '{path.suffix}'")
    return fmt


def _part_dir(part: VendorPart) -> Path:
    root = paths.VENDOR_PARTS.resolve()
    dest = (root / slug(part.supplier) / slug(part.part_number)).resolve()
    if not dest.is_relative_to(root):
        raise ValueError("vendor part path is outside the vendor library")
    return dest


def _prepare_local_cad(*, supplier: str, part_number: str, cad_path: str,
                       description: str = "", product_url: str = "",
                       specs: dict | None = None, source: str = "manual",
                       copy_file: bool = True,
                       source_mtime: float | None = None) -> tuple[str, dict]:
    if not part_number:
        raise ValueError("part_number is required")
    src = Path(os.path.expandvars(os.path.expanduser(cad_path))).resolve()
    if not src.exists():
        raise FileNotFoundError(f"CAD file not found: {src}")
    part = VendorPart(supplier=supplier, part_number=part_number, description=description,
                      product_url=product_url, specs=specs or {}, source=source,
                      source_mtime=float(source_mtime if source_mtime is not None
                                         else src.stat().st_mtime))
    part.cad_format = _detect_format(src)
    dest_dir = _part_dir(part)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if copy_file:
        shutil.copy2(src, dest)
        part.cad_path = str(dest)
    else:
        part.cad_path = str(src)
    return part.key, asdict(part)


def _rebuild_parts_index() -> None:
    """Best-effort PartsHub refresh after the vendor index is durable."""
    try:
        from services.parts import rebuild_index as _rebuild  # noqa: PLC0415
        _rebuild()
    except Exception:  # noqa: BLE001 — hub optional in pure-metadata contexts
        pass


def register_local_cads(registrations: list[dict[str, Any]], *,
                        index: dict[str, dict] | None = None) -> list[dict]:
    """Register a batch with one vendor-index write and one PartsHub rebuild.

    ``index`` may supply an already-loaded registry snapshot so a scanner can use the same
    read for change detection and registration. Records are fully prepared before the snapshot
    is changed, and the snapshot is atomically replaced before the best-effort hub refresh.
    """
    if not registrations:
        return []
    prepared = [_prepare_local_cad(**registration) for registration in registrations]
    current = _load_index() if index is None else index
    for key, rec in prepared:
        current[key] = rec
    _write_index(current)
    _rebuild_parts_index()
    return [rec for _, rec in prepared]


def register_local_cad(*, supplier: str, part_number: str, cad_path: str,
                       description: str = "", product_url: str = "",
                       specs: dict | None = None, source: str = "manual",
                       copy_file: bool = True, source_mtime: float | None = None) -> dict:
    return register_local_cads([{
        "supplier": supplier,
        "part_number": part_number,
        "cad_path": cad_path,
        "description": description,
        "product_url": product_url,
        "specs": specs,
        "source": source,
        "copy_file": copy_file,
        "source_mtime": source_mtime,
    }])[0]


def list_parts(supplier: str | None = None, query: str | None = None) -> list[dict]:
    idx = _load_index()
    rows = list(idx.values())
    if supplier:
        ss = supplier.lower()
        rows = [r for r in rows if str(r.get("supplier", "")).lower() == ss]
    if query:
        q = query.lower()
        def hay(r: dict) -> str:
            return " ".join([
                str(r.get("supplier", "")), str(r.get("part_number", "")),
                str(r.get("description", "")), json.dumps(r.get("specs", {}), sort_keys=True),
            ]).lower()
        rows = [r for r in rows if q in hay(r)]
    return sorted(rows, key=lambda r: (str(r.get("supplier", "")), str(r.get("part_number", ""))))


def get_part(supplier: str, part_number: str) -> dict:
    key = f"{slug(supplier)}::{slug(part_number)}"
    rec = _load_index().get(key)
    if not rec:
        raise KeyError(f"vendor part not registered: {supplier} {part_number}")
    return rec


def context_card(limit: int = 12, query: str | None = None) -> dict:
    parts = list_parts(query=query)[:max(1, int(limit))]
    lines = []
    for p in parts:
        specs = p.get("specs") or {}
        dims = ", ".join(f"{k}={v}" for k, v in list(specs.items())[:6])
        tail = f" specs: {dims}" if dims else ""
        lines.append(f"- {p.get('supplier')} {p.get('part_number')}: {p.get('description')}{tail}")
    return {
        "count": len(parts),
        "parts": parts,
        "llm_context": "\n".join(lines),
        "capabilities": [
            "Use vendor_part_insert to import cached CAD as a frozen import feature.",
            "Use vendor_part_register_local_cad after the user/API supplies a STEP/BREP/IGES/STL file.",
            "Do not scrape supplier websites; use approved APIs or user-provided downloads.",
        ],
    }


def import_ops_for_part(supplier: str, part_number: str, *, name: str | None = None) -> list[dict]:
    rec = get_part(supplier, part_number)
    cad_path = rec.get("cad_path") or ""
    if not cad_path or not Path(cad_path).exists():
        raise FileNotFoundError(f"registered CAD file is missing for {supplier} {part_number}")
    fname = name or f"{rec.get('supplier')} {rec.get('part_number')}"
    return [{
        "op": "add_feature",
        "feature": {
            "type": "import",
            "name": fname,
            "params": {
                "source_path": cad_path,
                "format": rec.get("cad_format") or None,
                "original_name": rec.get("description") or fname,
                "vendor_supplier": rec.get("supplier"),
                "vendor_part_number": rec.get("part_number"),
                "vendor_product_url": rec.get("product_url", ""),
            },
            "refs": {},
        },
    }]


EmitFn = Callable[[dict], None]


def _vendor_part_register_local_cad(params: dict, _emit: EmitFn) -> dict:
    return register_local_cad(
        supplier=str(params.get("supplier") or "unknown"),
        part_number=str(params.get("part_number") or ""),
        description=str(params.get("description") or ""),
        product_url=str(params.get("product_url") or ""),
        cad_path=str(params.get("cad_path") or ""),
        specs=dict(params.get("specs") or {}),
        source=str(params.get("source") or "manual"),
        copy_file=bool(params.get("copy_file", True)),
    )


def _vendor_part_list(params: dict, _emit: EmitFn) -> dict:
    return {"parts": list_parts(params.get("supplier"), params.get("query"))}


def _vendor_part_context(params: dict, _emit: EmitFn) -> dict:
    return context_card(limit=int(params.get("limit") or 12), query=params.get("query"))


def _vendor_part_scan_library(params: dict, emit: EmitFn) -> dict:
    # Imported lazily: parts_library imports this module (vendor_parts is the registry owner).
    from services.parts import parts_library
    return parts_library._vendor_part_scan_library(params, emit)


def _vendor_part_fetch(params: dict, emit: EmitFn) -> dict:
    # parts_providers was deleted — the C++ sourcing RPC owns the fetch path
    # (routed via worker_client.cpp); this Python endpoint degrades honestly.
    return {"ok": False, "error": "vendor_part_fetch moved to the C++ core; "
            "the Python provider chain was deleted"}


# vendor_part_insert moved to the C++ core (doc_vendor_part_insert): it mutates a doc by applying
# an `import` feature, which now lives in libchudware. The registry above (register/list/context +
# import_ops_for_part, get_part) is pure metadata and stays in Python — the C++ insert reads the
# SAME index.json this module writes. import_ops_for_part is retained as the op-shape contract.
WORKER_METHODS = {
    "vendor_part_register_local_cad": _vendor_part_register_local_cad,
    "vendor_part_list": _vendor_part_list,
    "vendor_part_context": _vendor_part_context,
    "vendor_part_scan_library": _vendor_part_scan_library,
    "vendor_part_fetch": _vendor_part_fetch,
}
