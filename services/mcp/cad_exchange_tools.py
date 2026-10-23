"""CAD import/export + standard-parts MCP tools (split out of cad_tools.py): export_part, import_part, list_standard_parts, make_standard_part."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
import os
import tempfile
import zipfile

from .cad_common import (
    CoreError,
    _CUR,
    _LAST_DELIVER_REV,
    _LAST_VOLUME,
    _autosave,
    _default_deliverable_path,
    _doc,
    _doc_revision,
    _dumps,
    _err,
    _publish_current,
    _sanitize_output_path,
    placement_warnings,
    cb,
)

def register_cad_exchange_tools(mcp: FastMCP) -> None:
    """CAD import/export + standard-parts MCP tools (split out of cad_tools.py): export_part, import_part, list_standard_parts, make_standard_part."""


    @mcp.tool()
    def export_part(fmt: str = "step", path: Optional[str] = None,
                    doc_id: Optional[str] = None, allow_empty: bool = False,
                    units: Optional[str] = None) -> str:
        """Export the current part to an interchange/mesh file; returns the written path.

        fmt: "draw+" (standalone native drawing) | "step+" (native part) | "step" | "stl" |
             "3mf" | "iges" | "brep" | "obj" | "ply" | "gltf" | "glb" | "3dm" | "dxf" |
             "dwg" | "sat". If `path` is omitted, the file is named from the document in
             CHUDWARE_PROJECT_SAVE_DIR; only clients with no configured project folder fall
             back to a temp file.
             STEP/IGES/BREP are exact B-rep; STL/OBJ/PLY/glTF are tessellated meshes.
             DXF/DWG (AutoCAD R2000): solids/shells become exact, editable ACIS 3DSOLID/BODY
             entities, 2D/wire bodies become LINE/ARC/CIRCLE/polylines, one layer per body
             (BODY1..N) when there are 2+. SAT: ACIS 7.0 solids (AutoCAD ACISIN/IMPORT).
             Use dwg to send a revision back to an AutoCAD user.
             For a 2D drawing sheet use export_drawing instead.
        units (dxf/dwg/sat only): "mm" | "in" | "ft" | "cm" | "m" | ... Omitted = the drawing
             units of the part's DWG/DXF/SAT import (an inch drawing goes back in inches),
             else mm. The reply echoes the $INSUNITS code written (`units`).

        NOTE: plain STEP/STL/etc. carry geometry only — NO editable feature tree or simulation
        studies. To hand the user a part they can keep editing, use save_part() (native .step+).
        fmt="step+" here is accepted as an alias and routed to the native saver.

        On an assembly (or any doc with 2+ world bodies) the reply carries non-blocking
        `warnings:["placement_unverified: N component(s) placed by transform with no
        check_interference since last mutation"]` — the export still happened, but N
        component poses you asserted with asm_add_component(at=/rotate=) have not been
        re-validated since the last mutation. Run check_interference (and measure_between /
        list_world_bodies against the expected envelope) and export again.
        Empty geometry is refused before writing unless allow_empty=True. Use native step+
        to deliberately preserve an empty editable document.
        """
        try:
            d = _doc(doc_id)
            fmt = fmt.lower().lstrip(".")
            if fmt in ("draw+", "drawplus"):
                out = (_sanitize_output_path(os.path.expanduser(path)) if path
                       else _default_deliverable_path(d, ".draw+"))
                if not out.lower().endswith(".draw+"):
                    out += ".draw+"
                r = cb.call("drawing_save_standalone",
                            {"drawing_id": d, "path": out})
                written = r.get("path", out)
                return _dumps({"ok": os.path.exists(written), "format": "draw+",
                               "native": True, "drawing_id": d, "doc_id": d,
                               "path": written, "exists": os.path.exists(written),
                               **{k: r[k] for k in ("source_state",) if k in r}})
            if not allow_empty and not cb.call(
                    "asm_world_bodies", {"doc_id": d, "explode": False}).get("bodies"):
                # An unresolved assembly is not an empty design: let the strict
                # geometry census name the missing component and source path.
                cb.call("export_tess_estimate", {"doc_id": d})
                return _dumps({"ok": False, "reason": "no_bodies_empty_document",
                               "doc_id": d, "content": "Export refused: document has 0 bodies."})
            # The native editable container is written by doc_save, not the interchange writer.
            if fmt in ("step+", "stepplus", "stp+", "native"):
                # Resolve against the selected workspace, not either process's cwd,
                # before forwarding the guarded absolute destination to the engine.
                out = (_sanitize_output_path(os.path.expanduser(path)) if path
                       else _default_deliverable_path(d, ".step+"))
                if not out.lower().endswith(".step+"):
                    out += ".step+"
                # A save can preserve an unfinished tree; an export cannot omit a child.
                census = cb.call("export_tess_estimate", {"doc_id": d})
                r = cb.call("doc_save", {"doc_id": d, "path": out})
                written = r.get("path", out)
                if os.path.exists(written):
                    rev = _doc_revision(d)
                    if rev is not None:
                        _LAST_DELIVER_REV[d] = rev  # delivered: quiet close_part until further edits
                res = {"ok": os.path.exists(written), "format": "step+",
                       "native": True, "doc_id": d, "path": written,
                       "exists": os.path.exists(written)}
                res["validated"] = False  # native tree save is not a geometry round-trip check
                if res["ok"] and census.get("bodies", 0):
                    with zipfile.ZipFile(written) as archive:
                        if ("geometry.step" not in archive.namelist() or
                                archive.getinfo("geometry.step").file_size == 0):
                            return _dumps({"ok": False, "doc_id": d, "path": written,
                                           "components": census.get("components", []),
                                           "content": "Export refused: native snapshot has no "
                                                      "geometry.step; resolve rebuild errors.",
                                           "reason": "native_geometry_missing"})
                res["components"] = census.get("components", [])
                res["content"] = f"Exported editable native document to {written}."
                warnings = placement_warnings(d)
                if warnings:
                    res["warnings"] = warnings
                return _dumps(res)
            # The same workspace-relative destination rule applies to interchange files.
            out = (_sanitize_output_path(os.path.expanduser(path)) if path
                   else _default_deliverable_path(d, "." + fmt))
            # The engine round-trips B-rep formats (re-reads + BRepCheck) and THROWS if the file is
            # truncated/malformed, so a returned ok:True means it actually re-opens. It echoes real
            # metadata (solid_count, valid_solids, bytes, validated) — surface it so the caller can
            # trust the export instead of a bare exists flag.
            call = {"doc_id": d, "path": out, "format": fmt}
            if units:
                call["units"] = units
            r = cb.call("export_file", call)
            written = r.get("path", out)
            if os.path.exists(written):
                rev = _doc_revision(d)
                if rev is not None:
                    _LAST_DELIVER_REV[d] = rev  # delivered: quiet close_part until further edits
            res = {"ok": True, "format": fmt, "doc_id": d, "path": written,
                   "exists": os.path.exists(written)}
            res["content"] = f"Exported {fmt} geometry to {written}."
            for k in ("bytes", "solid_count", "valid_solids", "expected_solids", "validated",
                      "validation_skipped", "units", "components"):
                if k in r:
                    res[k] = r[k]
            # Non-blocking placement provenance: a transform-placed component nobody
            # re-checked is exactly how a merged wheel ships off-frame.
            warnings = placement_warnings(d)
            if warnings:
                res["warnings"] = warnings
            return _dumps(res)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def import_part(path: str, into: Optional[str] = None, as_solid: bool = False,
                    resolve_features: bool = True, keep_bodies: Optional[list] = None,
                    exclude_bodies: Optional[list] = None, resume: bool = False) -> str:
        """Import a CAD/mesh file (STEP/IGES/BREP/STL/3MF/OBJ/PLY/glTF/3DM/DXF/DWG/
        STEP+/DRAW+/…) as a body, or open a native standalone drawing.
        Lets you assemble components built or exported separately, or recover geometry from a file
        after a session reset. `into` = an existing doc_id to add the body to the CURRENT part
        (for assembly); omit it to open the file as a NEW part (made current). `as_solid` sews a
        mesh (STL/OBJ/…) into a solid on import; B-rep formats already import as solids.

        Imports create a NEW document by default, even when the source file is already open.
        `resume=True` explicitly reuses the open document at that path (reply `reused:true`).
        `into=<doc_id>` adds geometry to that document instead of opening or resuming one.
        A pristine native import is reproducible from its source and is not copied into
        the project root at turn end. Authored design edits retain source-save behavior;
        study-only scratch is saved separately. Automatic saves never replace a source
        held by another user-opened document. Pass save_part(path=...) for an explicit
        destination, or resume=True to edit the document the user already has open.

        DXF/DWG (AutoCAD, any version incl. 2013-2018+ files): ACIS 3DSOLID/BODY/REGION/SURFACE
        entities import as exact B-rep bodies (`<stem>_solidN` / `<stem>_surfaceN`), including
        R2013+ files whose ACIS lives in the DWG/DXF data storage. 2D entities import as planar
        wires; in a purely 2D drawing closed loops also become faces (profile geometry to
        extrude or dimension), while beside 3D content they stay construction curves exactly
        as AutoCAD shows them. INSERT block references are expanded, entity extrusion (OCS) is honored,
        3DFACE/POLYFACE meshes import as mesh bodies (grouped per layer), and $INSUNITS is
        applied (the drawing units are remembered for export). Any ACIS entity that cannot be
        read is named in `warnings` by type and handle. DWG 2D entities are converted by LibreDWG
        `dwg2dxf` (PATH or `python3 scripts/provision_libredwg.py`); without it only the 3D
        solids/surfaces import, with that hint as a warning. SAT/SAB (ACIS) files import too.

        SOLIDWORKS (.sldprt/.sldasm): with `resolve_features=True` (default), Chudware first
        asks an installed SolidWorks COM server to reconstruct sketches/features into a fully
        parametric native tree. If SolidWorks is unavailable, modern (2015+) files fall back to
        their saved display tessellation: the watertight triangle strips are sewn into baked
        native B-rep bodies, assembly hierarchy/transforms are preserved, and the result can be
        edited, exported, saved as STEP+, and simulated. That portable path is approximate and
        reports `import_fidelity="saved_display_tessellation"`; use feature resolution or STEP
        when exact analytic surfaces are required. `resolve_features=False` skips feature-tree
        extraction but still prefers exact COM→STEP when SolidWorks is installed.

        TIP: pair with the `transform` feature (in add_features) to POSITION each imported body
        into a shared coordinate frame — translate:[x,y,z] and/or angle:<deg> with rotate:[ax,ay,az].

        MULTI-BODY SOURCES (a 760-body frame STEP, an 11-body wheel): the import lands as ONE
        feature holding every body, and describe_part prints an indexed roster of them
        (`embedded_body_names`) with the `ref` that addresses each: the first body is "<fid>"
        and the rest are "<fid>#1", "<fid>#2", … over the bodies that survived any filter.
        Select which ones come in AT IMPORT TIME — never by editing the source file:
          keep_bodies=[...]     keep ONLY these bodies
          exclude_bodies=[...]  drop these bodies (e.g. a legacy wheel that ships with a frame)
        Both take 1-based ordinal ints and/or body-name strings matched against
        `embedded_body_names` (import first with neither to read the roster, then re-import,
        or drop them from the import feature's params with edit_feature).

        POSITIONING an imported body uses the `transform` feature (add_features) and it moves
        exactly ONE body per call: body:"f5#3" names the 4th body of import feature f5 (the
        first is plain "f5"; omit `body` on a single-body part). The pivot is the ORIGIN
        unless you give one, and the
        order is ROTATE THEN TRANSLATE: angle:<deg> about rotate:[ax,ay,az] first, then
        translate:[x,y,z]. To move a whole multi-body import you issue one transform per body
        — or, better, place the parts as assembly COMPONENTS (asm_add_component + asm_mate),
        where one pose moves the whole component and mates derive it for you.
        NEVER hand-edit STEP/JSONL text to move geometry.
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
            # Resuming a saved session is opt-in: an import must never silently redirect
            # subsequent edits to the source document.
            if resume and not into:
                try:
                    open_docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                                 if isinstance(e, dict) and e.get("doc_id")]
                except Exception:  # noqa: BLE001 — listing is an optimisation, never fatal
                    open_docs = []
                want = os.path.realpath(path_abs)
                for e in open_docs:
                    p = e.get("path")
                    if not isinstance(p, str) or not p:
                        continue
                    if os.path.realpath(p) != want:
                        continue
                    did = e["doc_id"]
                    _CUR["doc_id"] = did
                    vol = e.get("volume")
                    if isinstance(vol, (int, float)):
                        _LAST_VOLUME.setdefault(did, vol)
                    _publish_current(did)
                    return _dumps({
                        "ok": True, "doc_id": did, "imported_from": path, "reused": True,
                        "name": e.get("name"), "revision": e.get("revision", 0),
                        "feature_count": e.get("feature_count"),
                        "content": f'Resumed "{os.path.basename(path_abs)}" as {did}.',
                        "note": "Pass resume=False to import an independent document."})
            # SolidWorks feature-tree resolution (opening as a NEW editable part/assembly).
            portable_fallback_reason: Optional[str] = None
            if resolve_features and not into and path_abs.lower().endswith((".sldprt", ".sldasm")):
                from services.sw_import.sw_feature_import import SolidWorksUnavailable
                from services.sw_import.sw_translate import resolve as _sw_resolve
                try:
                    plan = _sw_resolve(path_abs)
                except SolidWorksUnavailable as e:
                    # Portable modern-SolidWorks import lives in the C++ core. Keep going to the
                    # normal import_file call; its response explicitly labels tessellation
                    # fidelity, so this never masquerades as a resolved parametric tree.
                    plan = None
                    portable_fallback_reason = str(e)
                except Exception as e:  # noqa: BLE001
                    return _dumps({"ok": False, "error":
                                   f"SolidWorks feature extraction failed: {e}"})
                if plan is not None and plan["blocking"]:
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
                if plan is not None and plan.get("is_assembly"):
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
                if plan is not None:
                    d = cb.call("doc_new", {"name": plan["name"]}).get("doc_id")
                    r = cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
                    _CUR["doc_id"] = d
                    _publish_current(d)
                    return _dumps({
                        "ok": True, "doc_id": d, "imported_from": path, "format": "sldprt",
                        "feature_resolved": True, "feature_count": plan["feature_count"],
                        "degraded": plan["degraded"],
                        **{k: r[k] for k in ("revision", "body_count", "volume_mm3") if k in r}})
            # A standalone drawing is its own native registry/session. It must never fall
            # through import_file (which would treat the ZIP as 3D geometry).
            if path_abs.lower().endswith(".draw+"):
                if into:
                    raise ValueError("a .draw+ opens as its own drawing; 'into' is not supported")
                r = cb.call("drawing_open_standalone", {"path": path_abs})
                drawing_id = r.get("drawing_id") or r.get("doc_id")
                return _dumps({"ok": True, "doc_id": drawing_id,
                               "drawing_id": drawing_id, "imported_from": path,
                               "format": "draw+", "native": True,
                               **{k: r[k] for k in ("name", "source_doc_id", "source_state")
                                  if k in r}})
            # A3: .step+ is the engine's NATIVE format. With no `into`, route it through
            # doc_open (the same path used by recover_parts and the host's own file-open) to
            # open it as a fresh part. WITH `into`, the engine's import_file now extracts the
            # container's solids and bakes them as an embedded-BREP import feature on the target
            # doc (Issue 2 — native parts are assemblable without re-exporting to plain STEP).
            if path_abs.lower().endswith(".step+") and not into:
                r = cb.call("doc_open", {"path": path_abs, "detach_path": not resume})
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
                               "content": f"Opened independent native document {new_id}."
                                          if not resume else f"Opened native document {new_id}.",
                               **{k: r[k] for k in ("revision", "name", "body_count")
                                  if k in r}})
            params: dict = {"path": path_abs, "as_solid": bool(as_solid)}
            if into:
                params["into"] = into
            # Body selection rides the import feature's params (contracts/feature_params.md):
            # dropping a legacy body is an IMPORT-TIME choice, never a source-file edit.
            if keep_bodies is not None:
                params["keep_bodies"] = list(keep_bodies)
            if exclude_bodies is not None:
                params["exclude_bodies"] = list(exclude_bodies)
            r = cb.call("import_file", params)
            new_id = r.get("doc_id") or into
            # An import feature that FAILS to compile (keep_bodies/exclude_bodies naming a
            # body the source does not have, an unreadable source, a filter that dropped
            # every body) still produces a DOCUMENT — an empty one. Reporting ok:true there
            # sent the model on to model against nothing, so carry the engine's per-feature
            # reasons the way add_features' fail_result does (cad_doc_tools.py).
            fs = (r.get("rebuild") or {}).get("feature_status") or []
            failed = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                      if isinstance(s, dict) and not s.get("ok")]
            if failed:
                if new_id:
                    _publish_current(new_id)   # mirror the (empty) doc, don't claim a part
                parts = []
                for f in failed:
                    why = str(f.get("error") or "").strip() or "no reason reported"
                    parts.append(f'{f.get("id") or "?"}: {why}')
                return _dumps({
                    "ok": False, "doc_id": new_id, "imported_from": path, "into": into,
                    "error": "import failed, nothing was imported — " + "; ".join(parts),
                    "failed_features": failed, "feature_status": fs})
            if new_id and not into:
                _CUR["doc_id"] = new_id
            if new_id:
                _publish_current(new_id)
                # Seed the write-diff baseline when the importer reports a volume, so the
                # first add_features on an imported part shows a delta instead of "volume now X".
                vol = r.get("volume")
                if isinstance(vol, (int, float)):
                    _LAST_VOLUME[new_id] = vol
            payload = {"ok": True, "doc_id": new_id, "imported_from": path,
                       "into": into,
                       **{k: r[k] for k in (
                           "revision", "name", "body_count", "import_fidelity",
                           "feature_resolved", "editable_as", "component_names", "warning") if k in r}}
            if portable_fallback_reason and r.get("import_fidelity") == "saved_display_tessellation":
                payload["feature_fallback_reason"] = portable_fallback_reason
            return _dumps(payload)
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
