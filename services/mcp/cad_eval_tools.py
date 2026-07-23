"""CAD query/measure/check MCP tools (T-RF8 split of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from mcp.server.fastmcp import Image
from typing import Optional
from typing import Union
import json
import os

from .cad_common import (
    CoreError,
    _EMPHASIS_PALETTE,
    _apply_emphasis_overlay,
    _as_face_query,
    _between_from_batch,
    _build_emphasis_palette,
    _camera_dir_for_target,
    _crop_png_to_rect,
    _crop_rect_for_target,
    _doc,
    _dumps,
    _emphasis_color_name,
    _err,
    _palette_caption_block,
    _reading_from_measure,
    _resolve_measure_target,
    _resolve_one_selector,
    _target_bbox,
    _tmp,
    _vnorm,
    cb,
)

def register_cad_eval_tools(mcp: FastMCP) -> None:
    """CAD query/measure/check MCP tools (T-RF8 split of cad_tools.py): query_geometry, check_sketch, measure*, interference/overlap/symmetry/section/wall/draft checks."""


    @mcp.tool()
    def query_geometry(query: Union[str, dict], doc_id: Optional[str] = None) -> str:
        """Resolve a plain-English face/edge query against the part — explore/confirm geometry.

        `query` is a geometry query like {"of":"face","side":"top"}, {"of":"edge","parallel":"z"},
        {"of":"face","surf":"round","pick":"outer"} — or a bare side word ("top") for a face.
        Returns each matching face/edge with its shape (surf), side, centroid and size, so you can
        confirm "the top face" resolves before you reference it in a feature or a simulation.
        """
        try:
            q = query if isinstance(query, dict) else _as_face_query(query)
            return _dumps(cb.call("doc_query", {"doc_id": _doc(doc_id), "query": q}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def check_sketch(sketch: str, doc_id: Optional[str] = None) -> str:
        """Check whether a sketch is well-constrained (fully defined) and report what's missing.

        Solves the named sketch feature and returns its constraint health so you can author
        SolidWorks-quality sketches — explicit geometric RELATIONS (coincident, horizontal/vertical,
        parallel, perpendicular, tangent, equal, concentric, symmetric, midpoint, collinear) plus
        DRIVING DIMENSIONS — instead of loose geometry that only looks right.

        sketch: the sketch feature id (e.g. "s1") whose constraints to evaluate.

        Returns:
          status       "full" (fully defined — the goal), "under" (under-defined), or "over"
          dof          remaining degrees of freedom (0 = fully defined)
          conflicting  constraint ids that contradict each other (an over-defined sketch) — remove one
          redundant    constraint ids implied by the others (harmless, but a redundant DRIVING
                       dimension over-defines — prefer a relation or delete the duplicate)
          summary      one-line plain-English verdict + what to add/remove to reach dof 0

        Workflow: after authoring a sketch, READ the ASCII readback from the mutation response
        (or call render_sketch_ascii), then call this / sketch_health; if `under`, add the missing
        relations/driving dimensions and re-check; if `over`, drop a conflicting/redundant
        constraint. Aim for dof 0 so editing a driving dimension re-solves the geometry
        parametrically. Before declaring a sketch done: render_sketch_ascii + sketch_health /
        check_sketch with dof 0.
        """
        try:
            d = _doc(doc_id)
            # sketch_health is the read-only source of truth (never touches the interactive
            # active_sketch state). It returns status/dof plus the narrated read; its
            # conflicting/redundant are [{id,type,suggestion}] objects, from which we pull the
            # ids for check_sketch's id-list contract. (The old "doc_sketch_solve"/"sketch_fid"
            # call never dispatched — wrong method + key — so check_sketch silently errored.)
            r = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
            status = r.get("status", "")
            dof = r.get("dof", 0)

            def _ids(items):
                out = []
                for it in items or []:
                    if isinstance(it, dict) and it.get("id") is not None:
                        out.append(it["id"])
                    elif isinstance(it, str):
                        out.append(it)
                return out

            conflicting = _ids(r.get("conflicting"))
            redundant = _ids(r.get("redundant"))
            narration = r.get("dof_narration")
            if conflicting:
                summary = (f"Over-defined: {len(conflicting)} conflicting constraint(s) "
                           f"{conflicting} — remove one to resolve the contradiction.")
            elif status == "full" or dof == 0:
                summary = "Fully defined (dof 0) — every entity is pinned by relations + dimensions."
            else:
                summary = (f"Under-defined: {dof} degree(s) of freedom remain — add geometric "
                           f"relations and/or driving dimensions until dof reaches 0.")
                if narration:
                    summary += f" Free: {narration}"
                if redundant:
                    summary += f" (Also {len(redundant)} redundant: {redundant}.)"
            out = {"ok": True, "doc_id": d, "sketch": sketch, "status": status,
                   "dof": dof, "fully_defined": (status == "full" or dof == 0),
                   "conflicting": conflicting, "redundant": redundant, "summary": summary,
                   # narrated enrichment straight from sketch_health (the advisor source).
                   "open_endpoints": (r.get("closure") or {}).get("open_endpoints"),
                   "dof_narration": narration, "schedule": r.get("schedule"),
                   "advisory": r.get("advisory")}
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def measure(targets: list,
                doc_id: Optional[str] = None,
                view: Optional[Union[str, dict]] = "auto",
                annotate: bool = True) -> list:
        """Color-coded multi-element measurement with an annotated render.

        Resolves each plain-English target (or resolved selector dict — same vocabulary as
        inspect_part / measure_between / query_geometry), measures via the engine's
        `eval_measure`, paints every measured element a DISTINCT colour from the shared
        10-colour emphasis palette (per_face mode), and returns [image, caption] so vision
        and non-vision models both get the reading.

        targets: non-empty list of face/edge/vertex selectors — e.g. ["top",
          {"of":"edge","surf":"circle","pick":"largest"}, {"of":"face","side":"front"}].
          One target → that element's intrinsic measure (length / area / diameter / …).
          Two targets → also the distance (+ angle when applicable) between them.
          Three+ → chain multi-distance across the ordered refs.
        view: "auto" (default — frame along the measured elements' normals) or a named /
          custom camera like render_part / inspect_part.
        annotate: pass face labels / triad through to the underlying render (default True).

        Caption carries structured readings as JSON: each element gets
        {label, kind, value, unit, color, rgb, sid?, anchor?} plus an optional `between`
        block when 2+ refs yield a distance/angle. Prefer this over measure_between when
        you want the same colour-coded highlight view the Evaluate ribbon shows.

        Returns [image, caption].
        """
        d = _doc(doc_id)
        if not isinstance(targets, list) or not targets:
            raise ValueError(
                "measure(targets=[...]) needs a non-empty list of plain-English selectors "
                "(e.g. ['top', {'of':'edge','parallel':'z','pick':'largest'}]).")

        refs: list = []
        matches: list = []
        labels: list = []
        for t in targets:
            ref, label = _resolve_measure_target(d, t)
            refs.append(ref)
            matches.append(ref if isinstance(ref, dict) else {"kind": "face"})
            # Prefer the resolved selector dict for emphasis projection when available.
            if isinstance(ref, dict):
                matches[-1] = ref
            labels.append(label)

        # Batch measure (all refs at once) — single / pair / multi-distance.
        try:
            batch = cb.call("eval_measure", {"doc_id": d, "refs": refs})
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"eval_measure failed: {e}") from e

        # Per-element intrinsic readings with index-distinct palette colours.
        palette = _build_emphasis_palette(matches, mode="per_face")
        readings: list = []
        for i, (ref, label) in enumerate(zip(refs, labels)):
            entry = palette["entries"][i] if i < len(palette["entries"]) else {}
            rgb = tuple(entry.get("rgb") or _EMPHASIS_PALETTE[i % len(_EMPHASIS_PALETTE)])
            color_name = _emphasis_color_name(rgb)
            if len(refs) == 1:
                mres = batch if isinstance(batch, dict) else {}
            else:
                try:
                    mres = cb.call("eval_measure", {"doc_id": d, "refs": [ref]})
                except Exception as e:  # noqa: BLE001
                    mres = {"kind": "error", "error": str(e)}
            readings.append(_reading_from_measure(
                mres if isinstance(mres, dict) else {},
                label=label, color_name=color_name, rgb=rgb,
                sid=(ref.get("sid") if isinstance(ref, dict) else None)))

        between = None
        if len(refs) >= 2 and isinstance(batch, dict):
            between = _between_from_batch(batch)

        # Render + per_face emphasis so each measured element is colour-coded.
        try:
            part_bb = cb.call("doc_bbox", {"doc_id": d})
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"doc_bbox failed: {e}") from e
        if not part_bb or not part_bb.get("size"):
            raise ValueError("no part bounding box — build geometry first")

        target_bb = _target_bbox(d, matches)
        view_dir = _camera_dir_for_target(matches, view)
        png_full = _tmp(".png")
        r = cb.call("doc_render_view", {
            "doc_id": d, "out_path": png_full,
            "view": {"dir": list(view_dir)}, "annotate": bool(annotate),
        })
        if not r.get("ok") or not os.path.exists(png_full):
            r2 = cb.call("doc_render_png",
                         {"doc_id": d, "out_path": png_full, "annotate": bool(annotate)})
            if not r2.get("ok") or not os.path.exists(png_full):
                raise ValueError(
                    f"measure render failed: {r.get('reason', r.get('error', 'no bodies'))}")
            view_dir = _vnorm((1.0, 1.0, 0.8))

        rect = _crop_rect_for_target(part_bb, target_bb, view_dir,
                                    margin=0.18, zoom_factor=1.35)
        png_crop = _tmp(".png")
        _crop_png_to_rect(png_full, rect, png_crop)
        png_out, palette = _apply_emphasis_overlay(
            png_crop, d, matches, part_bb, view_dir, mode="per_face",
            palette=palette)


        # Human caption + structured block (non-vision models read the JSON).
        lines = []
        for rd in readings:
            val = rd.get("value")
            if isinstance(val, float):
                val_s = f"{val:.6g}"
            else:
                val_s = str(val) if val is not None else "?"
            unit = rd.get("unit") or ""
            lines.append(
                f"  [{rd.get('color')}] {rd.get('label')}: "
                f"{rd.get('kind')} = {val_s}{' ' + unit if unit else ''}")
        if between:
            bval = between.get("value")
            bval_s = f"{bval:.6g}" if isinstance(bval, float) else str(bval)
            extra = ""
            if between.get("angle") is not None:
                extra = f", angle={between['angle']:.4g} deg"
            lines.append(
                f"  between: {between.get('kind')} = {bval_s}"
                f"{' ' + (between.get('unit') or '') if between.get('unit') else ''}"
                f"{extra}")
        structured = {
            "ok": True,
            "doc_id": d,
            "readings": readings,
            "between": between,
            "n_targets": len(refs),
            "palette_mode": "per_face",
        }
        caption = (
            f"MEASURE ({len(refs)} target{'s' if len(refs) != 1 else ''}):\n"
            + "\n".join(lines)
            + _palette_caption_block(palette)
            + f" [measure_json={json.dumps(structured, separators=(',', ':'), default=str)}]"
        )
        return [Image(path=png_out), caption]



    @mcp.tool()
    def measure_part(doc_id: Optional[str] = None, density_g_cm3: float = 7.85) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        density_g_cm3 sets the material density used for mass (default 7.85 = steel). Also runs a
        geometry sanity check (closed/valid solid).

        Multi-body docs: the top-level volume/mass/com/inertia are the COMBINED total of ALL
        bodies, and `mass.bodies[]` lists each body's own properties (with `mass.body_count`).
        Pass body_id via a direct query if you need just one. (Previously this silently reported
        only the last body of an assembly.)
        """
        try:
            d = _doc(doc_id)
            mass = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": density_g_cm3})
            out = {"ok": True, "doc_id": d, "mass": mass}
            try:
                out["check"] = cb.call("eval_check", {"doc_id": d})
            except CoreError:
                pass
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- inspection

    @mcp.tool()
    def measure_between(a_query: Union[str, dict], b_query: Optional[Union[str, dict]] = None,
                        doc_id: Optional[str] = None) -> str:
        """Measure a face/edge — or the distance + angle BETWEEN two of them — in plain words.

        This is the engineer's measure/probe tool. Address geometry the same way you do
        everywhere else (a side word like "top", or a full query {"of":"face","side":"top"} /
        {"of":"edge","parallel":"z","pick":"largest"}).

        One query (b_query omitted) -> that single feature's intrinsic measure:
          * a face   -> its area (mm^2)
          * an edge  -> its length (mm), or radius (mm) for a circular edge
          * a vertex -> its point coordinates
        Two queries -> the minimum DISTANCE (mm) between them (with the two closest points) and,
        when applicable (face↔face normals or edge↔edge tangents), the ANGLE (degrees, 0..90).

        Each query must resolve to exactly ONE face/edge. If it matches several (e.g. "all the
        vertical edges"), narrow it with a predicate ("pick":"largest"/"side":"top"/…) — call
        query_geometry(query) first to see the candidates. Returns a readable `summary` line.
        """
        try:
            d = _doc(doc_id)
            ref_a = _resolve_one_selector(d, a_query)
            refs = [ref_a]
            if b_query is not None:
                refs.append(_resolve_one_selector(d, b_query))
            res = cb.call("eval_measure", {"doc_id": d, "refs": refs})
            kind = res.get("kind")
            out = {"ok": True, "doc_id": d, "a_query": a_query, **res}
            if b_query is not None:
                out["b_query"] = b_query
                parts = []
                if "distance" in res:
                    parts.append(f"distance {res['distance']:.4g} mm")
                if "angle" in res:
                    parts.append(f"angle {res['angle']:.4g} deg")
                out["summary"] = ("between the two selections: " + ", ".join(parts)) if parts \
                    else "no distance/angle computed"
            else:
                if kind == "area":
                    out["summary"] = f"area = {res.get('area'):.6g} mm^2"
                elif kind == "length":
                    out["summary"] = f"length = {res.get('length'):.6g} mm"
                elif kind == "radius":
                    out["summary"] = f"radius = {res.get('radius'):.6g} mm"
                elif kind == "point":
                    out["summary"] = f"point = {res.get('points')}"
                elif kind == "volume":
                    out["summary"] = f"volume = {res.get('volume'):.6g} mm^3"
                else:
                    out["summary"] = f"measured ({kind})"
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def check_interference(doc_id: Optional[str] = None, include_coincident: bool = False,
                           min_clearance_mm: float = 0.0) -> str:
        """Clash/clearance check — do the bodies OVERLAP, and if not, how tight is the gap?

        Runs a pairwise interference analysis over every body in the document (for an assembly
        doc, over the world-placed component bodies). For each pair it reports either an
        INTERFERENCE (overlap volume in mm^3 — a real collision the design must not have) or a
        CLEARANCE (minimum gap in mm). Use it to validate that mating/instanced parts fit without
        clashing, or to find the tightest gap in an assembly. For overlap WITHIN a single part's
        own features (two bosses merged, a cut swallowing a boss) use check_feature_overlap
        instead — this tool only sees whole bodies/components.

        include_coincident: also classify near-zero-clearance face-touching pairs (distance
        <= 1e-6 mm) as `contacts` entries instead of clearances — the "coincident faces" case
        SolidWorks flags separately (two parts touching exactly, not overlapping or gapped).
        min_clearance_mm: tag any clearance pair tighter than this with a "below min clearance"
        warning (e.g. flag a press-fit gap that's actually too tight to assemble by hand).

        Returns {ok, summary, parts:[{name,bbox,volume}], interferences:[{a,b,volume}],
        clearances:[{a,b,distance}], contacts:[{a,b,distance}]}. ok is True only when NOTHING
        interferes. A single-body part trivially reports no interference (build a multi-body
        part with the `transform` copy feature, or open an assembly, to get meaningful pairs).
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_interference", {"doc_id": d, "include_coincident": include_coincident,
                                                "min_clearance_mm": min_clearance_mm})
            return _dumps({"ok": res.get("ok", True), "doc_id": d, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def check_feature_overlap(doc_id: Optional[str] = None, features: Optional[list] = None) -> str:
        """Intra-PART feature-vs-feature overlap — do two of THIS part's own features collide?

        Tests each feature's own pre-boolean tool solid (the shape it added/removed BEFORE it
        got fused/cut into the body) against every other feature's tool, so it catches things a
        single compiled solid can't show you: two bosses that silently merged (add+add), a
        redundant cut carved twice over the same region (cut+cut), or — the dangerous one — a
        later cut that erased an EARLIER boss entirely (add+cut where the common volume ≈ the
        boss's whole volume). Use this for one part's feature tree; use check_interference for
        separate BODIES or assembly components.

        features (optional): restrict the scan to pairs involving these feature ids (e.g. the
        ids you just added) instead of the whole tree — add_features already runs this
        automatically on each new batch and surfaces it as `overlap_warnings` in its return, so
        you rarely need to call this yourself right after building; call it explicitly to
        re-check the whole part or a specific subset.

        Returns {ok, summary, pairs:[{a, b, a_name, b_name, kind, volume_mm3}], dead_features}.
          kind "boss_overlap"   — two ADDITIVE features share volume (often intentional — a
                                  boss deliberately overlapping a rib — but flagged as a warning).
          kind "cut_overlap"    — two cuts remove overlapping material (a redundant cut, usually
                                  a modeling mistake worth cleaning up).
          kind "cut_into_boss"  — a cut removes PART of an earlier additive feature.
          kind "boss_swallowed" — a cut removes essentially the WHOLE earlier boss (>=99% of its
                                  volume) — almost certainly an error; this is the ONLY kind that
                                  sets ok:false.
        dead_features: feature ids that built successfully but own NO surviving faces (fully
        consumed by a later op) — a feature silently doing nothing right now.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d}
            if features is not None:
                params["features"] = features
            res = cb.call("eval_feature_overlap", params)
            return _dumps({"ok": res.get("ok", True), "doc_id": d, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def check_symmetry(plane: str = "top", doc_id: Optional[str] = None) -> str:
        """Is the part mirror-symmetric about a principal datum plane? Returns the deviation.

        plane : "top" (XY, mirror across Z), "front" (XZ, mirror across Y), or "right" (YZ,
                mirror across X) — the reserved Chudware datum planes (Z-up).
        Mirrors the active body across the plane and measures the symmetric-difference volume.
        Returns {symmetric: bool, deviation_mm3}: symmetric is True when the leftover volume is
        within a tiny tolerance (1e-3 of the part volume). Handy to confirm a feature you meant
        to place symmetrically really is, or to pick a sensible mirror plane.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_symmetry", {"doc_id": d, "plane": plane})
            sym = res.get("symmetric")
            dev = res.get("deviation_mm3")
            summary = (f"symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)" if sym
                       else f"NOT symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)")
            return _dumps({"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def inspect_section(plane: str = "top", doc_id: Optional[str] = None) -> str:
        """Cut the part with a datum plane and report the CROSS-SECTION area + section moments.

        This is the "section the part to inspect its internals" probe. It slices the active
        body with one of the reserved datum planes and computes the real properties of the
        resulting cut face — the cross-section a draftsman would dimension.

        plane : "top" (XY, normal +Z), "front" (XZ, normal -Y), or "right" (YZ, normal +X) —
                the reserved Chudware datum planes (Z-up). The cut passes through the origin.

        Returns {ok, area_mm2, centroid:[x,y,z], Ixx, Iyy, Ixy, summary}:
          * area_mm2 — the cross-section area (mm^2), the headline number. For a solid this is
            the filled region; for a thin/hollow wall it's the material the plane cuts through.
          * centroid — the area centroid of the section, in world coordinates.
          * Ixx/Iyy/Ixy — the section second moments of area (mm^4), expressed in the cutting
            plane's frame (use for bending stiffness / section modulus reasoning).
        Use it to verify a beam's cross-section, check a wall's cut area, or feed a hand
        bending calc. A plane that misses the body (empty cut) returns area 0.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_section_props", {"doc_id": d, "plane": plane})
            area = res.get("area_mm2", 0.0)
            if area and area > 0:
                summary = f"cross-section area at plane '{plane}' = {area:.6g} mm^2"
            else:
                summary = (f"plane '{plane}' produced no closed cross-section (the cut may miss "
                           f"the body or only graze an edge) — area = 0")
            return _dumps({"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def wall_thickness(doc_id: Optional[str] = None) -> str:
        """How THIN is the thinnest wall? Inward ray-cast wall-thickness over the whole part.

        From every surface sample it casts a ray straight into the material and measures the
        distance to the far wall — the local wall thickness. The headline value is the MINIMUM
        ("the thinnest wall is X mm"), the number that governs whether a part can be molded,
        machined, or 3D-printed, and where it's likely to be weak or warp.

        Returns {ok, min_thickness_mm, mean_thickness_mm, max_thickness_mm, n_sampled,
        aicm_path, summary}:
          * min_thickness_mm — the thinnest wall found (the key value). null if no sample got a
            through-wall hit (e.g. an open sheet body with no opposite wall).
          * mean/max — the average and thickest sampled wall.
          * aicm_path — a temp field file for the in-app thickness colour-map viewer.
        Compare min_thickness_mm against your process minimum (e.g. ~1 mm injection wall,
        ~0.8 mm FDM) to flag thin spots before manufacturing.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_thickness", {"doc_id": d})
            mn = res.get("min_thickness_mm")
            if mn is not None:
                mean = res.get("mean_thickness_mm")
                summary = (f"thinnest wall = {mn:.4g} mm"
                           + (f" (mean {mean:.4g} mm)" if mean is not None else ""))
            else:
                summary = ("no through-wall thickness could be measured (open/sheet body, or no "
                           "opposite wall in line of sight)")
            return _dumps({"ok": True, "doc_id": d, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def draft_analysis(pull_dir: Optional[list] = None, min_draft_deg: float = 3.0,
                       doc_id: Optional[str] = None) -> str:
        """Moldability check: which faces lack enough DRAFT to release from the mold?

        For a given mold-pull (ejection) direction it measures each face's draft angle — how far
        its surface tilts away from the pull axis. Faces whose worst-case draft is below
        min_draft_deg would drag on the tool during ejection and need a draft feature added.

        pull_dir : the mold-opening / ejection direction as [x,y,z] (defaults to +Z, [0,0,1]).
        min_draft_deg : the minimum acceptable draft angle in degrees (default 3°, a common
                        injection-molding minimum).

        Returns {ok, min_draft_deg, max_draft_deg, threshold_deg, n_faces_below_threshold,
        faces_below_threshold:[{face_id, min_draft_deg, area_mm2, centroid}], aicm_path,
        summary}. n_faces_below_threshold == 0 means every face clears the threshold (good to
        mold along that direction). The faces_below_threshold list (centroid + area) tells you
        WHERE to add draft. aicm_path is the colour-map field for the in-app viewer.
        """
        try:
            d = _doc(doc_id)
            pull = pull_dir if (isinstance(pull_dir, (list, tuple)) and len(pull_dir) == 3) \
                else [0.0, 0.0, 1.0]
            res = cb.call("eval_draft",
                          {"doc_id": d, "pull": [float(x) for x in pull],
                           "angle_deg": float(min_draft_deg)})
            n_bad = res.get("n_faces_below_threshold", 0)
            thr = res.get("threshold_deg", min_draft_deg)
            if n_bad == 0:
                summary = (f"all faces have >= {thr:g}deg draft along pull {list(pull)} — "
                           f"moldable in that direction")
            else:
                summary = (f"{n_bad} face(s) below the {thr:g}deg draft minimum along pull "
                           f"{list(pull)} — add draft there before molding")
            return _dumps({"ok": True, "doc_id": d, "pull_dir": list(pull),
                           "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)
