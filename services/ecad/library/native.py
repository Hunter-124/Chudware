"""Native library resolver.

Provides a ``Library`` class and module-level singleton functions
``resolve``, ``resolve_footprint``, ``search`` so that::

    from services.ecad import library
    library.resolve("Device:R")

works out of the box for Python ECAD service callers.

Resolution order (first match wins):
  1. Configured project and agent-generated native JSON libraries
  2. Installed KiCad and native core libraries
  3. Python starter and explicitly configured KiCad library paths

Native dir layout::
  <dir>/symbols/<libid-encoded>.sym.json
  <dir>/footprints/<fpid-encoded>.fp.json

libid is encoded as ``<lib>__<name>.sym.json`` (double-underscore replaces ``:``)
to be filesystem-friendly.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .starter import SYMBOLS, FOOTPRINTS, get_symbol, get_footprint

def _core_library(method: str, query: str, *, resolve: bool = False,
                  strict: bool = False) -> list[dict]:
    """Query the native catalog lazily so standalone Python catalogs still work."""
    try:
        from services.mcp.ecad_common import _ECAD, _rpc
        params = {"query": query, "limit": 100, "resolve": resolve}
        doc_id = (_ECAD.get("current_sch") if method == "ecad_symbols" else
                  _ECAD.get("current_pcb") or _ECAD.get("current_sch"))
        if doc_id:
            params["doc_id"] = doc_id
        result = _rpc(method, params)
        field = "symbols" if method == "ecad_symbols" else "footprints"
        return [row for row in result.get(field, []) if isinstance(row, dict)]
    except Exception:  # noqa: BLE001 — standalone catalogs can operate without the core
        if strict:
            raise
        return []


def _libid_to_filename(libid: str, ext: str) -> str:
    """Encode a lib-qualified id to a safe filename."""
    return libid.replace(":", "__") + ext


def _filename_to_libid(fname: str, ext: str) -> str:
    stem = fname[: -len(ext)] if fname.endswith(ext) else fname
    return stem.replace("__", ":", 1)


class Library:
    """Multi-source library resolver."""

    def __init__(self) -> None:
        self._native_dirs: list[str] = []
        self._kicad_sym_paths: list[str] = []   # paths to .kicad_sym files
        self._kicad_mod_dirs: list[str] = []    # .pretty directories
        self._sym_cache: dict[str, dict | None] = {}
        self._fp_cache: dict[str, dict | None] = {}

    def add_native_dir(self, directory: str) -> None:
        """Add a native library directory, making the latest registration authoritative."""
        if directory in self._native_dirs:
            self._native_dirs.remove(directory)
        self._native_dirs.append(directory)
        self.clear_cache()

    def add_kicad_sym_path(self, path: str) -> None:
        """Add a .kicad_sym file path for symbol resolution."""
        if path not in self._kicad_sym_paths:
            self._kicad_sym_paths.append(path)

    def add_kicad_mod_dir(self, directory: str) -> None:
        """Add a .pretty directory for footprint resolution."""
        if directory not in self._kicad_mod_dirs:
            self._kicad_mod_dirs.append(directory)

    def clear_cache(self) -> None:
        self._sym_cache.clear()
        self._fp_cache.clear()

    def _native_definition(self, ident: str, subdir: str, extension: str) -> dict | None:
        filename = _libid_to_filename(ident, extension)
        for directory in reversed(self._native_dirs):
            path = os.path.join(directory, subdir, filename)
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as stream:
                        result = json.load(stream)
                    if isinstance(result, dict):
                        return result
                except (OSError, ValueError):
                    continue
        return None

    def custom_symbol(self, libid: str) -> dict | None:
        return self._native_definition(libid, "symbols", ".sym.json")

    def custom_footprint(self, fpid: str) -> dict | None:
        return self._native_definition(fpid, "footprints", ".fp.json")

    # ─── symbol resolution ───────────────────────────────────────────────────

    def resolve(self, libid: str) -> dict | None:
        """Resolve a symbol by libid. Returns a SymbolDef dict or None."""
        custom = self.custom_symbol(libid)
        if custom is not None:
            return custom
        for hit in _core_library("ecad_symbols", libid, resolve=True):
            if hit.get("libid") == libid and isinstance(hit.get("definition"), dict):
                return hit["definition"]
        if libid in self._sym_cache:
            return self._sym_cache[libid]

        result = self._resolve_sym(libid)
        self._sym_cache[libid] = result
        return result

    def _resolve_sym(self, libid: str) -> dict | None:
        # 1. starter
        sym = get_symbol(libid)
        if sym is not None:
            return sym


        # 3. KiCad .kicad_sym files
        lib_part = libid.split(":")[0] if ":" in libid else ""
        sym_name = libid.split(":", 1)[1] if ":" in libid else libid
        for kpath in self._kicad_sym_paths:
            basename = os.path.splitext(os.path.basename(kpath))[0]
            if basename != lib_part and lib_part:
                continue
            try:
                from .kicad_sym import load_kicad_sym
                syms = load_kicad_sym(kpath)
                if libid in syms:
                    return syms[libid]
            except Exception:
                pass

        return None

    # ─── footprint resolution ────────────────────────────────────────────────

    def resolve_footprint(self, fpid: str) -> dict | None:
        """Resolve a footprint by fpid. Returns a footprint dict or None."""
        custom = self.custom_footprint(fpid)
        if custom is not None:
            return custom
        for hit in _core_library("ecad_footprints", fpid, resolve=True):
            if hit.get("fpid") == fpid and isinstance(hit.get("definition"), dict):
                return hit["definition"]
        if fpid in self._fp_cache:
            return self._fp_cache[fpid]

        result = self._resolve_fp(fpid)
        self._fp_cache[fpid] = result
        return result

    def _resolve_fp(self, fpid: str) -> dict | None:
        # 1. starter
        fp = get_footprint(fpid)
        if fp is not None:
            return fp


        # 3. KiCad .pretty dirs
        lib_part = fpid.split(":")[0] if ":" in fpid else ""
        fp_name = fpid.split(":", 1)[1] if ":" in fpid else fpid
        for d in self._kicad_mod_dirs:
            basename = os.path.splitext(os.path.basename(d.rstrip("/\\")))[0]
            if basename != lib_part and lib_part:
                continue
            kmod_path = os.path.join(d, fp_name + ".kicad_mod")
            if os.path.isfile(kmod_path):
                try:
                    from .kicad_mod import load_kicad_mod
                    fp = load_kicad_mod(kmod_path)
                    fp["fpid"] = fpid
                    return fp
                except Exception:
                    pass

        return None

    # ─── search ─────────────────────────────────────────────────────────────

    def search(self, query: str, *, strict: bool = False) -> list[dict]:
        """Search symbols and footprints by keyword/name.

        Returns a list of hit dicts with keys: kind, libid/fpid, name,
        description, score (higher = better match).
        """
        q = query.lower().strip()
        hits: list[dict] = []
        shadowed: set[tuple[str, str]] = set()
        # Project/generated definitions shadow installed libraries with the same id.
        for directory in self._native_dirs:
            for subdir, ext, kind, key in (("symbols", ".sym.json", "symbol", "libid"),
                                            ("footprints", ".fp.json", "footprint", "fpid")):
                source = os.path.join(directory, subdir)
                if not os.path.isdir(source):
                    continue
                for filename in os.listdir(source):
                    if not filename.endswith(ext):
                        continue
                    ident = _filename_to_libid(filename, ext)
                    shadowed.add((kind, ident))
                    if any(hit.get(key) == ident for hit in hits):
                        continue
                    definition = self._native_definition(ident, subdir, ext) or {}
                    name = definition.get("name") or ident.split(":")[-1]
                    description = definition.get("description") or ""
                    keywords = definition.get("keywords") or ""
                    if isinstance(keywords, list):
                        keywords = " ".join(str(word) for word in keywords)
                    score = _score(q, ident, name, description, str(keywords))
                    if score > 0:
                        hits.append({"kind": kind, key: ident, "name": name,
                                     "description": description, "score": score})
        for method, kind, key in (("ecad_symbols", "symbol", "libid"),
                                  ("ecad_footprints", "footprint", "fpid")):
            rows = _core_library(method, query, strict=strict)
            # A single-word search such as "voltage" should find source symbols
            # even when broad matches fill the native catalog's bounded result.
            if method == "ecad_symbols" and len(q.split()) == 1 and q:
                rows += _core_library(method, f"{query} source", strict=strict)
            for item in rows:
                ident = item.get(key)
                if not isinstance(ident, str) or not ident or (kind, ident) in shadowed:
                    continue
                if any(hit.get(key) == ident for hit in hits):
                    continue
                name = ident.split(":")[-1]
                description = item.get("description", "")
                keywords = item.get("keywords") or ""
                if isinstance(keywords, list):
                    keywords = " ".join(str(word) for word in keywords)
                score = _score(q, ident, name, description, str(keywords))
                if description.lower().startswith(q + " source"):
                    score += 4
                hits.append({"kind": kind, key: ident, "name": name,
                             "description": description, "score": max(1, score),
                             "pin_count": item.get("pin_count"),
                             "pad_count": item.get("pad_count"),
                             "footprint": item.get("footprint") or item.get("default_footprint", "")})

        for libid, sdef in SYMBOLS.items():
            score = _score(q, libid, sdef.get("name", ""),
                           sdef.get("description", ""),
                           sdef.get("keywords", ""))
            if (score > 0 and ("symbol", libid) not in shadowed and
                    not any(h.get("libid") == libid for h in hits)):
                hits.append({
                    "kind": "symbol",
                    "libid": libid,
                    "name": sdef.get("name", ""),
                    "description": sdef.get("description", ""),
                    "score": score,
                })

        for fpid, fp in FOOTPRINTS.items():
            score = _score(q, fpid, fpid.split(":")[-1],
                           fp.get("description", ""), "")
            if (score > 0 and ("footprint", fpid) not in shadowed and
                    not any(h.get("fpid") == fpid for h in hits)):
                hits.append({
                    "kind": "footprint",
                    "fpid": fpid,
                    "name": fpid.split(":")[-1],
                    "description": fp.get("description", ""),
                    "score": score,
                })


        hits.sort(key=lambda h: -h["score"])
        return hits[:50]  # cap at 50


def _score(query: str, *fields: str) -> int:
    """Keyword scoring: exact field match = 10, whole-query substring = 1 per field.

    Multi-word queries ALSO credit individual words, so a natural request like 'red LED' or
    'connector header' still matches when no single field contains the whole phrase (the AND-only
    behaviour previously returned ZERO hits for such queries). Single-word queries are unaffected —
    the token loop is skipped, so their scores are byte-for-byte identical to before.
    """
    if not query:
        return 0
    total = 0
    for field in fields:
        fl = field.lower()
        if query == fl:
            total += 10
        elif query in fl:
            total += 1
    tokens = query.split()
    if len(tokens) > 1:
        for field in fields:
            fl = field.lower()
            if not fl:
                continue
            for tok in tokens:
                if not tok:
                    continue
                if tok == fl:
                    total += 5
                elif tok in fl:
                    total += 1
    return total


# ─── module-level singleton ───────────────────────────────────────────────────

_default: Library = Library()


def resolve(libid: str) -> dict | None:
    """Resolve a symbol by libid using the default library singleton."""
    return _default.resolve(libid)


def resolve_footprint(fpid: str) -> dict | None:
    """Resolve a footprint by fpid using the default library singleton."""
    return _default.resolve_footprint(fpid)


def search(query: str, *, strict: bool = False) -> list[dict]:
    """Search the default library for symbols and footprints."""
    return _default.search(query, strict=strict)


def get_default_library() -> Library:
    """Return the default Library singleton (for configuration)."""
    return _default
