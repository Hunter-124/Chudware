"""Generate a NEW symbol + footprint for a part the starter library does not have.

WHY THIS EXISTS
---------------
``starter.py`` is a clean-room set of ~53 GENERIC symbols / ~43 footprints, and
``substitute.py`` answers a miss with the closest generic by function. For a 2-pin
passive that is genuinely the right symbol. For a 38-pin module it is not: a live
request for an ``ESP32-WROOM-32`` used to come back as ``MCU:Generic_QFP48`` with a
pin-count-mismatch warning and ``ok: true`` — a schematic wired against that pinout is
a fabricated design, and there was NO tool that could produce the real part. This
module is that tool's engine.

WHAT THIS DOES NOT DO — read this before extending it
-----------------------------------------------------
It never invents an electrical fact.

* Pin NUMBER, NAME and TYPE come from the caller, always. Nothing here looks at
  ``name`` and decides that an "ESP32" has 38 pins or that pin 1 is GND.
* ``generate_symbol`` with an empty pin list REFUSES. A part cannot be produced from a
  part number alone; the refusal names exactly what the caller must supply.
* An unknown pin TYPE is an error, never a default: pin type drives ERC
  (``core/ecad/erc.cpp`` driver/power-source sets), so silently defaulting to
  ``passive`` would turn a real "two outputs shorted" error into a clean run.
* An AMBIGUOUS pin type is also an error. ``"power"`` is refused because it could mean
  ``power_in`` (a pin that must be driven) or ``power_out`` (a pin that drives) and
  those are opposite ERC roles.
* Pin SHAPE is cosmetic (no ERC meaning), so an unrenderable/unknown shape is mapped to
  ``line`` and NOTED rather than refused.
* GEOMETRY is computed from pitch / body / pad numbers the caller supplies. The only
  numbers baked in here are the IPC-7351-style nominals ``starter.py`` already
  documents in its module docstring, and each one is named in
  ``generate_footprint``'s docstring under "assumed nominals". When a family has no
  single documented nominal (QFN/QFP pad rings, module bodies) the missing parameter is
  REFUSED by name instead of guessed.

SCHEMA
------
Output dicts are built with ``starter.py``'s own helpers (``_pin``/``_sym``/``_fp``/
``_smd_pad``/``_tht_pad``/``_silk_box``/``_crtyd_box``), so a generated part is
byte-for-byte the same shape as a hand-authored one and every existing consumer
(``ecad_find_part``, ``ecad_place_symbol``'s inline ``def``, footprint resolution,
DFM, PDN) works unchanged.

PIN PERMUTATION
---------------
``pins`` carries ``number`` and ``name`` as INDEPENDENT caller-set fields, and layout
is derived from ``number`` alone, so an importer that has to remap a foreign pin order
only has to emit the right ``number`` per name — no new mechanism and no change here.
``partid.py`` now does exactly that for the KiCad pin-order variants (``Q_NPN_BCE``,
``Conn_02xNN_Counter_Clockwise``, ...) via its own hand-audited permutation table, and
needed nothing from this module to do it.

STORAGE
-------
``store_part`` writes into the native library dir (resolution tier 2,
``native.Library.add_native_dir``) and registers it on the default singleton, so the
part resolves on the very next call. There is no second resolution tier.
Because tier 1 (starter) wins over tier 2, a generated part may never take a libid the
starter library already owns — that would silently never resolve, so it is refused.
"""

from __future__ import annotations

import json
import math
import os

from ..schema import PIN_TYPES
from .native import _libid_to_filename, get_default_library
from .starter import (
    _circle,
    _crtyd_box,
    _fp,
    _pin,
    _rect,
    _silk_box,
    _smd_pad,
    _sym,
    _tht_pad,
)

__all__ = [
    "generate_symbol",
    "generate_footprint",
    "store_part",
    "make_part",
    "library_dir",
    "PIN_SHAPES",
    "PACKAGES",
    "register_library_dir",
]

# ─── pin vocabulary ───────────────────────────────────────────────────────────
# PIN_TYPES is the AUTHORITATIVE set (services/ecad/schema.py, mirrored in
# core/ecad/schema.cpp pin_types()) — the full KiCad electrical-type enum. Imported,
# never restated, so a core change cannot leave this module behind.

#: KiCad pin GRAPHIC shapes. Cosmetic only — no ERC meaning. Everything outside
#: ``{"line"}`` renders as a plain line today; the value is preserved on the pin so a
#: later renderer can honour it, and the downgrade is reported as a note.
PIN_SHAPES = frozenset({
    "line", "inverted", "clock", "inverted_clock", "input_low", "clock_low",
    "output_low", "falling_edge_clock", "non_logic",
})

#: Shapes this generator actually DRAWS. The rest are stored verbatim + noted.
_RENDERED_SHAPES = frozenset({"line"})

# Unambiguous spellings of a real type. Every mapping is IDENTITY, not similarity:
# "in" can only mean input, "power_input" can only mean power_in.
_TYPE_SYNONYMS = {
    "in": "input", "out": "output",
    "bidi": "bidirectional", "bidir": "bidirectional", "bi_directional": "bidirectional",
    "inout": "bidirectional", "in_out": "bidirectional", "io": "bidirectional",
    "tristate": "tri_state", "tri-state": "tri_state", "three_state": "tri_state",
    "power_input": "power_in", "power_output": "power_out",
    "pwr_in": "power_in", "pwr_out": "power_out",
    "nc": "no_connect", "no-connect": "no_connect", "notconnected": "no_connect",
    "not_connected": "no_connect", "unconnected": "no_connect",
    "oc": "open_collector", "open-collector": "open_collector",
    "oe": "open_emitter", "open-emitter": "open_emitter",
    "unspec": "unspecified",
}

# Words that name a CLASS containing two opposite ERC roles. Mapping either way would
# invent an electrical fact, so they are refused with the disambiguation spelled out.
_AMBIGUOUS_TYPES = {
    "power": "power_in (a pin that must be driven) or power_out (a pin that drives)",
    "supply": "power_in or power_out",
    "vcc": "power_in or power_out",
    "vdd": "power_in or power_out",
    "vss": "power_in or power_out",
    "gnd": "power_in or power_out",
    "ground": "power_in or power_out",
    "analog": "input, output or bidirectional",
    "digital": "input, output or bidirectional",
    "clock": "input or output (clock is a SHAPE, not a type — pass shape='clock')",
}

_SIDES = ("left", "right", "top", "bottom")
_SIDE_SYNONYMS = {
    "l": "left", "west": "left", "w": "left",
    "r": "right", "east": "right", "e": "right",
    "t": "top", "north": "top", "n": "top",
    "b": "bottom", "south": "bottom", "s": "bottom",
}

_NUMBER_KEYS = ("number", "num", "pin", "pin_number", "no", "n")
_NAME_KEYS = ("name", "pin_name", "function", "label", "signal")
_TYPE_KEYS = ("type", "pin_type", "etype", "electrical_type", "elec_type")
_SHAPE_KEYS = ("shape", "pin_shape", "graphic", "graphic_style")
_SIDE_KEYS = ("side", "orientation", "edge", "position_side")

#: Symbol grid (mm). KiCad's schematic grid is 1.27; pins sit on 2.54 like starter's.
_GRID = 2.54
#: Pin stub length (mm) — starter's ``_pin`` default.
_PIN_LEN = 2.54
#: Rough glyph advance (mm) used only to widen a body enough for its pin names.
_CHAR_W = 1.27


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _refuse(message: str, *, needs: str = "", next_call: str = "") -> ValueError:
    """A refusal that names the concrete next call. Never a silent default."""
    parts = [message]
    if needs:
        parts.append("Supply " + needs + ".")
    if next_call:
        parts.append("Next: " + next_call)
    return ValueError(" ".join(parts))


_EXAMPLE_CALL = (
    "ecad_make_part(name='ESP32-WROOM-32', libid='RF_Module:ESP32-WROOM-32', "
    "package='module_castellated', pitch=1.27, body_w=18.0, body_h=25.5, "
    "pad_len=2.0, pad_width=0.9, pins=[{'number':'1','name':'GND',"
    "'type':'power_in','side':'left'}, ...])"
)


def normalize_pin_type(raw, *, where: str = "pin") -> str:
    """One pin's electrical type, or raise. Never defaults — ERC depends on it."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _refuse(
            f"{where}: pin type is missing",
            needs="an explicit electrical type per pin, one of "
                  + ", ".join(sorted(PIN_TYPES)),
            next_call="re-call with type= on every pin (pin type drives ERC; this "
                      "generator will not default it to 'passive')")
    word = str(raw).strip().lower().replace(" ", "_")
    if word in PIN_TYPES:
        return word
    if word in _TYPE_SYNONYMS:
        return _TYPE_SYNONYMS[word]
    if word in _AMBIGUOUS_TYPES:
        raise _refuse(
            f"{where}: pin type {str(raw)!r} is AMBIGUOUS — it could mean "
            f"{_AMBIGUOUS_TYPES[word]}",
            needs="the specific type; these are opposite ERC roles and guessing one "
                  "would invent an electrical fact",
            next_call="re-call with the exact type, e.g. type='power_in' for a supply "
                      "pin the board must feed")
    raise _refuse(
        f"{where}: unknown pin type {str(raw)!r}",
        needs="one of " + ", ".join(sorted(PIN_TYPES)),
        next_call="re-call with a valid type (an unknown type is refused, not "
                  "defaulted, because it changes ERC)")


def normalize_pin_shape(raw, *, where: str = "pin", notes: list | None = None) -> str:
    """One pin's GRAPHIC shape. Cosmetic, so an unrenderable value is mapped + noted."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "line"
    word = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if word in PIN_SHAPES:
        if word not in _RENDERED_SHAPES and notes is not None:
            notes.append(f"{where}: shape {word!r} is a valid KiCad pin shape but is "
                         "drawn as a plain line here; the value is preserved on the "
                         "pin. Shape is cosmetic and has no ERC effect.")
        return word
    if notes is not None:
        notes.append(f"{where}: shape {str(raw)!r} is not a KiCad pin shape — stored "
                     f"as 'line'. Vocabulary: {', '.join(sorted(PIN_SHAPES))}. "
                     "Shape is cosmetic; the pin TYPE (which drives ERC) was not "
                     "touched.")
    return "line"


def _normalize_side(raw, *, where: str = "pin") -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    word = str(raw).strip().lower()
    if word in _SIDES:
        return word
    if word in _SIDE_SYNONYMS:
        return _SIDE_SYNONYMS[word]
    raise _refuse(f"{where}: unknown pin side {str(raw)!r}",
                  needs="one of " + ", ".join(_SIDES),
                  next_call="re-call with side='left'|'right'|'top'|'bottom'")


def _num_key(number: str):
    """Sort key that orders '2' before '10' and keeps non-numeric names stable."""
    s = str(number)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def normalize_pins(pins, *, notes: list | None = None) -> list[dict]:
    """Validate a caller-supplied pin list into ``[{number,name,type,shape,side}]``.

    REFUSES an empty list, a duplicate pin number, a missing number/name/type, and an
    unknown or ambiguous type. Side is optional but ALL-OR-NOTHING: a partly-sided list
    would interleave silently, so it is refused instead.
    """
    if pins is None or (isinstance(pins, (list, tuple)) and not pins):
        raise _refuse(
            "cannot generate a part with no pins — a part number is not a pinout",
            needs="the pin list: [{'number','name','type'[,'side','shape']}, ...], one "
                  "entry per pin, from the part's datasheet",
            next_call=_EXAMPLE_CALL)
    if not isinstance(pins, (list, tuple)):
        raise _refuse(f"pins must be a list of pin objects, got {type(pins).__name__}",
                      needs="a list like "
                            "[{'number':'1','name':'GND','type':'power_in'}]",
                      next_call=_EXAMPLE_CALL)

    out: list[dict] = []
    seen: dict[str, int] = {}
    sided = 0
    for i, raw in enumerate(pins):
        if not isinstance(raw, dict):
            raise _refuse(f"pins[{i}] must be an object, got {type(raw).__name__}",
                          needs="{'number','name','type'} per pin",
                          next_call=_EXAMPLE_CALL)
        where = f"pins[{i}]"
        number = _first(raw, _NUMBER_KEYS)
        if number is None:
            raise _refuse(f"{where}: pin number is missing",
                          needs="a 'number' per pin (the datasheet pad number — this "
                                "is what layout and the netlist key on)",
                          next_call=_EXAMPLE_CALL)
        number = str(number).strip()
        if number in seen:
            raise _refuse(
                f"{where}: duplicate pin number {number!r} (already at "
                f"pins[{seen[number]}])",
                needs="a unique number per pin; a repeated number would make two "
                      "different signals share one net node",
                next_call="re-call with the datasheet's real pad numbers")
        seen[number] = i
        name = _first(raw, _NAME_KEYS)
        if name is None:
            raise _refuse(f"{where}: pin name is missing",
                          needs="a 'name' per pin (the datasheet signal name, e.g. "
                                "'GND', 'IO0', 'EN'). '~' means deliberately unnamed",
                          next_call=_EXAMPLE_CALL)
        ptype = normalize_pin_type(_first(raw, _TYPE_KEYS), where=where)
        shape = normalize_pin_shape(_first(raw, _SHAPE_KEYS), where=where, notes=notes)
        side = _normalize_side(_first(raw, _SIDE_KEYS), where=where)
        if side:
            sided += 1
        out.append({"number": number, "name": str(name).strip(), "type": ptype,
                    "shape": shape, "side": side})

    if sided and sided != len(out):
        missing = [p["number"] for p in out if not p["side"]]
        raise _refuse(
            f"{sided} of {len(out)} pins declare a side; pins {', '.join(missing)} do "
            "not",
            needs="a side on EVERY pin or on none — a partly-sided list would place "
                  "the rest by guesswork",
            next_call="re-call with side= on every pin (or drop it entirely and take "
                      "the default left/right split)")
    return out


def _assign_sides(pins: list[dict], notes: list | None = None) -> dict[str, list[dict]]:
    """Group pins per side in DRAW order.

    Draw order is starter.py's own counterclockwise convention (see MCU:Generic_QFP48,
    Package_DIP:DIP-8): left top->bottom, bottom left->right, right bottom->top, top
    right->left. With no sides declared, pins split half/half onto left+right in number
    order — which for a DIP/SOIC-style part is exactly pin 1 top-left, last pin
    top-right. Purely graphical; it moves no pin NUMBER.
    """
    ordered = sorted(pins, key=lambda p: _num_key(p["number"]))
    if not any(p["side"] for p in ordered):
        half = (len(ordered) + 1) // 2
        for p in ordered[:half]:
            p["side"] = "left"
        for p in ordered[half:]:
            p["side"] = "right"
        if notes is not None and len(ordered) > 2:
            notes.append(
                f"no pin declared a side: pins split {half} left / "
                f"{len(ordered) - half} right in number order (graphical only — pin "
                "numbers and names are unchanged). Pass side= per pin to control it.")
    groups: dict[str, list[dict]] = {s: [] for s in _SIDES}
    for p in ordered:
        groups[p["side"]].append(p)
    # No reversal here: the per-side position formulas below already encode the
    # counterclockwise direction (right = bottom->top, top = right->left) from an
    # ASCENDING number list, exactly as starter's MCU:Generic_QFP48 loop does.
    return groups


def _snap_up(value: float, step: float = 1.27) -> float:
    return math.ceil(round(value / step, 6)) * step


def generate_symbol(name: str, pins, *, libid: str, body=None,
                    reference_prefix: str = "U", value: str = "",
                    description: str = "", keywords: str = "",
                    footprint_filters: list | None = None,
                    notes: list | None = None) -> dict:
    """Build a SymbolDef for a part the library does not have.

    ``pins`` — one object per pin: ``{number, name, type[, shape, side]}``. Every field
    is the CALLER's: nothing is inferred from ``name``. The full KiCad electrical-type
    enum is accepted (``PIN_TYPES``); an unknown or ambiguous type is refused because
    it changes ERC. The KiCad pin-SHAPE vocabulary (``PIN_SHAPES``) is accepted too and
    preserved on the pin; unrendered variants are noted, never fatal.

    ``body`` — optional ``[width, height]`` in mm for the body rectangle. Omitted, the
    body is sized to fit the pins and their names on a 2.54 mm grid. A body too small
    for its pins is enlarged (and noted): overlapping pins are not a valid symbol.

    Geometry follows starter.py exactly: mm, +y-up, ``pos`` is the pin's CONNECTION
    point (the outer end of the stub), ``angle`` is the direction the pin sticks out
    (180 left / 0 right / 90 top / 270 bottom).
    """
    if not str(name or "").strip():
        raise _refuse("name is empty", needs="the part name (e.g. 'ESP32-WROOM-32')",
                      next_call=_EXAMPLE_CALL)
    if not str(libid or "").strip():
        raise _refuse("libid is empty",
                      needs="a lib-qualified id, '<Library>:<Part>' (e.g. "
                            "'RF_Module:ESP32-WROOM-32')",
                      next_call=_EXAMPLE_CALL)
    libid = str(libid).strip()
    if ":" not in libid:
        raise _refuse(f"libid {libid!r} is not lib-qualified",
                      needs="'<Library>:<Part>', because every consumer splits on the "
                            "colon to get the family",
                      next_call=f"re-call with libid='Generated:{libid}'")

    norm = normalize_pins(pins, notes=notes)
    groups = _assign_sides(norm, notes=notes)
    n_v = max(len(groups["left"]), len(groups["right"]))
    n_h = max(len(groups["top"]), len(groups["bottom"]))

    def _name_w(side: str) -> float:
        return max((len(p["name"]) for p in groups[side]), default=0) * _CHAR_W

    hw_min = max((n_h - 1) * _GRID / 2.0 + _GRID if n_h else 0.0,
                 (_name_w("left") + _name_w("right")) / 2.0 + _GRID,
                 2 * _GRID)
    hh_min = max((n_v - 1) * _GRID / 2.0 + _GRID if n_v else 0.0,
                 (_name_w("top") + _name_w("bottom")) / 2.0 + _GRID,
                 2 * _GRID)
    hw, hh = _snap_up(hw_min), _snap_up(hh_min)
    if body:
        try:
            bw, bh = float(body[0]), float(body[1])
        except (TypeError, ValueError, IndexError):
            raise _refuse(f"body {body!r} is not a [width, height] pair",
                          needs="body=[width_mm, height_mm]",
                          next_call="re-call with body=[20.32, 40.64] or omit it")
        if bw / 2.0 < hw or bh / 2.0 < hh:
            if notes is not None:
                notes.append(
                    f"body [{bw}, {bh}] is too small for {len(norm)} pins on a "
                    f"{_GRID} mm grid — enlarged to "
                    f"[{hw * 2:.2f}, {hh * 2:.2f}] so no two pins overlap.")
        else:
            hw, hh = bw / 2.0, bh / 2.0

    sym_pins: list[dict] = []
    for side in _SIDES:
        row = groups[side]
        n = len(row)
        for k, p in enumerate(row):
            off = (k - (n - 1) / 2.0) * _GRID
            if side == "left":
                pos, angle = [-hw - _PIN_LEN, -off], 180.0
            elif side == "right":
                pos, angle = [hw + _PIN_LEN, off], 0.0
            elif side == "bottom":
                pos, angle = [off, -hh - _PIN_LEN], 270.0
            else:
                pos, angle = [-off, hh + _PIN_LEN], 90.0
            pin = _pin(p["number"], p["name"], pos, angle, p["type"], length=_PIN_LEN)
            pin["shape"] = p["shape"]
            sym_pins.append(pin)

    short = libid.split(":", 1)[1]
    return _sym(
        libid, short, str(reference_prefix or "U").strip() or "U",
        str(value or short),
        description or f"{name} ({len(norm)} pins, generated from a caller-supplied "
                       "pinout)",
        keywords or " ".join(dict.fromkeys(
            [str(name).lower(), short.lower(), "generated"])),
        pins=sym_pins,
        graphics=[_rect(-hw, -hh, hw, hh)],
        footprint_filters=list(footprint_filters or []),
    )


# ─── footprints ───────────────────────────────────────────────────────────────
# Package family per accepted spelling. The family decides the pad TOPOLOGY; every
# dimension is still the caller's (or a nominal named in generate_footprint's
# docstring).
_FAMILY_SPELLINGS = {
    "chip": ("chip", "chip_2t", "2terminal", "two_terminal", "0402", "0603", "0805",
             "1206", "r_0402", "r_0603", "r_0805", "r_1206", "c_0402", "c_0603",
             "c_0805", "c_1206"),
    "soic": ("soic", "sop", "so", "ssop", "tssop", "msop", "soic_dual", "dual"),
    "qfn": ("qfn", "dfn", "lga", "mlf"),
    "qfp": ("qfp", "lqfp", "tqfp"),
    "sot": ("sot", "sot23", "sot-23", "sot_23", "sot223", "sot-223"),
    "dip": ("dip", "pdip", "dil"),
    "header": ("header", "pinheader", "pin_header", "tht_header", "connector",
               "tht", "socket"),
    "module": ("module", "module_castellated", "castellated", "castellated_module",
               "module_edge", "smd_module"),
}
_FAMILY_OF = {sp: fam for fam, sps in _FAMILY_SPELLINGS.items() for sp in sps}

#: Public list of package words ``generate_footprint`` accepts.
PACKAGES = tuple(sorted(_FAMILY_OF))

#: IPC-7351 chip nominals, verbatim from starter.py's Resistor_SMD:R_* footprints:
#: EIA size -> (pad centre |x| offset, pad width, pad height) in mm.
_CHIP_NOMINALS = {
    "0402": (0.5, 0.50, 0.50),
    "0603": (0.8, 0.90, 0.90),
    "0805": (1.0, 1.15, 1.45),
    "1206": (1.6, 1.60, 1.80),
}

#: Per-family nominals taken from starter.py's documented IPC-7351 table / its own
#: footprints. ``pad_len`` is PERPENDICULAR to the body edge, ``pad_width`` ALONG it.
_FAMILY_NOMINALS = {
    #                pitch  row_span  pad_len  pad_width  drill
    "soic":   {"pitch": 1.27, "row_span": 5.40, "pad_len": 1.55, "pad_width": 0.60},
    "sot":    {"pitch": 0.95, "row_span": 2.60, "pad_len": 0.85, "pad_width": 0.55},
    "dip":    {"pitch": 2.54, "row_span": 7.62, "pad_len": 1.60, "pad_width": 1.60,
               "drill": 0.80},
    "header": {"pitch": 2.54, "row_span": 2.54, "pad_len": 1.70, "pad_width": 1.70,
               "drill": 1.00},
}

#: QFN pad toe protrusion past the body edge (mm) — starter's QFN-48 7x7 nominal
#: (pads at |3.45| for a 7 mm body and 0.85 mm pad: 3.5 - 0.425 + 0.375).
_QFN_TOE = 0.375
#: Courtyard clearance added around the silk outline (mm).
_CRTYD_CLEAR = 0.25
_ROUNDRECT = 0.25


def _family(package: str) -> str:
    word = str(package or "").strip().lower().replace(" ", "_")
    if not word:
        raise _refuse("package is empty",
                      needs="a package family, one of " + ", ".join(PACKAGES),
                      next_call="re-call with package='module_castellated' (an "
                                "ESP32-WROOM-class edge module), 'soic', 'qfn', "
                                "'qfp', 'sot', '0603', 'dip' or 'header'")
    if word in _FAMILY_OF:
        return _FAMILY_OF[word]
    # 'soic-8' / 'qfn48' / 'sot-23-5': the family is the leading word.
    for sp in sorted(_FAMILY_OF, key=len, reverse=True):
        if word.startswith(sp):
            return _FAMILY_OF[sp]
    raise _refuse(f"unknown package {package!r}",
                  needs="one of " + ", ".join(PACKAGES),
                  next_call="re-call with a supported package family")


def _chip_size(package: str) -> str | None:
    word = str(package or "").lower()
    for size in _CHIP_NOMINALS:
        if size in word:
            return size
    return None


def _need(value: float, what: str, family: str, why: str) -> float:
    if not value or float(value) <= 0:
        raise _refuse(
            f"{family} footprint needs {what} and none was supplied",
            needs=f"{what} in mm — {why}",
            next_call=f"re-call with {what}=<mm> (it is refused rather than guessed: "
                      "a made-up pad ring lands the part off its pads)")
    return float(value)


def _footprint_pin_sides(pins, count: int,
                         family: str) -> tuple[int, dict[str, list[str]], list[str]]:
    """(pad count, {side: [numbers in draw order]}, [all numbers in order]).

    ``pins`` may be an int (count only) or the same pin-object list
    ``generate_symbol`` takes. Only ``number`` and ``side`` are read here. The side
    grouping is populated ONLY when every pin declares a side — a partial grouping
    would place the rest by guesswork.
    """
    if isinstance(pins, bool):
        raise _refuse(f"{family} footprint: pins must be a count or a pin list",
                      needs="pins=<count> or the pin list",
                      next_call=_EXAMPLE_CALL)
    if isinstance(pins, int):
        return pins, {}, [str(i) for i in range(1, pins + 1)]
    if isinstance(pins, (list, tuple)) and pins:
        items: list[tuple[str, str | None]] = []
        for i, p in enumerate(pins):
            if isinstance(p, dict):
                num = _first(p, _NUMBER_KEYS)
                if num is None:
                    raise _refuse(f"pins[{i}]: pin number is missing",
                                  needs="a 'number' per pin",
                                  next_call=_EXAMPLE_CALL)
                items.append((str(num).strip(),
                              _normalize_side(_first(p, _SIDE_KEYS),
                                              where=f"pins[{i}]")))
            else:
                items.append((str(p).strip(), None))
        items.sort(key=lambda t: _num_key(t[0]))
        groups: dict[str, list[str]] = {}
        if all(side for _, side in items):
            for s in _SIDES:
                groups[s] = [n for n, side in items if side == s]
            # Ascending order per side; direction lives in the position formulas
            # (see starter's MCU:Generic_QFP48 / Package_DIP:DIP-8 loops).
        return len(items), groups, [n for n, _ in items]
    if count > 0:
        return int(count), {}, [str(i) for i in range(1, int(count) + 1)]
    raise _refuse(f"{family} footprint needs a pin count",
                  needs="pins=<count> or the same pin list the symbol got",
                  next_call=_EXAMPLE_CALL)


def generate_footprint(name: str, *, package: str, pins, fpid: str = "",
                       pitch: float = 0.0, body_w: float = 0.0, body_h: float = 0.0,
                       pad_len: float = 0.0, pad_width: float = 0.0,
                       pad_offset: float = 0.0, row_span: float = 0.0,
                       drill: float = 0.0, exposed_pad: float = 0.0,
                       numbering: str = "", count: int = 0,
                       description: str = "", notes: list | None = None) -> dict:
    """Build a footprint for a part the library does not have.

    Supported ``package`` families (spellings in ``PACKAGES``):

    * ``chip`` / ``0402`` / ``0603`` / ``0805`` / ``1206`` — 2-terminal chip.
    * ``soic`` / ``sop`` / ``ssop`` / ``tssop`` — dual gull-wing row.
    * ``qfn`` / ``dfn`` / ``lga`` — 4-sided leadless ring, optional exposed pad.
    * ``qfp`` / ``lqfp`` — 4-sided gull-wing ring.
    * ``sot`` — 3/5/6-lead small outline transistor.
    * ``dip`` — 2-row through-hole.
    * ``header`` — 1- or 2-row through-hole header (``numbering='odd_even'|'row'``).
    * ``module_castellated`` — edge-castellated MODULE (what an ESP32-WROOM-32 needs):
      pads flush with the body outline on the sides its pins declare.

    Every dimension is the CALLER's. ``pad_len`` is the pad dimension PERPENDICULAR to
    the body edge, ``pad_width`` the dimension ALONG it; ``pitch`` is pad-to-pad
    centre spacing; ``row_span`` is centre-to-centre spacing of two opposing rows;
    ``pad_offset`` overrides the derived perpendicular distance from origin to a pad
    centre.

    ASSUMED NOMINALS — used only when the caller omits the number, each taken from the
    IPC-7351 table starter.py's module docstring already documents (or from starter's
    own footprint for that package), never invented here:

    * chip 0402/0603/0805/1206 pad offset+size: (0.5, 0.50x0.50), (0.8, 0.90x0.90),
      (1.0, 1.15x1.45), (1.6, 1.60x1.80).
    * ``soic``: pitch 1.27, row_span 5.40, pad 1.55 x 0.60  (starter's SOIC-8).
    * ``sot``: pitch 0.95, row_span 2.60, pad 0.85 x 0.55  (starter's SOT-23).
    * ``dip``: pitch 2.54, row_span 7.62, pad 1.60 x 1.60, drill 0.80 (starter DIP-8).
    * ``header``: pitch 2.54, row_span 2.54, pad 1.70, drill 1.00 (starter Header_1x4).
    * ``qfn`` pad ring, when ``body_w``/``body_h`` are given: pad centre at
      ``body/2 - pad_len/2 + 0.375``, the 0.375 mm toe being starter's QFN-48 7x7
      nominal.
    * ``module_castellated``: pad centre at ``body/2 - pad_len/2`` — castellations are
      flush with the board edge.

    ``qfp`` has NO derivable ring (lead span is not the body), and ``qfn``/``module``
    without a body have none either: the missing ``pad_offset`` is REFUSED by name.
    """
    if not str(name or "").strip():
        raise _refuse("name is empty", needs="the part name",
                      next_call=_EXAMPLE_CALL)
    fam = _family(package)
    n, groups, all_nums = _footprint_pin_sides(pins, count, fam)
    if n <= 0:
        raise _refuse(f"{fam} footprint needs at least one pad",
                      needs="pins=<count> or the pin list",
                      next_call=_EXAMPLE_CALL)
    nom = _FAMILY_NOMINALS.get(fam, {})
    row_span_given = bool(float(row_span or 0.0))
    pitch = float(pitch) or nom.get("pitch", 0.0)
    row_span = float(row_span) or nom.get("row_span", 0.0)
    pad_len = float(pad_len) or nom.get("pad_len", 0.0)
    pad_width = float(pad_width) or nom.get("pad_width", 0.0)
    drill = float(drill) or nom.get("drill", 0.0)
    body_w, body_h = float(body_w or 0.0), float(body_h or 0.0)
    pad_offset = float(pad_offset or 0.0)

    fpid = str(fpid or "").strip()
    if not fpid:
        raise _refuse("fpid is empty",
                      needs="a lib-qualified footprint id, '<Library>:<Footprint>'",
                      next_call=f"re-call with fpid='Generated:{name}'")
    if ":" not in fpid:
        raise _refuse(f"fpid {fpid!r} is not lib-qualified",
                      needs="'<Library>:<Footprint>'",
                      next_call=f"re-call with fpid='Generated:{fpid}'")

    pads: list[dict] = []
    attr = "smd"
    silk: tuple[float, float, float, float]
    pin1: tuple[float, float] | None = None

    def _seq(start: int, length: int) -> list[str]:
        return [str(i) for i in range(start, start + length)]

    if fam == "chip":
        if n != 2:
            raise _refuse(f"a 2-terminal chip footprint has 2 pads, got {n}",
                          needs="pins=2 (or a different package family)",
                          next_call="re-call with package='soic'/'qfn'/... for a "
                                    "multi-pad part")
        size = _chip_size(package)
        if size:
            off, pw, ph = _CHIP_NOMINALS[size]
            off = pad_offset or off
            pw = float(pad_width) or pw
            ph = float(pad_len) or ph
        else:
            off = _need(pad_offset or (pitch / 2.0 if pitch else 0.0), "pad_offset",
                        "chip", "half the pad-to-pad centre spacing")
            pw = _need(pad_width, "pad_width", "chip", "the pad size across the body")
            ph = _need(pad_len, "pad_len", "chip", "the pad size along the body")
        a, b = all_nums[0], all_nums[1]
        pads.append(_smd_pad("p1", a, -off, 0.0, pw, ph, _ROUNDRECT))
        pads.append(_smd_pad("p2", b, off, 0.0, pw, ph, _ROUNDRECT))
        hbw = body_w / 2.0 if body_w else off
        hbh = body_h / 2.0 if body_h else ph / 2.0
        silk = (-hbw, -hbh, hbw, hbh)

    elif fam in ("soic", "dip", "header", "sot"):
        # One row only for a 1xN header; every other family here is two opposing rows.
        two_row = _wants_two_rows(numbering, n) if fam == "header" else True
        if fam == "sot":
            if n not in (3, 5, 6):
                raise _refuse(f"a SOT footprint has 3, 5 or 6 leads, got {n}",
                              needs="pins=3|5|6",
                              next_call="re-call with package='soic' for a wider part")
            if n in (5, 6) and not row_span_given:
                row_span = 2.40   # starter's SOT-23-5 / SOT-23-6 row span
            # JEDEC SOT-23 family: BOTH rows sit on the same 3-slot grid at `pitch`,
            # and a short row takes the OUTER slots (a 3-lead part is 2 bottom at
            # +/-pitch and 1 top at CENTRE). Reproduces starter's SOT-23 / -23-5 /
            # -23-6 pad coordinates exactly; a naive "centre the row" layout does not.
            slot_of = {3: (0, 1, 2), 2: (0, 2), 1: (1,)}
            bottom_n = 3 if n >= 5 else 2
            top_n = n - bottom_n
            bnums = groups.get("bottom") or _seq(1, bottom_n)
            tnums = groups.get("top") or _seq(bottom_n + 1, top_n)
            for num, slot in zip(bnums, slot_of[len(bnums)]):
                pads.append(_smd_pad(f"p{num}", num, (slot - 1) * pitch,
                                     -row_span / 2.0, pad_width, pad_len, _ROUNDRECT))
            for num, slot in zip(tnums, reversed(slot_of[len(tnums)])):
                pads.append(_smd_pad(f"p{num}", num, (slot - 1) * pitch,
                                     row_span / 2.0, pad_width, pad_len, _ROUNDRECT))
            hbw = (body_w / 2.0 if body_w
                   else pitch + pad_width / 2.0 + 0.15)
            hbh = body_h / 2.0 if body_h else row_span / 2.0 - pad_len / 2.0
            silk = (-hbw, -max(hbh, 0.4), hbw, max(hbh, 0.4))
            pin1 = (-hbw - 0.4, row_span / 2.0)
        elif two_row:
            if n % 2:
                raise _refuse(
                    f"a 2-row {fam} footprint needs an even pad count, got {n}",
                    needs="an even count (package='header' is the single-row family)",
                    next_call="re-call with an even pins= count, or "
                              "package='header' for a 1xN row")
            half = n // 2
            # Two distinct 2-row numbering topologies, both real:
            #   'row'      (default, DIP/SOIC): 1..half down the LEFT column, then
            #              half+1..n up the RIGHT — starter's Package_DIP:DIP-8 order.
            #   'odd_even' (KiCad 2xNN connectors): odd numbers down the left, even
            #              down the right, so 1 and 2 are the SAME top row.
            # They disagree about which pad pin 3 is (partid.py carries an explicit
            # audited permutation per Conn_02xNN variant for exactly this reason), so the
            # caller picks — nothing is inferred.
            odd_even = str(numbering or "").lower() in ("odd_even", "oddeven", "kicad")
            right_top_down = odd_even
            if groups:
                lnums, rnums = groups.get("left") or [], groups.get("right") or []
                right_top_down = False
            elif odd_even:
                lnums = [str(i) for i in range(1, n + 1, 2)]
                rnums = [str(i) for i in range(2, n + 1, 2)]
            else:
                lnums = _seq(1, half)
                rnums = _seq(half + 1, half)
            attr = "tht" if fam in ("dip", "header") else "smd"
            for xs, nums, top_down in ((-row_span / 2.0, lnums, True),
                                       (row_span / 2.0, rnums, right_top_down)):
                m = len(nums)
                for k, num in enumerate(nums):
                    off = ((m - 1) / 2.0 - k) * pitch if top_down \
                        else (k - (m - 1) / 2.0) * pitch
                    if attr == "tht":
                        pads.append(_tht_pad(f"p{num}", num, xs, off,
                                             drill or 0.8, pad_len, pad_width))
                    else:
                        pads.append(_smd_pad(f"p{num}", num, xs, off,
                                             pad_len, pad_width, _ROUNDRECT))
            hbw = body_w / 2.0 if body_w else row_span / 2.0 - pad_len / 2.0 + 0.2
            hbh = body_h / 2.0 if body_h else (half - 1) * pitch / 2.0 + pitch
            silk = (-max(hbw, 0.5), -hbh, max(hbw, 0.5), hbh)
            pin1 = (-row_span / 2.0 - pad_len / 2.0 - 0.4,
                    (half - 1) * pitch / 2.0 + pad_width / 2.0 + 0.4)
        else:
            attr = "tht"
            nums = groups.get("left") or groups.get("right") or _seq(1, n)
            for k, num in enumerate(nums):
                y = ((n - 1) / 2.0 - k) * pitch
                pads.append(_tht_pad(f"p{num}", num, 0.0, y,
                                     drill or 1.0, pad_len, pad_width))
            hbw = body_w / 2.0 if body_w else pad_width / 2.0 + 0.15
            hbh = body_h / 2.0 if body_h else (n - 1) * pitch / 2.0 + pitch / 2.0
            silk = (-hbw, -hbh, hbw, hbh)
            pin1 = (-hbw - 0.4, (n - 1) * pitch / 2.0 + 0.4)

    else:  # qfn / qfp / module_castellated — 4-sided or edge-sided ring
        pitch = _need(pitch, "pitch", fam, "pad-to-pad centre spacing")
        pad_len = _need(pad_len, "pad_len", fam,
                        "the pad dimension perpendicular to the body edge")
        pad_width = _need(pad_width, "pad_width", fam,
                          "the pad dimension along the body edge")
        if groups:
            side_nums = {s: list(groups.get(s) or []) for s in _SIDES}
        else:
            if fam == "module":
                raise _refuse(
                    "a castellated module has no fixed side layout, and no pin "
                    "declared a side",
                    needs="side='left'|'right'|'top'|'bottom' on every pin (an "
                          "ESP32-WROOM-32 is 15 left / 8 bottom / 15 right)",
                    next_call=_EXAMPLE_CALL)
            if n % 4:
                raise _refuse(
                    f"a 4-sided {fam} needs a pad count divisible by 4, got {n}, and "
                    "no pin declared a side",
                    needs="either a multiple-of-4 count or side= on every pin",
                    next_call=_EXAMPLE_CALL)
            per = n // 4
            side_nums = {"left": _seq(1, per),
                         "bottom": _seq(per + 1, per),
                         "right": _seq(2 * per + 1, per),
                         "top": _seq(3 * per + 1, per)}
        if pad_offset:
            off_x = off_y = pad_offset
        elif body_w and body_h:
            toe = 0.0 if fam == "module" else _QFN_TOE
            off_x = body_w / 2.0 - pad_len / 2.0 + toe
            off_y = body_h / 2.0 - pad_len / 2.0 + toe
        else:
            raise _refuse(
                f"{fam} pad ring is not derivable without a body",
                needs="body_w and body_h (the package body in mm) or pad_offset (the "
                      "perpendicular distance from origin to a pad centre)"
                + ("" if fam != "qfp" else
                   " — for a QFP the ring follows the LEAD SPAN, not the body, so "
                   "pass pad_offset explicitly"),
                next_call="re-call with body_w=/body_h= or pad_offset=")
        for side in _SIDES:
            nums = side_nums.get(side) or []
            m = len(nums)
            for k, num in enumerate(nums):
                if side == "left":
                    pos = (-off_x, ((m - 1) / 2.0 - k) * pitch)
                    size = (pad_len, pad_width)
                elif side == "right":
                    pos = (off_x, (k - (m - 1) / 2.0) * pitch)
                    size = (pad_len, pad_width)
                elif side == "bottom":
                    pos = ((k - (m - 1) / 2.0) * pitch, -off_y)
                    size = (pad_width, pad_len)
                else:
                    pos = (((m - 1) / 2.0 - k) * pitch, off_y)
                    size = (pad_width, pad_len)
                pads.append(_smd_pad(f"p{num}", num, pos[0], pos[1],
                                     size[0], size[1], _ROUNDRECT))
        if exposed_pad and float(exposed_pad) > 0:
            ep = float(exposed_pad)
            ep_num = str(max((int(p["number"]) for p in pads
                              if str(p["number"]).isdigit()), default=n) + 1)
            pads.append(_smd_pad(f"p{ep_num}", ep_num, 0.0, 0.0, ep, ep, 0.05))
            if notes is not None:
                notes.append(f"exposed thermal pad added as pad {ep_num} "
                             f"({ep}x{ep} mm). Wire it explicitly — nothing here "
                             "assumes it is ground.")
        hbw = body_w / 2.0 if body_w else off_x + pad_len / 2.0
        hbh = body_h / 2.0 if body_h else off_y + pad_len / 2.0
        silk = (-hbw, -hbh, hbw, hbh)
        pin1 = (-hbw - 0.4, hbh - 0.4)

    x1, y1, x2, y2 = silk
    graphics = _silk_box(x1, y1, x2, y2) + _crtyd_box(
        x1 - _CRTYD_CLEAR, y1 - _CRTYD_CLEAR, x2 + _CRTYD_CLEAR, y2 + _CRTYD_CLEAR)
    if pin1:
        graphics.append(_circle(pin1[0], pin1[1], 0.15))
    return _fp(fpid, pads, graphics, attr=attr,
               description=description or f"{name} — generated {fam} footprint, "
                                         f"{len(pads)} pads")


def _wants_two_rows(numbering: str, n: int) -> bool:
    """Header row count: two rows only when the caller asks (``2row``/``odd_even``)."""
    word = str(numbering or "").lower()
    if word in ("odd_even", "oddeven", "kicad", "2row", "two_row", "2"):
        return n % 2 == 0
    return False


# ─── storage ─────────────────────────────────────────────────────────────────

def library_dir() -> str:
    """Native library dir generated parts are written to (resolution tier 2).

    ``CHUDWARE_ECAD_LIB_DIR`` overrides (tests point it at a tmpdir); otherwise the
    platform app-data dir, matching the rest of services/ (``%APPDATA%/Chudware`` on
    Windows, ``$XDG_DATA_HOME`` or ``~/.local/share`` elsewhere).
    """
    override = os.environ.get("CHUDWARE_ECAD_LIB_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Chudware", "ecad_library")


def _register(directory: str) -> None:
    lib = get_default_library()
    lib.add_native_dir(directory)
    lib.clear_cache()


def register_library_dir(directory: str = "") -> str:
    """Register the generated-part dir on the default resolver and return it.

    Idempotent, no I/O, safe to call before the directory exists (``add_native_dir``
    just records a path and ``_resolve_sym`` skips a missing one).

    MUST be called once per process at startup by anything that resolves ECAD parts.
    ``store_part`` registers the dir as a side effect of writing, which covers the
    session that CREATED a part — but nothing imports this module at startup, so
    without an explicit call a part generated yesterday sits on disk unresolvable, and
    ``ecad_make_part``'s own "it resolves from now on" would be a lie after a restart.
    ``register_ecad_sch_tools`` calls it (services/mcp/ecad_sch_tools.py), which covers
    every consumer inside the MCP server process — including the parts-catalog adapter,
    since they all share the ``native._default`` singleton.
    """
    d = directory or library_dir()
    _register(d)
    return d


def store_part(symbol: dict | None = None, footprint: dict | None = None, *,
               overwrite: bool = False, directory: str = "",
               notes: list | None = None) -> dict:
    """Write a generated symbol/footprint into the native library dir and register it.

    Refuses a libid/fpid the STARTER library already owns whatever ``overwrite`` says:
    starter is resolution tier 1, so a native file with that id could never resolve —
    the write would look like success and change nothing.

    Without ``overwrite`` an existing native file is a clear error and the stored part
    is left untouched.
    """
    from .starter import FOOTPRINTS, SYMBOLS

    directory = directory or library_dir()
    written: dict[str, str] = {}
    for kind, obj, key, ext, sub, starter in (
            ("symbol", symbol, "libid", ".sym.json", "symbols", SYMBOLS),
            ("footprint", footprint, "fpid", ".fp.json", "footprints", FOOTPRINTS)):
        if not obj:
            continue
        ident = str(obj.get(key) or "")
        if ident in starter:
            raise _refuse(
                f"{ident!r} is a STARTER library {kind} — it cannot be replaced by a "
                "generated one",
                needs="a different id; the starter library resolves FIRST, so a "
                      "generated part with this id would never be found",
                next_call=f"re-call with {key}='Generated:{ident.split(':')[-1]}'")
        path = os.path.join(directory, sub, _libid_to_filename(ident, ext))
        if os.path.exists(path) and not overwrite:
            raise _refuse(
                f"{kind} {ident!r} already exists in the generated library",
                needs="overwrite=True to replace it, or a different id",
                next_call=f"ecad_make_part(..., {key}={ident!r}, overwrite=True)  "
                          "# replace the stored part")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
        written[kind] = path
    _register(directory)
    if notes is not None and written:
        notes.append(f"stored under {directory} (native library tier 2) and registered "
                     "on the default resolver — resolvable on the next call.")
    return {"directory": directory, **written}


def make_part(name: str, pins, *, libid: str = "", package: str = "",
              fpid: str = "", overwrite: bool = False, directory: str = "",
              reference_prefix: str = "U", value: str = "", description: str = "",
              keywords: str = "", **geometry) -> dict:
    """Generate + store a symbol (and a footprint when ``package`` is given).

    Returns ``{libid, fpid, pin_count, pad_count, paths, notes, id_collision}``. ``libid``
    defaults to ``Generated:<name>`` and ``fpid`` to ``Generated:<name>_<package>`` — a
    NAMING default, the only kind of default this module makes, and one that deliberately
    keeps the two ids DISTINCT (see ``id_collision`` below).

    ``id_collision`` is True when the caller passed ``fpid == libid``. That is legal here —
    the two artefacts live in separate namespaces (``symbols/`` vs ``footprints/`` in the
    native dir, mirroring KiCad's separate symbol libs and ``.pretty`` dirs) and both
    resolve correctly — but it produces two DIFFERENT records under one string, and a
    downstream consumer keyed on a flat id keeps only one of them. Measured by
    EcadRefPinDialect 2026-07-29: the parts index kept the FOOTPRINT record, whose
    geometry_ref carries no libid, so ``part_place`` reported "geometry_ref missing 'libid'"
    for a part that had just been created successfully. Their adapter now dedupes, but the
    ambiguity is real for any future flat-id consumer, so it is reported loudly rather than
    silently renamed (an auto-suffix would be exactly the silent substitution this module
    exists to avoid) and rather than refused (the same-name pattern is KiCad-idiomatic and
    resolves fine at our own tier).
    """
    notes: list[str] = []
    nm = str(name or "").strip()
    libid = str(libid or "").strip() or (nm if ":" in nm else f"Generated:{nm}")
    symbol = generate_symbol(nm, pins, libid=libid,
                             reference_prefix=reference_prefix, value=value,
                             description=description, keywords=keywords, notes=notes)
    footprint = None
    if str(package or "").strip():
        fp_id = str(fpid or "").strip() or (
            f"Generated:{libid.split(':')[-1]}_{_family(package)}")
        footprint = generate_footprint(nm, package=package, pins=pins, fpid=fp_id,
                                       notes=notes, **geometry)
        symbol["footprint_filters"] = [fp_id.split(":")[-1]]
    elif fpid:
        raise _refuse("fpid was given but package was not",
                      needs="package= so the pad topology is known",
                      next_call="re-call with package='module_castellated' (or another "
                                "of " + ", ".join(PACKAGES) + ")")
    paths = store_part(symbol, footprint, overwrite=overwrite, directory=directory,
                       notes=notes)
    fpid_out = (footprint or {}).get("fpid", "")
    collision = bool(fpid_out) and fpid_out == libid
    return {
        "libid": libid,
        "fpid": fpid_out,
        "pin_count": len(symbol["pins"]),
        "pad_count": len((footprint or {}).get("pads", [])),
        "paths": paths,
        "notes": notes,
        "id_collision": collision,
    }
