"""Native library resolver.

Provides a ``Library`` class and module-level singleton functions
``resolve``, ``resolve_footprint``, ``search`` so that::

    from engine.ecad import library
    library.resolve("Device:R")

works out of the box from engine/ecad/ops.py.

Resolution order (first match wins):
  1. Starter lib (always available)
  2. Native dirs (JSON files; configured via ``add_native_dir``)
  3. KiCad lib paths (configured via ``add_kicad_path``)
  4. Returns None

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
        """Add a native library directory to the search path."""
        if directory not in self._native_dirs:
            self._native_dirs.append(directory)

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

    # ─── symbol resolution ───────────────────────────────────────────────────

    def resolve(self, libid: str) -> dict | None:
        """Resolve a symbol by libid. Returns a SymbolDef dict or None."""
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

        # 2. native dirs
        fname = _libid_to_filename(libid, ".sym.json")
        for d in self._native_dirs:
            sym_path = os.path.join(d, "symbols", fname)
            if os.path.isfile(sym_path):
                try:
                    with open(sym_path, encoding="utf-8") as fh:
                        return json.load(fh)
                except Exception:
                    pass

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

        # 2. native dirs
        fname = _libid_to_filename(fpid, ".fp.json")
        for d in self._native_dirs:
            fp_path = os.path.join(d, "footprints", fname)
            if os.path.isfile(fp_path):
                try:
                    with open(fp_path, encoding="utf-8") as fh:
                        return json.load(fh)
                except Exception:
                    pass

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

    def search(self, query: str) -> list[dict]:
        """Search symbols and footprints by keyword/name.

        Returns a list of hit dicts with keys: kind, libid/fpid, name,
        description, score (higher = better match).
        """
        q = query.lower().strip()
        hits: list[dict] = []

        for libid, sdef in SYMBOLS.items():
            score = _score(q, libid, sdef.get("name", ""),
                           sdef.get("description", ""),
                           sdef.get("keywords", ""))
            if score > 0:
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
            if score > 0:
                hits.append({
                    "kind": "footprint",
                    "fpid": fpid,
                    "name": fpid.split(":")[-1],
                    "description": fp.get("description", ""),
                    "score": score,
                })

        # also scan native dirs
        for d in self._native_dirs:
            sym_dir = os.path.join(d, "symbols")
            if os.path.isdir(sym_dir):
                for fname in os.listdir(sym_dir):
                    if fname.endswith(".sym.json"):
                        libid = _filename_to_libid(fname, ".sym.json")
                        if any(h.get("libid") == libid for h in hits):
                            continue
                        score = _score(q, libid, libid.split(":")[-1], "", "")
                        if score > 0:
                            hits.append({"kind": "symbol", "libid": libid,
                                         "name": libid.split(":")[-1],
                                         "description": "", "score": score})

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


def search(query: str) -> list[dict]:
    """Search the default library for symbols and footprints."""
    return _default.search(query)


def get_default_library() -> Library:
    """Return the default Library singleton (for configuration)."""
    return _default
