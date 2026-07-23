"""CatalogProvider adapter for KiCad symbol/footprint libraries (T-P2).

Indexable domain adapter (domain="ecad") that enumerates:
  1. The clean-room starter symbol + footprint library (engine/ecad/library/starter).
  2. User .kicad_sym libraries listed in AICAD_KICAD_SYM_PATH (colon-separated
     paths, same env var as ecad_find_part in ecad_tools.py).

Wraps engine.ecad.library.search (the ecad_find_part surface) for runtime
.search() calls, and directly enumerates starter.SYMBOLS / starter.FOOTPRINTS
plus any user-lib files for iter_records() (index build).

Live search of user libs beyond AICAD_KICAD_SYM_PATH is handled by ecad_vendor
(T-P2 live provider) — this adapter covers the LOCAL, deterministic set only.

geometry_ref.kind = "symbol_footprint" (symbols) per contracts/parts.md §2.

Provenance / license: starter library = clean-room (AI-CAD proprietary).
User libs: license = "user-supplied".
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

log = logging.getLogger(__name__)

_ENV_VAR = "AICAD_KICAD_SYM_PATH"


class EcadKicadAdapter:
    """CatalogProvider for KiCad symbols/footprints (indexable, domain='ecad').

    Enumerates starter + user libs for FTS index build. .search() delegates to
    engine.ecad.library.search for ranked results; falls back to iter_records()
    substring scan when the library module is unavailable.
    """

    domain: str = "ecad"
    provider_id: str = "ecad_kicad"

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Delegate to engine.ecad.library.search (ecad_find_part surface)."""
        if domain and domain != "ecad":
            return []
        norm = query.strip().lower()
        if not norm:
            return list(self.iter_records())
        # Try the library search first
        hits = _lib_search(query)
        if hits:
            return [_hit_to_record(h) for h in hits if h]
        # Fallback: iterate index
        results = []
        for rec in self.iter_records():
            title = (rec.get("title") or "").lower()
            designation = (rec.get("designation") or "").lower()
            if norm in title or norm in designation:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        if not part_id or not part_id.startswith("ecad:"):
            return None
        for rec in self.iter_records():
            if rec.get("part_id") == part_id:
                return rec
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        """Enumerate starter symbols, starter footprints, then user-lib symbols."""
        # Starter symbols
        starter_syms, starter_fps = _load_starter()
        for libid, sdef in starter_syms.items():
            yield _symbol_record(libid, sdef, license_="proprietary",
                                 provenance="AI-CAD clean-room starter symbol library")
        # Starter footprints
        for fpid, fpdef in starter_fps.items():
            yield _footprint_record(fpid, fpdef, license_="proprietary",
                                    provenance="AI-CAD clean-room starter footprint library")
        # User-supplied .kicad_sym libraries
        sym_path = os.environ.get(_ENV_VAR, "")
        for lib_path in (p.strip() for p in sym_path.split(":") if p.strip()):
            yield from _load_user_sym_lib(lib_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_starter() -> tuple[dict, dict]:
    """Return (SYMBOLS, FOOTPRINTS) from starter; empty dicts on failure."""
    try:
        from engine.ecad.library.starter import SYMBOLS, FOOTPRINTS
        return dict(SYMBOLS), dict(FOOTPRINTS)
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: starter unavailable: %s", exc)
        return {}, {}


def _lib_search(query: str) -> list[dict]:
    """Delegate to engine.ecad.library.search; return [] on failure."""
    try:
        from engine.ecad.library import search as _search
        return _search(query) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: lib_search unavailable: %s", exc)
        return []


def _load_user_sym_lib(path: str) -> Iterator[dict]:
    """Load a .kicad_sym file and yield PartRecords for each symbol."""
    try:
        from engine.ecad.library.kicad_sym import load_kicad_sym
        syms = load_kicad_sym(path)
        lib_name = os.path.splitext(os.path.basename(path))[0]
        provenance = f"User-supplied KiCad symbol library: {path}"
        for name, sdef in syms.items():
            libid = f"{lib_name}:{name}"
            yield _symbol_record(libid, sdef, license_="user-supplied",
                                 provenance=provenance)
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: could not load user lib %r: %s", path, exc)


def _symbol_record(libid: str, sdef: dict, *, license_: str, provenance: str) -> dict:
    name = sdef.get("name") or libid.split(":")[-1]
    desc = sdef.get("description") or sdef.get("datasheet") or ""
    pins = sdef.get("pins") or []
    title = f"{name} KiCad symbol ({len(pins)} pins)" + (f" — {desc}" if desc else "")
    footprint = sdef.get("footprint") or ""
    return {
        "part_id":      f"ecad:{libid}",
        "domain":       "ecad",
        "title":        title,
        "standard":     None,
        "designation":  libid,
        "params":       {
            "nominal_size":      libid,
            "standard":          None,
            "kind":              "symbol",
            "pin_count":         len(pins),
            "reference_prefix":  sdef.get("reference_prefix", ""),
            "default_value":     sdef.get("value", ""),
            "footprint":         footprint,
            "description":       desc,
        },
        "geometry_ref": {
            "kind":      "symbol_footprint",
            "library":   libid.split(":")[0] if ":" in libid else "",
            "libid":     libid,
            "footprint": footprint,
        },
        "sourcing":     [],
        "provenance":   provenance,
        "license":      license_,
    }


def _footprint_record(fpid: str, fpdef: dict, *, license_: str, provenance: str) -> dict:
    desc = fpdef.get("description") or ""
    title = f"{fpid} KiCad footprint" + (f" — {desc}" if desc else "")
    pads = fpdef.get("pads") or []
    return {
        "part_id":      f"ecad:{fpid}",
        "domain":       "ecad",
        "title":        title,
        "standard":     None,
        "designation":  fpid,
        "params":       {
            "nominal_size": fpid,
            "standard":     None,
            "kind":         "footprint",
            "pad_count":    len(pads),
            "description":  desc,
        },
        "geometry_ref": {
            "kind":      "symbol_footprint",
            "library":   fpid.split(":")[0] if ":" in fpid else "",
            "libid":     None,
            "footprint": fpid,
        },
        "sourcing":     [],
        "provenance":   provenance,
        "license":      license_,
    }


def _hit_to_record(hit: dict) -> dict:
    """Convert an engine.ecad.library.search hit to a PartRecord."""
    kind = hit.get("kind", "symbol")
    if kind == "symbol":
        libid = hit.get("libid", "")
        name = hit.get("name", libid.split(":")[-1] if ":" in libid else libid)
        desc = hit.get("description", "")
        pin_count = hit.get("pin_count", 0)
        title = f"{name} KiCad symbol ({pin_count} pins)" + (f" — {desc}" if desc else "")
        return {
            "part_id":      f"ecad:{libid}",
            "domain":       "ecad",
            "title":        title,
            "standard":     None,
            "designation":  libid,
            "params":       {
                "nominal_size":     libid,
                "standard":         None,
                "kind":             "symbol",
                "pin_count":        pin_count,
                "description":      desc,
            },
            "geometry_ref": {
                "kind":      "symbol_footprint",
                "library":   libid.split(":")[0] if ":" in libid else "",
                "libid":     libid,
                "footprint": hit.get("footprint", ""),
            },
            "sourcing":     [],
            "provenance":   "engine.ecad.library search (starter + user libs)",
            "license":      "user-supplied",
        }
    else:
        fpid = hit.get("fpid", "")
        desc = hit.get("description", "")
        title = f"{fpid} KiCad footprint" + (f" — {desc}" if desc else "")
        return {
            "part_id":      f"ecad:{fpid}",
            "domain":       "ecad",
            "title":        title,
            "standard":     None,
            "designation":  fpid,
            "params":       {
                "nominal_size": fpid,
                "standard":     None,
                "kind":         "footprint",
                "description":  desc,
            },
            "geometry_ref": {
                "kind":      "symbol_footprint",
                "library":   fpid.split(":")[0] if ":" in fpid else "",
                "libid":     None,
                "footprint": fpid,
            },
            "sourcing":     [],
            "provenance":   "engine.ecad.library search (starter + user libs)",
            "license":      "user-supplied",
        }
