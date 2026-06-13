"""Schema validation + geometry helpers for aicad.sch/1 and aicad.pcb/1 documents.

Executable form of docs/contracts/ecad/sch_schema.md + pcb_schema.md. Structural validation only
(envelopes, types, id prefixes, enum membership, cross-refs). Connectivity/ERC/DRC are separate
passes (engine.ecad.netlist / erc / drc), exactly as engine.doc keeps semantic checks in the
compiler rather than the schema validator.

Every ``validate_*`` returns a list of error strings (empty = valid), each prefixed with a
JSON-path-ish locator so failures are actionable — same convention as engine.doc.schema.

Coordinates are ``[x, y]`` in **mm**, +x right, +y up (math convention; the C++ canvas flips for
screen). Angles in **degrees CCW**. The transform helpers (:func:`pin_world`, :func:`pad_world`)
are the single source of truth for placing symbol pins / footprint pads in world coordinates — no
other module should hand-roll the rotate/mirror math.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any

SCHEMA_SCH = "aicad.sch/1"
SCHEMA_PCB = "aicad.pcb/1"

# ── enums (kept as module constants so ops.py / importers share one definition) ──

PIN_TYPES = {
    "input", "output", "bidirectional", "tri_state", "passive", "power_in", "power_out",
    "open_collector", "open_emitter", "unspecified", "no_connect", "free",
}
LABEL_SCOPES = {"local", "global", "hier"}
SYM_MIRROR = {"none", "x", "y"}

COPPER_LAYERS_MAX = 32
TECH_LAYERS = {
    "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste", "B.Paste",
    "F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd", "Edge.Cuts", "Cmts.User", "Dwgs.User",
}
PAD_SHAPES = {"rect", "roundrect", "circle", "oval", "trapezoid", "custom"}
PAD_TYPES = {"smd", "tht", "np_tht", "connector"}
FP_SIDES = {"top", "bottom"}
VIA_TYPES = {"through", "blind", "buried", "micro"}
TRACK_KINDS = {"segment", "arc"}
ZONE_FILLS = {"solid", "hatch"}

# id prefix -> the document list key it indexes (used by next_id + validators)
_SCH_PREFIXES = {
    "s": "symbols", "w": "wires", "j": "junctions", "l": "labels",
    "bus": "buses", "pw": "power", "nc": "no_connect", "sh": "sheets", "g": "graphics",
}
_PCB_PREFIXES = {
    "fp": "footprints", "t": "tracks", "v": "vias", "z": "zones", "g": "graphics",
}

_COPPER_RE = re.compile(r"^(F|B|In\d+)\.Cu$")


def copper_layer_ids(copper_layers: int) -> list[str]:
    """Canonical ordered copper-layer id list for an N-copper stackup (F.Cu, In1.Cu, …, B.Cu)."""
    if copper_layers <= 1:
        return ["F.Cu"]
    inner = [f"In{i}.Cu" for i in range(1, copper_layers - 1)]
    return ["F.Cu", *inner, "B.Cu"]


def valid_layers(copper_layers: int) -> set[str]:
    return set(copper_layer_ids(copper_layers)) | TECH_LAYERS


# ── factories ────────────────────────────────────────────────────────────────

def new_schematic(name: str = "Schematic") -> dict:
    return {
        "schema": SCHEMA_SCH, "id": str(uuid.uuid4()), "name": name,
        "units": {"grid": 2.54},
        "sheets": [{"id": "sh1", "name": "root", "parent": None, "size": [297, 210], "ports": []}],
        "symbols": [], "wires": [], "junctions": [], "labels": [], "buses": [],
        "power": [], "no_connect": [], "graphics": [], "lib_cache": {}, "annotations": {},
    }


def new_board(name: str = "Board") -> dict:
    return {
        "schema": SCHEMA_PCB, "id": str(uuid.uuid4()), "name": name, "linked_sch": None,
        "stackup": {
            "copper_layers": 2, "thickness": 1.6,
            "layers": [
                {"id": "F.Cu", "type": "copper", "thickness": 0.035},
                {"id": "diel1", "type": "core", "thickness": 1.53, "er": 4.5},
                {"id": "B.Cu", "type": "copper", "thickness": 0.035},
            ],
        },
        "outline": {"shapes": [], "closed": False},
        "footprints": [], "tracks": [], "vias": [], "zones": [], "graphics": [],
        "rules": {
            "clearance": 0.2, "track_width_min": 0.15, "via_diameter_min": 0.45,
            "via_drill_min": 0.2, "annular_ring_min": 0.13, "hole_to_hole_min": 0.25,
            "edge_clearance": 0.3, "silk_to_mask_min": 0.15,
        },
        "nets": [{"name": "GND", "class": "default"}],
        "net_classes": {"default": {"clearance": 0.2, "track_width": 0.25,
                                     "via_diameter": 0.8, "via_drill": 0.4}},
        "origin": [0.0, 0.0],
    }


def is_schematic(doc: dict) -> bool:
    return doc.get("schema") == SCHEMA_SCH


def is_board(doc: dict) -> bool:
    return doc.get("schema") == SCHEMA_PCB


def doc_kind(doc: dict) -> str:
    return "sch" if is_schematic(doc) else "pcb" if is_board(doc) else ""


# ── id allocation ──────────────────────────────────────────────────────────────

def next_id(doc: dict, prefix: str) -> str:
    """Allocate the next unused ``<prefix><n>`` id (never reuses; scans current ids)."""
    table = _SCH_PREFIXES if is_schematic(doc) else _PCB_PREFIXES
    key = table.get(prefix)
    items = doc.get(key, []) if key else []
    n = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for it in items:
        m = pat.match(it.get("id", "")) if isinstance(it, dict) else None
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1}"


# ── geometry transforms (THE place pins/pads become world coords) ──────────────

def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


def _apply_mirror(x: float, y: float, mirror: str) -> tuple[float, float]:
    if mirror == "x":
        return (-x, y)
    if mirror == "y":
        return (x, -y)
    return (x, y)


def local_to_world(p, pos, angle: float = 0.0, mirror: str = "none") -> tuple[float, float]:
    """Transform a local point: mirror, then rotate CCW by ``angle``, then translate by ``pos``."""
    x, y = float(p[0]), float(p[1])
    x, y = _apply_mirror(x, y, mirror)
    x, y = _rot(x, y, angle)
    return (x + float(pos[0]), y + float(pos[1]))


def pin_world(symbol: dict, pin: dict) -> tuple[float, float]:
    """World endpoint of a symbol pin (the connection point — the tip away from the body).

    The pin's ``pos`` is its root on the symbol body; ``angle`` points away from the body, so the
    connection tip is ``pos + length * dir(angle)`` in symbol-local frame, then placed by the
    symbol's pos/angle/mirror.
    """
    px, py = float(pin["pos"][0]), float(pin["pos"][1])
    length = float(pin.get("length", 2.54))
    a = math.radians(float(pin.get("angle", 0.0)))
    tip = (px + length * math.cos(a), py + length * math.sin(a))
    return local_to_world(tip, symbol["pos"], symbol.get("angle", 0.0), symbol.get("mirror", "none"))


def pin_root_world(symbol: dict, pin: dict) -> tuple[float, float]:
    """World position of a pin's *root* (where it meets the body) — for drawing."""
    return local_to_world(pin["pos"], symbol["pos"], symbol.get("angle", 0.0),
                          symbol.get("mirror", "none"))


def pad_world(footprint: dict, pad: dict) -> tuple[float, float]:
    """World position of a footprint pad. Bottom-side footprints are mirrored across x."""
    mirror = "x" if footprint.get("side") == "bottom" else "none"
    return local_to_world(pad["pos"], footprint["pos"], footprint.get("angle", 0.0), mirror)


def symbol_def(doc: dict, libid: str) -> dict | None:
    """Resolve a symbol definition from the doc's embedded lib_cache."""
    return doc.get("lib_cache", {}).get(libid)


def iter_placed_pins(doc: dict):
    """Yield ``(symbol, pin, world_xy)`` for every pin of every placed symbol.

    Used by netlist.py / erc.py / canvas drawing so the transform lives in one place.
    """
    cache = doc.get("lib_cache", {})
    for sym in doc.get("symbols", []):
        sdef = cache.get(sym.get("libid"))
        if not sdef:
            continue
        unit = sym.get("unit", 1)
        for pin in sdef.get("pins", []):
            if pin.get("unit", 1) not in (unit, 0):
                continue
            yield sym, pin, pin_world(sym, pin)


# ── validation ─────────────────────────────────────────────────────────────────

def _is_xy(v: Any) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(c, (int, float)) for c in v))


def _check_ids(items, prefix: str, loc: str, errs: list[str]) -> None:
    seen = set()
    pat = re.compile(rf"^{re.escape(prefix)}\d+$")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errs.append(f"{loc}[{i}]: not an object")
            continue
        oid = it.get("id")
        if not isinstance(oid, str) or not pat.match(oid):
            errs.append(f"{loc}[{i}].id: missing or bad prefix (want {prefix}<n>) got {oid!r}")
        elif oid in seen:
            errs.append(f"{loc}[{i}].id: duplicate {oid!r}")
        else:
            seen.add(oid)


def validate_schematic(doc: dict) -> list[str]:
    errs: list[str] = []
    if doc.get("schema") != SCHEMA_SCH:
        return [f"schema: expected {SCHEMA_SCH!r} got {doc.get('schema')!r}"]
    if not isinstance(doc.get("id"), str):
        errs.append("id: missing")
    cache = doc.get("lib_cache", {})
    if not isinstance(cache, dict):
        errs.append("lib_cache: must be an object")
        cache = {}

    _check_ids(doc.get("symbols", []), "s", "symbols", errs)
    _check_ids(doc.get("wires", []), "w", "wires", errs)
    _check_ids(doc.get("junctions", []), "j", "junctions", errs)
    _check_ids(doc.get("labels", []), "l", "labels", errs)
    _check_ids(doc.get("power", []), "pw", "power", errs)
    _check_ids(doc.get("no_connect", []), "nc", "no_connect", errs)

    refs_seen: dict[str, str] = {}
    for i, s in enumerate(doc.get("symbols", [])):
        loc = f"symbols[{i}]"
        if s.get("libid") not in cache:
            errs.append(f"{loc}.libid: {s.get('libid')!r} not in lib_cache")
        if not _is_xy(s.get("pos")):
            errs.append(f"{loc}.pos: must be [x,y]")
        if s.get("mirror", "none") not in SYM_MIRROR:
            errs.append(f"{loc}.mirror: bad value {s.get('mirror')!r}")
        ref = s.get("ref", "")
        if isinstance(ref, str) and ref and not ref.endswith("?"):
            if ref in refs_seen:
                errs.append(f"{loc}.ref: duplicate {ref!r} (also {refs_seen[ref]})")
            refs_seen[ref] = loc

    for i, w in enumerate(doc.get("wires", [])):
        pts = w.get("pts")
        if not isinstance(pts, list) or len(pts) < 2 or not all(_is_xy(p) for p in pts):
            errs.append(f"wires[{i}].pts: need >=2 [x,y] points")

    for i, lb in enumerate(doc.get("labels", [])):
        if lb.get("scope", "local") not in LABEL_SCOPES:
            errs.append(f"labels[{i}].scope: bad value {lb.get('scope')!r}")
        if not isinstance(lb.get("text", ""), str) or not lb.get("text"):
            errs.append(f"labels[{i}].text: required non-empty string")

    for i, p in enumerate(doc.get("power", [])):
        if not p.get("net"):
            errs.append(f"power[{i}].net: required non-empty")

    # validate embedded symbol defs (pins)
    for libid, sdef in cache.items():
        if not isinstance(sdef, dict):
            errs.append(f"lib_cache[{libid}]: not an object")
            continue
        for j, pin in enumerate(sdef.get("pins", [])):
            ploc = f"lib_cache[{libid}].pins[{j}]"
            if pin.get("type", "passive") not in PIN_TYPES:
                errs.append(f"{ploc}.type: bad value {pin.get('type')!r}")
            if not _is_xy(pin.get("pos")):
                errs.append(f"{ploc}.pos: must be [x,y]")
            if not isinstance(pin.get("number", ""), str):
                errs.append(f"{ploc}.number: must be a string")
    return errs


def validate_board(doc: dict) -> list[str]:
    errs: list[str] = []
    if doc.get("schema") != SCHEMA_PCB:
        return [f"schema: expected {SCHEMA_PCB!r} got {doc.get('schema')!r}"]
    if not isinstance(doc.get("id"), str):
        errs.append("id: missing")

    stack = doc.get("stackup", {})
    n_cu = int(stack.get("copper_layers", 2))
    if n_cu < 1 or n_cu > COPPER_LAYERS_MAX or n_cu % 2 == 1 and n_cu != 1:
        errs.append(f"stackup.copper_layers: {n_cu} not a supported count")
    layers_ok = valid_layers(n_cu)
    cu_ok = set(copper_layer_ids(n_cu))

    _check_ids(doc.get("footprints", []), "fp", "footprints", errs)
    _check_ids(doc.get("tracks", []), "t", "tracks", errs)
    _check_ids(doc.get("vias", []), "v", "vias", errs)
    _check_ids(doc.get("zones", []), "z", "zones", errs)

    net_names = {n.get("name") for n in doc.get("nets", []) if isinstance(n, dict)}

    def _check_net(val, loc):
        if val is not None and val not in net_names:
            errs.append(f"{loc}: net {val!r} not in board nets")

    for i, fp in enumerate(doc.get("footprints", [])):
        loc = f"footprints[{i}]"
        if not _is_xy(fp.get("pos")):
            errs.append(f"{loc}.pos: must be [x,y]")
        if fp.get("side", "top") not in FP_SIDES:
            errs.append(f"{loc}.side: bad value {fp.get('side')!r}")
        for k, pad in enumerate(fp.get("pads", [])):
            ploc = f"{loc}.pads[{k}]"
            if pad.get("shape", "rect") not in PAD_SHAPES:
                errs.append(f"{ploc}.shape: bad value {pad.get('shape')!r}")
            ptype = pad.get("type", "smd")
            if ptype not in PAD_TYPES:
                errs.append(f"{ploc}.type: bad value {ptype!r}")
            has_drill = bool(pad.get("drill"))
            if ptype == "smd" and has_drill:
                errs.append(f"{ploc}: smd pad must not have a drill")
            if ptype in ("tht", "np_tht") and not has_drill:
                errs.append(f"{ploc}: {ptype} pad requires a drill")
            for lyr in pad.get("layers", []):
                if lyr not in layers_ok:
                    errs.append(f"{ploc}.layers: {lyr!r} not in stackup")
            _check_net(pad.get("net"), f"{ploc}.net")

    for i, t in enumerate(doc.get("tracks", [])):
        loc = f"tracks[{i}]"
        if t.get("layer") not in cu_ok:
            errs.append(f"{loc}.layer: {t.get('layer')!r} not a copper layer")
        if t.get("kind", "segment") not in TRACK_KINDS:
            errs.append(f"{loc}.kind: bad value {t.get('kind')!r}")
        _check_net(t.get("net"), f"{loc}.net")

    for i, v in enumerate(doc.get("vias", [])):
        loc = f"vias[{i}]"
        if v.get("type", "through") not in VIA_TYPES:
            errs.append(f"{loc}.type: bad value {v.get('type')!r}")
        for lyr in v.get("layers", []):
            if lyr not in cu_ok:
                errs.append(f"{loc}.layers: {lyr!r} not a copper layer")
        _check_net(v.get("net"), f"{loc}.net")

    for i, z in enumerate(doc.get("zones", [])):
        loc = f"zones[{i}]"
        if z.get("layer") not in cu_ok:
            errs.append(f"{loc}.layer: {z.get('layer')!r} not a copper layer")
        if z.get("fill", "solid") not in ZONE_FILLS:
            errs.append(f"{loc}.fill: bad value {z.get('fill')!r}")
        _check_net(z.get("net"), f"{loc}.net")

    outline = doc.get("outline", {})
    if outline.get("closed") and not outline.get("shapes"):
        errs.append("outline: marked closed but has no shapes")
    return errs


def validate(doc: dict) -> list[str]:
    """Dispatch to the right validator by schema tag."""
    if is_schematic(doc):
        return validate_schematic(doc)
    if is_board(doc):
        return validate_board(doc)
    return [f"schema: unknown {doc.get('schema')!r}"]
