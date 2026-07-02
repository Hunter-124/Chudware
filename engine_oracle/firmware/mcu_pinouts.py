"""MCU family lookup + net-name → pin-number decoding for the firmware pin-map (T-C2).

Two concerns, both driven off the ECAD side:

* **Auto-target the MCU** — :func:`select_mcu_family` scans an ECAD board/schematic doc's BOM and
  maps the first MCU part's identifier (its ``value`` / ``libid`` / a custom ``MPN`` field) to a
  firmware ``mcu_family`` string (``contracts/firmware_rpc.md`` §2). So ``fw_new_project`` can pick
  the toolchain target from the chip the schematic BOM already chose. :func:`resolve_board_doc`
  turns a ``board_doc_ref`` (an open ECAD ``doc_id`` **or** a file path) into that doc dict.

* **Decode positional net names** — :func:`resolve_pin` turns a net name that *encodes* a physical
  pin (STM32 ``PA5``, ESP32 ``GPIO4``, Arduino ``D13``) into a stable integer id for
  ``#define PIN_<NET> <n>``. Family-agnostic on purpose, so :func:`engine.firmware.pinmap` need not
  thread ``mcu_family`` through.

The ECAD data model is plain dicts (no ``Net``/``Component`` classes); ``engine.ecad`` is imported
lazily inside functions so importing this module never pulls the whole ECAD stack.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

# --------------------------------------------------------------------------- MCU family table
#
# Prefix (case-insensitive, substring — tolerant of libids like "MCU_ST_STM32F4:STM32F407VGTx")
# → firmware mcu_family. Order matters: first hit wins. Extend as new families are supported.
_FAMILY_PREFIXES: list[tuple[str, str]] = [
    ("stm32", "cortex-m4"),      # T-C2: STM32*  -> cortex-m4
    ("esp32", "xtensa-lx6"),     # T-C2: ESP32*  -> xtensa-lx6
    ("atmega", "avr"),           # T-C2: ATmega* -> avr
    ("attiny", "avr"),           # same toolchain family as ATmega
]

# BOM fields a caller might stash a real manufacturer part number under (the ECAD model has no
# dedicated MPN slot — ``fields`` is free-form, written via the ECAD ``set_field`` op).
_MPN_FIELD_KEYS = ("MPN", "mpn", "Mpn", "MFR_PN", "MfrPN", "manufacturer_part", "PartNumber")


def mpn_to_family(mpn: Optional[str]) -> Optional[str]:
    """Map a part identifier (MPN / value / libid) to a firmware ``mcu_family``, or ``None``.

    Matching is a case-insensitive substring test against the known prefixes so both a bare value
    (``"STM32F103"``) and a KiCad-style libid (``"MCU_ST_STM32F1:STM32F103C8Tx"``) resolve.
    """
    if not mpn:
        return None
    s = str(mpn).strip().lower()
    for prefix, family in _FAMILY_PREFIXES:
        if prefix in s:
            return family
    return None


def _component_part_strings(comp: dict) -> list[str]:
    """Candidate identifier strings for a BOM component, most-authoritative first: an explicit MPN
    field, then the part ``value``, then the symbol ``libid`` / footprint ``fpid``."""
    out: list[str] = []
    fields = comp.get("fields") or {}
    if isinstance(fields, dict):
        for k in _MPN_FIELD_KEYS:
            v = fields.get(k)
            if v:
                out.append(str(v))
    for key in ("value", "libid", "fpid"):
        v = comp.get(key)
        if v:
            out.append(str(v))
    return out


def component_family(comp: dict) -> Optional[str]:
    """Firmware family for a single BOM component dict, or ``None`` if it is not a known MCU."""
    for s in _component_part_strings(comp):
        fam = mpn_to_family(s)
        if fam:
            return fam
    return None


def iter_bom_components(doc: dict) -> list[dict]:
    """BOM components of an ECAD doc in stable order: board → ``footprints``, schematic → ``symbols``.
    Tolerates a raw dict carrying either key."""
    try:
        from engine.ecad import schema
        if schema.is_board(doc):
            return [c for c in doc.get("footprints", []) if isinstance(c, dict)]
        if schema.is_schematic(doc):
            return [c for c in doc.get("symbols", []) if isinstance(c, dict)]
    except Exception:
        pass
    items = doc.get("footprints") or doc.get("symbols") or []
    return [c for c in items if isinstance(c, dict)]


def select_mcu_family(doc: dict) -> Optional[str]:
    """Return the ``mcu_family`` of the FIRST MCU component in ``doc``'s BOM, or ``None`` if the
    board/schematic has no recognised MCU. Skips ``dnp`` (do-not-populate) parts."""
    if not isinstance(doc, dict):
        return None
    for comp in iter_bom_components(doc):
        if comp.get("dnp", False):
            continue
        fam = component_family(comp)
        if fam:
            return fam
    return None


def resolve_board_doc(ref: Optional[str]) -> Optional[dict]:
    """Resolve a ``board_doc_ref`` to a live doc dict, or ``None`` (never raises).

    A ref is either an **open ECAD session ``doc_id``** (checked first, the same registry
    ``ecad_update_from_sch`` uses) or a **file path** to a ``.ecad+`` / ``.json`` document.
    """
    if not ref:
        return None
    # 1) open session registry (doc_id)
    try:
        from engine.ecad import session as ecad_session
        try:
            return ecad_session.get_session(str(ref)).doc
        except Exception:
            pass
    except Exception:
        pass
    # 2) file path
    try:
        if os.path.exists(str(ref)):
            from engine.ecad import formats
            doc, _warnings = formats.open(str(ref))
            if isinstance(doc, dict):
                return doc
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- pin-name decoding

# A named signal net that does not encode a physical pin (MISO, GPIO_LED) gets a deterministic
# logical index offset by this base, so it never collides with a real decoded pin id.
LOGICAL_PIN_BASE = 1000

_STM32_RE = re.compile(r"^P([A-Z])(\d{1,2})$", re.IGNORECASE)   # PA5, PB12  -> port*16 + pin
_GPIO_RE = re.compile(r"^(?:GPIO|IO)(\d{1,3})$", re.IGNORECASE)  # GPIO4, IO21 -> N (ESP32)
_DPIN_RE = re.compile(r"^D(\d{1,3})$", re.IGNORECASE)            # D13         -> N (Arduino digital)


def resolve_pin(net_name: Optional[str], mcu_family: Optional[str] = None) -> Optional[int]:
    """Decode a *positional* net name to a stable integer pin id, or ``None`` when the name carries
    no physical-pin encoding (a named signal net like ``MISO`` — the caller then assigns a logical
    index). Family-agnostic: STM32 ``P<port><n>`` → ``(port-'A')*16 + n``, then ESP32/GPIO ``<n>``,
    then Arduino ``D<n>``. ``mcu_family`` is accepted for future family-specific maps but not
    required today."""
    if not net_name:
        return None
    s = str(net_name).strip()
    m = _STM32_RE.match(s)
    if m:
        port = m.group(1).upper()
        return (ord(port) - ord("A")) * 16 + int(m.group(2))
    m = _GPIO_RE.match(s)
    if m:
        return int(m.group(1))
    m = _DPIN_RE.match(s)
    if m:
        return int(m.group(1))
    return None
