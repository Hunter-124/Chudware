"""3D-print preparation: deterministic printability, orientation, real slicing, G-code proof.

Family ``cam`` (additive CAM). Seven tools:

* ``slicer_status`` — which PrusaSlicer the pipeline will run, or the exact install step.
* ``list_print_profiles`` — vendor printer/print/filament presets read from the installed
  slicer (never copied into this repo).
* ``optimize_print_orientation`` — deterministic orientation ranking (support volume, time,
  bed contact, tip risk, bed fit, preserved faces, strength direction).
* ``slice_part`` — the one supported way to produce G-code: vendor presets for the named
  printer + goal-driven settings, auto orientation and supports from the DFAM analysis,
  STOCK PrusaSlicer as an out-of-process subprocess (AGPL boundary: files and argv only),
  then ``verify_gcode`` on the result and a manifest beside it. ``parts=[...]`` slices
  several parts as one plate (layout: ``services/manufacturing/slicer/plate.py``) and
  ``complete_individual_objects=True`` prints them one at a time, clearance-checked.
* ``verify_gcode`` — deterministic proof of an existing G-code file (bounds, temperatures,
  cold extrusion, homing, layer heights, and per-layer mid-air extrusion detection).
* ``print_media_inventory`` — read-only inventory of a printer flashdrive/SD card: every
  file, its sha256, which ones the printer can select, duplicates, managed status, and a
  whole-drive token (``services/manufacturing/print_media.py``).
* ``publish_print_media`` — the managed delivery of a project's COMPLETE verified job set to
  that media: revision-prefixed names under ``PRINT/<project>/``, readback-verified, old
  managed toolpaths retired into a non-printable archive, unmanaged printables reported.

Why this exists: an agent once sliced a 17-part arm by downloading a PrusaSlicer RPM into
/tmp and hand-writing ~100 slicer keys from memory (flimsy sheathless snug supports, tall parts
stood upright on them, invented start G-code). Every check it ran was bounds and
temperatures, so nothing caught the support towers that collapsed. Slicing now starts from the
printer vendor's tuned presets, the orientation is chosen by measurement, and a G-code file is
only called print-ready when no layer extrudes into mid-air.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from services.mcp import core_bridge as cb
from services.mcp.cad_common import (
    _doc,
    _doc_mesh,
    _dumps,
    _err,
    _project_save_stem,
    _sanitize_output_path,
)

_MESH_SUFFIXES = frozenset({".stl", ".3mf", ".obj", ".ply"})
_GOALS = ("balanced", "strength", "speed", "material", "detail")
# Layer height a goal asks for when the caller names none (mm, 0.4 mm nozzle basis).
_GOAL_LAYER = {"balanced": 0.20, "strength": 0.20, "speed": 0.28, "material": 0.20,
               "detail": 0.12}
# Orientation objective per slicing goal (dfam.optimize_orientation goals).
_GOAL_ORIENT = {"balanced": "balanced", "strength": "strength", "speed": "min_time",
                "material": "min_support", "detail": "balanced"}
# Export tessellation for slicing: tighter than the display mesh so bores and gear flanks
# are not faceted into the toolpath (engine defaults are 0.05 mm / 0.35 rad).
_SLICE_LIN_DEFLECTION_MM = 0.02
_SLICE_ANG_DEFLECTION_RAD = 0.1
_CATALOG_CACHE: dict[str, Any] = {}


# ----------------------------------------------------------------------------- helpers

def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """First number of a PrusaSlicer config value ("0.4", "0.4,0.4", "15%")."""
    if value is None:
        return default
    text = str(value).strip().strip('"').split(",")[0].split(";")[0].strip().rstrip("%")
    try:
        number = float(text)
    except ValueError:
        return default
    return number if math.isfinite(number) else default


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _doc_name(doc_id: str) -> str:
    try:
        for entry in (cb.call("doc_list", {}) or {}).get("docs", []):
            if isinstance(entry, dict) and entry.get("doc_id") == doc_id:
                return str(entry.get("name") or doc_id)
    except Exception:  # noqa: BLE001 — the doc id is a stable label
        pass
    return doc_id


def _resolve_input_path(path: str) -> Path:
    resolved = Path(os.path.abspath(os.path.expanduser(path)))
    if not resolved.is_file():
        raise ValueError(f"mesh file not found: {resolved}")
    if resolved.suffix.lower() not in _MESH_SUFFIXES:
        raise ValueError(
            f"{resolved.name}: slice/analyze a mesh file (.stl/.3mf/.obj/.ply) by path, or "
            "open CAD files (STEP/STEP+) with import_part(path=...) and pass its doc_id")
    return resolved


def load_target(doc_id: Optional[str], path: Optional[str]):
    """(PrintMesh, label, source) for a document or a mesh file.

    Documents use the engine's display tessellation joined to doc_query face sids, so every
    DFAM region names the CAD face (and the feature that built it) to edit."""
    from services.dfam import load_mesh_file  # noqa: PLC0415

    if path:
        file_path = _resolve_input_path(path)
        return load_mesh_file(file_path), file_path.stem, {"kind": "file", "path": str(file_path)}
    return load_doc(_doc(doc_id))


def load_doc(d: str):
    """(PrintMesh, label, source) for an already-resolved document id."""
    from services.dfam import PrintMesh  # noqa: PLC0415

    matches = (cb.call("doc_query", {"doc_id": d, "query": {"of": "face"}}) or {}).get(
        "matches") or []
    mesh = _doc_mesh(d)
    if mesh is None:
        raise ValueError(f"document {d} has no current tessellation (empty or failed rebuild)")
    return (PrintMesh.from_doc_mesh(mesh, matches), _doc_name(d),
            {"kind": "doc", "doc_id": d})


def dfm_profile_params(process: str = "fdm") -> dict:
    """The engine's DfM ProcessProfile params (min wall, overhang, bridge) or {}."""
    try:
        reply = cb.call("dfm_get_profile", {"process": process})
    except Exception:  # noqa: BLE001 — a file-only analysis can run without the engine
        return {}
    if not reply.get("ok"):
        return {}
    return (reply.get("result") or {}).get("params") or {}


def print_params(*, process: str = "fdm", resolved=None, dfm: Optional[dict] = None,
                 material: Optional[str] = None, nozzle_mm: Optional[float] = None,
                 layer_height_mm: Optional[float] = None, bed_size_mm=None,
                 config: Optional[dict] = None):
    """PrintParams from the engine DfM profile, the resolved slicer preset and caller values.

    Precedence: caller values > the merged slicer config (printer+print+filament+goal) >
    the engine DfM profile > PrintParams defaults. One function so dfm_check, the orientation
    tool and slice_part judge a part against the same numbers."""
    from services.dfam import PrintParams  # noqa: PLC0415

    kw: dict[str, Any] = {"process": process}
    dfm = dfm or {}
    min_wall = _num(dfm.get("min_wall_mm"))
    if min_wall:
        kw["min_wall_mm"] = min_wall
    overhang = dfm.get("overhang") if isinstance(dfm.get("overhang"), dict) else {}
    if _num(overhang.get("max_deg")) is not None:
        kw["overhang_max_deg"] = _num(overhang.get("max_deg"))
    if _num(overhang.get("max_bridge_mm")):
        kw["max_bridge_mm"] = _num(overhang.get("max_bridge_mm"))
    cfg = dict(config or {})
    if resolved is not None:
        cfg = {**resolved.config, **cfg}
        try:
            polygon = resolved.bed_polygon()
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            kw["bed_size_mm"] = (max(xs) - min(xs), max(ys) - min(ys), float(resolved.max_height()))
        except Exception:  # noqa: BLE001 — a preset without a bed shape just skips build_volume
            pass
    if cfg:
        nozzle = _num(cfg.get("nozzle_diameter"))
        if nozzle:
            kw["nozzle_mm"] = nozzle
        layer = _num(cfg.get("layer_height"))
        if layer:
            kw["layer_height_mm"] = layer
        width = cfg.get("perimeter_extrusion_width") or cfg.get("extrusion_width")
        base = kw.get("nozzle_mm", 0.4)
        width_mm = _num(width)
        if width_mm and str(width).strip().endswith("%"):
            width_mm = base * width_mm / 100.0
        kw["line_width_mm"] = width_mm if width_mm else round(1.125 * base, 3)
        perimeters = _num(cfg.get("perimeters"))
        if perimeters:
            kw["walls"] = int(perimeters)
        top = _num(cfg.get("top_solid_layers"))
        if top and layer:
            kw["top_bottom_mm"] = max(top * layer, _num(cfg.get("top_solid_min_thickness"), 0.0))
        fill = _num(cfg.get("fill_density"))
        if fill is not None:
            kw["infill_fraction"] = fill / 100.0 if fill > 1.0 else fill
        density = _num(cfg.get("filament_density"))
        if density:
            kw["filament_density_g_cm3"] = density
        flow = _num(cfg.get("filament_max_volumetric_speed"))
        if flow:
            kw["volumetric_speed_mm3_s"] = min(flow, 15.0)
        kind = str(cfg.get("filament_type") or "").strip().strip('"')
        if kind:
            kw["material"] = kind
    if material:
        kw["material"] = material
    if nozzle_mm:
        kw["nozzle_mm"] = float(nozzle_mm)
    if layer_height_mm:
        kw["layer_height_mm"] = float(layer_height_mm)
    if bed_size_mm:
        kw["bed_size_mm"] = tuple(float(v) for v in bed_size_mm)
    return PrintParams(**kw)


def slicer_session():
    """(install, catalog) or (None, refusal-dict)."""
    from services.manufacturing.slicer import prusaslicer as ps  # noqa: PLC0415
    from services.manufacturing.slicer.contract import SliceRefusal  # noqa: PLC0415
    from services.manufacturing.slicer.vendor_bundle import ProfileCatalog  # noqa: PLC0415

    install = ps.discover()
    if isinstance(install, SliceRefusal):
        refusal = install.to_dict() if hasattr(install, "to_dict") else {"code": install.code}
        return None, {"ok": False, "reason": "slicer_not_installed", "refusal": refusal,
                      "install": ps.install_hint(),
                      "content": "No PrusaSlicer is installed. " + ps.install_hint()}
    if install.profiles_dir is None:
        return None, {"ok": False, "reason": "slicer_profiles_missing",
                      "slicer": install.to_dict(),
                      "content": "PrusaSlicer was found but its vendor profile directory was "
                                 "not; set CHUDWARE_SLICER_PROFILES to <PrusaSlicer>/profiles."}
    key = str(install.profiles_dir)
    catalog = _CATALOG_CACHE.get(key)
    if catalog is None:
        catalog = ProfileCatalog(install.profiles_dir)
        _CATALOG_CACHE.clear()
        _CATALOG_CACHE[key] = catalog
    return install, catalog


def pick_printer(catalog, printer: str, nozzle_mm: Optional[float]) -> tuple[Optional[dict], list]:
    matches = catalog.find_printers(printer, nozzle_mm=nozzle_mm, limit=6)
    return (matches[0] if matches else None), matches


def _goal_settings(goal: str, current: dict) -> dict:
    """Slicer keys a goal changes on top of the vendor preset (vendor otherwise wins)."""
    perimeters = int(_num(current.get("perimeters"), 3) or 3)
    if goal == "strength":
        return {"perimeters": str(max(perimeters, 4)), "fill_density": "35%",
                "fill_pattern": "cubic", "top_solid_layers": "6", "bottom_solid_layers": "5"}
    if goal == "speed":
        return {"perimeters": "2", "fill_density": "10%", "fill_pattern": "grid"}
    if goal == "material":
        return {"perimeters": "2", "fill_density": "10%", "fill_pattern": "adaptivecubic"}
    return {}


def _orientation_choice(mesh, params, *, orientation, build_dir, goal, preserve_faces,
                        strength_dir) -> dict:
    """{build_dir, yaw_deg, source, why, alternatives} for slice_part / the orientation tool."""
    from services.dfam import optimize_orientation  # noqa: PLC0415

    explicit = build_dir if build_dir is not None else (
        orientation if isinstance(orientation, (list, tuple)) else None)
    if explicit is not None:
        if len(explicit) != 3:
            raise ValueError("build_dir/orientation vector must be [x, y, z]")
        return {"build_dir": [float(v) for v in explicit], "yaw_deg": 0.0,
                "source": "caller", "why": "caller-specified build direction", "alternatives": []}
    mode = str(orientation or "auto").strip().lower()
    if mode in ("as_is", "asis", "current", "model"):
        return {"build_dir": [0.0, 0.0, 1.0], "yaw_deg": 0.0, "source": "as_is",
                "why": "model +Z kept as the build direction", "alternatives": []}
    if mode != "auto":
        raise ValueError("orientation must be 'auto', 'as_is', or an [x, y, z] build direction")
    ranked = optimize_orientation(mesh, params, top_k=4, goal=_GOAL_ORIENT.get(goal, "balanced"),
                                  preserve_faces=tuple(preserve_faces or ()),
                                  strength_dir=strength_dir)
    if not ranked:
        raise ValueError("orientation search produced no candidate")
    best = ranked[0]
    return {"build_dir": [float(v) + 0.0 for v in best["build_dir"]],
            "yaw_deg": best.get("yaw_deg", 0.0),
            "source": best.get("source", "auto"), "why": best.get("why", ""),
            "metrics": best.get("metrics", {}),
            "alternatives": [{"build_dir": row["build_dir"], "yaw_deg": row.get("yaw_deg", 0.0),
                              "score": row.get("score"), "why": row.get("why", ""),
                              "metrics": row.get("metrics", {})} for row in ranked[1:]]}


def _check_brief(rows: list[dict]) -> list[dict]:
    return [{"check": row.get("check"), "status": row.get("status"), "note": row.get("note"),
             **({"fix": row["fix"][:3]} if row.get("fix") else {})}
            for row in rows if row.get("status") in ("fail", "warn", "not_evaluated")]


def _write_binary_stl(path: Path, vertices, triangles) -> None:
    import numpy as np  # noqa: PLC0415

    tri = np.asarray(vertices, dtype=np.float64)[np.asarray(triangles, dtype=np.int64)]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    normals = np.divide(normals, lengths[:, None], out=np.zeros_like(normals),
                        where=lengths[:, None] > 0)
    record = np.zeros(len(tri), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    record["n"] = normals
    record["v"] = tri
    with open(path, "wb") as handle:
        handle.write(b"chudware slice_part".ljust(80, b"\0"))
        handle.write(np.uint32(len(tri)).tobytes())
        handle.write(record.tobytes())


def _slice_mesh_arrays(source: dict, work_dir: Path, raw_name: str = "model_frame.stl"):
    """(vertices, triangles) at slicing quality in the MODEL frame."""
    from services.dfam import load_mesh_file  # noqa: PLC0415

    if source["kind"] == "file":
        mesh = load_mesh_file(Path(source["path"]))
        return mesh.vertices, mesh.triangles
    raw = work_dir / raw_name
    cb.call("export_file", {"doc_id": source["doc_id"], "path": str(raw), "format": "stl",
                            "lin_deflection": _SLICE_LIN_DEFLECTION_MM,
                            "ang_deflection": _SLICE_ANG_DEFLECTION_RAD})
    mesh = load_mesh_file(raw)
    return mesh.vertices, mesh.triangles


def _oriented_arrays(source: dict, orient: dict, work_dir: Path, raw_name: str):
    """(vertices, triangles) in the chosen build pose, XY-centred on the origin, resting on z=0.

    Origin-centred because PrusaSlicer's --duplicate arrange fails with "No outline can be
    derived" on a mesh that sits far from the origin."""
    import numpy as np  # noqa: PLC0415
    from services.dfam import frame_for_build_dir  # noqa: PLC0415

    vertices, triangles = _slice_mesh_arrays(source, work_dir, raw_name)
    rotation = frame_for_build_dir(orient["build_dir"], orient["yaw_deg"])
    oriented = np.asarray(vertices, dtype=np.float64) @ np.asarray(rotation).T
    low, high = oriented.min(axis=0), oriented.max(axis=0)
    oriented = oriented - np.array([(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]])
    return oriented, triangles


_PART_KEYS = ("doc_id", "path", "copies", "orientation", "build_dir", "name", "preserve_faces",
              "strength_dir")
_MAX_OBJECTS = 64


def _slice_targets(*, doc_id, path, copies, orientation, build_dir, preserve_faces,
                   strength_dir, parts) -> list[dict]:
    """One {doc_id, path, copies, orientation, build_dir, …} per distinct part to slice."""
    copies = int(copies)
    if parts is None:
        if not 1 <= copies <= _MAX_OBJECTS:
            raise ValueError(f"copies must be 1..{_MAX_OBJECTS}")
        return [{"doc_id": doc_id, "path": path, "copies": copies, "orientation": orientation,
                 "build_dir": build_dir, "preserve_faces": preserve_faces,
                 "strength_dir": strength_dir, "name": None}]
    if doc_id or path:
        raise ValueError("pass either parts=[...] or doc_id/path, not both")
    if copies != 1 or build_dir is not None or preserve_faces or strength_dir is not None:
        raise ValueError("with parts=[...], give copies, build_dir, preserve_faces and "
                         "strength_dir inside each part entry")
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty list of {doc_id or path, copies, …}")
    targets = []
    for index, item in enumerate(parts):
        if not isinstance(item, dict):
            raise ValueError(f"parts[{index}] must be an object like {{'doc_id': ..., 'copies': 2}}")
        unknown = sorted(set(item) - set(_PART_KEYS))
        if unknown:
            raise ValueError(f"parts[{index}]: unknown keys {unknown}; allowed {list(_PART_KEYS)}")
        if bool(item.get("doc_id")) == bool(item.get("path")):
            raise ValueError(f"parts[{index}] needs exactly one of doc_id or path")
        count = int(item.get("copies", 1))
        if not 1 <= count <= _MAX_OBJECTS:
            raise ValueError(f"parts[{index}].copies must be 1..{_MAX_OBJECTS}")
        targets.append({"doc_id": item.get("doc_id"), "path": item.get("path"), "copies": count,
                        "orientation": item.get("orientation", orientation),
                        "build_dir": item.get("build_dir"),
                        "preserve_faces": item.get("preserve_faces"),
                        "strength_dir": item.get("strength_dir"), "name": item.get("name")})
    total = sum(t["copies"] for t in targets)
    if total > _MAX_OBJECTS:
        raise ValueError(f"a plate holds at most {_MAX_OBJECTS} objects; these parts ask for {total}")
    return targets


def _prepare_target(target: dict, params, goal: str) -> dict:
    """Load, orient and analyse one part: {label, source, orient, analysis, rows, copies}."""
    from services.dfam import analyze_printability  # noqa: PLC0415

    mesh, label, source = load_target(target.get("doc_id"), target.get("path"))
    orient = _orientation_choice(mesh, params, orientation=target.get("orientation") or "auto",
                                 build_dir=target.get("build_dir"), goal=goal,
                                 preserve_faces=target.get("preserve_faces"),
                                 strength_dir=target.get("strength_dir"))
    analysis = analyze_printability(mesh, params, orient["build_dir"], yaw_deg=orient["yaw_deg"])
    return {"label": str(target.get("name") or label), "source": source, "orient": orient,
            "analysis": analysis, "rows": {row["check"]: row for row in analysis["checks"]},
            "copies": target["copies"]}


def _support_brim_settings(prepared: list[dict], params, supports, brim) -> tuple[dict, bool, dict]:
    """Plate-wide support/brim keys decided from every part's DFAM rows.

    PrusaSlicer applies these per job; auto supports still only grow under the overhangs the
    slicer finds, so a part that needs none stays clean."""
    mode = str(supports or "auto").strip().lower()
    if mode not in ("auto", "on", "off", "buildplate"):
        raise ValueError("supports must be auto | on | off | buildplate")

    def needs_support(rows):
        row = rows.get("support") or {}
        return row.get("status") in ("warn", "fail") or float(
            (row.get("metrics") or {}).get("support_volume_mm3") or 0.0) > 1.0

    support_for = [p["label"] for p in prepared if needs_support(p["rows"])]
    enable = mode in ("on", "buildplate") or (mode == "auto" and bool(support_for))
    settings: dict[str, str] = {}
    if enable:
        settings.update({
            "support_material": "1", "support_material_auto": "1",
            "support_material_style": "organic",
            "support_material_threshold": f"{90.0 - params.overhang_max_deg:g}",
            "support_material_buildplate_only": "1" if mode == "buildplate" else "0"})
        # Vendor presets skip support under anything the slicer calls a bridge; a
        # span the analysis measured beyond twice the bridge limit then prints its
        # perimeters in mid-air. Support those spans instead.
        if any((p["rows"].get("bridges") or {}).get("status") == "fail" for p in prepared):
            settings["dont_support_bridges"] = "0"
    else:
        settings["support_material"] = "0"
    brim_for = [p["label"] for p in prepared
                if (p["rows"].get("bed_adhesion") or {}).get("status") in ("warn", "fail")]
    if isinstance(brim, (int, float)) and not isinstance(brim, bool):
        settings["brim_width"] = f"{float(brim):g}"
    elif str(brim).strip().lower() == "auto":
        if brim_for:
            settings["brim_width"] = "5"
            settings["brim_type"] = "outer_only"
    else:
        raise ValueError("brim must be 'auto' or a width in mm")
    return settings, enable, {"support_for": support_for, "brim_for": brim_for}


def _layout_plate(prepared: list[dict], config: dict, resolved, work_dir: Path,
                  sequential: bool) -> tuple[dict, list[Path], list[dict]]:
    """Plan the plate and write one placed STL per object, in print order.

    Returns (plan, mesh_paths, objects) or raises PlateRefusal. The STL file name is the
    object's label in the G-code (``; printing object 01_<part>_1.stl``), so the order and
    part of every printed object can be read back from the toolpath."""
    import numpy as np  # noqa: PLC0415
    from services.manufacturing.slicer.plate import PlateItem, footprint_hull, plan_plate  # noqa: PLC0415

    items, arrays = [], {}
    for index, part in enumerate(prepared):
        oriented, triangles = _oriented_arrays(part["source"], part["orient"], work_dir,
                                               f"model_frame_{index:02d}.stl")
        arrays[part["label"]] = (oriented, triangles)
        hull = footprint_hull(oriented[:, :2])
        height = float(oriented[:, 2].max())
        for copy in range(1, part["copies"] + 1):
            items.append(PlateItem(f"{part['label']}#{copy}", part["label"], copy, hull, height))
    brim_mm = _num(config.get("brim_width"), 0.0) or 0.0
    if str(config.get("brim_type") or "").strip() == "no_brim":
        brim_mm = 0.0
    skirts = int(_num(config.get("skirts"), 0.0) or 0)
    skirt_mm = (_num(config.get("skirt_distance"), 0.0) or 0.0) + 0.45 * skirts if skirts else 0.0
    plan = plan_plate(items, bed_polygon=resolved.bed_polygon(), sequential=sequential,
                      clearance_radius_mm=_num(config.get("extruder_clearance_radius")),
                      clearance_height_mm=_num(config.get("extruder_clearance_height")),
                      brim_mm=brim_mm, skirt_mm=skirt_mm)
    paths = []
    for obj in plan["objects"]:
        oriented, triangles = arrays[obj["part"]]
        placed = oriented + np.array([obj["offset_mm"][0], obj["offset_mm"][1], 0.0])
        stl = work_dir / f"{obj['print_order']:02d}_{_slug(obj['part'])}_{obj['copy']}.stl"
        _write_binary_stl(stl, placed, triangles)
        obj["gcode_label"] = stl.name
        obj["mesh_sha256"] = _sha256_file(stl)
        paths.append(stl)
    return plan, paths, plan["objects"]


def _output_dir(output_dir: Optional[str], stem: str, printer_slug: str) -> Path:
    if output_dir:
        directory = Path(_sanitize_output_path(os.path.expanduser(output_dir)))
    else:
        root = os.environ.get("CHUDWARE_PROJECT_SAVE_DIR")
        if root:
            directory = Path(os.path.abspath(os.path.expanduser(root))) / "print" / printer_slug
        else:
            import tempfile  # noqa: PLC0415
            directory = Path(tempfile.mkdtemp(prefix="chudware_print_"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in text)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "printer"


def _verify(gcode: Path, resolved, *, material, nozzle_c, bed_c, detail=False) -> dict:
    from services.manufacturing.slicer.gcode_check import verify_gcode  # noqa: PLC0415

    kwargs: dict[str, Any] = {"material": material, "expect_nozzle_c": nozzle_c,
                              "expect_bed_c": bed_c, "detail": detail}
    if resolved is not None:
        kwargs["bed_polygon"] = resolved.bed_polygon()
        kwargs["max_height_mm"] = resolved.max_height()
        kwargs["nozzle_mm"] = resolved.nozzle_mm()
    return verify_gcode(gcode, **kwargs)


# ----------------------------------------------------------------------------- tools

def register_print_tools(mcp: FastMCP) -> None:
    """3D-print preparation tools (family ``cam``)."""

    @mcp.tool()
    def slicer_status() -> str:
        """Which slicer slice_part will run (PrusaSlicer, subprocess only), or how to install it.

        Returns {ok, slicer:{kind, version, source, profiles_dir, identity}, vendors:[...]} when a
        stock PrusaSlicer is usable, else {ok:false, reason, install} with the exact command
        (Linux: a per-user Flathub install, no root). Never download or hand-assemble a slicer
        yourself — slice_part only trusts the discovered install."""
        try:
            install, catalog = slicer_session()
            if install is None:
                return _dumps(catalog)
            return _dumps({"ok": True, "slicer": install.to_dict(),
                           "vendors": catalog.vendors(),
                           "content": f"PrusaSlicer {install.version} via {install.source}; "
                                      f"{len(catalog.vendors())} vendor profile bundles."})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_print_profiles(printer: Optional[str] = None, material: Optional[str] = None,
                            nozzle_mm: Optional[float] = None, limit: int = 8) -> str:
        """Printer / print / filament presets from the installed slicer's vendor bundles.

        printer: free text ("ender 3", "prusa mk4", "bambu x1c"); omitted -> vendor list.
        With a printer: the best matching printer presets (bed, height, nozzle) and, for the
        top match, its compatible print presets (layer heights) and filament presets
        (filtered by `material`, e.g. "PLA", "PETG"). Use the exact names with slice_part's
        printer/print_preset/filament_preset when the default pick is not what you want."""
        try:
            install, catalog = slicer_session()
            if install is None:
                return _dumps(catalog)
            if not printer:
                return _dumps({"ok": True, "vendors": catalog.vendors(),
                               "content": "Pass printer='<make model>' to list its presets."})
            best, matches = pick_printer(catalog, printer, nozzle_mm)
            if best is None:
                return _dumps({"ok": False, "reason": "printer_not_found", "query": printer,
                               "vendors": catalog.vendors()})
            prints = catalog.compatible_prints(best["name"])
            filaments = catalog.compatible_filaments(best["name"])
            if material:
                wanted = material.strip().lower()
                filaments = [f for f in filaments if wanted in f.lower()] or filaments
            limit = max(1, min(int(limit), 50))
            return _dumps({
                "ok": True, "printers": matches[:limit],
                "selected": best["name"],
                "default_print": catalog.default_print(best["name"]),
                "default_filament": catalog.default_filament(best["name"], material or "PLA"),
                "prints": prints[:limit * 4], "prints_total": len(prints),
                "filaments": filaments[:limit * 2], "filaments_total": len(filaments)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def optimize_print_orientation(doc_id: Optional[str] = None, path: Optional[str] = None,
                                   printer: Optional[str] = None, goal: str = "balanced",
                                   preserve_faces: Optional[list] = None,
                                   strength_dir: Optional[list] = None,
                                   top_k: int = 5) -> str:
        """Rank FDM print orientations by measurement (deterministic; no slicer needed).

        Candidates: the 6 axis directions, stable convex-hull resting faces and large planar CAD
        faces. Each is scored on estimated support volume, overhang area, height/time, material,
        bed contact, tip risk and bed fit. goal: balanced | min_support | min_time | strength.
        preserve_faces: face sids (cosmetic/sealing/precision surfaces) that must not touch
        supports or the bed. strength_dir: [x,y,z] main load direction in the MODEL frame — it
        is kept in the layer plane (layer lines are the weak axis). printer: bed size for the fit
        check. Returns ranked rows {build_dir, yaw_deg, score, metrics, why}; pass the winner's
        build_dir to slice_part(build_dir=...) or let slice_part(orientation="auto") pick it."""
        try:
            from services.dfam import optimize_orientation  # noqa: PLC0415

            mesh, label, source = load_target(doc_id, path)
            resolved = None
            if printer:
                install, catalog = slicer_session()
                if install is None:
                    return _dumps(catalog)
                best, _ = pick_printer(catalog, printer, None)
                if best is None:
                    return _dumps({"ok": False, "reason": "printer_not_found", "query": printer})
                resolved = catalog.resolve(best["name"])
            params = print_params(resolved=resolved, dfm=dfm_profile_params("fdm"))
            ranked = optimize_orientation(mesh, params, top_k=max(1, min(int(top_k), 12)),
                                          goal=goal, preserve_faces=tuple(preserve_faces or ()),
                                          strength_dir=strength_dir)
            return _dumps({"ok": True, "part": label, "source": source, "goal": goal,
                           "printer": resolved.printer if resolved is not None else None,
                           "ranked": ranked,
                           "content": (f"best build_dir {ranked[0]['build_dir']}: "
                                       f"{ranked[0].get('why', '')}") if ranked else
                                      "no orientation candidate"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def slice_part(doc_id: Optional[str] = None, path: Optional[str] = None,
                   printer: Optional[str] = None, material: str = "PLA",
                   goal: str = "balanced", orientation: Any = "auto",
                   build_dir: Optional[list] = None, supports: str = "auto",
                   brim: Any = "auto", layer_height_mm: Optional[float] = None,
                   infill_percent: Optional[float] = None, walls: Optional[int] = None,
                   nozzle_temp_c: Optional[float] = None, bed_temp_c: Optional[float] = None,
                   nozzle_mm: Optional[float] = None, copies: int = 1,
                   print_preset: Optional[str] = None, filament_preset: Optional[str] = None,
                   overrides: Optional[dict] = None, preserve_faces: Optional[list] = None,
                   strength_dir: Optional[list] = None, output_dir: Optional[str] = None,
                   name: Optional[str] = None, export_3mf: bool = True,
                   parts: Optional[list[dict]] = None,
                   complete_individual_objects: bool = False) -> str:
        """Slice one part, or several parts on one plate, to printer-ready G-code with STOCK PrusaSlicer and prove the result.

        This is the ONLY supported way to make G-code. Never download a slicer, never write a
        slicer .ini by hand, never pass a design-time build_dir you did not measure.

        Target: doc_id (open part; default current) or path (.stl/.3mf/.obj/.ply).
        printer (required): the user's printer as free text, resolved against the installed
          vendor presets ("ender 3", "prusa mk4", "voron 2.4 350") — ask the user when you
          do not know it; never assume one. material: PLA|PETG|ABS|ASA|TPU|… (vendor filament
          preset). print_preset/filament_preset pin exact names from list_print_profiles.
        goal: balanced (vendor defaults: 3 walls, 15% infill) | strength (4+ walls, 35% cubic,
          thicker skins) | speed (0.28 mm layers, 2 walls, 10%) | material (2 walls, 10%
          adaptive cubic) | detail (0.12 mm layers).
        orientation: "auto" (default; measured by optimize_print_orientation for the goal) |
          "as_is" | [x,y,z]; build_dir=[x,y,z] is the same as an explicit vector.
        supports: auto (enabled with organic supports when the DFAM analysis finds unsupported
          overhangs at the chosen orientation) | on | off | buildplate.
        brim: auto (5 mm when the part is tall/narrow or has a small contact patch) | mm.
        nozzle_temp_c/bed_temp_c, layer_height_mm, infill_percent, walls: explicit user
          numbers win over goal and vendor. overrides: any other PrusaSlicer keys
          (validated; unknown keys are refused with close matches).
        copies: identical instances arranged on one plate.

        Several parts on ONE plate (one G-code file): parts=[{doc_id or path, copies?,
          orientation?, build_dir?, name?, preserve_faces?, strength_dir?}, ...] instead of
          doc_id/path. Each part is oriented and analysed on its own; supports/brim and the
          print settings apply to the whole plate; name= names the plate (default "plate").
          Objects are laid out deterministically, spaced and checked against the bed.
        complete_individual_objects: PrusaSlicer's "Complete individual objects"
          (complete_objects=1): each object prints to completion before the next starts, so
          one failure does not scrap the plate. Works with parts=[...] or copies. Spacing
          becomes the printer's extruder_clearance_radius (Ender-3: 55 mm) plus brims, the
          print order is ascending height, and at most ONE object may be taller than
          extruder_clearance_height (Ender-3: 25 mm) — it prints last. A plate that cannot
          meet that is refused naming the objects (split the plate or print layer by layer);
          gcode_check.sequential_clearance then proves from the toolpath that no move runs
          the hotend or gantry into a finished object.

        Writes <output_dir>/<name>.gcode (+ .3mf project for review in PrusaSlicer, + a
        .print.json manifest) — default <project>/print/<printer>/. Returns printer/presets,
        orientation (with alternatives), effective settings, slicer stats (time, grams,
        layers, support share), printability issues (dfm-style rows) and gcode_check
        (verify_gcode); a plate also returns plate.objects (print order, position, height)
        and per-part orientation/printability. print_ready is true only when the G-code
        verification passes and the printability analysis has no failure. Report
        print_ready=false honestly and fix the design (dfm_check lists the faces) or
        settings, then slice again. The manifest_path of a print-ready result is what
        publish_print_media takes to put it on printer media (one job per plate;
        print_media_inventory first); never copy the G-code there by hand."""
        started = time.monotonic()
        try:
            from services.manufacturing.slicer import prusaslicer as ps  # noqa: PLC0415
            from services.manufacturing.slicer.plate import PlateRefusal  # noqa: PLC0415

            goal = str(goal or "balanced").strip().lower()
            if goal not in _GOALS:
                raise ValueError(f"goal must be one of {', '.join(_GOALS)}")
            targets = _slice_targets(doc_id=doc_id, path=path, copies=copies,
                                     orientation=orientation, build_dir=build_dir,
                                     preserve_faces=preserve_faces, strength_dir=strength_dir,
                                     parts=parts)
            sequential = bool(complete_individual_objects)
            plate_mode = parts is not None or sequential
            if not printer or not str(printer).strip():
                return _dumps({"ok": False, "reason": "printer_required",
                               "content": "Ask the user which printer (make and model, nozzle "
                                          "if not 0.4 mm) and pass printer=...; "
                                          "list_print_profiles shows what resolves."})
            install, catalog = slicer_session()
            if install is None:
                return _dumps(catalog)
            best, matches = pick_printer(catalog, printer, nozzle_mm)
            if best is None:
                return _dumps({"ok": False, "reason": "printer_not_found", "query": printer,
                               "vendors": catalog.vendors(),
                               "content": f"No vendor printer preset matches {printer!r}; "
                                          "call list_print_profiles(printer=...)"})
            printer_name = best["name"]
            target_layer = layer_height_mm or _GOAL_LAYER[goal]
            print_name = print_preset or catalog.default_print(printer_name,
                                                               layer_height_mm=target_layer)
            filament_name = filament_preset or catalog.default_filament(printer_name, material)
            resolved = catalog.resolve(printer_name, print_name, filament_name)

            dfm = dfm_profile_params("fdm")
            settings: dict[str, str] = dict(_goal_settings(goal, resolved.config))
            if infill_percent is not None:
                settings["fill_density"] = f"{float(infill_percent):g}%"
            if walls is not None:
                settings["perimeters"] = str(int(walls))
            if nozzle_temp_c is not None:
                settings["temperature"] = settings["first_layer_temperature"] = \
                    f"{float(nozzle_temp_c):g}"
            if bed_temp_c is not None:
                settings["bed_temperature"] = settings["first_layer_bed_temperature"] = \
                    f"{float(bed_temp_c):g}"
            params = print_params(resolved=resolved, dfm=dfm, material=material,
                                  config=settings)

            prepared = [_prepare_target(target, params, goal) for target in targets]
            seen: dict[str, int] = {}
            for part in prepared:  # plate object labels must be unique
                count = seen.get(part["label"], 0) + 1
                seen[part["label"]] = count
                if count > 1:
                    part["label"] = f"{part['label']}_{count}"
            for part in prepared:
                volume_row = part["rows"].get("build_volume") or {}
                if volume_row.get("status") == "fail":
                    return _dumps({"ok": False, "reason": "does_not_fit_printer",
                                   "printer": printer_name, "part": part["label"],
                                   "orientation": part["orient"], "build_volume": volume_row,
                                   "content": f"{part['label']}: "
                                              f"{volume_row.get('note') or 'part exceeds the bed'}"})

            support_settings, enable, reasons = _support_brim_settings(prepared, params,
                                                                       supports, brim)
            settings.update(support_settings)
            # Plain-text G-code so verify_gcode can read every move (MK4-class presets
            # default to binary .bgcode, which the printer also accepts as text).
            settings["binary_gcode"] = "0"
            # Detect bridging perimeters: overhanging walls get bridge flow, speed and fan.
            # Creality-class vendor presets ship it off, which prints unsupported outer
            # walls at full perimeter speed.
            settings["overhangs"] = "1"
            if plate_mode:
                settings["complete_objects"] = "1" if sequential else "0"
            if overrides:
                requested = {str(key): str(value) for key, value in overrides.items()}
                refusal = ps.validate_overrides(install, resolved.config, requested)
                if refusal is not None:
                    return _dumps({"ok": False, "reason": "invalid_override",
                                   "refusal": refusal.to_dict(),
                                   "content": refusal.message})
                if "complete_objects" in requested:
                    raise ValueError("set Complete individual objects with "
                                     "complete_individual_objects=True, not overrides: the "
                                     "plate layout and clearance checks depend on it")
                settings.update(requested)

            first = prepared[0]
            if parts is not None:
                stem = _project_save_stem(name or "plate", "plate")
            else:
                stem = _project_save_stem(name or first["label"],
                                          first["source"].get("doc_id") or first["label"])
            out_dir = _output_dir(output_dir, stem, _slug(printer_name))
            work_dir = out_dir / f".{stem}.slice"
            work_dir.mkdir(parents=True, exist_ok=True)
            config = {**resolved.config, **settings}
            plan = None
            if plate_mode:
                try:
                    plan, mesh_paths, objects = _layout_plate(prepared, config, resolved,
                                                              work_dir, sequential)
                except PlateRefusal as refusal:
                    return _dumps({**refusal.to_dict(), "printer": printer_name,
                                   "complete_individual_objects": sequential})
                center_xy, instance_copies = None, 1
                mesh_sha = hashlib.sha256("".join(
                    o["mesh_sha256"] for o in objects).encode()).hexdigest()
            else:
                oriented, triangles = _oriented_arrays(first["source"], first["orient"],
                                                       work_dir, "model_frame.stl")
                oriented_stl = work_dir / f"{stem}.oriented.stl"
                _write_binary_stl(oriented_stl, oriented, triangles)
                mesh_paths, center_xy = [oriented_stl], resolved.bed_center()
                instance_copies = first["copies"]
                mesh_sha = _sha256_file(oriented_stl)

            gcode_path = out_dir / f"{stem}.gcode"
            project = out_dir / f"{stem}.3mf" if export_3mf else None
            run = ps.slice_mesh(install, mesh_paths=mesh_paths, config=config,
                                output_gcode=gcode_path, work_dir=work_dir,
                                center_xy=center_xy, copies=instance_copies,
                                export_3mf=project)
            if not run.ok:
                refusal = getattr(run, "refusal", None)
                return _dumps({"ok": False, "reason": "slicer_failed",
                               "printer": printer_name,
                               "presets": {"print": print_name, "filament": filament_name},
                               "refusal": refusal.to_dict() if hasattr(refusal, "to_dict")
                               else refusal,
                               "warnings": run.warnings, "stderr_tail": run.stderr_tail,
                               "stdout_tail": run.stdout_tail, "cmd": run.cmd})

            # Only temperatures the user stated are exact expectations; vendor filament
            # presets legitimately differ between the first and later layers (e.g. 210/205),
            # and those still have to sit inside the material window.
            check = _verify(Path(run.gcode_path), resolved, material=params.material,
                            nozzle_c=nozzle_temp_c, bed_c=bed_temp_c)
            # Overhang/bridge failures are what the enabled supports are for; everything
            # else (vanishing walls, trapped support, sealed cavities, floating bodies, bed
            # fit) survives slicing and blocks print_ready.
            solved = {"overhang", "bridges"} if enable else set()
            printability_fail = [{**r, "part": p["label"]} for p in prepared
                                 for r in p["analysis"]["checks"]
                                 if r.get("status") == "fail" and r.get("check") not in solved]
            print_ready = bool(check.get("ok")) and not printability_fail
            stats = check.get("stats") or {}
            effective = {k: config.get(k) for k in (
                "layer_height", "first_layer_height", "perimeters", "fill_density",
                "fill_pattern", "top_solid_layers", "bottom_solid_layers", "support_material",
                "support_material_style", "support_material_threshold", "dont_support_bridges",
                "overhangs", "brim_width",
                "temperature", "first_layer_temperature", "bed_temperature",
                "first_layer_bed_temperature", "nozzle_diameter", "retract_length")}
            if plate_mode:
                effective.update({k: config.get(k) for k in (
                    "complete_objects", "extruder_clearance_radius", "extruder_clearance_height")})
            gcode_issues = _check_brief(check.get("checks") or [])
            if plate_mode:
                part_rows = [{"part": p["label"], "copies": p["copies"], "source": p["source"],
                              "orientation": {k: p["orient"][k] for k in
                                              ("build_dir", "yaw_deg", "source", "why")},
                              "estimate": p["analysis"].get("estimate"),
                              "printability": {"status": p["analysis"].get("status"),
                                               "issues": _check_brief(p["analysis"]["checks"])}}
                             for p in prepared]
                statuses = [p["analysis"].get("status") for p in prepared]
                printability = {
                    "status": next((s for s in ("fail", "warn") if s in statuses), statuses[0]),
                    "issues": [{**row, "part": p["label"]} for p in prepared
                               for row in _check_brief(p["analysis"]["checks"])]}
                plate_info = {**plan, "parts": part_rows, "support_for": reasons["support_for"],
                              "brim_for": reasons["brim_for"]}
                label = stem
                source = {"kind": "plate", "parts": [
                    {"part": p["label"], "source": p["source"], "copies": p["copies"]}
                    for p in prepared]}
                orientation_out = None
                estimate = [{"part": p["label"], "estimate": p["analysis"].get("estimate")}
                            for p in prepared]
                total_copies = sum(p["copies"] for p in prepared)
            else:
                printability = {"status": first["analysis"].get("status"),
                                "issues": _check_brief(first["analysis"]["checks"])}
                plate_info = None
                label, source = first["label"], first["source"]
                orientation_out = {k: first["orient"][k]
                                   for k in ("build_dir", "yaw_deg", "source", "why")}
                estimate = first["analysis"].get("estimate")
                total_copies = first["copies"]
            manifest = {
                "schema": "chudware.print_job/1",
                "part": label, "source": source, "copies": total_copies,
                "printer": {"name": printer_name, "vendor": resolved.vendor,
                            "bed_polygon": resolved.bed_polygon(),
                            "max_height_mm": resolved.max_height()},
                "presets": {"print": resolved.print_, "filament": resolved.filament},
                "goal": goal, "settings_changed": settings, "effective": effective,
                "orientation": orientation_out,
                **({"plate": plate_info} if plate_info is not None else {}),
                "slicer": install.to_dict(),
                "config_sha256": _sha256_file(Path(run.config_path)),
                "mesh_sha256": mesh_sha,
                "gcode": str(run.gcode_path), "gcode_sha256": _sha256_file(Path(run.gcode_path)),
                "project_3mf": str(run.project_3mf) if run.project_3mf else None,
                "stats": stats, "estimate": estimate,
                "printability": printability,
                "gcode_check": {"status": check.get("status"), "issues": gcode_issues},
                "slicer_warnings": run.warnings, "print_ready": print_ready,
            }
            manifest_path = out_dir / f"{stem}.print.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            next_steps = []
            if printability_fail:
                next_steps.append("fix the failing printability rows in CAD (dfm_check lists "
                                  "faces + features), then slice again")
            if not check.get("ok"):
                next_steps.append("G-code verification failed: read gcode_check.issues; "
                                  "change orientation/supports or the design before printing")
            if any(row.get("check") == "sequential_clearance" and row.get("status") == "fail"
                   for row in gcode_issues):
                next_steps.append("sequential_clearance failed: the hotend or gantry would hit "
                                  "a finished object; split the plate or print it layer by "
                                  "layer (complete_individual_objects=False)")
            if print_ready:
                next_steps.append(
                    f"to deliver on a printer USB/SD card: print_media_inventory(root=<mount>) "
                    f"first, compare its printable files with the project's COMPLETE job set, "
                    f"then publish_print_media(root, project, jobs=[{{part_id, manifest_path}} "
                    f"for EVERY part{' or plate' if plate_mode else ''}, this one: "
                    f"{manifest_path}], expected_inventory="
                    f"<inventory_token>, media_idle=True only after the user confirms no "
                    f"printer is using the media). Never cp/fs_write G-code onto media; "
                    f"report the returned PRINT/<project>/ filenames and leftover unmanaged "
                    f"printables")
            hours = f"{(stats.get('print_time_s') or 0) / 3600:.2f} h"
            if plate_mode:
                mode_text = ("printed one at a time (Complete individual objects)" if sequential
                             else "printed together layer by layer")
                headline = (f"plate {stem}: {len(plan['objects'])} objects of "
                            f"{len(prepared)} parts, {mode_text}; {run.gcode_path} — ")
            else:
                headline = f"{run.gcode_path} — "
            result = {
                "ok": True, "print_ready": print_ready, "part": label,
                "gcode_path": str(run.gcode_path),
                "project_3mf": str(run.project_3mf) if run.project_3mf else None,
                "manifest_path": str(manifest_path),
                "printer": {"name": printer_name, "vendor": resolved.vendor,
                            "other_matches": [m["name"] for m in matches[1:4]]},
                "presets": {"print": resolved.print_, "filament": resolved.filament},
                "goal": goal, "settings": effective,
            }
            if plate_mode:
                result["plate"] = {k: plate_info[k] for k in (
                    "complete_individual_objects", "objects", "gap_mm", "min_object_gap_mm",
                    "clearance", "parts", "support_for", "brim_for")}
            else:
                alternatives = first["orient"].get("alternatives") or []
                result["orientation"] = {**orientation_out, "alternatives": alternatives[:2]}
            result.update({
                "stats": stats, "estimate": estimate, "printability": printability,
                "gcode_check": {"status": check.get("status"), "issues": gcode_issues},
                "slicer_warnings": run.warnings, "next_steps": next_steps,
                "elapsed_s": round(time.monotonic() - started, 1),
                "content": (f"{'PRINT-READY' if print_ready else 'NOT print-ready'}: "
                            f"{headline}{hours}, "
                            f"{stats.get('filament_g', '?')} g on {printer_name}"),
            })
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def verify_gcode(path: str, printer: Optional[str] = None, material: Optional[str] = None,
                     nozzle_temp_c: Optional[float] = None, bed_temp_c: Optional[float] = None,
                     detail: bool = False) -> str:
        """Deterministically check a G-code file before it goes to a printer.

        Checks: motion inside the printer's bed polygon and height (printer resolves through
        the vendor presets), homing before motion, no extrusion before the hotend is at
        temperature, temperatures inside the material window and equal to the requested
        nozzle/bed values, first-layer height, layer heights <= 0.8 x nozzle, E-mode
        consistency, retraction plausibility, part-cooling fan, heaters off at the end, and a
        per-layer FLOATING-EXTRUSION scan: extrusion more than one line width away from
        anything in the layer below (bridges/overhang perimeters reported separately) — the
        mid-air plastic that turns into spaghetti. For "Complete individual objects" files
        (complete_objects=1), sequential_clearance checks every move after an object is
        finished against it: hotend inside extruder_clearance_radius or gantry below its top
        fails. Returns {ok, status, checks:[...], stats}."""
        try:
            resolved = None
            if printer:
                install, catalog = slicer_session()
                if install is None:
                    return _dumps(catalog)
                best, _ = pick_printer(catalog, printer, None)
                if best is None:
                    return _dumps({"ok": False, "reason": "printer_not_found", "query": printer})
                resolved = catalog.resolve(best["name"])
            file_path = Path(os.path.abspath(os.path.expanduser(path)))
            if not file_path.is_file():
                raise ValueError(f"G-code file not found: {file_path}")
            result = _verify(file_path, resolved, material=material, nozzle_c=nozzle_temp_c,
                             bed_c=bed_temp_c, detail=detail)
            failing = [r for r in result.get("checks") or [] if r.get("status") == "fail"]
            return _dumps({**result, "path": str(file_path),
                           "printer": resolved.printer if resolved is not None else None,
                           "content": ("G-code verification PASSED" if result.get("ok") else
                                       "G-code verification FAILED: " + "; ".join(
                                           str(r.get("note")) for r in failing[:4]))})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def print_media_inventory(root: str, offset: int = 0, limit: int = 200) -> str:
        """Read-only inventory of printer media (USB stick / SD card): every file, which G-code is selectable.

        Changes nothing. Call it FIRST, before any delivery, and again to read back what a
        publish left. root: the mounted media root (e.g. /run/media/<user>/<LABEL>); it must
        already exist — a missing mount is reported, never created. Walks the whole real file
        tree without following symlinks and returns rows {path (relative to root), size,
        sha256, printable (.gcode/.gco/.g/.bgcode, any case), managed status current |
        superseded | modified}, duplicate groups, the managed revision per PRINT/<project>/,
        and inventory_token — a digest of the WHOLE drive, identical on every page.
        offset/limit page the rows (total, truncated). Compare the printable rows against the
        complete job set you intend to deliver: the printer shows names only, no timestamps, so
        a stale or duplicate G-code is indistinguishable from the current one on its screen.
        Never decide which file is current from mtime. Pass inventory_token unchanged to
        publish_print_media."""
        try:
            from services.manufacturing.print_media import inventory_print_media  # noqa: PLC0415

            return _dumps(inventory_print_media(root, offset=offset, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def publish_print_media(root: str, project: str, jobs: list[dict],
                            expected_inventory: str, media_idle: bool = False,
                            retire_paths: Optional[list[str]] = None) -> str:
        """Publish a project's COMPLETE verified print set to printer media; retire its old managed G-code.

        The one supported way to put G-code on a printer stick — never cp/rsync/fs_write
        G-code onto media (nothing can stop a shell copy; this tool is what makes the result
        verifiable). Mutates the media. Prerequisites the tool cannot detect for you:
        media_idle=True only after the USER confirmed the media is not in a printer that is
        printing or reading it (ejected/detached, or the printer idle); default False refuses.
        expected_inventory: the inventory_token from print_media_inventory on this root; any
        change since then refuses, so inventory, compare, then publish.

        project: the canonical folder name — files land in PRINT/<project>/.
        jobs: the COMPLETE desired set for the project, one {part_id, manifest_path,
          gcode_path?} per part: part_id a stable identifier (same part, same id across
          revisions), manifest_path the slice_part .print.json, gcode_path only when the
          saved G-code moved since slicing. Each needs print_ready true and the G-code
          sha256 equal to the manifest's gcode_sha256 — nothing is resliced or relaxed. A
          part omitted from jobs is retired, not kept.
        retire_paths: extra relative paths (e.g. old hand-copied G-code from the
          inventory's unmanaged printables) the user reviewed and approved for removal;
          nothing else unrecognised is touched.

        Files are named R<rev:03><part:02>_<PART>.gcode (e.g. R00101_UPPER_ARM.gcode): the
        first six characters encode release + part, so they stay unique and ordered even on
        a printer that truncates FAT short names. The same content set is idempotent (no new
        revision). Every file is staged and read back by sha256 before anything is retired;
        previous managed revisions of this project go into a non-printable ZIP archive; a
        sidecar manifest maps part_id -> filename with source hashes and warnings. Returns
        the exact current printable files, what was archived/replaced, unmanaged printables
        still on the media and warnings; a failure says what state the media is in. Report
        those filenames and the PRINT/<project>/ path verbatim, plus any remaining
        unmanaged printables — do not call the media clean while they exist."""
        try:
            from services.manufacturing.print_media import (  # noqa: PLC0415
                publish_print_media as publish,
            )

            return _dumps(publish(root, project, jobs, expected_inventory,
                                  media_idle=media_idle, retire_paths=retire_paths))
        except Exception as e:  # noqa: BLE001
            return _err(e)
