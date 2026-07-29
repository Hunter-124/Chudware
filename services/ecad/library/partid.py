"""ECAD part_id resolver — the ``ecad:<Library>:<Symbol>`` <-> ``libid`` boundary.

WHY THIS EXISTS (live gap, 2026-07-29 confirmation sweep, claude-sonnet-4-5, ecad turn):

    part_get(part_id="ecad:RF_Module:ESP32-WROOM-32")  -> "part_id not found"
    part_get(part_id="ecad:Device:R_0603")             -> "part_id not found"
    part_place(part_id="ecad:RF_Module:ESP32-WROOM-32") -> "unknown part_id ..."

Four of the six measured failures in that sweep were this one shape. The shape itself is
NOT wrong: ``services/parts/adapters/ecad_kicad_adapter.py`` mints every ECAD catalogue id
as ``f"ecad:{libid}"``, so ``ecad:Device:R`` and ``ecad:MCU:Generic_QFP48`` are real ids.
What the model got wrong was the SYMBOL — the clean-room starter library has no
``ESP32-WROOM-32`` and no ``Device:R_0603`` (0603 is a FOOTPRINT size: the real id is
``ecad:Resistor_SMD:R_0603``, and its symbol is ``ecad:Device:R``).

So the fix is deliberately split:

  * :func:`resolve` is EXACT-OR-REFUSE. It only canonicalises spelling noise that cannot
    change which record you get — case, surrounding whitespace, and a missing/duplicated
    ``ecad:`` domain prefix. It NEVER crosses a meaning boundary: it will not turn a symbol
    id into a footprint id, and it will not pick a "closest" part. A wrong component that
    reports ``ok: true`` is far worse than the honest refusal it would replace.

  * :func:`candidates` / :func:`hint` make that refusal USEFUL. The old dead end told the
    model to "Use part_search to discover valid part_ids" — a whole extra round trip for
    information this module can compute on the spot. It runs the same keyword search
    ``part_search`` would (``services.ecad.library.search``), adds the real siblings of the
    library the model named, and adds ids that carry the same symbol name under a different
    library, then names them in the error.

Nothing here places, substitutes, or guesses a part.
"""

from __future__ import annotations

import os
import re
from typing import Any

DOMAIN = "ecad"
_PREFIX = DOMAIN + ":"

# Cap on ids named in one hint: enough to pick from, short enough to read.
_MAX_CANDIDATES = 6
# Tokens carrying no discriminating power in an ECAD id.
_STOPWORDS = frozenset({"ecad", "module", "generic", "smd", "tht", "device", "package",
                        "the", "and"})


def _native_ids() -> tuple[list[str], list[str]]:
    """(symbol libids, footprint fpids) stored in the registered NATIVE library dirs.

    Resolution tier 2 — where ``ecad_make_part`` writes a generated part. ``search()``
    already scans these dirs, so a module that only knew the starter library would name a
    generated part as a candidate and then fail to fetch it. Never raises.
    """
    syms: list[str] = []
    fps: list[str] = []
    try:
        from .native import _filename_to_libid, get_default_library
        dirs = list(getattr(get_default_library(), "_native_dirs", ()) or ())
    except Exception:  # noqa: BLE001
        return syms, fps
    for d in dirs:
        for sub, ext, sink in (("symbols", ".sym.json", syms),
                               ("footprints", ".fp.json", fps)):
            try:
                names = os.listdir(os.path.join(d, sub))
            except OSError:
                continue
            sink.extend(_filename_to_libid(f, ext) for f in names if f.endswith(ext))
    return syms, fps


def _catalog() -> tuple[dict, dict]:
    """(symbols, footprints) the ECAD catalogue can actually serve: the clean-room starter
    library plus anything in the registered native dirs. Only the KEYS (libids) are relied
    on by this module. ``({}, {})`` when the library is unavailable — never raises."""
    try:
        from .starter import FOOTPRINTS, SYMBOLS
        symbols, footprints = dict(SYMBOLS), dict(FOOTPRINTS)
    except Exception:  # noqa: BLE001 — a hint path must never raise
        symbols, footprints = {}, {}
    native_syms, native_fps = _native_ids()
    for libid in native_syms:
        symbols.setdefault(libid, {})
    for fpid in native_fps:
        footprints.setdefault(fpid, {})
    return symbols, footprints


def strip_domain(part_id: Any) -> str:
    """``'ecad:Device:R'`` -> ``'Device:R'``; a bare libid passes through unchanged.

    A doubled prefix (``'ecad:ecad:Device:R'``) is folded too — models paste an id that
    already carries the domain into a template that adds one.
    """
    text = str(part_id or "").strip()
    while text[:len(_PREFIX)].lower() == _PREFIX:
        text = text[len(_PREFIX):].strip()
    return text


def _fold(libid: str) -> str:
    """Match key for a libid: case and whitespace are noise, ``:`` structure is not."""
    return re.sub(r"\s+", "", str(libid)).lower()


# --------------------------------------------------------- KiCad standard-library dialect
# The invented ids in the live sweep were not random. ``Device``, ``RF_Module``,
# ``Connector_Generic``, ``power`` are literal KiCad standard-library names, and KiCad's own
# ``Library:Symbol`` convention is what popular KiCad MCP servers expose, so a model reaches
# for it unprompted. Our starter library deliberately uses the same convention and the same
# names for the common parts, so most KiCad ids already resolve by exact match above.
#
# This table covers the rest: KiCad ids that denote THE SAME DEVICE our library already has,
# under KiCad's naming convention. Every row was hand-audited against the pin list in
# starter.py, and the bar is IDENTITY, not similarity:
#
#   * drawing-STYLE variants only (``_Small``, ``_US``, ``_ALT``) — same device, same pins;
#   * package/punctuation suffixes on the same part number (LM7805_TO220, MPU-6050);
#   * pin-ORDER suffixes ONLY where the order matches ours exactly. ``Device:Q_NPN_BEC``
#     maps because our Q_NPN really is 1=B, 2=E, 3=C; ``Q_NPN_BCE``/``_EBC``/``_CBE`` do
#     NOT map, because binding them would renumber the transistor's pins and every wire
#     drawn to them would land on the wrong leg while the call reported success.
#
# Deliberately ABSENT, and they must stay absent:
#   Device:Crystal        -> two candidates here (Crystal_HSE, Crystal_32k768) and picking a
#                            frequency class is a substitution, not a spelling.
#   Device:D_Schottky     -> a Schottky is not the generic diode; different Vf.
#   Device:R_Shunt        -> KiCad's is a 4-terminal Kelvin symbol; our R_Sense has 2 pins.
#   Switch:SW_Push        -> momentary; our Switch carries no momentary/latching semantics.
#   Connector_Generic:Conn_02xNN -> KiCad's Odd_Even/Counter_Clockwise numbering variants
#                            disagree about which physical pin is pin 3.
#   Timer:LM555 / ICM7555 -> different silicon (CMOS vs bipolar), not a spelling of NE555.
# Each of those still gets the candidate hint, which names the real ids and lets the MODEL
# choose. A mapping table is the right long-term home for a real KiCad importer; this one is
# closed, small and hand-checked on purpose.
_KICAD_IDENTITY: dict[str, str] = {}


def _kicad(native: str, *kicad_ids: str) -> None:
    for k in kicad_ids:
        _KICAD_IDENTITY[_fold(k)] = native


#                     our libid                KiCad spellings of the SAME device
_kicad("Device:R", "Device:R_Small", "Device:R_US", "Device:R_US_Small")
_kicad("Device:C", "Device:C_Small")
_kicad("Device:C_pol", "Device:CP", "Device:CP_Small",
       "Device:C_Polarized", "Device:C_Polarized_Small")
_kicad("Device:L", "Device:L_Small")
_kicad("Device:D", "Device:D_Small", "Device:D_ALT", "Device:D_Small_ALT")
_kicad("Device:LED", "Device:LED_Small", "Device:LED_ALT", "Device:LED_Small_ALT")
_kicad("Device:Zener", "Device:D_Zener", "Device:D_Zener_Small", "Device:D_Zener_ALT")
_kicad("Device:NTC", "Device:Thermistor_NTC", "Device:Thermistor_NTC_Small", "Device:R_NTC")
_kicad("Device:Ferrite_Bead", "Device:FerriteBead", "Device:FerriteBead_Small",
       "Device:Ferrite_Bead_Small")
_kicad("Device:Q_NPN", "Device:Q_NPN_BEC")      # ours: 1=B 2=E 3=C
_kicad("Device:Q_PNP", "Device:Q_PNP_BEC")      # ours: 1=B 2=E 3=C
_kicad("Device:NMOS", "Device:Q_NMOS_GSD")      # ours: 1=G 2=S 3=D
_kicad("Device:PMOS", "Device:Q_PMOS_GSD")      # ours: 1=G 2=S 3=D
_kicad("Device:Switch", "Switch:SW_SPST")
_kicad("Connector:Conn_2", "Connector_Generic:Conn_01x02")
_kicad("Connector:Conn_3", "Connector_Generic:Conn_01x03")
_kicad("Connector:Conn_4", "Connector_Generic:Conn_01x04")
_kicad("power:+3V3", "power:+3.3V")
_kicad("Regulator_Linear:7805", "Regulator_Linear:L7805", "Regulator_Linear:LM7805",
       "Regulator_Linear:LM7805_TO220", "Regulator_Linear:LM7805_TO263",
       "Regulator_Linear:7805_TO220")
_kicad("Timer:NE555", "Timer:NE555P", "Timer:NE555D", "Timer:NE555N")
_kicad("Sensor_Motion:MPU6050", "Sensor_Motion:MPU-6050")
_kicad("Sensor_Pressure:BMP280", "Sensor_Pressure:BMP-280")


def resolve(part_id: Any) -> dict | None:
    """EXACT resolution of a model-written id to a real catalogue id, else ``None``.

    Returns ``{"part_id", "libid", "kind", "note"}`` where ``kind`` is ``"symbol"`` or
    ``"footprint"`` and ``note`` is the interpretation to echo back (empty when the id was
    already canonical). Three things are forgiven, in this order, and nothing else:

      1. a real id, exactly as written;
      2. spelling noise that cannot change which record you get — ``ecad:`` domain prefix,
         case, whitespace;
      3. a KiCad standard-library spelling of a device we already have, from the closed
         hand-audited ``_KICAD_IDENTITY`` table above.

    A near miss is NEVER resolved — see the module docstring.
    """
    libid = strip_domain(part_id)
    if not libid or ":" not in libid:
        return None
    symbols, footprints = _catalog()

    def _hit(real: str, kind: str, why: str) -> dict:
        return {"part_id": _PREFIX + real, "libid": real, "kind": kind,
                "note": "" if why == "" else
                        (f"read part_id {str(part_id).strip()!r} as {_PREFIX + real!r} "
                         f"({why})")}

    for kind, table in (("symbol", symbols), ("footprint", footprints)):
        if libid in table:
            return _hit(libid, kind, "")
    key = _fold(libid)
    matches = [(kind, real)
               for kind, table in (("symbol", symbols), ("footprint", footprints))
               for real in table if _fold(real) == key]
    if len(matches) == 1:
        kind, real = matches[0]
        return _hit(real, kind, f"same {kind}, canonical spelling")
    if len(matches) > 1:
        return None  # the same spelling names a symbol AND a footprint -> ambiguous
    native = _KICAD_IDENTITY.get(key)
    if native:
        # The table only ever points at ids this library really has; a stale row must
        # refuse rather than resolve to nothing.
        for kind, table in (("symbol", symbols), ("footprint", footprints)):
            if native in table:
                return _hit(native, kind,
                            f"KiCad standard-library spelling of the same {kind}")
    return None


def _tokens(libid: str) -> list[str]:
    """Searchable words in a libid: ``'RF_Module:ESP32-WROOM-32'`` -> ``['esp32', 'wroom']``.

    Stopwords and fragments under 3 characters are dropped — a bare ``'32'`` or ``'rf'``
    matches half the catalogue on substring score and would bury the real candidates."""
    words = [w.lower() for w in re.split(r"[^A-Za-z0-9]+", libid) if w]
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _search_hits(query: str) -> list[dict]:
    """``services.ecad.library.search`` results; ``[]`` on any failure (hint path)."""
    try:
        from .native import search
        return search(query) or []
    except Exception:  # noqa: BLE001
        return []


def _add(out: list[dict], seen: set, part_id: str, kind: str, why: str) -> None:
    if part_id in seen:
        return
    seen.add(part_id)
    out.append({"part_id": part_id, "kind": kind, "why": why})


def is_ecad_shaped(part_id: Any) -> bool:
    """True when *part_id* claims the ECAD namespace: an explicit ``ecad:`` domain prefix, a
    ``Library:Symbol`` whose LIBRARY is a real ECAD library, or a KiCad standard-library name
    this module knows (``Switch:``, ``Connector_Generic:``). A mechanical id like
    ``'fastener:ISO4762:M8'`` is none of those, so this module stays out of its error."""
    text = str(part_id or "").strip()
    if text[:len(_PREFIX)].lower() == _PREFIX:
        return True
    libid = strip_domain(text)
    lib = libid.rpartition(":")[0]
    if not lib:
        return False
    symbols, footprints = _catalog()
    folded = _fold(lib)
    if any(_fold(k.rpartition(":")[0]) == folded for k in _KICAD_IDENTITY):
        return True
    return any(_fold(real.rpartition(":")[0]) == folded
               for real in list(symbols) + list(footprints))


def candidates(part_id: Any, limit: int = _MAX_CANDIDATES) -> list[dict]:
    """Real catalogue ids a model that wrote *part_id* plausibly meant.

    Ordered most-specific first: ids carrying the SAME symbol name under a different
    library, then keyword hits, then the real contents of the library the model named.
    Every entry is a genuine id — this is search output, not a substitution. Never raises.
    """
    libid = strip_domain(part_id)
    if not libid:
        return []
    lib, _, name = libid.rpartition(":")
    symbols, footprints = _catalog()
    kind_of = {k: "symbol" for k in symbols}
    kind_of.update({k: "footprint" for k in footprints})
    every = list(symbols) + list(footprints)
    out: list[dict] = []
    seen: set = set()

    # 1. Same symbol NAME, different library — the Device:R_0603 case (0603 is a FOOTPRINT
    #    size, so the real id lives under Resistor_SMD and the symbol is plain Device:R).
    if name:
        folded = _fold(name)
        for real in every:
            if _fold(real.rpartition(":")[2]) == folded:
                _add(out, seen, _PREFIX + real, kind_of.get(real, ""),
                     f"same name under library {real.rpartition(':')[0]!r}")

    # 2. The keyword search part_search would have run, per token, best score first.
    scored: dict[str, int] = {}
    for token in _tokens(libid):
        for hit in _search_hits(token):
            real = hit.get("libid") or hit.get("fpid")
            if real:
                scored[real] = max(scored.get(real, 0), int(hit.get("score") or 0))
    for real in sorted(scored, key=lambda r: (-scored[r], r)):
        _add(out, seen, _PREFIX + real, kind_of.get(real, ""), "keyword match")

    # 3. What the named library actually contains.
    if lib:
        folded_lib = _fold(lib)
        for real in every:
            if _fold(real.rpartition(":")[0]) == folded_lib:
                _add(out, seen, _PREFIX + real, kind_of.get(real, ""),
                     f"in library {lib!r}")
    return out[:max(0, int(limit))]


def hint(part_id: Any) -> str:
    """A one-paragraph teaching hint for an unresolvable ECAD-shaped id, or ``''``.

    Names real ids so the model can retry immediately instead of spending a round trip on
    ``part_search``. Returns ``''`` for ids that are not ECAD's to explain (a mechanical
    ``'fastener:ISO4762:M8'`` keeps the caller's own text) and never raises.
    """
    try:
        libid = strip_domain(part_id)
        if not libid or ":" not in libid or not is_ecad_shaped(part_id):
            return ""
        if resolve(part_id) is not None:
            return ""  # it IS a real id — nothing to teach
        found = candidates(part_id)
        lead = (f"'{str(part_id).strip()}' is a well-formed ECAD id "
                f"(ecad:<Library>:<Symbol>) but the library has no '{libid}'. ")
        # When the part genuinely is not here, the satisfiable request is to CREATE it:
        # ecad_make_part (services/mcp/ecad_sch_tools.py) builds symbol + footprint from a
        # caller-supplied pinout and stores it in the native tier, so passing libid=<the id
        # that just failed> makes that very id resolve on the next call. The pinout caveat
        # is stated because ecad_make_part refuses a part name with no pin list — a part
        # number is not a pinout, and sending the model there empty-handed buys a second
        # failure instead of a part.
        make = (f"CREATE it with ecad_make_part(name='{libid.rpartition(':')[2] or libid}', "
                f"libid='{libid}', pins=[{{'number': '1', 'name': 'GND', "
                f"'type': 'power_in'}}, ...], package='<e.g. qfn>') — you must supply the "
                f"pinout from the datasheet, and '{_PREFIX + libid}' then resolves on the "
                f"next call.")
        if found:
            return (lead + "Closest real catalogue ids: "
                    + "; ".join(f"'{c['part_id']}'"
                                + (f" ({c['kind']}, {c['why']})" if c["kind"] else
                                   f" ({c['why']})")
                                for c in found)
                    + ". Symbols are placed with ecad_place_symbol(libid=...) — drop the "
                      "'ecad:' domain prefix for that call — and ecad_find_part('<keywords>') "
                      "searches the same library. If none of those is the part, " + make)
        return (lead + "No symbol or footprint in the library matches those keywords. "
                "Search it with ecad_find_part('<keywords>'), or " + make)
    except Exception:  # noqa: BLE001 — a hint must never break the error it decorates
        return ""
