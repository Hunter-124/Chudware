"""KiCad .kicad_sym library importer.

Maps ``(kicad_symbol_lib (symbol "Name" ...))`` trees to SymbolDef dicts as
specified in docs/contracts/ecad/sch_schema.md §3.

Coordinate convention: KiCad uses +y-down internally; we convert all y coords to
+y-up by negating. Angles are already CCW-positive in both conventions so they
pass through unchanged.

Public API:
    load_kicad_sym(source) -> {libid: SymbolDef}
        source may be a filesystem path string or raw text.
"""

from __future__ import annotations

import os
from typing import Any

from .sexpr import parse

# Map KiCad pin-type strings → our PIN_TYPES enum values
_PIN_TYPE_MAP: dict[str, str] = {
    "input": "input",
    "output": "output",
    "bidirectional": "bidirectional",
    "tri_state": "tri_state",
    "passive": "passive",
    "power_in": "power_in",
    "power_input": "power_in",
    "power_out": "power_out",
    "power_output": "power_out",
    "open_collector": "open_collector",
    "open_emitter": "open_emitter",
    "unspecified": "unspecified",
    "no_connect": "no_connect",
    "free": "free",
}

_PIN_SHAPE_MAP: dict[str, str] = {
    "line": "line",
    "inverted": "inverted",
    "clock": "clock",
    "inverted_clock": "inverted_clock",
    "input_low": "input_low",
    "clock_low": "clock_low",
    "output_low": "output_low",
    "edge_clock_high": "edge_clock_high",
    "non_logic": "non_logic",
    # KiCad 6+ names
    "": "line",
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _tag(node: list) -> str:
    """Return the tag (first element) of an s-expr list node."""
    return node[0] if isinstance(node, list) and node else ""


def _children(node: list) -> list:
    return node[1:] if isinstance(node, list) else []


def _find(node: list, tag: str) -> list | None:
    """First direct child with the given tag, or None."""
    for child in _children(node):
        if isinstance(child, list) and _tag(child) == tag:
            return child
    return None


def _findall(node: list, tag: str) -> list[list]:
    return [c for c in _children(node) if isinstance(c, list) and _tag(c) == tag]


def _attr(node: list, tag: str, default: Any = None) -> Any:
    """Return the first atom value inside a child node with the given tag."""
    child = _find(node, tag)
    if child and len(child) > 1:
        return child[1]
    return default


def _flip_y(y: float) -> float:
    """KiCad uses +y-down; negate to get +y-up."""
    return -float(y)


# ─── unit suffix parser ───────────────────────────────────────────────────────

def _unit_from_name(name: str) -> int:
    """Extract unit number from e.g. 'R_1_1' → 1 (first underscore part)."""
    parts = name.rsplit("_", 2)
    if len(parts) == 3:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return 1


# ─── pin parser ───────────────────────────────────────────────────────────────

def _parse_pin(node: list, unit: int) -> dict:
    """Parse a KiCad (pin ...) node → Pin dict."""
    # (pin <type> <shape> (at x y angle) (length l) (name "~") (number "1"))
    children = _children(node)
    # type and shape are positional atoms after 'pin'
    pin_type_raw = children[0] if children else "passive"
    pin_shape_raw = children[1] if len(children) > 1 else "line"

    pin_type = _PIN_TYPE_MAP.get(str(pin_type_raw).lower(), "passive")
    pin_shape = _PIN_SHAPE_MAP.get(str(pin_shape_raw).lower(), "line")

    at_node = _find(node, "at")
    x, y, angle = 0.0, 0.0, 0.0
    if at_node and len(at_node) >= 3:
        x = float(at_node[1])
        y = _flip_y(float(at_node[2]))
        angle = float(at_node[3]) if len(at_node) > 3 else 0.0

    length_node = _find(node, "length")
    length = float(length_node[1]) if length_node and len(length_node) > 1 else 2.54

    name_node = _find(node, "name")
    pin_name = str(name_node[1]) if name_node and len(name_node) > 1 else "~"
    if pin_name == "~":
        pin_name = ""

    number_node = _find(node, "number")
    pin_number = str(number_node[1]) if number_node and len(number_node) > 1 else "1"

    return {
        "number": pin_number,
        "name": pin_name,
        "pos": [x, y],
        "angle": angle,
        "length": length,
        "type": pin_type,
        "shape": pin_shape,
        "unit": unit,
    }


# ─── graphics parser ─────────────────────────────────────────────────────────

def _parse_graphic(node: list) -> dict | None:
    """Convert a KiCad body-graphic node to a Graphic dict (best-effort)."""
    tag = _tag(node)
    if tag == "rectangle":
        start = _find(node, "start")
        end = _find(node, "end")
        if start and end and len(start) >= 3 and len(end) >= 3:
            return {
                "type": "rect",
                "p1": [float(start[1]), _flip_y(float(start[2]))],
                "p2": [float(end[1]), _flip_y(float(end[2]))],
            }
    elif tag == "polyline":
        pts_node = _find(node, "pts")
        if pts_node:
            pts = []
            for xy in _findall(pts_node, "xy"):
                if len(xy) >= 3:
                    pts.append([float(xy[1]), _flip_y(float(xy[2]))])
            if pts:
                return {"type": "polyline", "pts": pts}
    elif tag == "circle":
        center = _find(node, "center")
        radius_node = _find(node, "radius")
        if center and radius_node and len(center) >= 3 and len(radius_node) >= 2:
            return {
                "type": "circle",
                "center": [float(center[1]), _flip_y(float(center[2]))],
                "radius": float(radius_node[1]),
            }
    elif tag == "arc":
        start = _find(node, "start")
        mid = _find(node, "mid")
        end = _find(node, "end")
        if start and end and len(start) >= 3 and len(end) >= 3:
            g: dict = {
                "type": "arc",
                "p1": [float(start[1]), _flip_y(float(start[2]))],
                "p2": [float(end[1]), _flip_y(float(end[2]))],
            }
            if mid and len(mid) >= 3:
                g["mid"] = [float(mid[1]), _flip_y(float(mid[2]))]
            return g
    elif tag == "text":
        at = _find(node, "at")
        text_val = str(node[1]) if len(node) > 1 else ""
        if at and len(at) >= 3:
            return {
                "type": "text",
                "text": text_val,
                "pos": [float(at[1]), _flip_y(float(at[2]))],
            }
    return None


# ─── symbol parser ────────────────────────────────────────────────────────────

_GRAPHIC_TAGS = {"rectangle", "polyline", "circle", "arc", "text"}
_BODY_GRAPHIC_TAGS = _GRAPHIC_TAGS


def _parse_symbol(sym_node: list, lib_name: str = "Device") -> dict | None:
    """Parse a top-level (symbol "Name" ...) node → {libid: SymbolDef} entries.

    KiCad 6 uses sub-symbols like "R_1_1" inside "R"; we parse the parent and
    collect pins from sub-symbols.
    """
    if _tag(sym_node) != "symbol":
        return None
    if len(sym_node) < 2:
        return None
    raw_name = str(sym_node[1])

    # skip sub-symbols (they contain _<unit>_<demorgan> suffix)
    # but we DO need to iterate through them to pick up pins & graphics
    # We only create a SymbolDef for the top-level (no underscored suffix pattern)
    # Actually in KiCad 6, the top-level symbol IS the parent; sub-symbols are inside it.
    # In the file: (symbol "R" (symbol "R_0_1" ...) (symbol "R_1_1" ...))
    # The parent "R" has properties; the sub-symbols have pins/graphics.

    # Collect properties
    props: dict[str, str] = {}
    for prop in _findall(sym_node, "property"):
        if len(prop) >= 3:
            props[str(prop[1])] = str(prop[2])

    reference_prefix = props.get("Reference", raw_name[0] if raw_name else "U")
    value = props.get("Value", raw_name)
    footprint = props.get("Footprint", "")
    description = props.get("Description", "")
    keywords = props.get("Keywords", "")

    # Collect fields (arbitrary properties)
    reserved = {"Reference", "Value", "Footprint", "Datasheet", "Description", "Keywords", "ki_keywords"}
    fields: dict[str, str] = {k: v for k, v in props.items() if k not in reserved}

    # Collect pins and graphics from sub-symbols and direct body
    pins: list[dict] = []
    graphics: list[dict] = []
    units_seen: set[int] = set()

    def _harvest(node: list, unit: int) -> None:
        for child in _children(node):
            if not isinstance(child, list):
                continue
            ct = _tag(child)
            if ct == "pin":
                p = _parse_pin(child, unit)
                pins.append(p)
                units_seen.add(unit)
            elif ct in _BODY_GRAPHIC_TAGS:
                g = _parse_graphic(child)
                if g is not None:
                    graphics.append(g)
            elif ct == "symbol":
                # nested sub-symbol
                sub_name = str(child[1]) if len(child) > 1 else ""
                sub_unit = _unit_from_name(sub_name)
                _harvest(child, sub_unit)

    _harvest(sym_node, 1)

    units_count = max(units_seen, default=1)

    libid = f"{lib_name}:{raw_name}"

    return {
        "libid": libid,
        "name": raw_name,
        "description": description,
        "keywords": keywords,
        "reference_prefix": reference_prefix,
        "value": value,
        "footprint_filters": [footprint] if footprint else [],
        "pins": pins,
        "graphics": graphics,
        "units_count": units_count,
        "power": reference_prefix == "#PWR",
        "fields": fields,
    }


# ─── public API ───────────────────────────────────────────────────────────────

def load_kicad_sym(source: str) -> dict[str, dict]:
    """Load a KiCad symbol library file (.kicad_sym) and return {libid: SymbolDef}.

    *source* is either a file-system path (must end in .kicad_sym or .sym) or
    raw S-expression text.
    """
    if source.strip().startswith("("):
        text = source
        lib_name = "Device"
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        lib_name = os.path.splitext(os.path.basename(source))[0]

    tree = parse(text)
    if _tag(tree) != "kicad_symbol_lib":
        raise ValueError(f"Expected kicad_symbol_lib root, got {_tag(tree)!r}")

    result: dict[str, dict] = {}
    for child in _children(tree):
        if isinstance(child, list) and _tag(child) == "symbol":
            sym_name = str(child[1]) if len(child) > 1 else ""
            # Skip sub-symbol entries at top level (they contain '_' unit suffix pattern)
            # Top-level names have no '_<digit>_<digit>' suffix
            import re
            if re.search(r"_\d+_\d+$", sym_name):
                continue
            sdef = _parse_symbol(child, lib_name)
            if sdef:
                result[sdef["libid"]] = sdef

    return result
