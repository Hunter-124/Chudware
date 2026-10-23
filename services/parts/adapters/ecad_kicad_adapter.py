"""CatalogProvider adapter for KiCad symbol/footprint libraries.

Indexable domain adapter (domain="ecad") that enumerates:
  1. The clean-room starter symbol + footprint library (engine/ecad/library/starter).
  2. User .kicad_sym libraries listed in CHUDWARE_KICAD_SYM_PATH (colon-separated
     paths, same env var as ecad_find_part in ecad_tools.py).

Wraps engine.ecad.library.search (the ecad_find_part surface) for runtime
.search() calls, and directly enumerates starter.SYMBOLS / starter.FOOTPRINTS
plus any user-lib files for iter_records() (index build).

This adapter covers the LOCAL, deterministic set only: user libs beyond
CHUDWARE_KICAD_SYM_PATH are not searched.

geometry_ref.kind = "symbol_footprint" (symbols) per contracts/parts.md §2.

Provenance / license: starter library = clean-room (Chudware proprietary).
User libs: license = "user-supplied".
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

log = logging.getLogger(__name__)

_ENV_VAR = "CHUDWARE_KICAD_SYM_PATH"


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
        # A model-written id is canonicalised EXACTLY or not at all
        # (services/ecad/library/partid.resolve): only spelling noise — a missing or
        # doubled 'ecad:' domain prefix, case, whitespace — is forgiven. A near miss
        # (ecad:Device:R_0603) is NEVER mapped onto a different record; it stays a
        # refusal whose hint names the real ids instead.
        want = str(part_id or "")
        remap: list[dict] | None = None
        remap_note = ""
        try:
            from services.ecad.library.partid import resolve as _resolve_ecad_id
            from services.ecad.library.partid import resolved_pins as _resolved_ecad_pins
            hit = _resolve_ecad_id(part_id)
            if hit:
                want = hit["part_id"]
                # A pin PERMUTATION row (Device:Q_NPN_BCE, Conn_02x03_Counter_Clockwise:
                # the same device, numbered differently) resolves onto the record we really
                # have, whose pins carry OUR numbers. Carrying the translation on the record
                # is the whole point: a caller that read "pin 2 = C" off its own KiCad id
                # must wire pin 3 of this symbol, and an id rewrite that stayed silent about
                # the renumbering would move every such wire onto the wrong leg.
                if hit.get("pin_remap"):
                    remap = _resolved_ecad_pins(part_id)
                    remap_note = str(hit.get("note") or "")
        except Exception as exc:  # noqa: BLE001
            log.debug("EcadKicadAdapter: part-id resolver unavailable: %s", exc)
        if not want.startswith("ecad:"):
            return None

        def _out(rec: dict) -> dict:
            """The record, plus the pin translation when the id carried a permutation."""
            if not remap:
                return rec
            rec = dict(rec)
            rec["pin_remap"] = [dict(p) for p in remap]
            rec["pin_remap_note"] = (
                remap_note + ". Wire by native_number: "
                + ", ".join(f"your pin {p['number']} ({p['name']}) = pin "
                            f"{p['native_number']}" for p in remap))
            return rec

        for rec in self.iter_records():
            if rec.get("part_id") == want:
                return _out(rec)
        # Generated parts (ecad_make_part -> resolution tier 2) are served HERE and
        # deliberately NOT from iter_records(): iter_records feeds the FTS index, which is
        # snapshotted to <app data>/cache/parts_index.sqlite and revalidated against a
        # fingerprint of static data files only (hub._fingerprint_sources). A tier that
        # changes at runtime — and whose dir may be a tmpdir — would be baked into that
        # cache and outlive the parts themselves. Live lookup keeps part_get/part_place
        # able to fetch a part the agent just made, with nothing cached.
        for rec in _load_native_dirs():
            if rec.get("part_id") == want:
                return _out(rec)
        return None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        """Enumerate starter symbols, starter footprints, then user-lib symbols.

        INDEXED content only — see ``get`` for why the generated native tier is excluded.
        Keyword search still reaches it: ``search`` delegates to
        ``services.ecad.library.search``, which scans the native dirs itself.
        """
        # Starter symbols
        starter_syms, starter_fps = _load_starter()
        for libid, sdef in starter_syms.items():
            yield _symbol_record(libid, sdef, license_="proprietary",
                                 provenance="Chudware clean-room starter symbol library")
        # Starter footprints
        for fpid, fpdef in starter_fps.items():
            yield _footprint_record(fpid, fpdef, license_="proprietary",
                                    provenance="Chudware clean-room starter footprint library")
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
        from services.ecad.library.starter import SYMBOLS, FOOTPRINTS
        return dict(SYMBOLS), dict(FOOTPRINTS)
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: starter unavailable: %s", exc)
        return {}, {}


def _lib_search(query: str) -> list[dict]:
    """Delegate to engine.ecad.library.search; return [] on failure."""
    try:
        from services.ecad.library import search as _search
        return _search(query) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: lib_search unavailable: %s", exc)
        return []


def _load_native_dirs() -> Iterator[dict]:
    """PartRecords for every generated part in the registered native library dirs.

    Layout is native.Library's own: ``<dir>/symbols/<libid>.sym.json`` and
    ``<dir>/footprints/<fpid>.fp.json`` with ':' encoded as '__'. Best-effort — a
    malformed or unreadable file is skipped, never fatal to the whole enumeration.

    Symbols are emitted first and a footprint stored under the SAME id is then skipped:
    ecad_make_part accepts an fpid equal to the symbol's libid, which would otherwise emit
    two PartRecords sharing one part_id. The index keeps whichever it saw last, and the
    footprint record's geometry_ref has no ``libid`` — so part_place answered
    "geometry_ref missing 'libid'" for a part that had just been created successfully. The
    symbol record is the placeable one and already names its footprint.
    """
    try:
        import json

        from services.ecad.library.native import (_filename_to_libid,
                                                  get_default_library)
        dirs = list(getattr(get_default_library(), "_native_dirs", ()) or ())
    except Exception as exc:  # noqa: BLE001
        log.debug("EcadKicadAdapter: native tier unavailable: %s", exc)
        return
    seen: set = set()
    for directory in dirs:
        for sub, ext, build in (("symbols", ".sym.json", _symbol_record),
                                ("footprints", ".fp.json", _footprint_record)):
            folder = os.path.join(directory, sub)
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for fname in names:
                if not fname.endswith(ext):
                    continue
                libid = _filename_to_libid(fname, ext)
                if libid in seen:
                    continue
                try:
                    with open(os.path.join(folder, fname), encoding="utf-8") as fh:
                        definition = json.load(fh)
                except Exception as exc:  # noqa: BLE001
                    log.debug("EcadKicadAdapter: bad native part %r: %s", fname, exc)
                    continue
                seen.add(libid)
                yield build(libid, definition, license_="user-generated",
                            provenance=f"Generated by ecad_make_part ({directory})")


def _load_user_sym_lib(path: str) -> Iterator[dict]:
    """Load a .kicad_sym file and yield PartRecords for each symbol."""
    try:
        from services.ecad.library.kicad_sym import load_kicad_sym
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
