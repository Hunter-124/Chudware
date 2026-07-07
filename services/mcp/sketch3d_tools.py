"""3D-sketch + sketch-preview MCP tools — parity with the GUI sketch overlay and 3D sketcher.

Exposes to an AI driver the same capabilities the GUI gives a human in the Sketch3D workspace
(ui/imgui/src/ui/sketch3d/) and the 2D sketch overlay (ui/imgui/src/ui/sketch/):

    sketch3d_new(name)                   create a named 3D polyline sketch (points + segments)
    sketch3d_add_curves(id, curves)      append lines / spline-polylines / arcs-in-space
    sketch3d_get(id)                     read back the wire geometry + bounding info
    sketch3d_clear(id)                   erase all entities from a 3D sketch
    sketch3d_as_sweep_guide(id, doc_id)  record a 3D sketch as a designated sweep guide/spine
                                         and return a READY-TO-APPLY add_feature op for the real
                                         engine `sketch3d` feature (doc_schema §3B), plus the
                                         path/guides ref syntax for the sweep op.  The engine now
                                         plumbs sweep guides[] end-to-end and consumes sketch3d
                                         wires as sweep spine/guide.

TODO (follow-up MCP-helpers pass) — re-point map to the real engine feature:
    The tools below still operate on the SESSION-scoped Python store (_STORE), not on
    engine `sketch3d` features. Full re-pointing is a separate tooling-polish task:
      sketch3d_new/add_curves/clear -> doc_apply_ops add_feature/edit_feature on a
          type:"sketch3d" feature (entity mapping identical to _segs_to_engine_entities);
      sketch3d_get/list             -> read the doc tree's sketch3d features +
          the `sketch3d_health` RPC (entities/wires/DOF);
      sketch3d_preview              -> render from sketch3d_health entities;
      sketch3d_sample_at/section_at -> sample the engine wire (needs a small RPC or
          keep Python-side sampling over health entities);
      sketch3d_to/from_ledger       -> obsolete once sketches live in the document
          (persistence comes free with doc save) — keep for scratch sketches only.
    Contract for all of it: contracts/sketch3d_gui_contract.md.
    sketch3d_sample_at(id, t)           sample a world-space point + tangent on the path at
                                         fractional arc-length t ∈ [0,1] — useful for placing
                                         profile sketch planes along a sweep spine
    sketch3d_section_at(id, t, doc_id)  cross-section the part using the plane ORIENTATION derived
                                         from the 3D sketch tangent at t.  NOTE: eval_section_props
                                         always cuts at the world origin — t selects orientation
                                         only, not cut location.  Use add_section_view with
                                         offset_mm for an offset cut.
    sketch3d_to_ledger()                serialize all session 3D sketches to a JSON payload for
                                         ledger_note persistence across MCP restarts
    sketch3d_from_ledger(payload)       restore 3D sketches from a ledger_note payload

    render_sketch(sketch, doc_id)        render a 2D sketch as an image.  When solid geometry
                                         exists: renders the part from the specified camera views.
                                         When no solid yet (pre-extrude): falls back to a
                                         matplotlib wireframe of the sketch entity coords so the
                                         AI can SEE the sketch before extruding.  For text-only
                                         info (entity list, DOF, constraints) use sketch_preview_text.
    sketch_preview_text(sketch, doc_id)  non-vision equivalent: full text description of a 2D sketch
                                         (entities, constraints, DOF, regions) — wraps describe_sketch
                                         with additional health narration
    doc_sketch_contact_sheet(doc_id)     multi-view tiled image of the whole doc with per-sketch
                                         health summary — the quickest way to see the solid from
                                         all standard angles + a sketch health table in the caption

The 3D sketch store is in-process (Python dict, keyed by sketch3d id).  It is intentionally
lightweight: points + typed curve segments, no constraint solver (matching the GUI first-slice).
The geometry is serialisable to JSON so it can be round-tripped through ledger_note for
multi-session persistence.

Engine RPC methods actually available (verified against core/doc/session.cpp):
  doc_render_png, doc_render_view, doc_render_contact_sheet — solid-body renders
  sketch_health, doc_tree, doc_apply_ops, doc_sketch_overview, doc_sketch_regions
  drawing_add_section — section views (lives in the drawing subsystem, not show_in_app)

Registered onto the FastMCP instance by register_sketch3d_tools(mcp).
"""
from __future__ import annotations

import io
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


def _sketch3d_as_guide_record(sk: _S3Sketch, role: str) -> dict:
    """Serialise a _S3Sketch as a guide-record dict (Python session store bookkeeping)."""
    return {
        "sketch3d_id": sk.id,
        "name": sk.name,
        "role": role,
        "segments": [s.to_dict() for s in sk.segs],
        "total_length_mm": round(sk.total_length(), 4),
        "bbox": sk.bbox(),
    }


def _segs_to_engine_entities(sk: _S3Sketch) -> list:
    """Map session-store segments to engine `sketch3d` entities (doc_schema §3B).

    line (2 pts) -> line {p1,p2}; arc (3 pts) -> arc {p1,pm,p2} (three-point);
    spline -> spline {points}; polyline -> one line entity per consecutive pair.
    Ids are e1..eN in segment order so the engine's wire chaining follows authoring order.
    """
    entities: list = []
    n = 0

    def _next_id() -> str:
        nonlocal n
        n += 1
        return f"e{n}"

    for seg in sk.segs:
        pts = [list(map(float, p)) for p in seg.pts]
        if seg.kind == "line":
            entities.append({"id": _next_id(), "type": "line", "construction": False,
                             "p1": pts[0], "p2": pts[1]})
        elif seg.kind == "arc":
            entities.append({"id": _next_id(), "type": "arc", "construction": False,
                             "p1": pts[0], "pm": pts[1], "p2": pts[2]})
        elif seg.kind == "spline":
            entities.append({"id": _next_id(), "type": "spline", "construction": False,
                             "points": pts})
        else:  # polyline -> straight segments
            for i in range(len(pts) - 1):
                entities.append({"id": _next_id(), "type": "line", "construction": False,
                                 "p1": pts[i], "p2": pts[i + 1]})
    return entities


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="aicad_s3d_")
    os.close(fd)
    return path


def _render_sketch_wireframe(overview: dict, sketch_id: str, out_path: str) -> bool:
    """Render a 2D sketch wireframe to a PNG using matplotlib.

    Reads entity solved-coords from a doc_sketch_overview response and draws each entity
    as a colored line/arc in the sketch's 2D plane.  Returns True if the PNG was written.

    This is the visual half of deliverable (b) — lets an AI SEE a sketch before extruding
    without needing a solid body in the doc.  Works headless (Agg backend).

    Entity types handled:
      line      → straight segment using start/end solved-coords
      circle    → circle drawn from center + radius
      arc       → circular arc from start/end angles
      point     → a crosshair marker
      spline / polyline → polyline through control/solved points
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        return False

    entities = (overview.get("entities") or [])
    if not entities:
        return False

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.set_aspect("equal")
    ax.set_title(f"Sketch {sketch_id!r} — wireframe preview", fontsize=10)
    ax.set_xlabel("u (mm)")
    ax.set_ylabel("v (mm)")
    ax.grid(True, linestyle="--", alpha=0.4)

    color_map = {
        "line": "#2196F3",
        "circle": "#4CAF50",
        "arc": "#FF9800",
        "spline": "#9C27B0",
        "polyline": "#9C27B0",
        "point": "#F44336",
    }

    any_drawn = False
    for ent in entities:
        etype = (ent.get("type") or "").lower()
        color = color_map.get(etype, "#607D8B")
        coords = ent.get("coords") or ent.get("solved_coords") or {}
        eid = ent.get("id", "")

        if etype == "line":
            sx = coords.get("start_x") or coords.get("x1", 0)
            sy = coords.get("start_y") or coords.get("y1", 0)
            ex = coords.get("end_x") or coords.get("x2", 0)
            ey = coords.get("end_y") or coords.get("y2", 0)
            ax.plot([sx, ex], [sy, ey], color=color, linewidth=1.5)
            mx, my = (sx + ex) / 2, (sy + ey) / 2
            ax.annotate(eid, (mx, my), fontsize=6, color=color, alpha=0.7)
            any_drawn = True

        elif etype == "circle":
            cx = coords.get("center_x", 0)
            cy = coords.get("center_y", 0)
            r = coords.get("radius", 1)
            circ = mpatches.Circle((cx, cy), r, fill=False, color=color, linewidth=1.5)
            ax.add_patch(circ)
            ax.annotate(eid, (cx + r * 0.7, cy + r * 0.7), fontsize=6, color=color, alpha=0.7)
            ax.set_xlim(auto=True)
            ax.set_ylim(auto=True)
            any_drawn = True

        elif etype == "arc":
            cx = coords.get("center_x", 0)
            cy = coords.get("center_y", 0)
            r = coords.get("radius", 1)
            a1 = math.degrees(coords.get("start_angle", 0))
            a2 = math.degrees(coords.get("end_angle", math.pi))
            arc_patch = mpatches.Arc((cx, cy), 2 * r, 2 * r,
                                     angle=0, theta1=a1, theta2=a2,
                                     color=color, linewidth=1.5)
            ax.add_patch(arc_patch)
            any_drawn = True

        elif etype in ("spline", "polyline"):
            pts = coords.get("points") or coords.get("control_points") or []
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                ax.plot(xs, ys, color=color, linewidth=1.5, linestyle="--")
                ax.annotate(eid, (xs[0], ys[0]), fontsize=6, color=color, alpha=0.7)
                any_drawn = True

        elif etype == "point":
            px = coords.get("x", 0)
            py = coords.get("y", 0)
            ax.plot(px, py, "+", color=color, markersize=8)
            ax.annotate(eid, (px, py), fontsize=6, color=color, alpha=0.7)
            any_drawn = True

    if not any_drawn:
        plt.close(fig)
        return False

    ax.autoscale_view()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return os.path.exists(out_path)


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
        """Record a 3D sketch as the designated sweep GUIDE or SPINE and get the engine op.

        The engine now has a REAL `sketch3d` feature (doc_schema §3B) and the sweep op
        consumes it as `path` (spine) or a `guides:[...]` entry — guides[] is plumbed
        end-to-end.  This tool converts the session-store 3D sketch into a ready-to-apply
        `add_feature` op (returned as ``feature_op``): apply it via the doc ops path, then
        reference the resulting feature id in the sweep.

        sketch3d_id: the 3D sketch to designate.
        role: "guide" (default) — guide curve driving profile orientation/scaling
                                   (ONE guide per sweep; must differ from the path).
              "spine" — the primary sweep path.
        doc_id: part document context (echoed in the hint).

        Workflow (both roles):
          1. Apply ``feature_op`` (add_feature, type:"sketch3d") -> feature id "fN".
          2. Sweep refs: spine  -> {"path": "fN"}  (or {"path": {"feature":"fN","wire":k}}
             when the sketch has multiple disconnected chains);
             guide -> {"guides": [{"feature":"fN","wire":0}]}.
          Full contract: contracts/sketch3d_gui_contract.md.

        Returns {ok, sketch3d_id, role, guide_support, feature_op, coord_summary,
        workflow_hint}.
        """
        try:
            sk = _s3get(sketch3d_id)
            d = _doc(doc_id)
            if not sk.segs:
                return _dumps({"ok": False,
                               "error": "3D sketch is empty — call sketch3d_add_curves first"})

            record = _sketch3d_as_guide_record(sk, role)
            # Store the designation so sketch3d_list / sketch3d_preview can show it.
            sk.meta = getattr(sk, "meta", {})  # type: ignore[attr-defined]
            if not hasattr(sk, "meta"):
                sk.meta = {}  # type: ignore[attr-defined]

            length = sk.total_length()
            pts_total = sum(len(s.pts) for s in sk.segs)

            # Build a compact coordinate summary the driver can use to recreate the path.
            coord_lines: list[str] = []
            for i, seg in enumerate(sk.segs):
                pts_str = " → ".join(
                    f"({p[0]:.2f},{p[1]:.2f},{p[2]:.2f})" for p in seg.pts
                )
                coord_lines.append(f"  seg[{i}] {seg.kind}: {pts_str}")
            coord_summary = "\n".join(coord_lines)

            feature_op = {
                "op": "add_feature",
                "feature": {
                    "type": "sketch3d",
                    "name": sk.name or f"3DSketch-{role}",
                    "params": {
                        "entities": _segs_to_engine_entities(sk),
                        "constraints": [],
                    },
                },
            }
            if role == "spine":
                workflow_hint = (
                    "1. Apply the returned feature_op via doc ops -> a sketch3d feature id 'fN'.\n"
                    "2. Add the sweep with {\"path\": \"fN\"} (single chain) or "
                    "{\"path\": {\"feature\": \"fN\", \"wire\": k}} for a multi-chain sketch.\n"
                    "The 2D profile sketch must sit at the spine START, roughly perpendicular "
                    "to its tangent. Contract: contracts/sketch3d_gui_contract.md."
                )
            else:
                workflow_hint = (
                    "1. Apply the returned feature_op via doc ops -> a sketch3d feature id 'fN'.\n"
                    "2. On the sweep, set {\"guides\": [{\"feature\": \"fN\", \"wire\": 0}]} "
                    "(ONE guide; it must be a different feature than the path).\n"
                    "The guide drives profile orientation + scaling along the path "
                    "(SolidWorks Guide Curves). Contract: contracts/sketch3d_gui_contract.md."
                )

            return _dumps({
                "ok": True,
                "sketch3d_id": sketch3d_id,
                "role": role,
                "doc_id": d,
                # guide_support=True: the sweep guides[] ref is plumbed end-to-end
                # (doc_apply_ops -> document.cpp sweep branch -> geom::sweep_profile)
                # and sketch3d wires are consumable as spine/guide.
                "guide_support": True,
                "feature_op": feature_op,
                "length_mm": round(length, 4),
                "seg_count": len(sk.segs),
                "point_count": pts_total,
                "coord_summary": coord_summary,
                "workflow_hint": workflow_hint,
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
        """Render the part containing a 2D sketch as a multi-view image.

        Shows the SOLID BODIES from standard camera angles so you can see the model context
        around the sketch.  The caption includes the sketch health summary (DOF, closure).

        IMPORTANT LIMITS:
          • The engine has no sketch-isolated flat-view RPC — there is no "doc_render_sketch"
            method.  This renders solid bodies (doc_render_view / doc_render_contact_sheet),
            not bare sketch wireframes.  If the doc has no solid geometry yet the call will
            report "no bodies" and return an error.
          • For the 2D sketch geometry itself (entity coords, constraint list, DOF, regions)
            use sketch_preview_text — it gives full text without needing a rendered image.
          • For section views through a built solid, use drawing_add_section (drawing subsystem).

        sketch: the sketch feature id (e.g. "s1", "s2").  Used only for the health caption.
        doc_id: part document (default: current part).
        views: camera list — any subset of ["front","right","top","iso","left","back","bottom"]
               (default: ["top", "iso"]).  "top" approximates the sketch-normal view for
               XY-plane sketches.

        Returns [image, caption] — the PNG contact sheet plus a sketch health summary.

        Workflow: add_features([sketch+extrude]) → render_sketch("s1") to see the result →
        sketch_preview_text("s1") for the 2D entity details → iterate.
        """
        try:
            d = _doc(doc_id)
            view_list = views or ["top", "iso"]
            # Map any "flat" alias to "top" (there is no sketch-normal RPC).
            view_list = ["top" if v == "flat" else v for v in view_list]
            png = _tmp(".png")

            # Try contact sheet first (multiple views tiled).
            rendered = False
            try:
                r = cb.call("doc_render_contact_sheet", {
                    "doc_id": d,
                    "out_path": png,
                    "views": view_list,
                })
                rendered = r.get("ok") and os.path.exists(png)
            except Exception:  # noqa: BLE001
                rendered = False

            # Fallback: single iso render.
            if not rendered:
                try:
                    r2 = cb.call("doc_render_view", {
                        "doc_id": d,
                        "out_path": png,
                        "view": "iso",
                    })
                    rendered = r2.get("ok") and os.path.exists(png)
                except Exception:  # noqa: BLE001
                    rendered = False

            if not rendered:
                # Fallback: matplotlib wireframe of the 2D sketch entities.
                # This is the visual path for pre-extrude / no-solid-yet workflows.
                try:
                    overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
                    wf_ok = _render_sketch_wireframe(overview, sketch, png)
                except Exception:  # noqa: BLE001
                    wf_ok = False
                if wf_ok:
                    rendered = True
                    # Build a minimal health caption for the wireframe case.
                    try:
                        h = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
                        status = h.get("status", "unknown")
                        dof = h.get("dof", "?")
                        health_text = (
                            f"Sketch {sketch!r} wireframe (pre-extrude): "
                            f"{status.upper()} dof={dof}"
                        )
                    except Exception:  # noqa: BLE001
                        health_text = f"Sketch {sketch!r} wireframe (no solid yet)"
                    return [Image(path=png), health_text]
                return _dumps({
                    "ok": False,
                    "error": (
                        "could not render — no solid geometry built yet and "
                        "sketch entity coords not available. "
                        "Call sketch_preview_text for text-only sketch info."
                    ),
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

            caption = f"Part render (views: {', '.join(view_list)})\n{health_text}"
            return [Image(path=png), caption]
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

    # ──────────────────────────────────── 3D sketch path sampling ────────────────────────────────

    @mcp.tool()
    def sketch3d_sample_at(sketch3d_id: str, t: float = 0.5) -> str:
        """Sample a POINT on a 3D sketch path at fractional arc-length parameter t ∈ [0, 1].

        Walks the polyline chain (in segment order) to the fraction t of its total arc-length
        and returns the interpolated world-space point plus the local tangent direction.  Useful
        for placing profile sketches along a sweep spine, sizing a section cut, or locating a
        datum point on a guide curve.

        sketch3d_id: the 3D sketch to sample.
        t: fraction of total arc-length, 0.0 = start of first segment, 1.0 = end of last.
           Values outside [0, 1] are clamped.

        Returns {ok, t, point:[x,y,z], tangent:[dx,dy,dz] (unit vector), seg_index,
                 seg_t (local t within that segment), total_length_mm}.

        Example: sketch3d_sample_at(id, 0.5) → midpoint of the sweep spine.
        Use this point as a ``plane_origin`` for a profile sketch:
          add_features([{op:'sketch', plane:'custom',
                         plane_origin: <point>, plane_normal: <tangent>}])
        """
        try:
            sk = _s3get(sketch3d_id)
            if not sk.segs:
                return _dumps({"ok": False, "error": "3D sketch is empty"})
            t_clamped = max(0.0, min(1.0, float(t)))
            total = sk.total_length()
            if total < 1e-9:
                # Degenerate zero-length sketch: return first point.
                first_pt = sk.segs[0].pts[0]
                return _dumps({
                    "ok": True, "t": t_clamped, "point": first_pt,
                    "tangent": [0.0, 0.0, 1.0], "seg_index": 0,
                    "seg_t": 0.0, "total_length_mm": 0.0,
                })
            target = t_clamped * total
            walked = 0.0
            for seg_idx, seg in enumerate(sk.segs):
                pts = seg.pts
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    step = math.sqrt(sum((b[k] - a[k]) ** 2 for k in range(3)))
                    if walked + step >= target - 1e-9:
                        local_t = (target - walked) / step if step > 1e-9 else 0.0
                        local_t = max(0.0, min(1.0, local_t))
                        px = a[0] + local_t * (b[0] - a[0])
                        py = a[1] + local_t * (b[1] - a[1])
                        pz = a[2] + local_t * (b[2] - a[2])
                        # Tangent = direction of this sub-segment, normalised.
                        tlen = math.sqrt((b[0] - a[0]) ** 2 +
                                         (b[1] - a[1]) ** 2 +
                                         (b[2] - a[2]) ** 2)
                        if tlen > 1e-12:
                            tx = (b[0] - a[0]) / tlen
                            ty = (b[1] - a[1]) / tlen
                            tz = (b[2] - a[2]) / tlen
                        else:
                            tx, ty, tz = 0.0, 0.0, 1.0
                        seg_local_t = (walked + local_t * step) / total
                        return _dumps({
                            "ok": True,
                            "t": t_clamped,
                            "point": [round(px, 4), round(py, 4), round(pz, 4)],
                            "tangent": [round(tx, 6), round(ty, 6), round(tz, 6)],
                            "seg_index": seg_idx,
                            "seg_t": round(seg_local_t, 6),
                            "total_length_mm": round(total, 4),
                        })
                    walked += step
            # Reached end (floating point edge case): return last point.
            last_seg = sk.segs[-1]
            last_pt = last_seg.pts[-1]
            prev_pt = last_seg.pts[-2] if len(last_seg.pts) > 1 else last_seg.pts[0]
            dx = last_pt[0] - prev_pt[0]
            dy = last_pt[1] - prev_pt[1]
            dz = last_pt[2] - prev_pt[2]
            tlen = math.sqrt(dx * dx + dy * dy + dz * dz)
            tang = [dx / tlen, dy / tlen, dz / tlen] if tlen > 1e-12 else [0.0, 0.0, 1.0]
            return _dumps({
                "ok": True, "t": t_clamped,
                "point": [round(last_pt[0], 4), round(last_pt[1], 4), round(last_pt[2], 4)],
                "tangent": tang,
                "seg_index": len(sk.segs) - 1,
                "seg_t": 1.0,
                "total_length_mm": round(total, 4),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_section_at(
        sketch3d_id: str,
        t: float = 0.5,
        doc_id: Optional[str] = None,
    ) -> str:
        """Cross-section the part with a plane whose ORIENTATION is derived from the 3D sketch.

        ENGINE LIMIT: eval_section_props always cuts at the WORLD ORIGIN (0,0,0) — the engine's
        reserved datum planes (top/front/right) are fixed at origin.  The parameter ``t`` selects
        the tangent direction at that arc-length fraction, which in turn selects the nearest
        reserved plane ORIENTATION.  The cut LOCATION is always the origin regardless of t.

        This is useful for:
          • Verifying the cross-sectional shape of a part whose centroid is near the origin.
          • Picking the right section orientation (XY vs XZ vs YZ) based on the sweep direction.
          • Use sketch3d_sample_at(id, t) separately to get the actual on-path POINT.

        To see a section at an offset location, use the drawing subsystem:
          add_section_view(doc_id, plane='top', offset_mm=25)

        sketch3d_id: 3D sketch path whose tangent at t selects the plane orientation.
        t: arc-length fraction — selects which segment's tangent drives the plane choice.
           0.0 = first segment tangent, 0.5 = mid-path tangent, 1.0 = last segment tangent.
        doc_id: part document to section (default: current part).

        Plane selection from dominant tangent axis:
          • tangent primarily along X → section plane 'right'  (YZ, normal = +X)
          • tangent primarily along Y → section plane 'front'  (XZ, normal = +Y)
          • tangent primarily along Z → section plane 'top'    (XY, normal = +Z)

        Returns {ok, t, point (on-path world point), tangent, plane_used,
                 area_mm2 (at-origin cross-section area), centroid, Ixx, Iyy, Ixy, summary}.
        area_mm2 will be 0 if the origin-plane misses the solid (body not centred at origin).

        Workflow example:
          sketch3d_sample_at(id, 0.5)   → get on-path midpoint world coords
          sketch3d_section_at(id, 0.5)  → check which plane the path tangent selects + area
          add_features([{op:'sweep', profile:'sk1', spine:'sk2'}])
          sketch3d_section_at(id, 0.0)  → confirm plane orientation of the sweep profile face
        """
        try:
            sk = _s3get(sketch3d_id)
            d = _doc(doc_id)
            if not sk.segs:
                return _dumps({"ok": False, "error": "3D sketch is empty"})

            # Sample the path at t to get the cutting plane normal.
            t_clamped = max(0.0, min(1.0, float(t)))
            total = sk.total_length()
            target = t_clamped * total
            walked = 0.0
            point = None
            tangent = None
            for seg in sk.segs:
                pts = seg.pts
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    step = math.sqrt(sum((b[k] - a[k]) ** 2 for k in range(3)))
                    if walked + step >= target - 1e-9:
                        local_t = (target - walked) / step if step > 1e-9 else 0.0
                        local_t = max(0.0, min(1.0, local_t))
                        point = [a[j] + local_t * (b[j] - a[j]) for j in range(3)]
                        tlen = math.sqrt(sum((b[j] - a[j]) ** 2 for j in range(3)))
                        if tlen > 1e-12:
                            tangent = [(b[j] - a[j]) / tlen for j in range(3)]
                        else:
                            tangent = [0.0, 0.0, 1.0]
                        break
                    walked += step
                if point is not None:
                    break
            if point is None:
                last_seg = sk.segs[-1]
                point = last_seg.pts[-1]
                tangent = [0.0, 0.0, 1.0]

            # Map dominant tangent axis to reserved plane name.
            tx, ty, tz = tangent[0], tangent[1], tangent[2]
            abs_axes = [abs(tx), abs(ty), abs(tz)]
            dominant = abs_axes.index(max(abs_axes))
            plane_map = {0: "right", 1: "front", 2: "top"}
            plane_used = plane_map[dominant]

            # Call engine section probe.
            try:
                res = cb.call("eval_section_props", {"doc_id": d, "plane": plane_used})
            except Exception:  # noqa: BLE001
                res = {"area_mm2": 0.0}

            area = res.get("area_mm2", 0.0)
            summary = (
                f"Section plane='{plane_used}' (at world origin, normal from tangent at t={t_clamped:.2f} "
                f"tangent=({tx:.2f},{ty:.2f},{tz:.2f})): "
                f"area = {area:.4g} mm^2"
            )
            if not area or area == 0:
                summary += (
                    " — area=0: the origin-plane may not intersect the solid "
                    "(body not centred at origin). Use add_section_view with offset_mm for "
                    "an offset cut."
                )

            return _dumps({
                "ok": True,
                "t": t_clamped,
                "point": [round(p, 4) for p in point],
                "tangent": [round(v, 6) for v in tangent],
                "plane_used": plane_used,
                "area_mm2": area,
                "centroid": res.get("centroid"),
                "Ixx": res.get("Ixx"),
                "Iyy": res.get("Iyy"),
                "Ixy": res.get("Ixy"),
                "summary": summary,
                "doc_id": d,
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ──────────────────────────────────── ledger / session persistence ────────────────────────────

    @mcp.tool()
    def sketch3d_to_ledger() -> str:
        """Serialize the ENTIRE 3D sketch session store to a JSON string for ledger_note persistence.

        The in-process 3D sketch store is lost when the MCP server restarts (it's a Python dict,
        not persisted to the C++ doc).  Call this at the end of a design session and pass the
        returned ``payload`` to ledger_note(run_id, 'sketch3d_state', payload) so future sessions
        can restore the paths via sketch3d_from_ledger.

        Returns {ok, sketch_count, payload} where payload is a JSON string that
        sketch3d_from_ledger can round-trip back into the store.
        """
        try:
            data = {
                "version": 1,
                "sketches": [sk.to_dict() for sk in _STORE.values()],
            }
            payload = json.dumps(data)
            return _dumps({
                "ok": True,
                "sketch_count": len(_STORE),
                "payload": payload,
                "hint": (
                    "Pass `payload` to ledger_note(run_id, 'sketch3d_state', payload). "
                    "Restore with sketch3d_from_ledger(payload)."
                ),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sketch3d_from_ledger(payload: str) -> str:
        """Restore 3D sketches from a ledger_note payload created by sketch3d_to_ledger.

        payload: the JSON string returned by sketch3d_to_ledger (retrieved from ledger_get).

        Merges the restored sketches into the current session store — sketches whose id
        already exists in the store are skipped (not overwritten).  Returns {ok, restored,
        skipped, ids} so you can tell which sketches came back.

        Typical multi-session workflow:
          Session A: sketch3d_new → sketch3d_add_curves → sketch3d_to_ledger
                     → ledger_note(run_id, 'sketch3d_state', payload)
          Session B: ledger_get(run_id) → locate sketch3d_state entry
                     → sketch3d_from_ledger(payload) → sketch3d_list → continue
        """
        try:
            data = json.loads(payload)
            version = data.get("version", 1)
            if version != 1:
                return _dumps({"ok": False,
                               "error": f"unsupported sketch3d_state version {version}"})
            sketches_raw = data.get("sketches", [])
            restored: list[str] = []
            skipped: list[str] = []
            for raw in sketches_raw:
                sid = raw.get("id")
                if not sid:
                    continue
                if sid in _STORE:
                    skipped.append(sid)
                    continue
                sk = _S3Sketch(raw.get("name", "restored"))
                sk.id = sid  # restore original id
                for seg_raw in raw.get("segs", []):
                    try:
                        seg = _seg_from_dict(seg_raw)
                        sk.segs.append(seg)
                    except Exception:  # noqa: BLE001
                        pass  # skip malformed segments
                _STORE[sk.id] = sk
                restored.append(sk.id)
            return _dumps({
                "ok": True,
                "restored": len(restored),
                "skipped": len(skipped),
                "ids": restored,
                "skipped_ids": skipped,
                "hint": "call sketch3d_list() to confirm the restored sketches",
            })
        except json.JSONDecodeError as e:
            return _err(ValueError(f"payload is not valid JSON: {e}"))
        except Exception as e:  # noqa: BLE001
            return _err(e)
