"""3D-print preparation: deterministic printability, orientation, real slicing, G-code proof.

Family ``cam`` (additive CAM). Five tools:

* ``slicer_status`` — which PrusaSlicer the pipeline will run, or the exact install step.
* ``list_print_profiles`` — vendor printer/print/filament presets read from the installed
  slicer (never copied into this repo).
* ``optimize_print_orientation`` — deterministic orientation ranking (support volume, time,
  bed contact, tip risk, bed fit, preserved faces, strength direction).
* ``slice_part`` — the one supported way to produce G-code: vendor presets for the named
  printer + goal-driven settings, auto orientation and supports from the DFAM analysis,
  STOCK PrusaSlicer as an out-of-process subprocess (AGPL boundary: files and argv only),
  then ``verify_gcode`` on the result and a manifest beside it.
* ``verify_gcode`` — deterministic proof of an existing G-code file (bounds, temperatures,
  cold extrusion, homing, layer heights, and per-layer mid-air extrusion detection).

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


def _slice_mesh_arrays(source: dict, work_dir: Path):
    """(vertices, triangles) at slicing quality in the MODEL frame."""
    from services.dfam import load_mesh_file  # noqa: PLC0415

    if source["kind"] == "file":
        mesh = load_mesh_file(Path(source["path"]))
        return mesh.vertices, mesh.triangles
    raw = work_dir / "model_frame.stl"
    cb.call("export_file", {"doc_id": source["doc_id"], "path": str(raw), "format": "stl",
                            "lin_deflection": _SLICE_LIN_DEFLECTION_MM,
                            "ang_deflection": _SLICE_ANG_DEFLECTION_RAD})
    mesh = load_mesh_file(raw)
    return mesh.vertices, mesh.triangles


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
                   name: Optional[str] = None, export_3mf: bool = True) -> str:
        """Slice a part to printer-ready G-code with STOCK PrusaSlicer and prove the result.

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

        Writes <output_dir>/<name>.gcode (+ .3mf project for review in PrusaSlicer, + a
        .print.json manifest) — default <project>/print/<printer>/. Returns printer/presets,
        orientation (with alternatives), effective settings, slicer stats (time, grams,
        layers, support share), printability issues (dfm-style rows) and gcode_check
        (verify_gcode). print_ready is true only when the G-code verification passes and the
        printability analysis has no failure. Report print_ready=false honestly and fix the
        design (dfm_check lists the faces) or settings, then slice again."""
        started = time.monotonic()
        try:
            from services.dfam import analyze_printability, frame_for_build_dir  # noqa: PLC0415
            from services.manufacturing.slicer import prusaslicer as ps  # noqa: PLC0415

            goal = str(goal or "balanced").strip().lower()
            if goal not in _GOALS:
                raise ValueError(f"goal must be one of {', '.join(_GOALS)}")
            copies = int(copies)
            if not 1 <= copies <= 64:
                raise ValueError("copies must be 1..64")
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

            mesh, label, source = load_target(doc_id, path)
            stem = _project_save_stem(name or label, source.get("doc_id") or label)
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

            orient = _orientation_choice(mesh, params, orientation=orientation,
                                         build_dir=build_dir, goal=goal,
                                         preserve_faces=preserve_faces,
                                         strength_dir=strength_dir)
            analysis = analyze_printability(mesh, params, orient["build_dir"],
                                            yaw_deg=orient["yaw_deg"])
            rows = {row["check"]: row for row in analysis["checks"]}
            volume_row = rows.get("build_volume") or {}
            if volume_row.get("status") == "fail":
                return _dumps({"ok": False, "reason": "does_not_fit_printer",
                               "printer": printer_name, "orientation": orient,
                               "build_volume": volume_row,
                               "content": volume_row.get("note") or "part exceeds the bed"})

            support_row = rows.get("support") or {}
            mode = str(supports or "auto").strip().lower()
            if mode not in ("auto", "on", "off", "buildplate"):
                raise ValueError("supports must be auto | on | off | buildplate")
            needs_support = support_row.get("status") in ("warn", "fail") or float(
                (support_row.get("metrics") or {}).get("support_volume_mm3") or 0.0) > 1.0
            enable = mode in ("on", "buildplate") or (mode == "auto" and needs_support)
            if enable:
                settings.update({
                    "support_material": "1", "support_material_auto": "1",
                    "support_material_style": "organic",
                    "support_material_threshold": f"{90.0 - params.overhang_max_deg:g}",
                    "support_material_buildplate_only": "1" if mode == "buildplate" else "0"})
                # Vendor presets skip support under anything the slicer calls a bridge; a
                # span the analysis measured beyond twice the bridge limit then prints its
                # perimeters in mid-air. Support those spans instead.
                if (rows.get("bridges") or {}).get("status") == "fail":
                    settings["dont_support_bridges"] = "0"
            else:
                settings["support_material"] = "0"
            adhesion = rows.get("bed_adhesion") or {}
            if isinstance(brim, (int, float)) and not isinstance(brim, bool):
                settings["brim_width"] = f"{float(brim):g}"
            elif str(brim).strip().lower() == "auto":
                if adhesion.get("status") in ("warn", "fail"):
                    settings["brim_width"] = "5"
                    settings["brim_type"] = "outer_only"
            else:
                raise ValueError("brim must be 'auto' or a width in mm")
            # Plain-text G-code so verify_gcode can read every move (MK4-class presets
            # default to binary .bgcode, which the printer also accepts as text).
            settings["binary_gcode"] = "0"
            # Detect bridging perimeters: overhanging walls get bridge flow, speed and fan.
            # Creality-class vendor presets ship it off, which prints unsupported outer
            # walls at full perimeter speed.
            settings["overhangs"] = "1"
            if overrides:
                requested = {str(key): str(value) for key, value in overrides.items()}
                refusal = ps.validate_overrides(install, resolved.config, requested)
                if refusal is not None:
                    return _dumps({"ok": False, "reason": "invalid_override",
                                   "refusal": refusal.to_dict(),
                                   "content": refusal.message})
                settings.update(requested)

            out_dir = _output_dir(output_dir, stem, _slug(printer_name))
            work_dir = out_dir / f".{stem}.slice"
            work_dir.mkdir(parents=True, exist_ok=True)
            vertices, triangles = _slice_mesh_arrays(source, work_dir)
            import numpy as np  # noqa: PLC0415
            rotation = frame_for_build_dir(orient["build_dir"], orient["yaw_deg"])
            oriented = np.asarray(vertices, dtype=np.float64) @ np.asarray(rotation).T
            oriented_stl = work_dir / f"{stem}.oriented.stl"
            _write_binary_stl(oriented_stl, oriented, triangles)

            config = {**resolved.config, **settings}
            gcode_path = out_dir / f"{stem}.gcode"
            project = out_dir / f"{stem}.3mf" if export_3mf else None
            run = ps.slice_mesh(install, mesh_paths=[oriented_stl], config=config,
                                output_gcode=gcode_path, work_dir=work_dir,
                                center_xy=resolved.bed_center(), copies=copies,
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
            printability_fail = [r for r in analysis["checks"]
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
            manifest = {
                "schema": "chudware.print_job/1",
                "part": label, "source": source, "copies": copies,
                "printer": {"name": printer_name, "vendor": resolved.vendor,
                            "bed_polygon": resolved.bed_polygon(),
                            "max_height_mm": resolved.max_height()},
                "presets": {"print": resolved.print_, "filament": resolved.filament},
                "goal": goal, "settings_changed": settings, "effective": effective,
                "orientation": {k: orient[k] for k in ("build_dir", "yaw_deg", "source", "why")},
                "slicer": install.to_dict(),
                "config_sha256": _sha256_file(Path(run.config_path)),
                "mesh_sha256": _sha256_file(oriented_stl),
                "gcode": str(run.gcode_path), "gcode_sha256": _sha256_file(Path(run.gcode_path)),
                "project_3mf": str(run.project_3mf) if run.project_3mf else None,
                "stats": stats, "estimate": analysis.get("estimate"),
                "printability": {"status": analysis.get("status"),
                                 "issues": _check_brief(analysis["checks"])},
                "gcode_check": {"status": check.get("status"),
                                "issues": _check_brief(check.get("checks") or [])},
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
            alternatives = orient.get("alternatives") or []
            return _dumps({
                "ok": True, "print_ready": print_ready, "part": label,
                "gcode_path": str(run.gcode_path),
                "project_3mf": str(run.project_3mf) if run.project_3mf else None,
                "manifest_path": str(manifest_path),
                "printer": {"name": printer_name, "vendor": resolved.vendor,
                            "other_matches": [m["name"] for m in matches[1:4]]},
                "presets": {"print": resolved.print_, "filament": resolved.filament},
                "goal": goal, "settings": effective,
                "orientation": {**{k: orient[k] for k in ("build_dir", "yaw_deg", "source",
                                                           "why")},
                                "alternatives": alternatives[:2]},
                "stats": stats, "estimate": analysis.get("estimate"),
                "printability": {"status": analysis.get("status"),
                                 "issues": _check_brief(analysis["checks"])},
                "gcode_check": {"status": check.get("status"),
                                "issues": _check_brief(check.get("checks") or [])},
                "slicer_warnings": run.warnings, "next_steps": next_steps,
                "elapsed_s": round(time.monotonic() - started, 1),
                "content": (f"{'PRINT-READY' if print_ready else 'NOT print-ready'}: "
                            f"{run.gcode_path} — {stats.get('print_time_s', 0) / 3600:.2f} h, "
                            f"{stats.get('filament_g', '?')} g on {printer_name}"),
            })
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
        mid-air plastic that turns into spaghetti. Returns {ok, status, checks:[...], stats}."""
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
