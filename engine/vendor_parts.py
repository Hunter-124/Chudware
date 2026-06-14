"""Supplier/vendor part cache and import helpers.

The first supported workflow is intentionally compliance-friendly:

* register a CAD file the user downloaded manually or through an approved supplier API;
* cache supplier metadata next to the CAD file under %APPDATA%/AI-CAD/vendor_parts;
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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from engine import paths


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

    @property
    def key(self) -> str:
        return f"{slug(self.supplier)}::{slug(self.part_number)}"


def slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value).strip()).strip("-").lower()
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
    INDEX.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _detect_format(path: Path) -> str:
    fmt = CAD_EXTS.get(path.suffix.lower())
    if not fmt:
        raise ValueError(f"unsupported CAD extension '{path.suffix}'")
    return fmt


def _part_dir(part: VendorPart) -> Path:
    return paths.VENDOR_PARTS / slug(part.supplier) / slug(part.part_number)


def register_local_cad(*, supplier: str, part_number: str, cad_path: str,
                       description: str = "", product_url: str = "",
                       specs: dict | None = None, source: str = "manual",
                       copy_file: bool = True) -> dict:
    if not part_number:
        raise ValueError("part_number is required")
    src = Path(os.path.expandvars(os.path.expanduser(cad_path))).resolve()
    if not src.exists():
        raise FileNotFoundError(f"CAD file not found: {src}")
    part = VendorPart(supplier=supplier, part_number=part_number, description=description,
                      product_url=product_url, specs=specs or {}, source=source)
    part.cad_format = _detect_format(src)
    dest_dir = _part_dir(part)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if copy_file:
        shutil.copy2(src, dest)
        part.cad_path = str(dest)
    else:
        part.cad_path = str(src)

    rec = asdict(part)
    idx = _load_index()
    idx[part.key] = rec
    _write_index(idx)
    return rec


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


def _vendor_part_insert(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess

    supplier = str(params.get("supplier") or "")
    part_number = str(params.get("part_number") or "")
    into = params.get("doc_id") or params.get("into")
    if into:
        sess = docsess.get_session(str(into))
    else:
        sess = docsess.new_session("part", name=f"{supplier} {part_number}".strip() or "Vendor Part")
    ops = import_ops_for_part(supplier, part_number, name=params.get("name"))
    resp = docsess.apply_ops(sess, params.get("base_revision"), ops, emit,
                             coalesce=bool(params.get("coalesce", False)))
    resp["doc_id"] = sess.doc_id
    resp["vendor_part"] = get_part(supplier, part_number)
    return resp


WORKER_METHODS = {
    "vendor_part_register_local_cad": _vendor_part_register_local_cad,
    "vendor_part_list": _vendor_part_list,
    "vendor_part_context": _vendor_part_context,
    "vendor_part_insert": _vendor_part_insert,
}
