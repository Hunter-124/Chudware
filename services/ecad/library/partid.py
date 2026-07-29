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
from typing import Any, NamedTuple

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
#   * pin-ORDER variants, via an explicit hand-audited PERMUTATION (``pins=``) that states
#     which of OUR pins each KiCad pin is. ``Device:Q_NPN_BEC`` needs none — our Q_NPN
#     really is 1=B, 2=E, 3=C — while ``Device:Q_NPN_BCE`` carries ``{1:1, 2:3, 3:2}``.
#
# A permutation is only ever acceptable when it is KNOWN and audited, never guessed, and it
# MUST name every pin of the native symbol exactly once (:func:`_remap_problem`). A partial
# permutation completed by an identity fallback would put some wires on the right leg and
# some on the wrong one while the call reported success — the exact failure this table is
# built to prevent — so a partial or unauditable permutation is a hard refusal, both at
# import (:func:`_audit_remaps`) and per call in :func:`resolve`.
#
# Deliberately ABSENT, and they must stay absent — none of these is a pin-numbering
# question, so no permutation can rescue them:
#   Device:Crystal        -> two candidates here (Crystal_HSE, Crystal_32k768) and picking a
#                            frequency class is a substitution, not a spelling.
#   Device:D_Schottky     -> a Schottky is not the generic diode; different Vf.
#   Device:R_Shunt        -> KiCad's is a 4-terminal Kelvin symbol (2 force + 2 sense) and
#                            our R_Sense has 2 pins. There is no permutation: a mapping
#                            would have to DROP the sense pair, and dropped pins are
#                            silently unconnected sense lines.
#   Switch:SW_Push        -> momentary; our Switch carries no momentary/latching semantics.
#   Timer:ICM7555         -> CMOS silicon, not a spelling of the bipolar NE555 (its bipolar
#                            siblings LM555/SA555 ARE the same device and do map, below).
#   Connector_Generic:Conn_02xNN_Row_Letter_First / _Row_Letter_Last / _Top_Bottom
#                         -> the letter variants number pins ``a1..bN`` rather than 1..2N,
#                            and for _Top_Bottom we could not establish from a primary
#                            source which physical pad the second row starts at. An
#                            UNVERIFIED permutation is worse than a refusal.
# Each of those still gets the candidate hint, which names the real ids and lets the MODEL
# choose. A mapping table is the right long-term home for a real KiCad importer; this one is
# closed, small and hand-checked on purpose.


class _Entry(NamedTuple):
    """One row of the KiCad dialect table.

    ``native``  the libid in THIS library that the KiCad id denotes.
    ``pins``    ``{KiCad pin number: our pin number}`` when the two symbols number the same
                device differently, or ``None`` when the numbering already agrees. When
                present it MUST be a one-to-one cover of every pin of ``native``.
    """

    native: str
    pins: dict[str, str] | None = None


def _pins_of(libid: str) -> list[dict]:
    """The pin list of a native libid (starter tier, then the generated tier); ``[]`` when
    unknown. Never raises — a resolver must not die on a library import."""
    sdef: Any = None
    try:
        from .starter import SYMBOLS
        sdef = SYMBOLS.get(libid)
    except Exception:  # noqa: BLE001
        sdef = None
    if sdef is None:
        try:
            from .native import resolve as _lib_resolve
            sdef = _lib_resolve(libid)
        except Exception:  # noqa: BLE001
            sdef = None
    return list((sdef or {}).get("pins") or [])


def _pin_names(libid: str) -> dict[str, str]:
    """``{pin number: pin name}`` for a native libid."""
    return {str(p.get("number")): str(p.get("name") or "") for p in _pins_of(libid)}


def _pin_key(number: Any) -> tuple[int, str]:
    """Sort key that orders pin numbers 1,2,...,10 rather than 1,10,2."""
    text = str(number)
    return (int(text), "") if text.isdigit() else (10 ** 9, text)


def _remap_problem(entry: _Entry) -> str:
    """``''`` when *entry*'s permutation is a full one-to-one cover of the native symbol's
    pins, else the reason the row must be REFUSED.

    A permutation that misses a pin, sends two KiCad pins to one of ours, or names a pin the
    symbol does not have is not a renumbering — it is a partial guess. Completing it with an
    identity fallback is what produces a netlist that looks finished with some wires on the
    wrong leg, so it refuses instead.
    """
    if entry.pins is None:
        return ""
    native = _pin_names(entry.native)
    if not native:
        return (f"pin permutation for {entry.native!r} cannot be audited — that symbol has "
                f"no pin list here")
    mapped = [str(v) for v in entry.pins.values()]
    unknown = sorted(set(mapped) - set(native), key=_pin_key)
    if unknown:
        return (f"pin permutation for {entry.native!r} names pin(s) {', '.join(unknown)}, "
                f"which that symbol does not have")
    dupes = sorted({p for p in mapped if mapped.count(p) > 1}, key=_pin_key)
    if dupes:
        return (f"pin permutation for {entry.native!r} sends more than one source pin to "
                f"pin(s) {', '.join(dupes)}")
    missing = sorted(set(native) - set(mapped), key=_pin_key)
    if missing:
        return (f"pin permutation for {entry.native!r} covers {len(mapped)} of "
                f"{len(native)} pins — pin(s) {', '.join(missing)} unaccounted for")
    return ""


def _remap_sentence(entry: _Entry) -> str:
    """The renumbering, spelled out pin by pin, for the ``note`` a caller reads."""
    if not entry.pins:
        return ""
    names = _pin_names(entry.native)
    moved = [f"your pin {k} ({names.get(str(v), '?')}) is pin {v} here"
             for k, v in sorted(entry.pins.items(), key=lambda kv: _pin_key(kv[0]))
             if str(k) != str(v)]
    if not moved:
        return (f"pin numbering is IDENTICAL to {entry.native}, audited pin by pin for all "
                f"{len(entry.pins)} pins")
    return "PIN REMAP: " + ", ".join(moved)


_KICAD_IDENTITY: dict[str, _Entry] = {}


def _kicad(native: str, *kicad_ids: str, pins: dict | None = None) -> None:
    entry = _Entry(native, {str(k): str(v) for k, v in pins.items()} if pins else None)
    for k in kicad_ids:
        _KICAD_IDENTITY[_fold(k)] = entry


def _audit_remaps() -> None:
    """Fail loudly at import if any row's permutation is not a full one-to-one cover.

    Same posture as ``substitute._validate``: a table this small is hand-checked, and a bad
    row must stop the process rather than quietly mis-wire one device.
    """
    bad = []
    for key, entry in _KICAD_IDENTITY.items():
        problem = _remap_problem(entry)
        if problem:
            bad.append(f"{key}: {problem}")
    if bad:
        raise RuntimeError("KiCad pin permutations are not one-to-one covers: "
                           + "; ".join(sorted(bad)))


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

# ---- discrete three-legged devices: KiCad states the pin order in the suffix -------------
# SOURCE OF THE KiCad NUMBERING: the KiCad ``Device`` library names a discrete's pin order in
# the suffix, one letter per pin in pin-number order — ``Q_NPN_BCE`` is 1=B, 2=C, 3=E. That
# is the same convention the pre-existing ``Q_NPN_BEC`` row was audited against.
# SOURCE OF OUR NUMBERING: starter.py, which numbers these for the SOT-23 JEDEC pad order —
# Q_NPN/Q_PNP 1=B, 2=E, 3=C (MMBT3904/MMBT3906 class) and NMOS/PMOS 1=G, 2=S, 3=D
# (2N7002/BSS84 class). So for the BJTs B->1, E->2, C->3 and for the FETs G->1, S->2, D->3,
# and each permutation below is just the suffix read through that map.
for _fam, _ours in (("Q_NPN", "Device:Q_NPN"), ("Q_PNP", "Device:Q_PNP")):
    _slot = {"B": "1", "E": "2", "C": "3"}
    for _order in ("BCE", "BEC", "CBE", "CEB", "EBC", "ECB"):
        _kicad(_ours, f"Device:{_fam}_{_order}",
               pins={str(i): _slot[letter] for i, letter in enumerate(_order, start=1)})
for _fam, _ours in (("Q_NMOS", "Device:NMOS"), ("Q_PMOS", "Device:PMOS")):
    _slot = {"G": "1", "S": "2", "D": "3"}
    for _order in ("DGS", "DSG", "GDS", "GSD", "SDG", "SGD"):
        _kicad(_ours, f"Device:{_fam}_{_order}",
               pins={str(i): _slot[letter] for i, letter in enumerate(_order, start=1)})
del _fam, _ours, _slot, _order

_kicad("Device:Switch", "Switch:SW_SPST")
_kicad("Connector:Conn_2", "Connector_Generic:Conn_01x02")
_kicad("Connector:Conn_3", "Connector_Generic:Conn_01x03")
_kicad("Connector:Conn_4", "Connector_Generic:Conn_01x04")

# ---- 2xN connectors: the two KiCad numbering schemes ------------------------------------
# SOURCE OF THE KiCad NUMBERING: the ``Connector_Generic`` 2xNN variants, as documented and
# already implemented in-repo at services/ecad/library/generate.py (the ``two_row`` branch) —
# ``_Odd_Even`` runs the odd numbers down the first row and the even numbers down the second,
# so 1 and 2 are the same physical row; ``_Counter_Clockwise`` is the DIP topology, 1..N down
# the first row then N+1..2N back UP the second. Pin 1 is the same corner pad in both.
# SOURCE OF OUR NUMBERING: starter.py ``_header_2xn`` — odd pins down the left column top to
# bottom, even pins down the right, i.e. Odd_Even exactly, hence the identity permutation.
_kicad("Connector:Header_2x03", "Connector_Generic:Conn_02x03_Odd_Even",
       pins={"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6"})
_kicad("Connector:Header_2x03", "Connector_Generic:Conn_02x03_Counter_Clockwise",
       pins={"1": "1", "2": "3", "3": "5", "4": "6", "5": "4", "6": "2"})
_kicad("Connector:Header_2x05", "Connector_Generic:Conn_02x05_Odd_Even",
       pins={str(i): str(i) for i in range(1, 11)})
_kicad("Connector:Header_2x05", "Connector_Generic:Conn_02x05_Counter_Clockwise",
       pins={"1": "1", "2": "3", "3": "5", "4": "7", "5": "9",
             "6": "10", "7": "8", "8": "6", "9": "4", "10": "2"})

_kicad("power:+3V3", "power:+3.3V")
_kicad("Regulator_Linear:7805", "Regulator_Linear:L7805", "Regulator_Linear:LM7805",
       "Regulator_Linear:LM7805_TO220", "Regulator_Linear:LM7805_TO263",
       "Regulator_Linear:7805_TO220")
_kicad("Timer:NE555", "Timer:NE555P", "Timer:NE555D", "Timer:NE555N")
# LM555 (TI/National, datasheet SNAS548) and SA555 (TI SLFS022, the same datasheet as our
# NE555 seed in starter.py) are the BIPOLAR 555 — the same device as NE555 from another
# vendor, exactly like the L7805/LM7805 rows above — and the datasheet DIP-8/SOIC-8 pinout is
# identical: 1=GND, 2=TRIG, 3=OUT, 4=RESET, 5=CONT, 6=THRES, 7=DISCH, 8=VCC, which is
# starter.py's Timer:NE555 numbering pin for pin. The permutation is spelled out anyway
# rather than left absent, so the audit records that all EIGHT pins were checked. The CMOS
# 555s (ICM7555/TLC555/LMC555) are different silicon and stay refused.
_kicad("Timer:NE555", "Timer:LM555", "Timer:SA555",
       pins={str(i): str(i) for i in range(1, 9)})
_kicad("Sensor_Motion:MPU6050", "Sensor_Motion:MPU-6050")
_kicad("Sensor_Pressure:BMP280", "Sensor_Pressure:BMP-280")

_audit_remaps()


def resolve(part_id: Any) -> dict | None:
    """EXACT resolution of a model-written id to a real catalogue id, else ``None``.

    Returns ``{"part_id", "libid", "kind", "note", "pin_remap"}`` where ``kind`` is
    ``"footprint"`` and ``note`` is the interpretation to echo back (empty when the id was
    already canonical). Three things are forgiven, in this order, and nothing else:

      1. a real id, exactly as written;
      2. spelling noise that cannot change which record you get — ``ecad:`` domain prefix,
         case, whitespace;
      3. a KiCad standard-library spelling of a device we already have, from the closed
         hand-audited ``_KICAD_IDENTITY`` table above, including its PIN PERMUTATION when
         KiCad numbers that device's pins differently (``pin_remap``, ``{KiCad pin: our
         pin}``, ``None`` when the numbering already agrees). A row whose permutation is
         partial or unauditable is REFUSED, never identity-mapped.

    A near miss is NEVER resolved — see the module docstring.
    """
    libid = strip_domain(part_id)
    if not libid or ":" not in libid:
        return None
    symbols, footprints = _catalog()

    def _hit(real: str, kind: str, why: str, remap: dict | None = None) -> dict:
        return {"part_id": _PREFIX + real, "libid": real, "kind": kind,
                "pin_remap": dict(remap) if remap else None,
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
    entry = _KICAD_IDENTITY.get(key)
    if entry:
        if _remap_problem(entry):
            return None  # partial/unauditable permutation -> refuse, never identity-fill
        # The table only ever points at ids this library really has; a stale row must
        # refuse rather than resolve to nothing.
        for kind, table in (("symbol", symbols), ("footprint", footprints)):
            if entry.native in table:
                why = f"KiCad standard-library spelling of the same {kind}"
                sentence = _remap_sentence(entry)
                if sentence:
                    why += "; " + sentence
                return _hit(entry.native, kind, why, entry.pins)
    return None


def resolved_pins(part_id: Any) -> list[dict] | None:
    """The pin table for *part_id* IN THE NUMBERING THE CALLER WROTE, or ``None``.

    This is where the permutation is applied. Each item is
    ``{"number", "native_number", "name", "type"}``: ``number`` is the pin number in the
    dialect of the id that was passed in, ``native_number`` is the pin on the symbol that
    will actually be placed. For a ``Device:Q_NPN_BCE`` the entry for ``number='2'`` carries
    ``native_number='3'`` and ``name='C'`` — a caller that meant "KiCad pin 2" must wire our
    pin 3, and this says so instead of leaving it to be inferred.

    ``None`` when the id does not resolve, does not name a symbol, or the symbol's pin list
    is not available — never a partial or identity-filled table.
    """
    hit = resolve(part_id)
    if hit is None or hit.get("kind") != "symbol":
        return None
    by_number = {str(p.get("number")): p for p in _pins_of(hit["libid"])}
    if not by_number:
        return None
    remap = hit.get("pin_remap") or {n: n for n in by_number}
    out: list[dict] = []
    for caller, native in sorted(remap.items(), key=lambda kv: _pin_key(kv[0])):
        pin = by_number.get(str(native))
        if pin is None:
            return None  # unreachable past _remap_problem; still never guess a pin
        out.append({"number": str(caller), "native_number": str(native),
                    "name": str(pin.get("name") or ""),
                    "type": str(pin.get("type") or "")})
    return out


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
