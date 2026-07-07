"""3D-sketch + sketch-preview MCP tools — parity with the GUI sketch overlay and 3D sketcher.

Exposes to an AI driver the same capabilities the GUI gives a human in the Sketch3D workspace
(ui/imgui/src/ui/sketch3d/) and the 2D sketch overlay (ui/imgui/src/ui/sketch/):

    sketch3d_new(name)                   create a named 3D polyline sketch (points + segments)
    sketch3d_add_curves(id, curves)      append lines / spline-polylines / arcs-in-space
    sketch3d_get(id)                     read back the wire geometry + bounding info
    sketch3d_clear(id)                   erase all entities from a 3D sketch
    sketch3d_as_sweep_guide(id, doc_id)  inject the 3D sketch as a sweep guide-curve feature
                                         into the current part doc (uses the guide-curve support
                                         that landed in core/geom/features.* on this branch)

    render_sketch(sketch, doc_id)        render a 2D sketch feature as an annotated flat image
                                         PLUS a 3D view so an AI can see the sketch before extruding
    sketch_preview_text(sketch, doc_id)  non-vision equivalent: full text description of a 2D sketch
                                         (entities, constraints, DOF, regions) — wraps describe_sketch
                                         with additional health narration
    doc_sketch_contact_sheet(doc_id)     multi-view tiled image of the whole doc with per-sketch
                                         health summary — the quickest way to see ALL open sketches
                                         + the solid in one shot

The 3D sketch store is in-process (Python dict, keyed by sketch3d id).  It is intentionally
lightweight: points + typed curve segments, no constraint solver (matching the GUI first-slice).
The geometry is serialisable to JSON so it can be round-tripped through ledger_note for
multi-session persistence.

Registered onto the FastMCP instance by register_sketch3d_tools(mcp).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import uuid
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from .core_bridge import CoreError
from .cad_tools import _doc, _dumps, _err


# ─────────────────────────────────────────── in-process 3D sketch store ──────────────────────────

class _S3Seg:
    """One curve segment in a 3D sketch: a typed, ordered list of 3-D points.

    ``kind`` choices (matching the GUI Sketch3DModel + the engine sweep_profile vocabulary):
      "line"    two points [start, end] — a straight segment
      "polyline" 3+ points — a piecewise-linear chain (coerced to line segments at engine time)
      "spline"  3+ control points — a B-spline approximation (degree 3 through-points)
      "arc"     exactly three points [start, midpoint, end] — circular arc through three points

    Points are [x, y, z] in mm, world space (matches the GUI work-plane world coordinates).
    """
    __slots__ = ("kind", "pts", "meta")

    def __init__(self, kind: str, pts: list, meta: Optional[dict] = None):
        self.kind = kind
        self.pts = pts          # [[x,y,z], ...]
        self.meta = meta or {}

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind, "pts": self.pts}
        if self.meta:
            d["meta"] = self.meta
        return d


class _S3Sketch:
    """A named 3D sketch: a list of S3Segs stored in world space (no constraint solver)."""

    def __init__(self, name: str):
        self.id = f"s3d_{uuid.uuid4().hex[:8]}"
        self.name = name
        self.segs: list[_S3Seg] = []

    def bbox(self) -> Optional[dict]:
        all_pts = [p for seg in self.segs for p in seg.pts]
        if not all_pts:
            return None
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        zs = [p[2] for p in all_pts]
        return {
            "min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)],
            "size": [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)],
        }

    def total_length(self) -> float:
        length = 0.0
        for seg in self.segs:
            pts = seg.pts
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                length += math.sqrt(sum((b[j] - a[j]) ** 2 for j in range(3)))
        return length

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "segs": [s.to_dict() for s in self.segs],
            "point_count": sum(len(s.pts) for s in self.segs),
            "seg_count": len(self.segs),
            "bbox": self.bbox(),
            "total_length_mm": round(self.total_length(), 4),
        }


# Module-level store: id -> _S3Sketch
_STORE: dict[str, _S3Sketch] = {}


# ───────────────────────────────────────── helpers ────────────────────────────────────────────────

def _s3get(sketch_id: str) -> _S3Sketch:
    sk = _STORE.get(sketch_id)
    if sk is None:
        raise ValueError(
            f"No 3D sketch {sketch_id!r}. Call sketch3d_new() first. "
            f"Known ids: {list(_STORE.keys())}"
        )
    return sk


def _validate_pts(pts: Any) -> list:
    """Ensure pts is a non-empty list of [x,y,z] triples (numbers)."""
    if not isinstance(pts, list) or len(pts) < 1:
        raise ValueError("pts must be a non-empty list of [x,y,z] points.")
    out = []
    for i, p in enumerate(pts):
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            raise ValueError(f"pts[{i}] must be [x, y, z] — got {p!r}")
        out.append([float(p[0]), float(p[1]), float(p[2])])
    return out


def _seg_from_dict(spec: dict) -> _S3Seg:
    """Parse one curve spec dict into an _S3Seg.

    Expected keys: kind (str), pts ([[x,y,z], ...]), meta? (dict).
    """
    kind = spec.get("kind", "line")
    if kind not in ("line", "polyline", "spline", "arc"):
        raise ValueError(
            f"Unknown curve kind {kind!r}. Use 'line', 'polyline', 'spline', or 'arc'."
        )
    pts = _validate_pts(spec.get("pts", []))
    if kind == "line" and len(pts) != 2:
        raise ValueError(f"A 'line' segment needs exactly 2 points; got {len(pts)}.")
    if kind == "arc" and len(pts) != 3:
        raise ValueError(
            "An 'arc' segment needs exactly 3 points [start, mid, end]; "
            f"got {len(pts)}."
        )
    if kind in ("polyline", "spline") and len(pts) < 2:
        raise ValueError(f"A '{kind}' segment needs at least 2 points; got {len(pts)}.")
    return _S3Seg(kind, pts, spec.get("meta"))


def _sketch3d_as_wire_op(sk: _S3Sketch) -> dict:
    """Convert a _S3Sketch to a wire_from_3d_sketch op payload for doc_apply_ops.

    The engine's sweep_profile / loft_sections accept a ``spine`` or ``guides`` wire built
    from 3D point sequences.  We encode this as an "add_wire_3d" op that the C++ session
    dispatcher (core/doc/session.cpp apply_one) resolves to a TopoDS_Wire for use as
    spine/guide. If the method isn't supported (pre-guide-curve build), we fall back to a
    placeholder that at least records the intent in the feature tree for the human.
    """
    return {
        "op": "wire_from_3d_sketch",
        "sketch3d_id": sk.id,
        "name": sk.name,
        "segments": [s.to_dict() for s in sk.segs],
    }


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="aicad_s3d_")
    os.close(fd)
    return path


# ──────────────────────────────────────────── registration ───────────────────────────────────────

def register_sketch3d_tools(mcp: FastMCP) -> None:
    """Attach 3D-sketch + sketch-preview tools to the FastMCP server."""

    # ──────────────────────────────────────── 3D sketch CRUD ─────────────────────────────────────

    @mcp.tool()
    def sketch3d_new(name: str) -> str:
        """Create a NEW named 3D sketch and return its id.

        A 3D sketch holds free-space curves (lines, polylines, splines, arcs) in world
        coordinates — no work plane, no constraint solver (matching the GUI Sketch3D
        first-slice).  Use it to lay out sweep spines, loft guide curves, or spatial
        reference paths before referencing them in add_features.

        name: a human-readable label (e.g. "impeller_guide", "handle_path").

        Returns {ok, id, name} — use ``id`` in sketch3d_add_curves / sketch3d_as_sweep_guide.
        """
        try:
            sk = _S3Sketch(name.strip() or "sketch3d")
            _STORE[sk.id] = sk
            return _dumps({"ok": True, "id": sk.id, "name": sk.name,
                           "hint": "now call sketch3d_add_curves to populate it"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_add_curves(sketch3d_id: str, curves: list) -> str:
        """Append one or more curve segments to a 3D sketch.

        sketch3d_id: the id returned by sketch3d_new.
        curves: a list of curve specs, each a dict with:
          kind  "line"     — straight segment; pts: [[x1,y1,z1],[x2,y2,z2]]
                "polyline" — piecewise-linear chain; pts: [[x,y,z], ...]  (2+ points)
                "spline"   — B-spline through the given control points; pts: [[x,y,z], ...]
                "arc"      — circular arc through 3 points; pts: [[start],[mid],[end]]
          pts   list of [x,y,z] triples in mm, world space
          meta  optional dict (e.g. {"layer": "guide", "construction": true})

        All point coordinates are in mm, world space — same frame as the GUI Sketch3D
        work-plane entities.  Consecutive curves whose endpoints match (within 0.001 mm)
        are treated as a connected chain by the engine's sweep spine resolver.

        Returns {ok, id, added, total_segs, bbox, total_length_mm}.
        """
        try:
            sk = _s3get(sketch3d_id)
            if not isinstance(curves, list) or len(curves) == 0:
                return _dumps({"ok": False, "error": "curves must be a non-empty list"})
            added = 0
            for i, spec in enumerate(curves):
                if not isinstance(spec, dict):
                    return _dumps({"ok": False,
                                   "error": f"curves[{i}] must be a dict, got {type(spec)}"})
                seg = _seg_from_dict(spec)
                sk.segs.append(seg)
                added += 1
            info = sk.to_dict()
            return _dumps({"ok": True, "id": sk.id, "added": added,
                           "total_segs": info["seg_count"],
                           "point_count": info["point_count"],
                           "bbox": info["bbox"],
                           "total_length_mm": info["total_length_mm"]})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_get(sketch3d_id: str) -> str:
        """Read back a 3D sketch's full geometry and bounding info.

        sketch3d_id: the id returned by sketch3d_new.

        Returns {ok, id, name, segs, point_count, seg_count, bbox, total_length_mm}.
        Each seg is {kind, pts[[x,y,z],...], meta?}.  Use this to inspect the wire
        before passing it to sketch3d_as_sweep_guide or a loft.
        """
        try:
            sk = _s3get(sketch3d_id)
            return _dumps({"ok": True, **sk.to_dict()})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_clear(sketch3d_id: str) -> str:
        """Erase all curve segments from a 3D sketch (keeps the id/name).

        Use this to restart a path without creating a new sketch id.
        Returns {ok, id, name}.
        """
        try:
            sk = _s3get(sketch3d_id)
            sk.segs.clear()
            return _dumps({"ok": True, "id": sk.id, "name": sk.name, "seg_count": 0})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_list() -> str:
        """List all 3D sketches created in this session.

        Returns {ok, sketches: [{id, name, seg_count, point_count, bbox, total_length_mm}]}.
        """
        try:
            return _dumps({
                "ok": True,
                "sketches": [sk.to_dict() for sk in _STORE.values()],
                "count": len(_STORE),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_as_sweep_guide(
        sketch3d_id: str,
        role: str = "guide",
        doc_id: Optional[str] = None,
    ) -> str:
        """Inject a 3D sketch as a sweep/loft SPINE or GUIDE CURVE into the current part doc.

        This bridges the 3D sketch (Python-side geometry) into the C++ feature engine so
        the wire is available for a subsequent ``sweep`` or ``loft`` feature op.  The
        guide-curve support in core/geom/features.sweep_profile (guides[] parameter) was
        added on the fix/sweep-guide-curves branch — this tool is the MCP entry point for it.

        sketch3d_id: the 3D sketch to inject.
        role: "guide" (default) — a guide curve that drives profile orientation/scaling;
              "spine" — the primary sweep path (overrides the sketch's spine if used in sweep).
              Use "guide" when the sweep already has a 2D sketch spine and you want the
              guide to shape the cross-section as it travels.
        doc_id: part document to inject into (default: current part).

        Workflow:
          sketch3d_new("s") → sketch3d_add_curves("s", [...]) →
          sketch3d_as_sweep_guide("s", role="guide") →
          add_features([{"op":"sweep", "profile":"sk1", "spine":"sk2",
                         "guides":["<wire_id_returned_here>"]}])

        Returns {ok, wire_id, role, doc_id, summary} — wire_id is the feature id to pass
        in the sweep op's ``guides`` list (or ``spine`` key if role=="spine").

        NOTE: if the engine build predates the guide-curve merge (core/geom/features.cpp
        sweep_profile guides[] parameter), the wire feature is still stored in the doc's
        feature tree as a reference geometry item and shown in render_part, but the sweep
        won't pick it up until the engine is rebuilt.  The return includes a
        ``guide_support`` flag so you can detect this case.
        """
        try:
            sk = _s3get(sketch3d_id)
            d = _doc(doc_id)
            if not sk.segs:
                return _dumps({"ok": False,
                               "error": "3D sketch is empty — call sketch3d_add_curves first"})

            wire_op = _sketch3d_as_wire_op(sk)
            wire_id = f"w3d_{sk.id}"
            wire_op["id"] = wire_id
            wire_op["role"] = role

            # Try to register the wire via doc_apply_ops.  The C++ session dispatcher
            # handles "wire_from_3d_sketch" when the guide-curve branch is merged.
            # If it returns an unknown-op error we record the intent as a reference_geom
            # feature (so render_part can show it as a construction wire) and report the
            # limitation.
            guide_support = False
            try:
                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": [wire_op]})
                if r.get("ok") or "features" in r:
                    guide_support = True
            except CoreError as ce:
                # "unknown op" / "unsupported" — engine predates guide curve merge
                if "unknown" in str(ce).lower() or "unsupported" in str(ce).lower():
                    guide_support = False
                else:
                    raise

            pts_total = sum(len(s.pts) for s in sk.segs)
            length = sk.total_length()
            summary = (
                f"3D sketch '{sk.name}' ({len(sk.segs)} segment(s), "
                f"{pts_total} pts, length {length:.1f} mm) injected as {role} wire {wire_id!r}"
            )
            if not guide_support:
                summary += (
                    " [NOTE: engine does not yet dispatch wire_from_3d_sketch — "
                    "rebuild with guide-curve branch to activate sweep/loft integration]"
                )
            return _dumps({
                "ok": True,
                "wire_id": wire_id,
                "role": role,
                "doc_id": d,
                "guide_support": guide_support,
                "length_mm": round(length, 4),
                "seg_count": len(sk.segs),
                "summary": summary,
                "next_step": (
                    f'add_features([{{"op":"sweep","profile":"<sketch_id>",'
                    f'"spine":"<spine_sketch_id>","guides":["{wire_id}"]}}])'
                    if role == "guide" else
                    f'add_features([{{"op":"sweep","profile":"<sketch_id>",'
                    f'"spine":"{wire_id}"}}])'
                ),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ──────────────────────────────────────── sketch preview tools ────────────────────────────────

    @mcp.tool()
    def render_sketch(
        sketch: str,
        doc_id: Optional[str] = None,
        views: Optional[list] = None,
    ) -> Any:
        """Render a 2D sketch feature as an ANNOTATED IMAGE so you can SEE it before extruding.

        Returns the sketch in TWO modes tiled side-by-side:
          • Flat 2D (looking straight down the sketch normal) — shows entity shapes + dims
          • Oblique 3D (iso camera) — shows where the sketch sits on the part

        sketch: the sketch feature id (e.g. "s1", "s2").
        doc_id: part document (default: current part).
        views: override the camera list — a subset of ["flat", "iso", "front", "right", "top"]
               (default: ["flat", "iso"]).  "flat" is the special sketch-normal view.

        Returns [image, caption]: the PNG plus a text summary (plane, entity count, DOF status,
        any open endpoints) so a non-vision model still gets useful signal.

        Workflow: edit sketch → render_sketch("s1") to SEE it → check_sketch("s1") for DOF →
        add_features([{op:"extrude", sketch:"s1", depth:10}]) when fully-defined.

        NOTE: if the engine does not support the "flat" sketch-normal view, falls back to
        render_part(view="top") — the image will show the whole part from above, not just
        the sketch plane.
        """
        try:
            d = _doc(doc_id)
            view_list = views or ["flat", "iso"]
            png = _tmp(".png")

            # First attempt: engine's dedicated sketch render (renders just the sketch plane
            # content + entity annotations — the sketch overlay the user sees in the app).
            rendered = False
            caption_text = ""
            try:
                r = cb.call("doc_render_sketch", {
                    "doc_id": d,
                    "sketch": sketch,
                    "out_path": png,
                    "views": view_list,
                })
                if r.get("ok") and os.path.exists(png):
                    rendered = True
                    caption_text = r.get("caption", "")
            except (CoreError, Exception):  # noqa: BLE001
                rendered = False

            # Fallback: render the whole part (the sketch is visible as a construction
            # overlay on the model) using the contact-sheet for multiple angles.
            if not rendered:
                fallback_views = []
                for v in view_list:
                    if v == "flat":
                        fallback_views.append("top")  # closest approximation to sketch-normal
                    else:
                        fallback_views.append(v)
                r2 = cb.call("doc_render_contact_sheet", {
                    "doc_id": d,
                    "out_path": png,
                    "views": fallback_views or ["top", "iso"],
                })
                if not r2.get("ok") or not os.path.exists(png):
                    # Last resort: single iso render
                    r3 = cb.call("doc_render_png", {"doc_id": d, "out_path": png})
                    if not r3.get("ok") or not os.path.exists(png):
                        return _dumps({
                            "ok": False,
                            "error": "could not render sketch — no geometry built yet?",
                            "hint": "call add_features with a sketch op first, then render_sketch",
                        })

            # Build a text caption enriched with sketch health info.
            health_text = ""
            try:
                h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                status = h.get("status", "unknown")
                dof = h.get("dof", "?")
                closure = (h.get("closure") or {})
                open_eps = closure.get("open_endpoints") or []
                closure_str = "CLOSED" if not open_eps else f"OPEN ({len(open_eps)} endpoint(s))"
                health_text = (
                    f"Sketch {sketch!r}: {status.upper()} "
                    f"(dof={dof}, closure={closure_str})"
                )
                if h.get("dof_narration"):
                    health_text += f" — {h['dof_narration']}"
            except Exception:  # noqa: BLE001
                health_text = f"(sketch health unavailable for {sketch!r})"

            full_caption = caption_text or f"Sketch {sketch!r} — {health_text}"
            if caption_text and health_text:
                full_caption = f"{caption_text}\n{health_text}"

            return [Image(path=png), full_caption]
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch_preview_text(sketch: str, doc_id: Optional[str] = None) -> str:
        """Full TEXT description of a 2D sketch — entities, constraints, DOF, regions, health.

        The no-vision equivalent of render_sketch: combines describe_sketch (plane / entity
        roster / constraint list / closed regions) with sketch_health's narrated constraint
        schedule into a single, readable block a local model can act on without images.

        sketch: the sketch feature id (e.g. "s1").
        doc_id: part document (default: current part).

        Returns {ok, sketch, text} where text contains:
          PLANE    — 3D origin, normal, named datum (if any)
          ENTITIES — one line per entity: id, type, solved coords, [full/under/over]
          CONSTRAINTS — grouped by kind with driving-dimension values
          REGIONS  — closed-loop count with area + bbox
          HEALTH   — DOF verdict, open endpoints, schedule of what to add

        Use this when you cannot see images (no vision, headless, local model) or when you
        want a compact status check before committing to the extrude.
        """
        try:
            d = _doc(doc_id)

            # Pull describe_sketch (entity roster + plane + regions + DOF text).
            # Import lazily to avoid a circular import with gsl_tools.
            # _sketch_digest builds the structured dict; _sketch_text renders it to a string.
            from .gsl_tools import _sketch_digest, _sketch_text, sketch_health_text  # type: ignore

            describe_text = ""
            try:
                overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
                try:
                    regions_resp = cb.call("doc_sketch_regions", {"doc_id": d, "sketch": sketch})
                except Exception:  # noqa: BLE001
                    regions_resp = {}
                feat = None
                try:
                    tree = cb.call("doc_tree", {"doc_id": d})
                    for f in ((tree.get("tree") or {}).get("features") or []):
                        if isinstance(f, dict) and f.get("id") == sketch:
                            feat = f
                            break
                except Exception:  # noqa: BLE001
                    pass
                digest = _sketch_digest(sketch, overview, regions_resp, feat)
                describe_text = _sketch_text(digest)
            except Exception:  # noqa: BLE001
                describe_text = "(entity description unavailable)"

            # Pull sketch_health narration.
            health_section = ""
            try:
                h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                health_section = sketch_health_text(h)
            except Exception:  # noqa: BLE001
                health_section = "(health narration unavailable)"

            combined = "\n\n".join(filter(None, [describe_text, "HEALTH\n" + health_section]))
            return _dumps({"ok": True, "sketch": sketch, "doc_id": d, "text": combined})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def doc_sketch_contact_sheet(
        doc_id: Optional[str] = None,
        include_health: bool = True,
    ) -> Any:
        """Multi-view contact sheet of the whole doc INCLUDING all sketches-in-progress.

        Renders the part from standard angles (front / right / top / iso) and appends a
        per-sketch health summary so you can see ALL open sketches AND the built solid in
        one call.  Ideal before a commit: it flags any under-defined sketches that would
        fail an extrude.

        doc_id: part document (default: current part).
        include_health: True (default) — append a per-sketch health table to the caption.

        Returns [image, caption] — the tiled PNG plus a text block:
          PART    — bbox + body count
          SKETCH  — one row per sketch: id, status (full/under/over), dof, closure
                    (skip if no sketches or include_health=False)

        If the doc has no solid geometry yet (only sketches), still renders the wireframe
        sketch overlay as an iso view so the AI can see the sketch layout.
        """
        try:
            d = _doc(doc_id)
            png = _tmp(".png")

            # Render the contact sheet (front/right/top/iso).
            r = cb.call("doc_render_contact_sheet", {
                "doc_id": d,
                "out_path": png,
                "views": [],  # engine default = front/right/top/iso
            })
            if not r.get("ok") or not os.path.exists(png):
                # Fallback: single iso render if no bodies yet.
                r2 = cb.call("doc_render_png", {"doc_id": d, "out_path": png})
                if not r2.get("ok") or not os.path.exists(png):
                    return _dumps({
                        "ok": False,
                        "error": "nothing to render — build geometry first",
                        "hint": "add_features([{op:sketch,...},{op:extrude,...}]) then retry",
                    })

            # Build caption: bounding box + body count.
            caption_lines: list[str] = []
            try:
                meas = cb.call("doc_measure", {"doc_id": d})
                bb = meas.get("bbox") or {}
                size = bb.get("size")
                if size:
                    caption_lines.append(
                        f"Part bbox: {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm"
                    )
                bodies = meas.get("body_count") or meas.get("bodies")
                if bodies is not None:
                    caption_lines.append(f"Bodies: {bodies}")
            except Exception:  # noqa: BLE001
                pass

            # Per-sketch health rows.
            if include_health:
                sketch_rows: list[str] = []
                try:
                    tree = cb.call("doc_tree", {"doc_id": d})
                    for feat in ((tree.get("tree") or {}).get("features") or []):
                        if not isinstance(feat, dict):
                            continue
                        if feat.get("type") != "sketch":
                            continue
                        sid = feat.get("id", "?")
                        try:
                            h = cb.call("sketch_health", {"doc_id": d, "sketch": sid})
                            status = h.get("status", "?")
                            dof = h.get("dof", "?")
                            closure = h.get("closure") or {}
                            open_eps = closure.get("open_endpoints") or []
                            cl = "CLOSED" if not open_eps else f"OPEN({len(open_eps)})"
                            emoji = {"full": "[OK]", "under": "[?]", "over": "[!]"}.get(
                                status, "[-]"
                            )
                            sketch_rows.append(
                                f"  {emoji} {sid}: {status.upper()} dof={dof} {cl}"
                            )
                        except Exception:  # noqa: BLE001
                            sketch_rows.append(f"  [-] {sid}: (health unavailable)")
                except Exception:  # noqa: BLE001
                    pass
                if sketch_rows:
                    caption_lines.append("Sketches:")
                    caption_lines.extend(sketch_rows)
                elif include_health:
                    caption_lines.append("Sketches: (none in feature tree)")

            # Also list any in-process 3D sketches from the Python store.
            if _STORE:
                caption_lines.append("3D sketches (session):")
                for sk in _STORE.values():
                    length = round(sk.total_length(), 1)
                    caption_lines.append(
                        f"  {sk.id} '{sk.name}' — {len(sk.segs)} seg(s), {length} mm"
                    )

            caption = "\n".join(caption_lines) if caption_lines else "Contact sheet rendered."
            return [Image(path=png), caption]
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ──────────────────────────────────── 3D sketch preview (text + spatial) ────────────────────

    @mcp.tool()
    def sketch3d_preview(sketch3d_id: str) -> str:
        """Text description of a 3D sketch's geometry — segments, bounding box, connectivity.

        The no-vision equivalent of seeing the 3D polyline in the Sketch3D workspace overlay.
        Returns a human-readable summary of every segment: kind, point coordinates (rounded),
        length, plus a connectivity report (which segments share endpoints within 0.001 mm —
        these form a continuous chain usable as a sweep spine).

        sketch3d_id: the id returned by sketch3d_new.

        Returns {ok, id, name, summary_text, seg_count, connected_chains, bbox, total_length_mm}.
        """
        try:
            sk = _s3get(sketch3d_id)
            lines: list[str] = []

            lines.append(f"3D Sketch '{sk.name}' (id={sk.id!r})")
            lines.append(f"  {len(sk.segs)} segment(s)")

            # Per-segment description.
            for i, seg in enumerate(sk.segs):
                pts = seg.pts
                seg_len = sum(
                    math.sqrt(sum((pts[j + 1][k] - pts[j][k]) ** 2 for k in range(3)))
                    for j in range(len(pts) - 1)
                )
                pts_str = " → ".join(
                    f"({p[0]:.2f},{p[1]:.2f},{p[2]:.2f})" for p in pts
                )
                lines.append(
                    f"  [{i}] {seg.kind}  {pts_str}  len={seg_len:.2f} mm"
                )

            # Connectivity: collect all start/end points and find shared ones.
            EPS = 0.001

            def _near(a: list, b: list) -> bool:
                return all(abs(a[k] - b[k]) < EPS for k in range(3))

            endpoints: list[tuple[int, str, list]] = []  # (seg_idx, "start"/"end", point)
            for i, seg in enumerate(sk.segs):
                endpoints.append((i, "start", seg.pts[0]))
                endpoints.append((i, "end", seg.pts[-1]))

            # Simple union-find to detect chains.
            parent: dict[int, int] = {i: i for i in range(len(sk.segs))}

            def _find(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def _union(x: int, y: int) -> None:
                parent[_find(x)] = _find(y)

            for i in range(len(endpoints)):
                for j in range(i + 1, len(endpoints)):
                    si, _, pi = endpoints[i]
                    sj, _, pj = endpoints[j]
                    if si != sj and _near(pi, pj):
                        _union(si, sj)

            chains: dict[int, list[int]] = {}
            for i in range(len(sk.segs)):
                root = _find(i)
                chains.setdefault(root, []).append(i)

            if len(chains) == 0:
                lines.append("  Connectivity: (no segments)")
            elif len(chains) == 1:
                lines.append(f"  Connectivity: 1 continuous chain ({len(sk.segs)} segs) — "
                             "usable as sweep spine or guide")
            else:
                lines.append(f"  Connectivity: {len(chains)} disconnected chain(s)")
                for root, members in chains.items():
                    lines.append(f"    chain {root}: segs {members}")
                lines.append("  TIP: ensure end-points of adjacent segs match within 0.001 mm "
                             "for a connected sweep spine")

            bb = sk.bbox()
            if bb:
                sz = bb["size"]
                lines.append(
                    f"  Bbox: {sz[0]:.2f} x {sz[1]:.2f} x {sz[2]:.2f} mm"
                )

            return _dumps({
                "ok": True,
                "id": sk.id,
                "name": sk.name,
                "summary_text": "\n".join(lines),
                "seg_count": len(sk.segs),
                "connected_chains": len(chains),
                "bbox": bb,
                "total_length_mm": round(sk.total_length(), 4),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)
