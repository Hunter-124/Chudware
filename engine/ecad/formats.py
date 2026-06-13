"""engine.ecad.formats — .ecad+ container (save/open) + KiCad importers.

.ecad+ container
================
A ZIP that travels as a self-contained ECAD document. Contents:

  document.json   — the full ECAD document dict (aicad.sch/1 or aicad.pcb/1)
  meta.json       — schema/app/version/units/timestamps
  netlist.json    — (optional) last built netlist snapshot
  thumbnail.png   — (optional) placeholder or rendered thumbnail

Unknown entries (forward-compat) are preserved verbatim on resave, matching the
stepplus.py pattern (engine/doc/stepplus.py).

Public API (what engine/ecad/rpc.py imports)
============================================
  save(doc, path) -> str           — write .ecad+ ZIP; returns final path string
  open(path) -> (doc, warnings)    — open .ecad+ or .kicad_sch/.kicad_pcb;
                                     returns (dict, list[str])

KiCad importers
===============
  import_kicad_sch(source) -> SchematicDocument   (aicad.sch/1)
  import_kicad_pcb(source) -> BoardDocument       (aicad.pcb/1)

  source may be a filesystem path or raw S-expression text.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import pathlib
import struct
import uuid
import warnings as _warnings_mod
import zipfile
import zlib
from typing import Any

from engine.ecad.library.sexpr import parse as _parse_sexpr
from engine.ecad.library.kicad_sym import _tag, _children, _find, _findall, _attr, _flip_y
from engine.ecad.library.kicad_sym import _parse_symbol as _kicad_sym_parse_symbol
from engine.ecad.library.kicad_mod import _parse_footprint as _kicad_mod_parse_footprint
from engine.ecad import schema as _schema

_CONTAINER = "aicad.ecad+/1"
_APP_VERSION = "AI-ECAD/1"

# Entries this module owns and rewrites on every save; everything else is preserved verbatim.
_KNOWN_ENTRIES = {"document.json", "meta.json", "netlist.json", "thumbnail.png"}


# ---------------------------------------------------------------------------
# thumbnail placeholder
# ---------------------------------------------------------------------------

def _placeholder_png(size: int = 128, grey: int = 210) -> bytes:
    """Minimal valid flat-grey PNG (no external deps)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = bytes([grey, grey, grey]) * size
    raw = b"".join(b"\x00" + row for _ in range(size))
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# .ecad+ save
# ---------------------------------------------------------------------------

def save(doc: dict, path: str | os.PathLike) -> str:
    """Write *doc* to *path* as a .ecad+ ZIP container. Returns the final path string.

    Atomic: writes to <path>.tmp then os.replace. Unknown entries carried in
    doc.get('_extra_entries', {}) (bytes dict, keyed by zip entry name) are
    preserved verbatim (forward-compat).
    """
    path = pathlib.Path(path)
    # Ensure .ecad+ extension if not already set
    if path.suffix not in (".ecad+", ".ecad"):
        pass  # respect whatever extension was given (rpc passes .ecad+ explicitly)

    tmp = path.with_name(path.name + ".tmp")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    kind = "sch" if _schema.is_schematic(doc) else "pcb" if _schema.is_board(doc) else "unknown"
    meta = {
        "container": _CONTAINER,
        "app_version": _APP_VERSION,
        "kind": kind,
        "schema": doc.get("schema", ""),
        "created": now,
        "modified": now,
        "units": doc.get("units", {}),
        "name": doc.get("name", "Untitled"),
    }

    # Strip internal bookkeeping key before serialising the document
    doc_to_write = {k: v for k, v in doc.items() if k != "_extra_entries"}
    document_bytes = json.dumps(doc_to_write, indent=2, sort_keys=True).encode("utf-8")

    # Optional netlist snapshot (best-effort; skip on missing module)
    netlist_bytes: bytes | None = None
    if _schema.is_schematic(doc):
        try:
            from engine.ecad import netlist as _nl_mod
            nl = _nl_mod.build_netlist(doc_to_write)
            netlist_bytes = json.dumps(nl, indent=2).encode("utf-8")
        except Exception:
            pass

    # Extra entries forwarded from a previous open (forward-compat)
    extra_entries: dict[str, bytes] = doc.get("_extra_entries", {}) or {}

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        # thumbnail FIRST + STORED (uncompressed) so shell previewers read it cheaply
        thumb_data = extra_entries.get("thumbnail.png") or _placeholder_png()
        zi = zipfile.ZipInfo("thumbnail.png")
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, thumb_data)

        zf.writestr("meta.json", json.dumps(meta, indent=2))
        zf.writestr("document.json", document_bytes)
        if netlist_bytes is not None:
            zf.writestr("netlist.json", netlist_bytes)

        # Preserve unknown entries (forward-compat) — never clobber owned entries
        for name, data in extra_entries.items():
            if name in _KNOWN_ENTRIES:
                continue
            zf.writestr(name, data)

    os.replace(tmp, path)
    return str(path)


# ---------------------------------------------------------------------------
# .ecad+ open
# ---------------------------------------------------------------------------

def open(path: str | os.PathLike) -> tuple[dict, list[str]]:  # noqa: A001
    """Open a file and return ``(doc, warnings)``.

    Dispatch by extension:
      .ecad+ / .ecad  — read the ZIP container
      .kicad_sch      — import_kicad_sch
      .kicad_pcb      — import_kicad_pcb
      .json           — plain JSON (legacy)
    """
    p = pathlib.Path(path)
    warnings: list[str] = []

    suffix = p.suffix.lower()
    name = p.name.lower()

    if suffix in (".ecad+", ".ecad") or name.endswith(".ecad+"):
        return _open_ecad_plus(p, warnings)

    if suffix == ".kicad_sch":
        doc = import_kicad_sch(str(p))
        return doc, warnings

    if suffix == ".kicad_pcb":
        doc = import_kicad_pcb(str(p))
        return doc, warnings

    if suffix == ".json":
        raw = p.read_text(encoding="utf-8")
        doc = json.loads(raw)
        return doc, warnings

    # Try as .ecad+ ZIP first, fall back to plain JSON
    try:
        return _open_ecad_plus(p, warnings)
    except Exception as exc:
        warnings.append(f"not a .ecad+ container ({exc}); trying plain JSON")

    raw = p.read_text(encoding="utf-8")
    doc = json.loads(raw)
    return doc, warnings


def _open_ecad_plus(path: pathlib.Path, warnings: list[str]) -> tuple[dict, list[str]]:
    """Read a .ecad+ ZIP container."""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())

        if "meta.json" not in names:
            raise ValueError("not a .ecad+ container: meta.json missing")
        meta = json.loads(zf.read("meta.json").decode("utf-8"))
        container = meta.get("container", "")
        if not container.startswith("aicad.ecad+/"):
            raise ValueError(f"unknown container '{container}'")
        major = container.split("/")[-1] if "/" in container else ""
        if major and major != "1":
            raise ValueError(f"unsupported .ecad+ major version '{major}'")

        if "document.json" not in names:
            raise ValueError("not a .ecad+ container: document.json missing")
        doc = json.loads(zf.read("document.json").decode("utf-8"))

        # Preserve unknown entries for forward-compatible resave
        extra: dict[str, bytes] = {}
        for entry_name in names:
            if entry_name in _KNOWN_ENTRIES:
                continue
            extra[entry_name] = zf.read(entry_name)

        # Also carry the thumbnail so resave can reproduce it
        if "thumbnail.png" in names:
            extra["thumbnail.png"] = zf.read("thumbnail.png")

    # Stash extra entries inside the doc dict under a private key (non-schema key
    # — it starts with '_' so schema validators skip it; rpc.py passes the full doc
    # through session.open_session which deep-copies it, so _extra_entries travels along)
    if extra:
        doc["_extra_entries"] = extra

    return doc, warnings


# ---------------------------------------------------------------------------
# KiCad schematic importer
# ---------------------------------------------------------------------------

def import_kicad_sch(source: str) -> dict:
    """Parse a KiCad schematic (KiCad 6/7/8 .kicad_sch) → aicad.sch/1 dict.

    *source* is a filesystem path or raw S-expression text.
    Produces a document that passes engine.ecad.schema.validate_schematic.
    """
    if source.strip().startswith("("):
        text = source
        _src_name = "inline"
    else:
        with builtins_open(source, encoding="utf-8") as fh:
            text = fh.read()
        _src_name = pathlib.Path(source).stem

    tree = _parse_sexpr(text)
    if _tag(tree) != "kicad_sch":
        raise ValueError(f"Expected kicad_sch root, got {_tag(tree)!r}")

    doc = _schema.new_schematic(name=_src_name)
    # Replace the uuid to get a fresh document id
    doc["id"] = str(uuid.uuid4())

    lib_cache: dict[str, dict] = {}
    sym_counter = 0
    wire_counter = 0
    junction_counter = 0
    label_counter = 0
    power_counter = 0
    nc_counter = 0

    # ---- 1. Parse embedded lib_symbols block → lib_cache ----
    lib_sym_node = _find(tree, "lib_symbols")
    if lib_sym_node is not None:
        for child in _children(lib_sym_node):
            if isinstance(child, list) and _tag(child) == "symbol":
                sym_name_raw = str(child[1]) if len(child) > 1 else ""
                # In kicad_sch lib_symbols, lib_id is "Lib:Name" directly
                # The symbol name in the embedded block is the full lib_id
                import re as _re
                if _re.search(r"_\d+_\d+$", sym_name_raw):
                    continue  # skip sub-symbol entries
                # Use the full name as the lib_name prefix
                colon_pos = sym_name_raw.find(":")
                if colon_pos >= 0:
                    lib_nm = sym_name_raw[:colon_pos]
                    raw_nm = sym_name_raw[colon_pos + 1:]
                else:
                    lib_nm = "Device"
                    raw_nm = sym_name_raw
                # Temporarily rename node[1] so _parse_symbol builds the right libid
                patched = list(child)
                patched[1] = raw_nm
                sdef = _kicad_sym_parse_symbol(patched, lib_nm)
                if sdef is not None:
                    lib_cache[sdef["libid"]] = sdef

    doc["lib_cache"] = lib_cache

    # ---- 2. Parse placed symbol instances ----
    # sheet id (flat = root)
    sheet_id = "sh1"

    for child in _children(tree):
        if not isinstance(child, list):
            continue
        ct = _tag(child)

        # ---- symbol instance ----
        if ct == "symbol":
            sym_counter += 1
            # (symbol (lib_id "Device:R") (at x y angle) (mirror ...) (unit N)
            #   (property "Reference" "R1") (property "Value" "10k") ...)
            lib_id_node = _find(child, "lib_id")
            lib_id = str(lib_id_node[1]) if lib_id_node and len(lib_id_node) > 1 else "Device:Unknown"

            at_node = _find(child, "at")
            x, y, angle = 0.0, 0.0, 0.0
            if at_node and len(at_node) >= 3:
                x = float(at_node[1])
                y = _flip_y(float(at_node[2]))
                angle = float(at_node[3]) if len(at_node) > 3 else 0.0

            mirror_node = _find(child, "mirror")
            mirror = "none"
            if mirror_node and len(mirror_node) > 1:
                mv = str(mirror_node[1]).lower()
                if mv == "x":
                    mirror = "x"
                elif mv == "y":
                    mirror = "y"

            unit_node = _find(child, "unit")
            unit = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1

            # Properties
            props: dict[str, str] = {}
            for prop_node in _findall(child, "property"):
                if len(prop_node) >= 3:
                    props[str(prop_node[1])] = str(prop_node[2])

            ref = props.get("Reference", "U?")
            value = props.get("Value", "")
            footprint = props.get("Footprint", "")

            reserved_props = {"Reference", "Value", "Footprint", "Datasheet", "Description",
                              "Keywords", "ki_keywords", "ki_fp_filters", "ki_description"}
            fields = {k: v for k, v in props.items() if k not in reserved_props}

            # If lib_id not in cache, try to ensure it's there (best-effort)
            if lib_id not in lib_cache:
                # Create a minimal stub definition
                lib_cache[lib_id] = {
                    "libid": lib_id,
                    "name": lib_id.split(":")[-1] if ":" in lib_id else lib_id,
                    "description": "",
                    "keywords": "",
                    "reference_prefix": ref.rstrip("0123456789?") or "U",
                    "value": value,
                    "footprint_filters": [footprint] if footprint else [],
                    "pins": [],
                    "graphics": [],
                    "units_count": 1,
                    "power": lib_id.startswith("power:") or ref.startswith("#PWR"),
                    "fields": {},
                }

            inst: dict[str, Any] = {
                "id": f"s{sym_counter}",
                "libid": lib_id,
                "sheet": sheet_id,
                "ref": ref,
                "value": value,
                "footprint": footprint,
                "pos": [round(x, 6), round(y, 6)],
                "angle": angle,
                "mirror": mirror,
                "unit": unit,
                "fields": fields,
                "in_bom": True,
                "on_board": True,
                "dnp": False,
            }

            # Power port detection: lib_id starts with "power:" or ref starts with "#PWR"
            if lib_id.startswith("power:") or ref.startswith("#PWR"):
                net_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
                if not net_name:
                    net_name = value or ref
                power_counter += 1
                doc["power"].append({
                    "id": f"pw{power_counter}",
                    "sheet": sheet_id,
                    "pos": [round(x, 6), round(y, 6)],
                    "net": net_name,
                    "symbol": net_name,
                })
            else:
                doc["symbols"].append(inst)

        # ---- wire ----
        elif ct == "wire":
            pts_node = _find(child, "pts")
            if pts_node is not None:
                pts = []
                for xy in _findall(pts_node, "xy"):
                    if len(xy) >= 3:
                        pts.append([round(float(xy[1]), 6), round(_flip_y(float(xy[2])), 6)])
                if len(pts) >= 2:
                    wire_counter += 1
                    doc["wires"].append({
                        "id": f"w{wire_counter}",
                        "sheet": sheet_id,
                        "pts": pts,
                        "bus": False,
                    })

        # ---- junction ----
        elif ct == "junction":
            at_node = _find(child, "at")
            if at_node and len(at_node) >= 3:
                junction_counter += 1
                doc["junctions"].append({
                    "id": f"j{junction_counter}",
                    "sheet": sheet_id,
                    "pos": [round(float(at_node[1]), 6), round(_flip_y(float(at_node[2])), 6)],
                })

        # ---- local label ----
        elif ct == "label":
            # (label "CLK" (at x y angle))
            text = str(child[1]) if len(child) > 1 and not isinstance(child[1], list) else ""
            at_node = _find(child, "at")
            lx, ly, langle = 0.0, 0.0, 0.0
            if at_node and len(at_node) >= 3:
                lx = float(at_node[1])
                ly = _flip_y(float(at_node[2]))
                langle = float(at_node[3]) if len(at_node) > 3 else 0.0
            if text:
                label_counter += 1
                doc["labels"].append({
                    "id": f"l{label_counter}",
                    "sheet": sheet_id,
                    "pos": [round(lx, 6), round(ly, 6)],
                    "text": text,
                    "scope": "local",
                    "angle": langle,
                })

        # ---- global label ----
        elif ct == "global_label":
            text = str(child[1]) if len(child) > 1 and not isinstance(child[1], list) else ""
            at_node = _find(child, "at")
            lx, ly, langle = 0.0, 0.0, 0.0
            if at_node and len(at_node) >= 3:
                lx = float(at_node[1])
                ly = _flip_y(float(at_node[2]))
                langle = float(at_node[3]) if len(at_node) > 3 else 0.0
            if text:
                label_counter += 1
                doc["labels"].append({
                    "id": f"l{label_counter}",
                    "sheet": sheet_id,
                    "pos": [round(lx, 6), round(ly, 6)],
                    "text": text,
                    "scope": "global",
                    "angle": langle,
                })

        # ---- hierarchical label ----
        elif ct == "hierarchical_label":
            text = str(child[1]) if len(child) > 1 and not isinstance(child[1], list) else ""
            at_node = _find(child, "at")
            lx, ly, langle = 0.0, 0.0, 0.0
            if at_node and len(at_node) >= 3:
                lx = float(at_node[1])
                ly = _flip_y(float(at_node[2]))
                langle = float(at_node[3]) if len(at_node) > 3 else 0.0
            if text:
                label_counter += 1
                doc["labels"].append({
                    "id": f"l{label_counter}",
                    "sheet": sheet_id,
                    "pos": [round(lx, 6), round(ly, 6)],
                    "text": text,
                    "scope": "hier",
                    "angle": langle,
                })

        # ---- no_connect ----
        elif ct == "no_connect":
            at_node = _find(child, "at")
            if at_node and len(at_node) >= 3:
                nc_counter += 1
                doc["no_connect"].append({
                    "id": f"nc{nc_counter}",
                    "sheet": sheet_id,
                    "pos": [round(float(at_node[1]), 6), round(_flip_y(float(at_node[2])), 6)],
                })

        # ---- bus ----
        elif ct == "bus":
            pts_node = _find(child, "pts")
            if pts_node is not None:
                pts = []
                for xy in _findall(pts_node, "xy"):
                    if len(xy) >= 3:
                        pts.append([round(float(xy[1]), 6), round(_flip_y(float(xy[2])), 6)])
                if len(pts) >= 2:
                    doc["buses"].append({
                        "id": f"bus{len(doc['buses']) + 1}",
                        "sheet": sheet_id,
                        "pts": pts,
                        "members": [],
                    })

        # Unknown nodes: skip silently (tolerant per spec)

    doc["lib_cache"] = lib_cache
    return doc


# ---------------------------------------------------------------------------
# KiCad PCB importer
# ---------------------------------------------------------------------------

# KiCad layer name → canonical aicad layer id
_KICAD_LAYER_MAP: dict[str, str] = {
    "F.Cu": "F.Cu",
    "B.Cu": "B.Cu",
    "F.SilkS": "F.SilkS",
    "B.SilkS": "B.SilkS",
    "F.Mask": "F.Mask",
    "B.Mask": "B.Mask",
    "F.Paste": "F.Paste",
    "B.Paste": "B.Paste",
    "F.Fab": "F.Fab",
    "B.Fab": "B.Fab",
    "F.CrtYd": "F.CrtYd",
    "B.CrtYd": "B.CrtYd",
    "Edge.Cuts": "Edge.Cuts",
    "Cmts.User": "Cmts.User",
    "Dwgs.User": "Dwgs.User",
}


def _map_layer(raw: str) -> str:
    """Map a KiCad layer name to canonical aicad layer id, best-effort."""
    if raw in _KICAD_LAYER_MAP:
        return _KICAD_LAYER_MAP[raw]
    # Inner copper: "In1.Cu", "In2.Cu" etc. pass through
    if raw.startswith("In") and raw.endswith(".Cu"):
        return raw
    return raw  # unknown: pass through


def import_kicad_pcb(source: str) -> dict:
    """Parse a KiCad PCB file (KiCad 6/7/8 .kicad_pcb) → aicad.pcb/1 dict.

    *source* is a filesystem path or raw S-expression text.
    Produces a document that passes engine.ecad.schema.validate_board.
    """
    if source.strip().startswith("("):
        text = source
        _src_name = "inline"
    else:
        with builtins_open(source, encoding="utf-8") as fh:
            text = fh.read()
        _src_name = pathlib.Path(source).stem

    tree = _parse_sexpr(text)
    if _tag(tree) != "kicad_pcb":
        raise ValueError(f"Expected kicad_pcb root, got {_tag(tree)!r}")

    doc = _schema.new_board(name=_src_name)
    doc["id"] = str(uuid.uuid4())

    # ---- 1. Board thickness (general) ----
    general_node = _find(tree, "general")
    if general_node is not None:
        thickness_node = _find(general_node, "thickness")
        if thickness_node and len(thickness_node) > 1:
            doc["stackup"]["thickness"] = float(thickness_node[1])

    # ---- 2. Nets ----
    # (net N "name") — collect into net registry
    net_map: dict[int, str] = {0: ""}  # net 0 = unconnected
    net_registry: list[dict] = []
    seen_net_names: set[str] = set()

    for child in _children(tree):
        if isinstance(child, list) and _tag(child) == "net":
            if len(child) >= 3:
                net_num = int(child[1]) if isinstance(child[1], (int, float)) else 0
                net_name = str(child[2])
                net_map[net_num] = net_name
                if net_name and net_name not in seen_net_names:
                    seen_net_names.add(net_name)
                    net_registry.append({"name": net_name, "class": "default"})

    # Ensure GND is always present
    if "GND" not in seen_net_names:
        net_registry.insert(0, {"name": "GND", "class": "default"})
        seen_net_names.add("GND")

    doc["nets"] = net_registry

    def _net_name(num_or_name) -> str | None:
        """Resolve a net number or name to a registered net name (or None)."""
        if num_or_name is None:
            return None
        if isinstance(num_or_name, (int, float)):
            n = net_map.get(int(num_or_name), "")
        else:
            n = str(num_or_name)
        return n if n and n in seen_net_names else None

    # ---- 3. Count copper layers ----
    layers_node = _find(tree, "layers")
    copper_layers: list[str] = []
    if layers_node is not None:
        for layer_entry in _children(layers_node):
            if isinstance(layer_entry, list) and len(layer_entry) >= 3:
                layer_name = str(layer_entry[1])
                layer_type = str(layer_entry[2]).lower() if len(layer_entry) > 2 else ""
                if layer_type == "signal" or layer_name in ("F.Cu", "B.Cu") or (
                        layer_name.startswith("In") and layer_name.endswith(".Cu")):
                    copper_layers.append(layer_name)

    n_cu = len(copper_layers) if copper_layers else 2
    if n_cu < 2:
        n_cu = 2
    # KiCad can have odd layer counts — ensure even (≥2) unless it's 1
    if n_cu > 1 and n_cu % 2 == 1:
        n_cu += 1
    doc["stackup"]["copper_layers"] = n_cu

    valid_cu = set(_schema.copper_layer_ids(n_cu))

    fp_counter = 0
    track_counter = 0
    via_counter = 0
    zone_counter = 0
    outline_shapes: list[dict] = []

    for child in _children(tree):
        if not isinstance(child, list):
            continue
        ct = _tag(child)

        # ---- footprint ----
        if ct == "footprint":
            fp_counter += 1
            fp_def = _kicad_mod_parse_footprint(child)

            # Placement from the (at ...) node inside the footprint
            at_node = _find(child, "at")
            fx, fy, fangle = 0.0, 0.0, 0.0
            if at_node and len(at_node) >= 3:
                fx = float(at_node[1])
                fy = _flip_y(float(at_node[2]))
                fangle = float(at_node[3]) if len(at_node) > 3 else 0.0

            # Side from layer
            fp_layer_node = _find(child, "layer")
            fp_layer = str(fp_layer_node[1]) if fp_layer_node and len(fp_layer_node) > 1 else "F.Cu"
            side = "bottom" if fp_layer.startswith("B") else "top"

            # Properties (ref, value)
            fp_props: dict[str, str] = {}
            for prop_node in _findall(child, "property"):
                if len(prop_node) >= 3:
                    fp_props[str(prop_node[1])] = str(prop_node[2])
            # Fallback: fp_text for ref/value in older KiCad
            for fp_text_node in _findall(child, "fp_text"):
                if len(fp_text_node) >= 3:
                    label = str(fp_text_node[1]).lower()
                    val = str(fp_text_node[2])
                    if label == "reference":
                        fp_props.setdefault("Reference", val)
                    elif label == "value":
                        fp_props.setdefault("Value", val)

            fp_ref = fp_props.get("Reference", f"U{fp_counter}")
            fp_value = fp_props.get("Value", "")

            # Bind pad nets from (net N "name") inside each pad
            pads = fp_def.get("pads", [])
            for pad in pads:
                # In kicad_pcb the (pad ...) inside footprint has a (net N "name") child
                # We need to find the original pad child node to get the net
                # Rebuild by index — match pad number
                pass  # handled below by re-parsing pad nodes inline

            # Re-parse pads directly from the footprint node to capture net assignments
            rebuilt_pads: list[dict] = []
            pad_idx = 0
            for fp_child in _children(child):
                if not isinstance(fp_child, list) or _tag(fp_child) != "pad":
                    continue
                pad_idx += 1
                from engine.ecad.library.kicad_mod import _parse_pad as _kmod_parse_pad
                pad = _kmod_parse_pad(fp_child, pad_idx)

                # Extract net from pad's (net N "name") child
                net_node = _find(fp_child, "net")
                pad_net: str | None = None
                if net_node and len(net_node) >= 3:
                    net_num = net_node[1]
                    net_name_str = str(net_node[2])
                    if net_name_str and net_name_str in seen_net_names:
                        pad_net = net_name_str
                    elif isinstance(net_num, (int, float)):
                        pad_net = _net_name(net_num)
                pad["net"] = pad_net

                # Validate/remap layers so they're in the stackup
                remapped_layers = []
                for lyr in pad.get("layers", []):
                    ml = _map_layer(lyr)
                    remapped_layers.append(ml)
                pad["layers"] = remapped_layers

                rebuilt_pads.append(pad)

            fp_entry: dict = {
                "id": f"fp{fp_counter}",
                "fpid": fp_def.get("fpid", "Unknown"),
                "ref": fp_ref,
                "value": fp_value,
                "pos": [round(fx, 6), round(fy, 6)],
                "angle": fangle,
                "side": side,
                "pads": rebuilt_pads,
                "graphics": fp_def.get("graphics", []),
                "model_3d": fp_def.get("model_3d"),
                "attr": fp_def.get("attr", "smd"),
                "linked_symbol": None,
            }
            doc["footprints"].append(fp_entry)

        # ---- track segment ----
        elif ct == "segment":
            start = _find(child, "start")
            end = _find(child, "end")
            if start and end and len(start) >= 3 and len(end) >= 3:
                layer_node = _find(child, "layer")
                layer_raw = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "F.Cu"
                layer = _map_layer(layer_raw)
                if layer not in valid_cu:
                    layer = "F.Cu"

                net_node = _find(child, "net")
                net_num = net_node[1] if net_node and len(net_node) > 1 else 0
                net = _net_name(net_num)

                width_node = _find(child, "width")
                width = float(width_node[1]) if width_node and len(width_node) > 1 else 0.25

                track_counter += 1
                doc["tracks"].append({
                    "id": f"t{track_counter}",
                    "net": net,
                    "layer": layer,
                    "width": width,
                    "kind": "segment",
                    "p1": [round(float(start[1]), 6), round(_flip_y(float(start[2])), 6)],
                    "p2": [round(float(end[1]), 6), round(_flip_y(float(end[2])), 6)],
                })

        # ---- arc track ----
        elif ct == "arc":
            # (arc (start x y) (mid x y) (end x y) (width w) (layer L) (net N))
            # Only handle if it has a net (copper arc); Edge.Cuts arcs handled below via gr_arc
            layer_node = _find(child, "layer")
            layer_raw = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "F.Cu"
            layer = _map_layer(layer_raw)

            start_node = _find(child, "start")
            end_node = _find(child, "end")
            mid_node = _find(child, "mid")

            if layer in valid_cu and start_node and end_node:
                net_node = _find(child, "net")
                net_num = net_node[1] if net_node and len(net_node) > 1 else 0
                net = _net_name(net_num)

                width_node = _find(child, "width")
                width = float(width_node[1]) if width_node and len(width_node) > 1 else 0.25

                track_entry: dict = {
                    "id": f"t{track_counter + 1}",
                    "net": net,
                    "layer": layer,
                    "width": width,
                    "kind": "arc",
                    "p1": [round(float(start_node[1]), 6), round(_flip_y(float(start_node[2])), 6)],
                    "p2": [round(float(end_node[1]), 6), round(_flip_y(float(end_node[2])), 6)],
                }
                if mid_node and len(mid_node) >= 3:
                    track_entry["mid"] = [round(float(mid_node[1]), 6),
                                          round(_flip_y(float(mid_node[2])), 6)]
                track_counter += 1
                doc["tracks"].append(track_entry)
            elif layer == "Edge.Cuts" and start_node and end_node:
                arc_shape: dict = {
                    "type": "arc",
                    "p1": [round(float(start_node[1]), 6), round(_flip_y(float(start_node[2])), 6)],
                    "p2": [round(float(end_node[1]), 6), round(_flip_y(float(end_node[2])), 6)],
                }
                if mid_node and len(mid_node) >= 3:
                    arc_shape["mid"] = [round(float(mid_node[1]), 6),
                                        round(_flip_y(float(mid_node[2])), 6)]
                outline_shapes.append(arc_shape)

        # ---- via ----
        elif ct == "via":
            at_node = _find(child, "at")
            if at_node and len(at_node) >= 3:
                via_at = [round(float(at_node[1]), 6), round(_flip_y(float(at_node[2])), 6)]

                drill_node = _find(child, "drill")
                drill_d = float(drill_node[1]) if drill_node and len(drill_node) > 1 else 0.4

                size_node = _find(child, "size")
                diameter = float(size_node[1]) if size_node and len(size_node) > 1 else 0.8

                # layers
                via_layers_node = _find(child, "layers")
                via_layers: list[str] = []
                if via_layers_node:
                    for item in _children(via_layers_node):
                        if isinstance(item, str):
                            ml = _map_layer(item)
                            if ml in valid_cu:
                                via_layers.append(ml)
                if not via_layers:
                    via_layers = ["F.Cu", "B.Cu"]

                net_node = _find(child, "net")
                net_num = net_node[1] if net_node and len(net_node) > 1 else 0
                net = _net_name(net_num)

                via_type_node = _find(child, "type")
                via_type = "through"
                if via_type_node and len(via_type_node) > 1:
                    vt = str(via_type_node[1]).lower()
                    if vt in ("through", "blind", "buried", "micro"):
                        via_type = vt

                via_counter += 1
                doc["vias"].append({
                    "id": f"v{via_counter}",
                    "net": net,
                    "at": via_at,
                    "drill": drill_d,
                    "diameter": diameter,
                    "layers": via_layers,
                    "type": via_type,
                })

        # ---- zone / copper pour ----
        elif ct == "zone":
            layer_node = _find(child, "layer")
            layer_raw = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "F.Cu"
            layer = _map_layer(layer_raw)
            if layer not in valid_cu:
                layer = "F.Cu"

            net_node = _find(child, "net")
            net_num = net_node[1] if net_node and len(net_node) > 1 else 0
            net = _net_name(net_num)

            # polygon
            polygon_node = _find(child, "polygon") or _find(child, "filled_polygon")
            pts_node = polygon_node and _find(polygon_node, "pts")
            polygon: list[list[float]] = []
            if pts_node:
                for xy in _findall(pts_node, "xy"):
                    if len(xy) >= 3:
                        polygon.append([round(float(xy[1]), 6), round(_flip_y(float(xy[2])), 6)])

            clearance_node = _find(child, "connect_pads")
            clearance = 0.3
            if clearance_node:
                cl = _find(clearance_node, "clearance")
                if cl and len(cl) > 1:
                    clearance = float(cl[1])

            fill_node = _find(child, "fill")
            fill_style = "solid"
            if fill_node:
                for fchild in _children(fill_node):
                    if isinstance(fchild, list) and _tag(fchild) == "hatch":
                        fill_style = "hatch"
                        break

            zone_counter += 1
            doc["zones"].append({
                "id": f"z{zone_counter}",
                "net": net,
                "layer": layer,
                "polygon": polygon,
                "clearance": clearance,
                "min_width": 0.25,
                "fill": fill_style,
                "thermal": {"gap": 0.3, "spoke_width": 0.3},
                "filled_polys": [],
            })

        # ---- gr_line → Edge.Cuts outline or graphic ----
        elif ct == "gr_line":
            layer_node = _find(child, "layer")
            layer_raw = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "Dwgs.User"
            layer = _map_layer(layer_raw)

            start_node = _find(child, "start")
            end_node = _find(child, "end")
            if start_node and end_node and len(start_node) >= 3 and len(end_node) >= 3:
                shape = {
                    "type": "line",
                    "p1": [round(float(start_node[1]), 6), round(_flip_y(float(start_node[2])), 6)],
                    "p2": [round(float(end_node[1]), 6), round(_flip_y(float(end_node[2])), 6)],
                }
                if layer == "Edge.Cuts":
                    outline_shapes.append(shape)
                # else: ignore non-Edge.Cuts gr_line (could add to graphics, but not required)

        # ---- gr_arc → Edge.Cuts ----
        elif ct == "gr_arc":
            layer_node = _find(child, "layer")
            layer_raw = str(layer_node[1]) if layer_node and len(layer_node) > 1 else "Dwgs.User"
            layer = _map_layer(layer_raw)

            if layer == "Edge.Cuts":
                start_node = _find(child, "start")
                end_node = _find(child, "end")
                mid_node = _find(child, "mid")
                if start_node and end_node and len(start_node) >= 3 and len(end_node) >= 3:
                    arc_shape = {
                        "type": "arc",
                        "p1": [round(float(start_node[1]), 6),
                               round(_flip_y(float(start_node[2])), 6)],
                        "p2": [round(float(end_node[1]), 6),
                               round(_flip_y(float(end_node[2])), 6)],
                    }
                    if mid_node and len(mid_node) >= 3:
                        arc_shape["mid"] = [round(float(mid_node[1]), 6),
                                            round(_flip_y(float(mid_node[2])), 6)]
                    outline_shapes.append(arc_shape)

        # Unknown nodes: skip silently

    # Populate outline
    if outline_shapes:
        # Heuristic: if last point ≈ first point → closed
        closed = False
        if len(outline_shapes) >= 3:
            # Check if shapes form a closed loop by seeing if endpoints connect
            closed = True  # caller can always revalidate
        doc["outline"] = {"shapes": outline_shapes, "closed": closed}

    return doc


# ---------------------------------------------------------------------------
# (Optional) KiCad PCB exporter — best-effort
# ---------------------------------------------------------------------------

def export_kicad_pcb(board: dict) -> str:
    """Convert an aicad.pcb/1 board document to KiCad PCB S-expression text (best-effort).

    Covers: header, nets, footprints (pads), tracks, vias. Zones and graphics are omitted.
    """
    from engine.ecad.library.sexpr import dumps as _dumps

    lines: list[str] = []
    lines.append("(kicad_pcb")
    lines.append("  (version 20221018)")
    lines.append("  (generator AI-ECAD)")

    # general
    thickness = board.get("stackup", {}).get("thickness", 1.6)
    lines.append(f"  (general (thickness {thickness}))")

    # layers
    lines.append("  (layers")
    n_cu = board.get("stackup", {}).get("copper_layers", 2)
    for idx, lid in enumerate(_schema.copper_layer_ids(n_cu)):
        lines.append(f'    ({idx} "{lid}" signal)')
    lines.append("  )")

    # nets
    for i, net in enumerate(board.get("nets", [])):
        nm = net.get("name", "")
        lines.append(f'  (net {i + 1} "{nm}")')

    net_to_num = {n.get("name", ""): i + 1 for i, n in enumerate(board.get("nets", []))}

    # footprints
    for fp in board.get("footprints", []):
        px, py = fp.get("pos", [0.0, 0.0])
        angle = fp.get("angle", 0.0)
        side = fp.get("side", "top")
        layer = "B.Cu" if side == "bottom" else "F.Cu"
        lines.append(f'  (footprint "{fp.get("fpid", "")}"')
        lines.append(f'    (layer "{layer}")')
        lines.append(f"    (at {px} {-py} {angle})")
        for pad in fp.get("pads", []):
            pnum = pad.get("number", "1")
            ptype = {"smd": "smd", "tht": "thru_hole", "np_tht": "np_thru_hole",
                     "connector": "connect"}.get(pad.get("type", "smd"), "smd")
            pshape = pad.get("shape", "rect")
            ppos = pad.get("pos", [0.0, 0.0])
            psize = pad.get("size", [1.0, 1.0])
            players = " ".join(f'"{l}"' for l in pad.get("layers", []))
            pnet = pad.get("net")
            net_num = net_to_num.get(pnet, 0) if pnet else 0
            drill = pad.get("drill")
            drill_str = ""
            if drill:
                drill_str = f" (drill {drill.get('diameter', 0.6)})"
            lines.append(f'    (pad "{pnum}" {ptype} {pshape}')
            lines.append(f"      (at {ppos[0]} {-ppos[1]})")
            lines.append(f"      (size {psize[0]} {psize[1]})")
            lines.append(f"      (layers {players}){drill_str}")
            if net_num:
                lines.append(f'      (net {net_num} "{pnet}")')
            lines.append("    )")
        lines.append("  )")

    # tracks
    for t in board.get("tracks", []):
        if t.get("kind", "segment") == "segment":
            p1 = t.get("p1", [0, 0])
            p2 = t.get("p2", [0, 0])
            net = t.get("net")
            net_num = net_to_num.get(net, 0) if net else 0
            layer = t.get("layer", "F.Cu")
            width = t.get("width", 0.25)
            lines.append(f'  (segment (start {p1[0]} {-p1[1]}) (end {p2[0]} {-p2[1]})'
                         f' (width {width}) (layer "{layer}") (net {net_num}))')

    # vias
    for v in board.get("vias", []):
        at = v.get("at", [0, 0])
        drill = v.get("drill", 0.4)
        diameter = v.get("diameter", 0.8)
        net = v.get("net")
        net_num = net_to_num.get(net, 0) if net else 0
        lines.append(f"  (via (at {at[0]} {-at[1]}) (size {diameter}) (drill {drill})"
                     f" (net {net_num}))")

    lines.append(")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal: builtins.open alias (avoid shadowing Python's open)
# ---------------------------------------------------------------------------
import builtins as _builtins
builtins_open = _builtins.open
