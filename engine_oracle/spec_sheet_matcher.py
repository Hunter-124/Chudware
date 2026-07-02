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
from engine import paths
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


# ======================================================================================
# 6. AI path (T-B5): datasheet text -> dimension dict -> parametric template -> VendorPart
# ======================================================================================
#
# The deterministic lane above resolves a *known* standard part (a screw, a bearing) from its
# designation. This lane handles the long tail: a purchased part whose datasheet or listing gives
# real dimensions but no catalog match. An LLM reads the datasheet text and returns a dimension
# dict keyed to a small **parametric template** (a capacitor can, a generic box/cylinder); we fill
# the template, build editable geometry, render a GSL text preview for the user to confirm, and
# register the result as a ``VendorPart`` with ``source="spec_sheet"``.
#
# Provenance is still never fabricated: the LLM may only populate keys the template defines; a
# missing REQUIRED dimension is surfaced (``missing_required``), never guessed, and
# :func:`register_from_spec_sheet` refuses to build until every required dim is present. Every LLM
# call goes through the T-F9 ``Provider`` contract wrapped in the T-H7 ``LoggingProvider`` so it is
# flywheel-logged (insertion point IP-16, "standards/parts lookup" per the model-integration survey).

import json as _json

_SPEC_INSERTION_POINT = "IP-16"  # survey IP-16: standards/parts lookup & substitution


class SpecTemplate:
    """A parametric template: the dims an LLM must extract + how to build editable geometry.

    ``required`` dims gate the build (never defaulted — a missing one is reported, not guessed);
    ``optional`` dims are recorded on the part's specs and may inform geometry. ``build_ops`` maps a
    validated dim dict to a list of ``doc_apply_ops`` feature ops (box/cylinder primitives) — the
    editable geometry the ``script``-feature path produces.
    """

    def __init__(self, name: str, required: tuple[str, ...], optional: tuple[str, ...],
                 build_ops: Callable[[dict[str, float]], list[dict[str, Any]]],
                 *, description: str = "") -> None:
        self.name = name
        self.required = required
        self.optional = optional
        self._build_ops = build_ops
        self.description = description

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.required) + tuple(self.optional)

    def build_ops(self, dims: dict[str, float]) -> list[dict[str, Any]]:
        return self._build_ops(dims)


def _p(value: float) -> dict[str, Any]:
    """A parametric-value param cell (``{"value": v, "expr": null}``) per the doc-ops schema."""
    return {"value": float(value), "expr": None}


def _cylinder_ops(radius_mm: float, height_mm: float, name: str = "Body") -> list[dict[str, Any]]:
    return [{"op": "add_feature", "feature": {
        "type": "cylinder", "name": name,
        "params": {"radius": _p(radius_mm), "height": _p(height_mm)}}}]


def _box_ops(length_mm: float, width_mm: float, height_mm: float,
             name: str = "Body") -> list[dict[str, Any]]:
    return [{"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": _p(length_mm), "width": _p(width_mm), "height": _p(height_mm)}}}]


def _cap_radial_ops(dims: dict[str, float]) -> list[dict[str, Any]]:
    # Radial (can) electrolytic: an upright cylinder can. Diameter -> radius, length -> height.
    return _cylinder_ops(float(dims["body_diameter_mm"]) / 2.0,
                         float(dims["body_length_mm"]), name="CapBody")


def _cap_axial_ops(dims: dict[str, float]) -> list[dict[str, Any]]:
    # Axial cap: a cylinder body (leads exit each end; recorded in specs, not modeled here).
    return _cylinder_ops(float(dims["body_diameter_mm"]) / 2.0,
                         float(dims["body_length_mm"]), name="CapBody")


def _generic_cylinder_ops(dims: dict[str, float]) -> list[dict[str, Any]]:
    return _cylinder_ops(float(dims["diameter_mm"]) / 2.0, float(dims["height_mm"]))


def _generic_box_ops(dims: dict[str, float]) -> list[dict[str, Any]]:
    return _box_ops(float(dims["length_mm"]), float(dims["width_mm"]), float(dims["height_mm"]))


# Registry of supported parametric templates. Small by design — this is the fallback lane, not a
# catalog. New templates are additive: name -> SpecTemplate.
TEMPLATES: dict[str, SpecTemplate] = {
    "capacitor_radial": SpecTemplate(
        "capacitor_radial",
        required=("body_diameter_mm", "body_length_mm"),
        optional=("lead_spacing_mm", "lead_diameter_mm", "capacitance_uf",
                  "voltage_v", "tolerance_pct"),
        build_ops=_cap_radial_ops,
        description="Radial (can) electrolytic capacitor body"),
    "capacitor_axial": SpecTemplate(
        "capacitor_axial",
        required=("body_diameter_mm", "body_length_mm"),
        optional=("lead_diameter_mm", "capacitance_uf", "voltage_v", "tolerance_pct"),
        build_ops=_cap_axial_ops,
        description="Axial-lead capacitor body"),
    "cylinder": SpecTemplate(
        "cylinder", required=("diameter_mm", "height_mm"), optional=(),
        build_ops=_generic_cylinder_ops, description="Generic cylindrical body"),
    "box": SpecTemplate(
        "box", required=("length_mm", "width_mm", "height_mm"), optional=(),
        build_ops=_generic_box_ops, description="Generic rectangular body"),
}

# Dimension keys are always numeric (mm / SI); non-dimension metadata keys the LLM may also fill.
_META_KEYS = ("capacitance_uf", "voltage_v", "tolerance_pct")


def list_templates() -> list[dict[str, Any]]:
    """The supported parametric templates + their required/optional dimension keys."""
    return [{"name": t.name, "required": list(t.required), "optional": list(t.optional),
             "description": t.description} for t in TEMPLATES.values()]


def _resolve_text(text_or_url: str, *,
                  fetch: Callable[[str], dict] | None = None) -> str:
    """Return datasheet text: pass-through for pasted text, or fetch a listing URL to markdown.

    A ``http(s)://`` argument is fetched through the research fetch_page helper (offline/cache
    aware, no scraping of blocked hosts); anything else is treated as already-pasted text. A fetch
    that fails degrades to an empty string (the extractor then simply finds no dims).
    """
    s = str(text_or_url or "").strip()
    if not re.match(r"^https?://", s, re.IGNORECASE):
        return s
    try:
        fetch = fetch or _default_fetch
        res = fetch(s)
    except Exception:  # noqa: BLE001 — network/offline failure -> no text, never a traceback
        return ""
    if isinstance(res, dict) and res.get("ok") and res.get("markdown"):
        return str(res["markdown"])
    return ""


def _default_fetch(url: str) -> dict:
    from engine.research.fetch_page import fetch_page  # lazy: keep HTTP surface off import
    return fetch_page(url)


def _coerce_num(value: Any) -> float | None:
    """Coerce an LLM-returned scalar to a float, tolerating ``"6.3 mm"`` / ``"470uF"`` strings."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[-+]?\d*\.?\d+", value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _extract_json_object(reply: str) -> dict[str, Any]:
    """Pull the first balanced ``{...}`` JSON object out of an LLM reply (tolerates code fences)."""
    if not reply:
        return {}
    start = reply.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(reply)):
        c = reply[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(reply[start:i + 1])
                    return obj if isinstance(obj, dict) else {}
                except Exception:  # noqa: BLE001
                    return {}
    return {}


def _spec_prompt(template: SpecTemplate) -> str:
    keys = ", ".join(template.keys)
    return (
        "You extract mechanical dimensions from an electronic-component datasheet or product "
        "listing. Read the text and return ONLY a JSON object. Use these keys where the datasheet "
        f"gives the value (omit any you cannot find — NEVER guess): {keys}. All *_mm keys are "
        "millimetres, *_uf microfarads, *_v volts. Also include 'mpn' (manufacturer part number) "
        "and 'description' if present. Output JSON only, no prose."
    )


def _make_journal_sink() -> Callable[[dict], None]:
    """Durable default event sink: append each ai_artifact event to the telemetry journal.

    Keeps the "every LLM call is flywheel-logged" guarantee real when no caller-supplied sink is
    passed (e.g. the MCP tool path). Failures are swallowed — telemetry must never break a call.
    """
    def _sink(event: dict) -> None:
        try:
            out = paths.CACHE / "telemetry" / "events.jsonl"
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
    return _sink


def _default_provider(on_event: Callable[[dict], None] | None):
    """The configured T-F9 provider wrapped in the T-H7 LoggingProvider (flywheel-logged, IP-16)."""
    from services.agent.config import get_provider
    from services.agent.providers import logging_provider
    base = get_provider()
    # Pass sidecar_dir explicitly: LoggingProvider's default resolves engine.paths.DATA, which is
    # absent on the Linux paths module (only %APPDATA%-style constants exist) — see the T-B5 note.
    return logging_provider.wrap(
        base, on_event=on_event or _make_journal_sink(),
        sidecar_dir=paths.CACHE / "telemetry" / "sidecars",
        insertion_point=_SPEC_INSERTION_POINT, suppress_emit=False)


def ai_extract_dims(text_or_url: str, template: str, *,
                    provider: Any = None,
                    on_event: Callable[[dict], None] | None = None,
                    fetch: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Extract a template-keyed dimension dict from datasheet text (or a listing URL) via the LLM.

    Prompts the model (through the T-F9 ``Provider`` contract, wrapped in the T-H7
    ``LoggingProvider`` so the call is flywheel-logged at IP-16) with the datasheet text and the
    template's dimension keys, then parses + validates the JSON reply. Only keys the template
    defines are kept; numeric dims are coerced to floats (tolerating ``"6.3 mm"``); anything the
    model could not find is simply absent — never fabricated.

    Returns ``{"template", "dims", "missing_required", "mpn", "description", "raw"}``. ``dims`` holds
    the validated numeric dimensions; ``missing_required`` lists required keys the datasheet did not
    yield (empty ⇒ ready to build). ``provider`` is injectable (a mock in tests); the default
    resolves the configured provider — never constructed in CI.
    """
    tmpl = TEMPLATES.get(template)
    if tmpl is None:
        raise ValueError(f"unknown template {template!r}; known: {sorted(TEMPLATES)}")

    text = _resolve_text(text_or_url, fetch=fetch)
    prov = provider if provider is not None else _default_provider(on_event)

    from services.agent.providers.base import text_part, user
    system = _spec_prompt(tmpl)
    reply = prov.complete(system, [user([text_part(text)])], max_tokens=1024)

    obj = _extract_json_object(reply)
    dims: dict[str, Any] = {}
    for key in tmpl.keys:
        if key in obj and obj[key] is not None:
            num = _coerce_num(obj[key])
            if num is not None:
                dims[key] = num
    missing = [k for k in tmpl.required if k not in dims]
    return {
        "template": tmpl.name,
        "dims": dims,
        "missing_required": missing,
        "mpn": str(obj.get("mpn") or "").strip() or None,
        "description": str(obj.get("description") or "").strip(),
        "raw": reply,
    }


def gsl_preview(dims: dict[str, Any], template: str, *, mpn: str = "") -> str:
    """A deterministic GSL-style text render of the parametric part, for user confirmation.

    Offline-safe (no core, no vision): summarizes the template and its filled dimensions so the
    user can confirm the extracted spec before it is registered. When the native core is present
    the caller may additionally attach a rendered preview, but this text always exists.
    """
    tmpl = TEMPLATES.get(template)
    head = f"{template}" + (f"  [{mpn}]" if mpn else "")
    lines = [f"spec-sheet part: {head}",
             f"template: {tmpl.description if tmpl else template}"]
    for key in (tmpl.keys if tmpl else sorted(dims)):
        if key in dims:
            unit = "mm" if key.endswith("_mm") else ""
            lines.append(f"  {key} = {dims[key]:g} {unit}".rstrip())
    return "\n".join(lines)


def _default_build_model(template: str, dims: dict[str, float]) -> str | None:
    """Build editable geometry for a filled template via the core and save a STEP; ``None`` offline.

    Runs the ``script``-feature build path: ``doc_new`` → ``doc_apply_ops`` (the template's
    box/cylinder ops) → ``doc_export_step``. Returns the saved model path, or ``None`` when the
    native core is unavailable (CI/offline) or the build fails — the contract "no model" state,
    never a traceback. Injectable via :func:`register_from_spec_sheet`'s ``build_model`` seam.
    """
    tmpl = TEMPLATES.get(template)
    if tmpl is None:
        return None
    try:
        ops = tmpl.build_ops(dims)
    except Exception:  # noqa: BLE001 — a missing dim reaches here as KeyError; treat as no-build
        return None
    try:
        import sys
        import tempfile
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        svc = repo / "services" / "mcp"
        if str(svc) not in sys.path:
            sys.path.insert(0, str(svc))
        import core_bridge as cb  # type: ignore

        r = cb.call("doc_new", {"name": f"spec_{template}"})
        doc_id = r.get("doc_id") if isinstance(r, dict) else None
        if not doc_id:
            return None
        cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        stem = re.sub(r"[^A-Za-z0-9]+", "_", template).strip("_") or "spec_part"
        out = Path(tempfile.gettempdir()) / "aicad_spec_models" / f"{stem}_{doc_id}.step"
        out.parent.mkdir(parents=True, exist_ok=True)
        cb.call("doc_export_step", {"doc_id": doc_id, "path": str(out)})
        return str(out) if out.exists() else None
    except Exception:  # noqa: BLE001 — core absent / build failure -> contract "no model"
        return None


def register_from_spec_sheet(dims: dict[str, Any], template: str, mpn: str, *,
                             supplier: str = "spec_sheet",
                             description: str = "",
                             build_model: Callable[[str, dict], str | None] | None = None,
                             ) -> dict[str, Any]:
    """Build a filled template's geometry and register it as a ``source="spec_sheet"`` VendorPart.

    Validates that every REQUIRED dim for ``template`` is present (raises ``ValueError`` otherwise —
    a part is never built from incomplete dims). Builds editable geometry (``build_model`` seam;
    the default is core-backed and returns ``None`` offline), renders a GSL text preview, and — when
    a model was produced — registers a ``VendorPart`` whose ``cad_path`` links the saved doc,
    ``source="spec_sheet"``, and ``specs`` carries the extracted dims + template.

    Returns ``{"registered", "source", "mpn", "template", "params", "model_path", "preview",
    "vendor_part"}``. With no core (CI/offline) ``registered`` is ``False`` and ``model_path`` is
    ``None`` — the spec + preview are still returned, but no hollow record is written.
    """
    tmpl = TEMPLATES.get(template)
    if tmpl is None:
        raise ValueError(f"unknown template {template!r}; known: {sorted(TEMPLATES)}")
    if not str(mpn or "").strip():
        raise ValueError("mpn is required to register a spec-sheet part")
    missing = [k for k in tmpl.required if k not in dims]
    if missing:
        raise ValueError(
            f"cannot build {template!r}: missing required dims {missing} "
            f"(spec-sheet parts are never built from guessed dimensions)")

    numeric = {k: float(v) for k, v in dims.items()
               if k in tmpl.keys and _coerce_num(v) is not None}
    preview = gsl_preview(numeric, template, mpn=mpn)

    build = build_model or _default_build_model
    model_path = build(template, numeric)

    out: dict[str, Any] = {
        "registered": False,
        "source": "spec_sheet",
        "mpn": str(mpn).strip(),
        "template": template,
        "params": numeric,
        "model_path": model_path,
        "preview": preview,
        "vendor_part": None,
    }
    if not model_path:
        out["reason"] = "no editable model produced (native core unavailable offline)"
        return out

    from engine import vendor_parts
    specs = {**numeric, "template": template}
    rec = vendor_parts.register_local_cad(
        supplier=supplier, part_number=str(mpn).strip(),
        cad_path=model_path, description=description or preview.splitlines()[0],
        specs=specs, source="spec_sheet")
    out["registered"] = True
    out["vendor_part"] = rec
    return out


def spec_sheet_match(text_or_url: str, mpn: str, template: str, *,
                     provider: Any = None,
                     on_event: Callable[[dict], None] | None = None,
                     build_model: Callable[[str, dict], str | None] | None = None,
                     ) -> dict[str, Any]:
    """Full AI path: datasheet -> LLM dims -> parametric build -> GSL preview -> VendorPart.

    Orchestrates :func:`ai_extract_dims` then :func:`register_from_spec_sheet`. Returns a
    contract-shaped dict: ``{matched, source, mpn, template, params, missing_required, model_path,
    preview, registered, vendor_part}``. When the datasheet does not yield every required dim,
    ``matched`` is ``False`` and ``missing_required`` names the gaps (nothing is built or
    registered) — never a fabricated part. ``provider``/``build_model`` are injectable seams
    (mocked in tests); defaults use the configured LLM + native core.
    """
    extracted = ai_extract_dims(text_or_url, template, provider=provider, on_event=on_event)
    resolved_mpn = str(mpn or "").strip() or extracted.get("mpn") or ""
    base: dict[str, Any] = {
        "matched": False,
        "source": "spec_sheet",
        "mpn": resolved_mpn or None,
        "template": extracted["template"],
        "params": extracted["dims"],
        "missing_required": extracted["missing_required"],
        "model_path": None,
        "preview": gsl_preview(extracted["dims"], template, mpn=resolved_mpn),
        "registered": False,
        "vendor_part": None,
    }
    if extracted["missing_required"] or not resolved_mpn:
        if not resolved_mpn:
            base["reason"] = "no manufacturer part number supplied or extracted"
        return base

    reg = register_from_spec_sheet(
        extracted["dims"], template, resolved_mpn,
        description=extracted["description"], build_model=build_model)
    base.update({
        "matched": True,
        "model_path": reg["model_path"],
        "preview": reg["preview"],
        "registered": reg["registered"],
        "vendor_part": reg["vendor_part"],
    })
    if reg.get("reason"):
        base["reason"] = reg["reason"]
    return base
