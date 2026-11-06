"""Parts catalog adapter for native KiCad libraries and local generated parts.

The native bridge searches the installed and project KiCad libraries on demand.
The small Python starter/user catalog remains indexed for offline discovery;
generated parts are read live so they cannot outlive their source in the index.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterator

log = logging.getLogger(__name__)

_ENV_VAR = "CHUDWARE_KICAD_SYM_PATH"


class EcadKicadAdapter:
    """Search KiCad through the native bridge, plus the local Python libraries."""

    domain: str = "ecad"
    provider_id: str = "ecad_kicad"

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None,
               limit: int = 20) -> list[dict]:
        if domain and domain != "ecad":
            return []
        limit = max(1, int(limit))
        norm = str(query or "").strip().lower()
        results = _native_search(query, limit, doc_id=str((filters or {}).get("doc_id") or ""))
        seen = {rec["part_id"] for rec in results}
        # Python-only generated/user libraries still work. Native KiCad rows win
        # collisions, since their geometry and pin numbering are authoritative.
        if len(results) >= limit:
            return results[:limit]
        from itertools import chain
        for rec in chain(self.iter_records(), _load_native_dirs()):
            text = " ".join([str(rec.get("title") or ""), str(rec.get("designation") or ""),
                             str((rec.get("params") or {}).get("description") or "")]).lower()
            if (not norm or all(term in text for term in norm.split())) and rec["part_id"] not in seen:
                seen.add(rec["part_id"])
                results.append(rec)
            if len(results) >= limit:
                break
        return results[:limit]

    def get(self, part_id: str, *, doc_id: str = "") -> dict | None:
        from services.ecad.library.partid import strip_domain

        raw = str(part_id or "").strip()
        libid = strip_domain(raw)
        # A bare KiCad libid has exactly one ':'; do not treat unrelated parts
        # domains as library nicknames.
        if libid.count(":") != 1:
            return None
        native = _native_search(libid, 200, doc_id=doc_id)
        exact = [rec for rec in native if rec["designation"] == libid]
        if not exact:
            folded = [rec for rec in native
                      if rec["designation"].casefold() == libid.casefold()]
            # Case normalization is safe only when it selects one actual ID.
            if len({rec["designation"] for rec in folded}) == 1:
                exact = folded
        if exact:
            return exact[0]

        # Local generated/user records are not cached with the KiCad library.
        # Match spelling only; never substitute a different symbol or pinout.
        from itertools import chain
        local = list(chain(self.iter_records(), _load_native_dirs()))
        exact = [rec for rec in local if rec["designation"] == libid]
        if not exact:
            folded = [rec for rec in local
                      if rec["designation"].casefold() == libid.casefold()]
            if len({rec["designation"] for rec in folded}) == 1:
                exact = folded
        return exact[0] if exact else None

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())

    def iter_records(self) -> Iterator[dict]:
        """Enumerate the compact local catalog for the persistent FTS index.

        Installed/project KiCad libraries and generated parts are queried live,
        not snapshotted into an index shared across projects.
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
        for lib_path in (p.strip() for p in sym_path.split(os.pathsep) if p.strip()):
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


def _native_search(query: str, limit: int, *, doc_id: str = "") -> list[dict]:
    """Search native symbols and footprints using their actual RPC envelopes."""
    from itertools import zip_longest
    from services.mcp import core_bridge

    results: list[dict] = []
    groups: list[list[dict]] = []
    for method, key, kind, id_key in (
            ("ecad_symbols", "symbols", "symbol", "libid"),
            ("ecad_footprints", "footprints", "footprint", "fpid")):
        try:
            params = {"query": str(query or ""), "limit": limit}
            if doc_id:
                params["doc_id"] = doc_id
            response = core_bridge.call(method, params)
            rows = response.get(key) if isinstance(response, dict) else None
            if response.get("ok") is False or not isinstance(rows, list):
                raise ValueError(f"invalid {method} response")
        except Exception as exc:  # noqa: BLE001 — local catalog remains usable
            if doc_id:
                # Do not silently substitute a global/starter library for the
                # project the caller explicitly selected.
                raise
            log.debug("EcadKicadAdapter: %s unavailable: %s", method, exc)
            continue
        group = []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get(id_key), str) and row[id_key]:
                group.append(_hit_to_record({**row, "kind": kind}))
        groups.append(group)
    # Both catalogs are independently ranked; neither may hide the other.
    seen: dict[str, int] = {}
    for pair in zip_longest(*groups):
        for rec in pair:
            if not rec:
                continue
            part_id = rec["part_id"]
            if part_id not in seen:
                seen[part_id] = len(results)
                results.append(rec)
                continue
            index = seen[part_id]
            existing = results[index]
            # A KiCad symbol and footprint can have the same Library:Name.
            # Keep one stable ID, but preserve both exact geometries so the
            # receiving document determines which one is placed.
            if existing["params"]["kind"] != rec["params"]["kind"]:
                symbol, footprint = ((existing, rec) if existing["params"]["kind"] == "symbol"
                                     else (rec, existing))
                symbol["geometry_ref"]["board_footprint"] = footprint["geometry_ref"]["footprint"]
                symbol["params"]["pad_count"] = footprint["params"]["pad_count"]
                results[index] = symbol
    return results


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


def _kicad_label(name: str) -> str:
    name = name.replace("_", " ")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return re.sub(r"(?<=\d)(?=mm\b)", " ", name, flags=re.IGNORECASE)


def _symbol_record(libid: str, sdef: dict, *, license_: str, provenance: str) -> dict:
    name = sdef.get("name") or libid.split(":")[-1]
    desc = sdef.get("description") or sdef.get("datasheet") or ""
    pins = sdef.get("pins") or []
    pin_count = sdef.get("pin_count", len(pins))
    title = f"{name} KiCad symbol ({pin_count} pins)"
    footprint = sdef.get("footprint") or sdef.get("default_footprint") or ""
    return {
        "part_id":      f"ecad:{libid}",
        "domain":       "ecad",
        "title":        title,
        "family":       _kicad_label(libid.split(":", 1)[0]) if ":" in libid else "",
        "standard":     None,
        "designation":  libid,
        "params":       {
            "nominal_size":      libid,
            "standard":          None,
            "kind":              "symbol",
            "pin_count":         pin_count,
            "reference_prefix":  sdef.get("reference_prefix") or sdef.get("prefix", ""),
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


def _footprint_title(fpid: str) -> str:
    return f"{_kicad_label(fpid.rsplit(':', 1)[-1])} (footprint)"


def _footprint_record(fpid: str, fpdef: dict, *, license_: str, provenance: str) -> dict:
    desc = fpdef.get("description") or ""
    title = _footprint_title(fpid)
    pads = fpdef.get("pads") or []
    return {
        "part_id":      f"ecad:{fpid}",
        "domain":       "ecad",
        "title":        title,
        "family":       _kicad_label(fpid.split(":", 1)[0]) if ":" in fpid else "",
        "standard":     None,
        "designation":  fpid,
        "params":       {
            "nominal_size": fpid,
            "standard":     None,
            "kind":         "footprint",
            "pad_count":    fpdef.get("pad_count", len(pads)),
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
    """Convert a native catalog hit without inventing pins, pads or licensing."""
    provenance = "Native KiCad library search; consult the source library for its license"
    if hit.get("kind") == "symbol":
        return _symbol_record(hit["libid"], hit, license_="user-supplied",
                              provenance=provenance)
    return _footprint_record(hit["fpid"], hit, license_="user-supplied",
                             provenance=provenance)
