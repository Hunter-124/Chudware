"""CAD import/export + standard-parts MCP tools (T-RF8 split of cad_tools.py): export_part, import_part, list_standard_parts, make_standard_part."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
import os
import tempfile

from .cad_common import (
    CoreError,
    _CUR,
    _LAST_DELIVER_REV,
    _LAST_VOLUME,
    _autosave,
    _doc,
    _doc_revision,
    _dumps,
    _err,
    _publish_current,
    _sanitize_output_path,
    _tmp,
    cb,
)

def register_cad_exchange_tools(mcp: FastMCP) -> None:
    """CAD import/export + standard-parts MCP tools (T-RF8 split of cad_tools.py): export_part, import_part, list_standard_parts, make_standard_part."""


    @mcp.tool()
    def export_part(fmt: str = "step", path: Optional[str] = None,
                    doc_id: Optional[str] = None) -> str:
        """Export the current part to an interchange/mesh file; returns the written path.

        fmt: "step+" (native) | "step" | "stl" | "3mf" | "iges" | "brep" | "obj" | "ply" |
             "gltf" | "glb" | "3dm". If `path` is omitted, a temp file with the right extension is
             written. STEP/IGES/BREP are exact B-rep; STL/OBJ/PLY/glTF are tessellated meshes.

        NOTE: plain STEP/STL/etc. carry geometry only — NO editable feature tree or simulation
        studies. To hand the user a part they can keep editing, use save_part() (native .step+).
        fmt="step+" here is accepted as an alias and routed to the native saver.
        """
        try:
            d = _doc(doc_id)
            fmt = fmt.lower().lstrip(".")
            # The native editable container is written by doc_save, not the interchange writer.
            if fmt in ("step+", "stepplus", "stp+", "native"):
                # K3: canonicalise caller-supplied path before passing to the engine — expanduser
                # first so "~/…" doesn't collapse to "<cwd>/~/…" under abspath, and abspath so a
                # relative destination lands in THIS cwd, not the engine daemon's.
                out = _sanitize_output_path(os.path.expanduser(path)) if path else _tmp(".step+")
                if not out.lower().endswith(".step+"):
                    out += ".step+"
                r = cb.call("doc_save", {"doc_id": d, "path": out})
                written = r.get("path", out)
                if os.path.exists(written):
                    rev = _doc_revision(d)
                    if rev is not None:
                        _LAST_DELIVER_REV[d] = rev  # delivered: quiet close_part until further edits
                return _dumps({"ok": os.path.exists(written), "format": "step+",
                               "native": True, "doc_id": d, "path": written,
                               "exists": os.path.exists(written)})
            # K3: canonicalise caller-supplied path before passing to the engine (expanduser +
            # abspath — same reasoning as the step+ branch above).
            out = _sanitize_output_path(os.path.expanduser(path)) if path else _tmp("." + fmt)
            # The engine round-trips B-rep formats (re-reads + BRepCheck) and THROWS if the file is
            # truncated/malformed, so a returned ok:True means it actually re-opens. It echoes real
            # metadata (solid_count, valid_solids, bytes, validated) — surface it so the caller can
            # trust the export instead of a bare exists flag.
            r = cb.call("export_file", {"doc_id": d, "path": out, "format": fmt})
            written = r.get("path", out)
            if os.path.exists(written):
                rev = _doc_revision(d)
                if rev is not None:
                    _LAST_DELIVER_REV[d] = rev  # delivered: quiet close_part until further edits
            res = {"ok": True, "format": fmt, "doc_id": d, "path": written,
                   "exists": os.path.exists(written)}
            for k in ("bytes", "solid_count", "valid_solids", "expected_solids", "validated"):
                if k in r:
                    res[k] = r[k]
            return _dumps(res)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def import_part(path: str, into: Optional[str] = None, as_solid: bool = False,
                    resolve_features: bool = True) -> str:
        """Import a CAD/mesh file (STEP/IGES/BREP/STL/3MF/OBJ/PLY/glTF/3DM/DXF/DWG/STEP+/…) as a body.

        Lets you assemble components built or exported separately, or recover geometry from a file
        after a session reset. `into` = an existing doc_id to add the body to the CURRENT part
        (for assembly); omit it to open the file as a NEW part (made current). `as_solid` sews a
        mesh (STL/OBJ/…) into a solid on import; B-rep formats already import as solids.

        DXF/DWG are 2D vector drawings: they import as planar wires + faces (closed loops become
        faces) — reference/profile geometry to extrude or dimension, not a solid. DWG needs a
        DWG→DXF converter (LibreDWG `dwg2dxf`) on PATH; without one it errors with a hint.

        SOLIDWORKS (.sldprt): with `resolve_features=True` (default) and no `into`, the part is
        opened by RECONSTRUCTING its SolidWorks feature tree (sketches with relations, reference
        planes, extrude/cut/revolve/loft, …) into a fully EDITABLE Chudware part — not a frozen
        body. This needs SolidWorks installed locally (driven via its COM API). If any feature
        can't be faithfully rebuilt the import is REFUSED with the exact list of what blocked it
        (so the gap can be fixed) — it does NOT silently fall back to a dumb solid. Set
        `resolve_features=False` to force the legacy COM→STEP single-body import instead.

        TIP: pair with the `transform` feature (in add_features) to POSITION each imported body
        into a shared coordinate frame — translate:[x,y,z] and/or angle:<deg> with rotate:[ax,ay,az].
        """
        try:
            # Resolve the caller-supplied path ONCE against THIS process's cwd: the worker
            # chdir's to the project root, but the engine/daemon has a DIFFERENT working
            # directory, so a raw relative path that resolves fine here is unfindable there.
            # Every cb.call below gets the absolute form; `imported_from` keeps the caller's
            # original string so run records stay readable.
            path_abs = os.path.abspath(os.path.expanduser(path))
            # K2: validate the path exists BEFORE calling the engine so we never open a
            # zombie doc.  The engine's import_file does not distinguish "file missing" from
            # "format unsupported" and may silently create an empty document.
            if not os.path.isfile(path_abs):
                return _dumps({"ok": False, "error": f"file not found: {path}",
                               "hint": f"looked for {path_abs} — relative paths resolve "
                                       "against the MCP server's working directory, not "
                                       "necessarily your project folder. Run fs_list on the "
                                       "containing directory for the exact name, or pass an "
                                       "absolute path."})
            # SolidWorks feature-tree resolution (opening as a NEW editable part/assembly).
            if resolve_features and not into and path_abs.lower().endswith((".sldprt", ".sldasm")):
                from services.sw_import.sw_feature_import import SolidWorksUnavailable
                from services.sw_import.sw_translate import resolve as _sw_resolve
                try:
                    plan = _sw_resolve(path_abs)
                except SolidWorksUnavailable as e:
                    return _dumps({"ok": False, "error": str(e),
                                   "hint": "Open the part in SolidWorks and Save As STEP, then "
                                           "import that — or call import_part(resolve_features="
                                           "False) for a single-body COM→STEP import."})
                except Exception as e:  # noqa: BLE001
                    return _dumps({"ok": False, "error":
                                   f"SolidWorks feature extraction failed: {e}"})
                if plan["blocking"]:
                    # Strict-fidelity contract: refuse rather than import a quietly-wrong model.
                    return _dumps({
                        "ok": False,
                        "error": "cannot fully resolve this SolidWorks part's feature tree — "
                                 f"{len(plan['blocking'])} feature(s) are not supported yet",
                        "unsupported": plan["blocking"],
                        "degraded": plan["degraded"],
                        "hint": "These features need importer support before this part can be "
                                "rebuilt faithfully. Use import_part(resolve_features=False) to "
                                "fall back to a single-body COM→STEP import in the meantime."})
                if plan.get("is_assembly"):
                    # Build each DISTINCT component part once -> save a .step+ (keeps its editable
                    # feature tree) -> place every instance in an ASSEMBLY doc via
                    # asm_insert_component with its 4x4 transform (the engine's native assembly model).
                    comps = plan["components"]
                    if not comps:
                        return _dumps({"ok": False, "error":
                                       "no reconstructable components in this assembly",
                                       "degraded": plan["degraded"]})
                    # Component .step+ sidecars live in a cache dir (NOT the user's source folder);
                    # the assembly references them, so they must persist for the session.
                    sidecar = os.path.join(tempfile.gettempdir(), "chudware_sw_components",
                                           plan["name"])
                    os.makedirs(sidecar, exist_ok=True)
                    built: dict = {}   # part_key -> .step+ path
                    for c in comps:
                        key = c["part_key"]
                        if key in built:
                            continue
                        cd = cb.call("doc_new", {"name": os.path.basename(key)}).get("doc_id")
                        cb.call("doc_apply_ops", {"doc_id": cd, "ops": c["ops"]})
                        stem = os.path.splitext(os.path.basename(key))[0]
                        cpath = os.path.join(sidecar, stem + ".step+")
                        cb.call("doc_save", {"doc_id": cd, "path": cpath})
                        cb.call("doc_close", {"doc_id": cd})
                        built[key] = cpath
                    ad = cb.call("doc_new", {"name": plan["name"],
                                             "kind": "assembly"}).get("doc_id")
                    asm_ops = [{"op": "asm_insert_component", "name": c["name"],
                                "source": {"path": built[c["part_key"]]},
                                "transform": c["xform16"]} for c in comps]
                    cb.call("doc_apply_ops", {"doc_id": ad, "ops": asm_ops})
                    try:
                        cb.call("doc_solve_mates", {"doc_id": ad})
                    except CoreError:
                        pass
                    _CUR["doc_id"] = ad
                    _publish_current(ad)
                    return _dumps({
                        "ok": True, "doc_id": ad, "imported_from": path, "format": "sldasm",
                        "feature_resolved": True, "is_assembly": True,
                        "component_count": len(comps), "distinct_parts": len(built),
                        "feature_count": plan["feature_count"], "degraded": plan["degraded"]})
                d = cb.call("doc_new", {"name": plan["name"]}).get("doc_id")
                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
                _CUR["doc_id"] = d
                _publish_current(d)
                return _dumps({
                    "ok": True, "doc_id": d, "imported_from": path, "format": "sldprt",
                    "feature_resolved": True, "feature_count": plan["feature_count"],
                    "degraded": plan["degraded"],
                    **{k: r[k] for k in ("revision", "body_count", "volume_mm3") if k in r}})
            # A3: .step+ is the engine's NATIVE format. With no `into`, route it through
            # doc_open (the same path used by recover_parts and the host's own file-open) to
            # open it as a fresh part. WITH `into`, the engine's import_file now extracts the
            # container's solids and bakes them as an embedded-BREP import feature on the target
            # doc (Issue 2 — native parts are assemblable without re-exporting to plain STEP).
            if path_abs.lower().endswith(".step+") and not into:
                r = cb.call("doc_open", {"path": path_abs})
                new_id = r.get("doc_id")
                if new_id:
                    _CUR["doc_id"] = new_id
                    _publish_current(new_id)
                    # Seed the write-diff baseline so the first add_features on a freshly
                    # opened part reports a volume delta, not a volume-only "volume now X".
                    vol = r.get("volume")
                    if isinstance(vol, (int, float)):
                        _LAST_VOLUME[new_id] = vol
                return _dumps({"ok": True, "doc_id": new_id, "imported_from": path,
                               "format": "step+", "native": True,
                               **{k: r[k] for k in ("revision", "name", "body_count")
                                  if k in r}})
            params: dict = {"path": path_abs, "as_solid": bool(as_solid)}
            if into:
                params["into"] = into
            r = cb.call("import_file", params)
            new_id = r.get("doc_id") or into
            if new_id and not into:
                _CUR["doc_id"] = new_id
            if new_id:
                _publish_current(new_id)
                # Seed the write-diff baseline when the importer reports a volume, so the
                # first add_features on an imported part shows a delta instead of "volume now X".
                vol = r.get("volume")
                if isinstance(vol, (int, float)):
                    _LAST_VOLUME[new_id] = vol
            return _dumps({"ok": True, "doc_id": new_id, "imported_from": path,
                           "into": into, **{k: r[k] for k in ("revision", "name", "body_count")
                                            if k in r}})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_standard_parts() -> str:
        """List the parametric standard-parts catalog (screws, nuts, washers, bearings, ...):
        each family's key, sizes, and lengths. Feed a family + size to make_standard_part."""
        try:
            return _dumps(cb.call("parts_catalog_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def make_standard_part(family: str, size: str, length_mm: Optional[float] = None,
                           name: Optional[str] = None, options: Optional[dict] = None) -> str:
        """Build a standard hardware part as a NEW editable part doc from the catalog.

        family : a catalog key from list_standard_parts (e.g. "shcs", "hex_nut", "washer").
        size   : a size string for that family (e.g. "M5"). length_mm where the family needs it.
        Returns the new part's doc_id (and makes it current) so you can drop it into an assembly
        or export it — PLUS `bbox` (min/max/size/center, mm) and a `datum` (base_center + axis)
        so you can position it with the `transform` feature WITHOUT a measure round-trip. Standard
        parts seat their base at z=0 with the axis along +Z, so translate `datum.base_center` to
        the seat point. (Tip: transform's `body` selector targets a named body — see cad_guide.)
        """
        try:
            params: dict = {"family": family, "size": size}
            if length_mm is not None:
                params["length_mm"] = length_mm
            if name:
                params["name"] = name
            if options:
                params["options"] = options
            r = cb.call("parts_make", params)
            if not r.get("ok"):
                return _dumps({"ok": False, "error": r.get("error", "parts_make failed")})
            _CUR["doc_id"] = r.get("doc_id")
            _publish_current(r.get("doc_id"))
            _autosave(r.get("doc_id"), r.get("name"))
            # bbox + datum let the caller position this part in an assembly without a
            # measure round-trip (A-1): standard parts seat base at z=0, axis +Z.
            return _dumps({"ok": True, "doc_id": r.get("doc_id"), "name": r.get("name"),
                           "meta": r.get("meta"), "volume": r.get("volume"),
                           "bbox": r.get("bbox"), "datum": r.get("datum")})
        except Exception as e:  # noqa: BLE001
            return _err(e)
