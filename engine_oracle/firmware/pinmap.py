"""ECAD netlist → firmware pin-map header (T-C2).

:func:`generate_pinmap` reads the net names from an ECAD **board** (or schematic) doc and emits a C
header of ``#define PIN_<NETNAME> <n>`` lines — one per GPIO / signal net (power & ground rails are
skipped). Positional net names (STM32 ``PA5``, ESP32 ``GPIO4``) decode to a stable pin id via
:func:`engine.firmware.mcu_pinouts.resolve_pin`; a named signal net (``MISO``, ``GPIO_LED``) with no
positional encoding gets a deterministic logical index (offset by ``LOGICAL_PIN_BASE`` so it never
collides with a real pin id). This links the schematic's net names to firmware pin constants with
no manual bookkeeping — the Day-2 killer feature. See ``contracts/firmware_rpc.md``.

:func:`write_pinmap` writes the header to ``<project.build_dir>/include/pinmap.h``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from engine.firmware import mcu_pinouts

# Power / ground rails — never emitted as GPIO pins.
_POWER_NAMES = {
    "GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND", "EARTH", "CHASSIS",
    "VSS", "VSSA", "VDD", "VDDA", "VCC", "VCCA", "VEE", "VBAT", "VREF", "VREFP",
    "VREFN", "VDDIO", "AVDD", "AVCC", "DVDD", "VBUS", "VPP",
}
_VOLT_RE = re.compile(r"^[+-]?\d+(?:V\d*|\.\d+V?|V)$", re.IGNORECASE)  # +3V3, 3V3, +5V, -12V, 3.3V, 1V8

_NON_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]")


def is_power_net(name: Optional[str]) -> bool:
    """True if ``name`` is a power/ground rail (excluded from the pin-map). Empty names count as
    power (nothing to emit)."""
    if not name:
        return True
    s = str(name).strip()
    if s.upper() in _POWER_NAMES:
        return True
    return bool(_VOLT_RE.match(s))


def _macro_token(name: str) -> str:
    """Sanitise a net name into a valid C macro suffix: upper-case, non-alnum → ``_``, and a
    leading digit gets a ``_`` prefix so the identifier is legal."""
    tok = _NON_TOKEN_RE.sub("_", str(name).strip().upper())
    if not tok:
        return "NET"
    if tok[0].isdigit():
        tok = "_" + tok
    return tok


def net_names(doc: dict) -> list[str]:
    """Net names of an ECAD doc: a **board** has an explicit ``nets`` list; a **schematic**'s nets
    are computed by :func:`engine.ecad.netlist.build_netlist`. Tolerates a raw dict carrying either
    a ``nets`` list (``[{"name": ...}]``) or a ``nets`` mapping (``{name: members}``)."""
    try:
        from engine.ecad import schema
        if schema.is_board(doc):
            return [n.get("name", "") for n in doc.get("nets", [])
                    if isinstance(n, dict) and n.get("name")]
        if schema.is_schematic(doc):
            from engine.ecad import netlist as netlist_mod
            nl = netlist_mod.build_netlist(doc)
            return list(nl.get("nets", {}).keys())
    except Exception:
        pass
    nets = doc.get("nets")
    if isinstance(nets, dict):
        return [k for k in nets.keys() if k]
    if isinstance(nets, list):
        return [n.get("name", "") for n in nets if isinstance(n, dict) and n.get("name")]
    return []


def pin_model(board_doc: dict, *, mcu_family: Optional[str] = None) -> list[dict]:
    """Enriched pin model for the co-verification loop (T-FIN6).

    One record per emitted ``PIN_<token>`` (same net selection, ordering, and dedup rules as
    :func:`generate_pinmap`, so the model and the header can never disagree):

    ``{"net", "token", "pin_id", "positional", "strap", "input_only", "direction", "mux"}``

    ``direction`` / ``mux`` are honest ``None`` here — the board doc alone does not know them;
    :mod:`engine.firmware.coverify` fills direction expectations from the linked schematic's
    netlist pin types when one is supplied. ``strap`` / ``input_only`` come from the per-family
    tables in :mod:`engine.firmware.mcu_pinouts`.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for n in net_names(board_doc):
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    ordered.sort()

    out: list[dict] = []
    seq = 0
    used_tokens: set[str] = set()
    for name in ordered:
        if is_power_net(name):
            continue
        token = _macro_token(name)
        if token in used_tokens:
            continue
        used_tokens.add(token)
        pin = mcu_pinouts.resolve_pin(name, mcu_family)
        positional = pin is not None
        if pin is None:
            pin = mcu_pinouts.LOGICAL_PIN_BASE + seq
            seq += 1
        out.append({
            "net": name,
            "token": token,
            "pin_id": pin,
            "positional": positional,
            "strap": mcu_pinouts.is_strap_net(name, mcu_family),
            "input_only": mcu_pinouts.is_input_only_pin(name, mcu_family),
            "direction": None,
            "mux": None,
        })
    return out


def generate_pinmap(board_doc: dict, *, mcu_family: Optional[str] = None,
                    guard: str = "AICAD_PINMAP_H") -> str:
    """Produce a C header mapping ECAD net names to firmware pin ids.

    Nets are emitted in sorted order (deterministic, byte-stable). Power/ground rails are skipped.
    Positional names decode via :func:`mcu_pinouts.resolve_pin`; the rest get a logical index.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for n in net_names(board_doc):
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    ordered.sort()

    lines = [
        f"#ifndef {guard}",
        f"#define {guard}",
        "/* Auto-generated by AI-CAD firmware pin-map (T-C2). DO NOT EDIT. */",
        "/* Source: ECAD netlist net names -> firmware pin ids. Power/ground rails skipped. */",
        "",
    ]
    seq = 0
    emitted = 0
    used_tokens: set[str] = set()
    for name in ordered:
        if is_power_net(name):
            continue
        token = _macro_token(name)
        if token in used_tokens:          # two nets sanitising to the same macro — keep the first
            continue
        used_tokens.add(token)
        pin = mcu_pinouts.resolve_pin(name, mcu_family)
        if pin is None:
            pin = mcu_pinouts.LOGICAL_PIN_BASE + seq
            seq += 1
            comment = "  /* logical index; net name encodes no physical pin */"
        else:
            comment = ""
        lines.append(f"#define PIN_{token} {pin}{comment}")
        emitted += 1

    if emitted == 0:
        lines.append("/* (no GPIO/signal nets found in this board) */")
    lines += ["", f"#endif /* {guard} */", ""]
    return "\n".join(lines)


def write_pinmap(project, board_doc: dict, *, mcu_family: Optional[str] = None) -> Path:
    """Write ``generate_pinmap`` output to ``<project.build_dir>/include/pinmap.h`` and return the
    path. ``mcu_family`` defaults to the project's own family."""
    fam = mcu_family or getattr(project, "mcu_family", None)
    header = generate_pinmap(board_doc, mcu_family=fam)
    out_dir = project.build_path / "include"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "pinmap.h"
    out.write_text(header, encoding="utf-8")
    return out
