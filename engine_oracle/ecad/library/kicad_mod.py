"""KiCad .kicad_mod footprint importer.

Maps ``(footprint "Name" ...)`` trees to Footprint dicts as specified in
docs/contracts/ecad/pcb_schema.md §4.

Coordinate convention: KiCad .kicad_mod uses +y-down; we negate y to get +y-up.

Public API:
    load_kicad_mod(source) -> footprint-def dict
        source may be a filesystem path or raw text.

    load_pretty(directory) -> {fpid: footprint-def}
        Scans a .pretty directory for all .kicad_mod files.
"""

from __future__ import annotations

import os
from typing import Any

from .sexpr import parse

# KiCad pad-type → our PAD_TYPES
_PAD_TYPE_MAP = {
    "smd": "smd",
    "thru_hole": "tht",
    "np_thru_hole": "np_tht",
    "connect": "connector",
    "connector": "connector",
}

# KiCad pad-shape → our PAD_SHAPES
_PAD_SHAPE_MAP = {
    "rect": "rect",
    "roundrect": "roundrect",
    "circle": "circle",
    "oval": "oval",
    "trapezoid": "trapezoid",
    "custom": "custom",
    "square": "rect",
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _tag(node: list) -> str:
    return node[0] if isinstance(node, list) and node else ""


def _children(node: list) -> list:
    return node[1:] if isinstance(node, list) else []


def _find(node: list, tag: str) -> list | None:
    for child in _children(node):
        if isinstance(child, list) and _tag(child) == tag:
            return child
    return None


def _findall(node: list, tag: str) -> list[list]:
    return [c for c in _children(node) if isinstance(c, list) and _tag(c) == tag]


def _flip_y(y: float) -> float:
    return -float(y)


def _at_xy(node: list) -> tuple[float, float, float]:
    """Return (x, y_flipped, angle) from an (at x y [angle]) node."""
    at = _find(node, "at")
    if at and len(at) >= 3:
        x = float(at[1])
        y = _flip_y(float(at[2]))
        angle = float(at[3]) if len(at) > 3 else 0.0
        return x, y, angle
    return 0.0, 0.0, 0.0


# ─── pad parser ───────────────────────────────────────────────────────────────

_pad_counter = 0


def _parse_pad(node: list, index: int) -> dict:
    """Parse a KiCad (pad ...) node → Pad dict."""
    global _pad_counter
    children = _children(node)
    # (pad "1" smd rect (at x y [angle]) (size w h) (layers ...) (drill ...) ...)
    number = str(children[0]) if children else str(index)
    pad_type_raw = str(children[1]).lower() if len(children) > 1 else "smd"
    pad_shape_raw = str(children[2]).lower() if len(children) > 2 else "rect"

    pad_type = _PAD_TYPE_MAP.get(pad_type_raw, "smd")
    pad_shape = _PAD_SHAPE_MAP.get(pad_shape_raw, "rect")

    x, y, angle = _at_xy(node)

    size_node = _find(node, "size")
    w, h = 1.0, 1.0
    if size_node and len(size_node) >= 3:
        w = float(size_node[1])
        h = float(size_node[2])

    # layers
    layers_node = _find(node, "layers")
    layers: list[str] = []
    if layers_node:
        for item in _children(layers_node):
            if isinstance(item, str):
                layers.append(item)

    # roundrect ratio
    rr_node = _find(node, "roundrect_rratio")
    roundrect_ratio = float(rr_node[1]) if rr_node and len(rr_node) > 1 else 0.0

    # drill
    drill_node = _find(node, "drill")
    drill: dict | None = None
    if drill_node:
        # (drill [oval] diameter [width] (offset x y))
        drill_children = _children(drill_node)
        if drill_children:
            first = drill_children[0]
            if first == "oval":
                d_shape = "oval"
                diameter = float(drill_children[1]) if len(drill_children) > 1 else 0.6
            else:
                d_shape = "round"
                try:
                    diameter = float(first)
                except (ValueError, TypeError):
                    diameter = 0.6
            offset_node = _find(drill_node, "offset")
            offset = [0.0, 0.0]
            if offset_node and len(offset_node) >= 3:
                offset = [float(offset_node[1]), _flip_y(float(offset_node[2]))]
            drill = {"diameter": diameter, "shape": d_shape, "offset": offset}

    pad_id = f"p{index}"
    pad: dict = {
        "id": pad_id,
        "number": number,
        "pos": [x, y],
        "angle": angle,
        "shape": pad_shape,
        "size": [w, h],
        "roundrect_ratio": roundrect_ratio,
        "type": pad_type,
        "layers": layers,
        "drill": drill,
        "net": None,
        "pin": number,
    }
    return pad


# ─── graphics parser ─────────────────────────────────────────────────────────

_GRAPHIC_TAGS = {"fp_line", "fp_circle", "fp_arc", "fp_poly", "fp_text"}


def _parse_graphic(node: list) -> dict | None:
    tag = _tag(node)
    layer_node = _find(node, "layer")
    layer = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "F.SilkS"

    if tag == "fp_line":
        start = _find(node, "start")
        end = _find(node, "end")
        if start and end and len(start) >= 3 and len(end) >= 3:
            return {
                "type": "line",
                "p1": [float(start[1]), _flip_y(float(start[2]))],
                "p2": [float(end[1]), _flip_y(float(end[2]))],
                "layer": layer,
            }
    elif tag == "fp_circle":
        center = _find(node, "center")
        end = _find(node, "end")
        if center and end and len(center) >= 3 and len(end) >= 3:
            # radius = distance from center to end
            cx, cy = float(center[1]), float(center[2])
            ex, ey = float(end[1]), float(end[2])
            radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            return {
                "type": "circle",
                "center": [cx, _flip_y(cy)],
                "radius": radius,
                "layer": layer,
            }
    elif tag == "fp_arc":
        start = _find(node, "start")
        mid = _find(node, "mid")
        end = _find(node, "end")
        if start and end and len(start) >= 3 and len(end) >= 3:
            g: dict = {
                "type": "arc",
                "p1": [float(start[1]), _flip_y(float(start[2]))],
                "p2": [float(end[1]), _flip_y(float(end[2]))],
                "layer": layer,
            }
            if mid and len(mid) >= 3:
                g["mid"] = [float(mid[1]), _flip_y(float(mid[2]))]
            return g
    elif tag == "fp_text":
        text_val = str(node[2]) if len(node) > 2 else ""
        at = _find(node, "at")
        if at and len(at) >= 3:
            return {
                "type": "text",
                "text": text_val,
                "pos": [float(at[1]), _flip_y(float(at[2]))],
                "layer": layer,
            }
    elif tag == "fp_poly":
        pts_node = _find(node, "pts")
        if pts_node:
            pts = []
            for xy in _findall(pts_node, "xy"):
                if len(xy) >= 3:
                    pts.append([float(xy[1]), _flip_y(float(xy[2]))])
            if pts:
                return {"type": "polygon", "pts": pts, "layer": layer}
    return None


# ─── 3D model parser ─────────────────────────────────────────────────────────

def _parse_model(node: list) -> dict | None:
    if _tag(node) != "model":
        return None
    path = str(node[1]) if len(node) > 1 else ""
    offset_node = _find(node, "offset")
    scale_node = _find(node, "scale")
    rotate_node = _find(node, "rotate")

    def _xyz(n: list | None) -> list[float]:
        if n is None:
            return [0.0, 0.0, 0.0]
        xyz = _find(n, "xyz")
        if xyz and len(xyz) >= 4:
            return [float(xyz[1]), float(xyz[2]), float(xyz[3])]
        return [0.0, 0.0, 0.0]

    return {
        "path": path,
        "offset": _xyz(offset_node),
        "scale": _xyz(scale_node) if scale_node else [1.0, 1.0, 1.0],
        "rotate": _xyz(rotate_node),
    }


# ─── footprint parser ─────────────────────────────────────────────────────────

def _parse_footprint(tree: list, fpid_override: str | None = None) -> dict:
    """Parse a (footprint ...) root node → footprint dict."""
    if _tag(tree) != "footprint":
        raise ValueError(f"Expected footprint root, got {_tag(tree)!r}")

    fp_name = str(tree[1]) if len(tree) > 1 else "Unknown"
    fpid = fpid_override or fp_name

    # attr: smd | through_hole | virtual
    attr_node = _find(tree, "attr")
    attr = "smd"
    if attr_node:
        for val in _children(attr_node):
            v = str(val).lower()
            if v in ("smd", "virtual"):
                attr = v
                break
            if v in ("through_hole", "tht"):
                attr = "tht"
                break

    pads: list[dict] = []
    graphics: list[dict] = []
    model_3d: dict | None = None

    for i, child in enumerate(_children(tree)):
        if not isinstance(child, list):
            continue
        ct = _tag(child)
        if ct == "pad":
            pads.append(_parse_pad(child, len(pads) + 1))
        elif ct in _GRAPHIC_TAGS:
            g = _parse_graphic(child)
            if g is not None:
                graphics.append(g)
        elif ct == "model":
            m = _parse_model(child)
            if m is not None:
                model_3d = m

    return {
        "fpid": fpid,
        "pads": pads,
        "graphics": graphics,
        "model_3d": model_3d["path"] if model_3d else None,
        "model_3d_transform": model_3d,
        "attr": attr,
        "description": "",
    }


# ─── public API ───────────────────────────────────────────────────────────────

def load_kicad_mod(source: str) -> dict:
    """Load a single .kicad_mod file and return a footprint def dict.

    *source* is a filesystem path or raw S-expression text.
    """
    if source.strip().startswith("("):
        text = source
        fpid = None
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        basename = os.path.basename(source)
        fpid = os.path.splitext(basename)[0]

    tree = parse(text)
    return _parse_footprint(tree, fpid)


def load_pretty(directory: str) -> dict[str, dict]:
    """Scan a .pretty directory and return {fpid: footprint-def} for all .kicad_mod files."""
    result: dict[str, dict] = {}
    lib_name = os.path.splitext(os.path.basename(directory.rstrip("/\\")))[0]
    for fname in os.listdir(directory):
        if not fname.endswith(".kicad_mod"):
            continue
        fp_name = os.path.splitext(fname)[0]
        fpid = f"{lib_name}:{fp_name}"
        try:
            fp = load_kicad_mod(os.path.join(directory, fname))
            fp["fpid"] = fpid
            result[fpid] = fp
        except Exception:
            pass  # skip corrupt files
    return result
