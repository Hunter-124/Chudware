"""Deterministic spec-sheet matching v1 — purchase part number → parametric model (T-B4).

The deterministic lane of spec-sheet matching. A purchased part number carries vendor
packaging cruft (``M3x8-ND``, ``608-2RS-CT-ND``); we:

1. :func:`normalize_mpn` — strip vendor/packaging suffixes to recover the manufacturer PN.
2. :func:`match_bolts` — look the designation up in the clean-room ISO/DIN tables
   (``engine_oracle/data/bolts/``, via :mod:`engine.bolts_loader`) → nominal params + the
   ``core/parts`` catalog family/size that builds it.
3. :func:`match_kicad` — clean-room fallback that reads the user's **own** installed KiCad
   symbol libraries (never vendored — ``contracts/licenses.md`` ECAD posture) via the
   existing clean-room ``.kicad_sym`` parser, keyed off ``AICAD_KICAD_SYM_PATH``.
4. :func:`generate_model` — build editable geometry for a match through the ``parts_make``
   core RPC and return the saved model path.
5. :func:`match_part_number` — the orchestrator (normalize → BOLTS → KiCad) returning a
   contract-shaped ``{matched, source, params, model_path, ...}``.

Provenance is never fabricated: a hit carries ``catalog_id`` + ``source``; a miss returns the
contract-shaped *unknown* (``matched: False``), never a guessed part. No network, no keys, no
core dependency at import time — everything degrades gracefully offline (contract §0).
"""
from __future__ import annotations

import os
import re
from typing import Any

from engine import bolts_loader
from engine.bolts_loader import Standard

# --------------------------------------------------------------------------------------
# 1. Part-number normalization
# --------------------------------------------------------------------------------------

# Vendor/packaging suffixes, longest/most-specific first. Applied iteratively at the END of
# the token until the string stabilizes, so stacked qualifiers (``-CT-ND``, ``-1-ND``) peel
# off cleanly. The bare quantity qualifier (``-\d+``) is ONLY stripped when it is glued to a
# distributor token (``-6-ND``) — never standalone — so legitimate ``PART-100`` PNs survive.
_SUFFIX_PATTERNS: tuple[str, ...] = (
    r"-\d{1,2}-ND",   # DigiKey quantity + distributor:  -1-ND, -6-ND, -25-ND
                      # (1-2 digits only, so a PN like "ABC-123-ND" keeps its "-123")
    r"-DKR-ND",       # DigiReel + distributor
    r"-CT-ND",        # cut-tape + distributor
    r"-TR-ND",        # tape&reel + distributor
    r"-ND",           # DigiKey distributor suffix
    r"-DKR",          # DigiReel
    r"-CT",           # cut tape
    r"-TR",           # tape and reel
    r"-TB",           # tube
    r"-BULK",         # bulk pack
    r"-REEL",         # full reel
    r"-TAPE",         # tape pack
    r"-PBFREE",       # Pb-free marketing suffix
    r"-EA",           # each
)
_SUFFIX_RE = re.compile("(?:%s)$" % "|".join(_SUFFIX_PATTERNS), re.IGNORECASE)


def normalize_mpn(pn: str) -> str:
    """Strip common vendor/packaging suffixes from a purchase PN → the manufacturer PN.

    ``"M3x8-ND"`` → ``"M3x8"``; ``"608-2RS-CT-ND"`` → ``"608-2RS"``; ``"PART-100"`` stays
    ``"PART-100"`` (bare numeric suffixes are preserved). Idempotent; never raises.
    """
    s = str(pn or "").strip()
    # Peel packaging notation in parentheses/brackets, e.g. "GRM188(Cut Tape)".
    s = re.sub(r"[\(\[][^)\]]*[\)\]]\s*$", "", s).strip()
    prev = None
    while prev != s and s:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return s or str(pn or "").strip()


# --------------------------------------------------------------------------------------
# 2. BOLTS / clean-room ISO lookup
# --------------------------------------------------------------------------------------

# Bearing seal/shield codes appended to a bearing number: 608-2RS, 6000ZZ, 6900-DDU, ...
_BEARING_SEAL_RE = re.compile(
    r"[-\s]?(?:2RS1?|RS1?|2Z|ZZ|Z|DDU|LLU|LU|LB|2RU|C3|C4)+$", re.IGNORECASE)
# Metric fastener designation: M3, M3x8, M3-0.5x8, M3X8 (thread, optional pitch, optional length).
_METRIC_RE = re.compile(
    r"\bM(?P<d>\d+(?:\.\d+)?)"
    r"(?:[-x×*](?P<pitch>\d+(?:\.\d+)?))?"       # optional pitch (only if a length follows)
    r"(?:[-x×*](?P<length>\d+(?:\.\d+)?))?\b",
    re.IGNORECASE)

# Family hints in a free-text designation → the catalog family key to prefer.
_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(?:hex\s*nut|nut)\b", "hex_nut"),
    (r"\b(?:flat\s*washer|washer)\b", "washer_flat"),
    (r"\b(?:button\s*head|bhcs)\b", "bhcs"),
    (r"\b(?:flat\s*head|fhcs|countersunk|csk)\b", "fhcs"),
    (r"\b(?:hex\s*bolt|hex\s*head|hhcs)\b", "hex_bolt"),
    (r"\b(?:socket\s*head|shcs|cap\s*screw)\b", "shcs"),
)
_DEFAULT_FASTENER_FAMILY = "shcs"  # the default machine screw (per core/parts catalog)


def _standard_for_family(tables: dict[str, Standard], family: str) -> Standard | None:
    for std in tables.values():
        if std.family == family:
            return std
    return None


def _bearing_standards(tables: dict[str, Standard]) -> list[Standard]:
    return [s for s in tables.values() if s.kind == "bearing"]


def _match_bearing(mpn: str, tables: dict[str, Standard]) -> dict[str, Any] | None:
    core = _BEARING_SEAL_RE.sub("", mpn).strip().upper()
    core = re.sub(r"[-\s]+$", "", core)
    for std in _bearing_standards(tables):
        params = std.get(core)
        if params is not None:
            return _build_match(std, core, None, params, designation=mpn)
    return None


def _match_metric_fastener(mpn: str, tables: dict[str, Standard]) -> dict[str, Any] | None:
    m = _METRIC_RE.search(mpn)
    if not m:
        return None
    thread = "M" + m.group("d")
    length = m.group("length")
    pitch = m.group("pitch")
    # A single trailing number is the LENGTH (M3x8 = M3 × 8 mm), not the pitch; pitch only
    # appears when both are given (M8x1.25x20 / M8-1.0x20).
    if length is None and pitch is not None:
        length = pitch
    length_mm = float(length) if length is not None else None

    family = _DEFAULT_FASTENER_FAMILY
    for pat, fam in _FAMILY_HINTS:
        if re.search(pat, mpn, re.IGNORECASE):
            family = fam
            break

    std = _standard_for_family(tables, family)
    if std is None:
        return None
    params = std.get(thread)
    if params is None:
        return None
    if std.needs_length and length_mm is None:
        length_mm = None  # unknown length is allowed; geometry can default it
    return _build_match(std, thread, length_mm, params, designation=mpn)


def _match_explicit_standard(mpn: str, tables: dict[str, Standard]) -> dict[str, Any] | None:
    """Handle designations that name a standard, e.g. 'ISO 4762 M3x8' / 'ISO4762-M3X8'."""
    idx = bolts_loader.designation_index(tables)
    norm = mpn.upper().replace("-", " ").replace("_", " ")
    for key, std in idx.items():
        if key and key in norm:
            # A standard was named; find the size token in the remainder.
            m = _METRIC_RE.search(mpn)
            if m and std.kind == "fastener":
                thread = "M" + m.group("d")
                length = m.group("length")
                pitch = m.group("pitch")
                if length is None and pitch is not None:
                    length = pitch
                params = std.get(thread)
                if params is not None:
                    length_mm = float(length) if length is not None else None
                    return _build_match(std, thread, length_mm, params, designation=mpn)
            if std.kind == "bearing":
                return _match_bearing(mpn, {std.id: std})
    return None


def _build_match(std: Standard, size: str, length_mm: float | None,
                 params: dict[str, Any], *, designation: str) -> dict[str, Any]:
    catalog_id = f"{std.id} {size}" + (f"x{length_mm:g}" if length_mm else "")
    return {
        "source": "bolts",
        "standard": std.id,
        "standard_name": std.name,
        "family": std.family,
        "size": size,
        "length_mm": length_mm,
        "needs_length": std.needs_length,
        "params": dict(params),
        "catalog_id": catalog_id,          # provenance
        "designation": designation,
    }


def match_bolts(mpn: str) -> dict[str, Any] | None:
    """Look ``mpn`` up in the clean-room ISO/DIN tables → a match dict, or ``None``.

    Accepts a raw or already-normalized PN (normalization is idempotent). Recognizes bearing
    numbers (with seal codes), metric fastener designations (``M3``, ``M3x8``) with an optional
    family hint, and explicit standard designations (``ISO 4762 M3x8``). The returned dict
    carries ``family``/``size``/``length_mm`` (for :func:`generate_model`), the nominal
    ``params``, and ``catalog_id`` provenance. ``None`` when nothing matches (never a guess).
    """
    norm = normalize_mpn(mpn)
    if not norm:
        return None
    tables = bolts_loader.load_bolts_tables()
    if not tables:
        return None
    return (
        _match_explicit_standard(norm, tables)
        or _match_bearing(norm, tables)
        or _match_metric_fastener(norm, tables)
    )


# --------------------------------------------------------------------------------------
# 3. KiCad symbol-library fallback (clean-room; user-supplied libs only)
# --------------------------------------------------------------------------------------

# Property keys a KiCad symbol may use to carry a manufacturer part number.
_KICAD_MPN_KEYS = ("MPN", "Manufacturer Part Number", "Mfr Part #", "Part Number",
                   "Mouser Part Number", "Digi-Key Part Number")


def _kicad_lib_files() -> list[str]:
    """User-configured `.kicad_sym` files/dirs from ``AICAD_KICAD_SYM_PATH`` (os.pathsep)."""
    raw = os.environ.get("AICAD_KICAD_SYM_PATH", "").strip()
    if not raw:
        return []
    files: list[str] = []
    for entry in raw.split(os.pathsep):
        entry = os.path.expanduser(entry.strip())
        if not entry:
            continue
        if os.path.isdir(entry):
            for name in sorted(os.listdir(entry)):
                if name.lower().endswith((".kicad_sym", ".sym")):
                    files.append(os.path.join(entry, name))
        elif os.path.isfile(entry):
            files.append(entry)
    return files


def _symbol_mpns(sdef: dict[str, Any]) -> set[str]:
    vals: set[str] = set()
    for key in ("libid", "value", "name"):
        v = sdef.get(key)
        if v:
            vals.add(str(v))
    fields = sdef.get("fields") or {}
    if isinstance(fields, dict):
        for key in _KICAD_MPN_KEYS:
            if fields.get(key):
                vals.add(str(fields[key]))
    return {v.strip() for v in vals if v.strip()}


def match_kicad(mpn: str) -> dict[str, Any] | None:
    """Clean-room lookup of ``mpn`` in the user's OWN installed KiCad symbol libraries.

    Reads only libraries the user pointed at via ``AICAD_KICAD_SYM_PATH`` (KiCad's official
    libraries are CC-BY-SA and are NOT vendored — ``contracts/licenses.md`` ECAD posture). Uses
    the existing clean-room ``.kicad_sym`` parser (no KiCad code imported). Returns spec metadata
    (footprint/description/fields) with ``source: "kicad"``, or ``None`` when no library is
    configured or nothing matches — fully offline/CI-safe.
    """
    norm = normalize_mpn(mpn)
    if not norm:
        return None
    files = _kicad_lib_files()
    if not files:
        return None
    try:
        from engine.ecad.library.kicad_sym import load_kicad_sym
    except Exception:  # noqa: BLE001 — ECAD lib optional; degrade to no-match
        return None

    target = norm.upper()
    for path in files:
        try:
            symbols = load_kicad_sym(path)
        except Exception:  # noqa: BLE001 — a broken lib must not sink the lookup
            continue
        for libid, sdef in symbols.items():
            candidates = {c.upper() for c in _symbol_mpns(sdef)}
            if target in candidates or norm in _symbol_mpns(sdef):
                return {
                    "source": "kicad",
                    "library": path,
                    "libid": sdef.get("libid", libid),
                    "footprint": sdef.get("footprint") or "",
                    "description": sdef.get("description") or "",
                    "params": dict(sdef.get("fields") or {}),
                    "catalog_id": f"kicad:{sdef.get('libid', libid)}",  # provenance
                    "designation": norm,
                }
    return None


# --------------------------------------------------------------------------------------
# 4. Geometry generation via the core `parts_make` RPC
# --------------------------------------------------------------------------------------

def generate_model(match: dict[str, Any]) -> str | None:
    """Build editable geometry for a BOLTS/ISO ``match`` and return the saved model path.

    Delegates to the ``core/parts`` catalog through the ``parts_make`` core RPC (same generator
    the ``make_standard_part`` MCP tool uses), then saves a STEP next to the vendor-parts cache.
    Only ``source == "bolts"`` matches (which carry a catalog ``family``/``size``) are buildable;
    KiCad metadata matches return ``None`` (spec reference only). Returns ``None`` — never
    raises — when the native core is unavailable (offline/CI) or the build fails, so callers get
    the contract's "no model" state rather than a traceback.
    """
    if not match or match.get("family") is None or match.get("source") != "bolts":
        return None
    family = str(match["family"])
    size = str(match["size"])
    length_mm = match.get("length_mm")
    try:
        import sys
        import tempfile
        from pathlib import Path

        # The core bridge lives under the MCP services package.
        repo = Path(__file__).resolve().parent.parent
        svc = repo / "services" / "mcp"
        if str(svc) not in sys.path:
            sys.path.insert(0, str(svc))
        import core_bridge as cb  # type: ignore

        params: dict[str, Any] = {"family": family, "size": size}
        if length_mm is not None:
            params["length_mm"] = float(length_mm)
        r = cb.call("parts_make", params)
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        doc_id = r.get("doc_id")
        if not doc_id:
            return None
        stem = re.sub(r"[^A-Za-z0-9]+", "_", match.get("catalog_id") or f"{family}_{size}").strip("_")
        out = Path(tempfile.gettempdir()) / "aicad_spec_models" / f"{stem}.step"
        out.parent.mkdir(parents=True, exist_ok=True)
        cb.call("doc_save", {"doc_id": doc_id, "path": str(out)})
        return str(out) if out.exists() else None
    except Exception:  # noqa: BLE001 — core absent / build failure → contract "no model"
        return None


# --------------------------------------------------------------------------------------
# 5. Orchestrator
# --------------------------------------------------------------------------------------

def match_part_number(mpn: str, *, generate: bool = True) -> dict[str, Any]:
    """Resolve a purchase part number to a spec match (+ optional model). Contract-shaped.

    Pipeline: :func:`normalize_mpn` → :func:`match_bolts` → :func:`match_kicad`. On a hit,
    returns ``{matched: True, source, mpn, normalized, params, catalog_id, model_path, ...}``
    (``model_path`` is populated when ``generate`` and the native core can build the part; it is
    ``None`` offline). On a miss returns the contract-shaped *unknown*:
    ``{matched: False, source: None, mpn, normalized, params: None, model_path: None}`` —
    never a fabricated part.
    """
    normalized = normalize_mpn(mpn)
    match = match_bolts(mpn) or match_kicad(mpn)
    if not match:
        return {
            "matched": False,
            "source": None,
            "mpn": str(mpn or ""),
            "normalized": normalized,
            "params": None,
            "model_path": None,
        }
    model_path = generate_model(match) if generate else None
    out: dict[str, Any] = {
        "matched": True,
        "source": match.get("source"),
        "mpn": str(mpn or ""),
        "normalized": normalized,
        "catalog_id": match.get("catalog_id"),
        "params": match.get("params"),
        "model_path": model_path,
    }
    # Pass through the geometry-selecting fields when present (bolts matches).
    for key in ("standard", "standard_name", "family", "size", "length_mm",
                "needs_length", "library", "libid", "footprint", "description"):
        if key in match:
            out[key] = match[key]
    return out
