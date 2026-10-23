"""CAM (computer-aided machining) MCP tools — toolpath generation, feeds & speeds,
material-removal simulation, and post-processed G-code, driven through the C++ core
(libchudware ``cam_*`` RPCs). Mirrors the in-app CAM workspace.

Call ``cad_guide("cam")`` for the workflow. The typical loop:
    cam_new_job -> cam_add_tool -> cam_extract (stock/holes/regions from the part)
    -> cam_add_setup -> cam_add_operation -> cam_generate -> cam_simulate -> cam_post.

Registered onto the FastMCP instance by ``register_cam_tools(mcp)``.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import core_bridge as cb
from .core_bridge import CoreError
from .cad_common import (  # engine-side registry env default + model-dialect guards
    _describe_type, _sanitize_output_path, _wire_materials_registry,
    coerce_json_or_repr, require_mapping,
)
from .cad_tools import _CUR  # the "current part" doc_id (shared with the modeling tools)

# The "current CAM job" so single-argument calls just work.
_CAM: dict = {"doc_id": None}

# Point the C++ core at the bundled materials registry at IMPORT time:
# the CuttingDB folds the registry's ``cam`` sub-sections in, and material_cam_id /
# material_resolve_key (used by _resolve_cam_material) read it. Import time matters — the
# core caches the registry once per process, so the env must be set before the first
# engine RPC can spawn/attach an engine without it.
_wire_materials_registry()


def _resolve_cam_material(material: str) -> tuple[str, Optional[str]]:
    """Map a material id/alias to the CuttingDB group id the feeds calculator resolves.

    Accepts any registry material ID (DoD): a registry id/alias that carries a ``cam`` fold-in is
    translated to its cutting-group id; a bare CuttingDB group id (or an unknown id) passes through
    unchanged. Returns (resolved_id, note) — ``note`` is a non-fatal advisory when the material has
    no cam correspondence (feeds will use the calculator's defaults). Resolution goes through the
    C++ core's ``material_cam_id`` / ``material_resolve_key`` methods."""
    m = str(material or "").strip() or "aluminum"
    try:
        gid = cb.call("material_cam_id", {"name": m}).get("result")
        if gid:
            return gid, None
        # Not a cam-annotated registry material. If it is a known registry material at all, keep the
        # user's id but flag that it has no dedicated feeds row; else pass through (may be a raw
        # CuttingDB group id the core knows).
        if cb.call("material_resolve_key", {"name": m}).get("result") is not None:
            return m, f"material '{m}' has no CAM feeds data in the registry; using calculator defaults"
    except Exception:  # noqa: BLE001
        pass
    return m, None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2)


def _err(e: Exception) -> str:
    if isinstance(e, CoreError):
        return _dumps({"ok": False, "error": str(e), "detail": getattr(e, "detail", None)})
    return _dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _job_stats(tree: Optional[dict]) -> Optional[dict]:
    """Dense counts from a cam_apply_ops/cam_tree ``tree`` blob — tools/setups/operations —
    so mutating tools can confirm a measurable job size instead of echoing the whole nested
    tree (which cam_job_tree already exists to fetch on demand)."""
    if not isinstance(tree, dict):
        return None
    setups = tree.get("setups") or []
    n_ops = sum(len((su or {}).get("operations") or []) for su in setups if isinstance(su, dict))
    return {"tools": len(tree.get("tools") or []), "setups": len(setups), "operations": n_ops}


def _job() -> str:
    d = _CAM["doc_id"]
    if not d:
        raise ValueError("No current CAM job. Call cam_new_job first.")
    return d


def _apply(ops: list) -> dict:
    """Apply a batch of cam_apply_ops edits and trim the (potentially large) echoed job
    ``tree`` down to dense ``job_stats`` counts — callers layer a specific ``changes``
    summary on top of this (see cam_add_tool / cam_add_setup / etc.)."""
    r = cb.call("cam_apply_ops", {"doc_id": _job(), "base_revision": -1, "ops": ops})
    stats = _job_stats(r.pop("tree", None))
    if stats is not None:
        r["job_stats"] = stats
    return r


def add_tool_from_dict(tool: dict) -> dict:
    """Add (or replace by number) one tool in the current CAM job's library.

    Module-level seam shared by ``cam_add_tool`` and the parts-library
    ``cam_tool_import`` tool (services/mcp/parts_tools.py). Fills the same
    defaults cam_add_tool historically applied; raises when no CAM job is
    current, or when ``type`` is not a tool type the engine knows (callers
    surface both as data)."""
    # LIVE-DIALECT guard: `dict(tool or {})` crashes on a stringified tool dict (a bare
    # ValueError with no naming of the offence) — this seam is shared with parts_tools.py's
    # cam_tool_import, which forwards a model-supplied `tool` verbatim. Coerce first.
    t = dict(require_mapping(tool, "tool", "a tool dict, e.g. {'number':1,'type':'endmill_flat'}")) if tool else {}
    number = int(t.get("number", 0))
    # VALUE guard (not a shape guard): tool_type_from_string falls back to a FLAT ENDMILL
    # for anything it does not recognise, so a wrong-but-plausible type silently rewrites
    # the tool AND the cutting-data class feeds/speeds resolves from it. Canonicalize here,
    # before the name is derived from it, so the record and its label agree.
    t["type"] = _resolve_tool_type(t.get("type") or "endmill_flat")
    t.setdefault("diameter", 6.0)
    t.setdefault("flutes", 2)
    t.setdefault("corner_radius", 0.0)
    t.setdefault("tool_material", "carbide")
    t.setdefault("tip_angle", 0.0)
    t.setdefault("thread_pitch", 0.0)
    if not t.get("name"):
        t["name"] = f"T{number} {t['type']}"
    t["number"] = number
    r = _apply([{"op": "add_tool", "tool": t}])
    stats = r.get("job_stats") or {}
    r["changes"] = (f"tool #{number} added ({t['type']}, \u2300{t['diameter']:g}mm, "
                     f"{t['flutes']}fl); library now has {stats.get('tools', '?')} tool(s)")
    return r


def existing_tool_numbers() -> list[int]:
    """Tool numbers already present in the current CAM job's library (sorted).

    Reads the same job tree ``cam_job_tree`` renders. Raises when no CAM job
    is current."""
    r = cb.call("cam_tree", {"doc_id": _job()})
    tree = r.get("tree") if isinstance(r.get("tree"), dict) else r
    nums: list[int] = []
    for t in (tree.get("tools") or []):
        try:
            nums.append(int(t.get("number")))
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(set(nums))


# ───────────────── tool `type` value guard (silent-fall-through class) ────────────────────
#
# core/cam/model.cpp:100 resolves the string with
#     ToolType tool_type_from_string(const std::string& s)
#         { return lookup_rev(kTool, s, ToolType::EndmillFlat); }
# — an unknown value falls back to a FLAT ENDMILL, silently. Nothing downstream ever sees
# the string the caller wrote, so the tool record, the cutting-data class the feeds/speeds
# calculator resolves, and every rpm/feed derived from it are all wrong while the call
# reports success. Measured against this engine (2026-07-29), stored `type` in cam_tree:
#     "drill"     -> drill          (correct)
#     "ball"      -> endmill_flat   (a ball nose became a flat endmill)
#     "DRILL"     -> endmill_flat   (case alone silently changed the tool)
#     "driller"   -> endmill_flat   name kept "T5 driller", type did not
# `cutter_type='drill'` reconciles onto this parameter, so the plausible-but-wrong value is
# exactly what arrives. A rejection costs one turn; wrong feeds/speeds cost a part, so the
# value is validated strictly here.
#
#: Mirror of core/cam/model.cpp's kTool table (all 24 rows, in table order). Used as the
#: floor when the engine is unreachable; the live ``cam_tool_types`` RPC is folded in below
#: so a type the engine gains is never rejected by a stale copy of this list.
_TOOL_TYPES_KTOOL = (
    "endmill_flat", "endmill_ball", "endmill_bull", "chamfer_mill", "face_mill", "vbit",
    "slot_mill", "dovetail_mill", "thread_mill", "drill", "spot_drill", "center_drill",
    "tap", "reamer", "boring_bar", "turning_general", "grooving_tool", "threading_tool",
    "parting_tool", "boring_bar_id", "laser_head", "plasma_torch", "waterjet_head", "probe",
)
#: Equivalent spellings a caller plausibly writes for a type that exists. Every entry maps
#: to the tool the words plainly mean — never a near-miss guess (a bare "mill" or "cutter"
#: names no specific tool and stays rejected, because picking one would be the very silent
#: substitution this guard exists to stop).
_TOOL_TYPE_ALIASES = {
    "flat": "endmill_flat", "endmill": "endmill_flat", "end_mill": "endmill_flat",
    "flat_endmill": "endmill_flat", "endmill_square": "endmill_flat",
    "square_endmill": "endmill_flat", "flatmill": "endmill_flat",
    "ball": "endmill_ball", "ballnose": "endmill_ball", "ball_nose": "endmill_ball",
    "ballmill": "endmill_ball", "ball_endmill": "endmill_ball",
    "ball_end_mill": "endmill_ball", "endmill_ballnose": "endmill_ball",
    "bull": "endmill_bull", "bullnose": "endmill_bull", "bull_nose": "endmill_bull",
    "torus": "endmill_bull", "toroidal": "endmill_bull", "corner_radius": "endmill_bull",
    "chamfer": "chamfer_mill", "chamfermill": "chamfer_mill",
    "facemill": "face_mill", "shell_mill": "face_mill",
    "v_bit": "vbit", "vcarve": "vbit", "engraver": "vbit", "engraving_bit": "vbit",
    "slotmill": "slot_mill", "slotting_mill": "slot_mill",
    "dovetail": "dovetail_mill",
    "threadmill": "thread_mill", "thread_milling_cutter": "thread_mill",
    "drill_bit": "drill", "twist_drill": "drill", "driller": "drill", "jobber": "drill",
    "spotdrill": "spot_drill", "spotting_drill": "spot_drill",
    "centerdrill": "center_drill", "centre_drill": "center_drill",
    "tapping_tool": "tap", "thread_tap": "tap",
    "boringbar": "boring_bar", "boring": "boring_bar",
    "id_boring_bar": "boring_bar_id", "internal_boring_bar": "boring_bar_id",
    "turning_tool": "turning_general", "turning": "turning_general",
    "grooving": "grooving_tool", "groover": "grooving_tool",
    "threading": "threading_tool",
    "parting": "parting_tool", "parting_blade": "parting_tool", "cutoff": "parting_tool",
    "laser": "laser_head", "plasma": "plasma_torch", "waterjet": "waterjet_head",
    "probe_tool": "probe", "touch_probe": "probe",
}
#: Engine-reported ids, folded in on first use (the RPC is authoritative; the mirror above
#: only guarantees the guard still works when the engine cannot be reached).
_TOOL_TYPES_CACHE: dict = {"ids": None}


def _tool_type_vocabulary() -> tuple[str, ...]:
    """Accepted ``type`` ids: the engine's own ``cam_tool_types`` list unioned with the
    kTool mirror.

    The engine's answer is cached (the enum cannot change while it runs), but ONLY when the
    RPC actually answered — a momentarily-unreachable engine must not pin the mirror-only
    floor for the rest of the process."""
    ids = _TOOL_TYPES_CACHE["ids"]
    if ids is not None:
        return ids
    live: list[str] = []
    try:
        for row in (cb.call("cam_tool_types", {}).get("tool_types") or []):
            tid = row.get("id") if isinstance(row, dict) else row
            if isinstance(tid, str) and tid:
                live.append(tid)
    except Exception:  # noqa: BLE001 — engine unreachable; the mirror alone is the floor
        return tuple(_TOOL_TYPES_KTOOL)
    ids = tuple(_TOOL_TYPES_KTOOL) + tuple(t for t in live if t not in _TOOL_TYPES_KTOOL)
    _TOOL_TYPES_CACHE["ids"] = ids
    return ids


def _resolve_tool_type(value: Any) -> str:
    """Validate a CAM tool ``type``, case/whitespace-insensitively.

    A canonical id passes through unchanged; a plain equivalent spelling is mapped. Anything
    else raises ValueError naming the whole vocabulary, because the engine would otherwise
    substitute a flat endmill and feed the wrong cutting-data class to feeds/speeds."""
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    word = raw.strip().lower()
    vocab = _tool_type_vocabulary()
    if word in vocab:
        return word
    canon = _TOOL_TYPE_ALIASES.get(word)
    if canon in vocab:
        return canon
    raise ValueError(
        f"unknown tool type {raw!r} — no tool was added. Use one of: "
        + ", ".join(vocab)
        + " (cam_tool_types() lists them with their cutting-data class). Short forms are "
        "accepted too: flat|endmill -> endmill_flat; ball|ballnose -> endmill_ball; "
        "bull|bullnose -> endmill_bull; chamfer -> chamfer_mill; facemill -> face_mill; "
        "v_bit|vcarve -> vbit; drill_bit|twist_drill -> drill. An unrecognised type does "
        "NOT error in the engine: it silently becomes endmill_flat, so the feeds/speeds "
        "you get back would be a flat endmill's.")


# ───────────────── extract `kind` value guard (same silent-fall-through) ───────────────────
#
# core/cam/extract.cpp:343-366 chains if/else-if on `kind` and its final ``else`` is the
# "all" payload — so an unrecognised kind quietly returns "all" instead of what was asked
# for. Measured (2026-07-29) on a 50x50x10 plate, response keys:
#     kind="section"  -> [bbox, regions]                    the planar regions asked for
#     kind="regions"  -> [bbox, holes, mesh, points, stock]  NO regions at all
#     kind="STOCK"    -> [bbox, holes, mesh, points, stock]  case alone changed the answer
# The consequence is milder than a wrong tool (a caller reading ["regions"] gets a KeyError
# rather than bad geometry), but it is the same defect, so it gets the same treatment.
_EXTRACT_KINDS = ("all", "stock", "mesh", "section", "sections", "holes", "profile")
_EXTRACT_KIND_ALIASES = {
    "regions": "section", "region": "section", "slice": "section",
    "cross_section": "section", "z_section": "section",
    "levels": "sections", "waterline": "sections", "z_levels": "sections",
    "slices": "sections",
    "drill_points": "holes", "drills": "holes", "hole": "holes", "points": "holes",
    "silhouette": "profile", "turning_profile": "profile",
    "bbox": "stock", "bounds": "stock", "stock_bounds": "stock",
    "triangles": "mesh", "tessellation": "mesh", "tris": "mesh",
    "everything": "all", "": "all",
}


def _resolve_extract_kind(value: Any) -> str:
    """Validate a ``cam_extract`` kind, case/whitespace-insensitively."""
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    word = raw.strip().lower()
    if word in _EXTRACT_KINDS:
        return word
    canon = _EXTRACT_KIND_ALIASES.get(word)
    if canon:
        return canon
    raise ValueError(
        f"unknown extract kind {raw!r} — nothing was extracted. Use one of: "
        + " | ".join(_EXTRACT_KINDS)
        + ". Also accepted: regions|slice -> section; levels|waterline -> sections; "
        "drill_points|points -> holes; silhouette -> profile; bounds -> stock; "
        "triangles -> mesh. An unrecognised kind does NOT error in the engine: it returns "
        "the 'all' payload, which carries no `regions` and no `profile`.")


#: Geometry containers `cam_add_operation` forwards to the engine. An operation whose
#: geometry dict carries ONLY empty ones cannot generate a single move.
_GEOMETRY_SLOTS = ("regions", "contours", "points", "profile", "mesh")


def _current_part_step() -> Optional[str]:
    """Export the current part to a temp STEP for the cam_extract RPC, or None when there
    is no current part. Same export cam_extract itself performs."""
    doc = _CUR.get("doc_id")
    if not doc:
        return None
    p = os.path.join(tempfile.gettempdir(), f"chudware_cam_stock_{os.getpid()}.step")
    cb.call("export_file", {"doc_id": doc, "path": p, "format": "step"})
    return p


def _materialize_offset_stock(st: dict) -> tuple[dict, Optional[str]]:
    """Turn an OFFSET stock into a concrete box around the current part's bbox.

    ``box_offset`` is the engine's default stock type AND the default this tool hands the
    core, but core/cam/stock_sim.cpp 181-190 resolves it from ``min``/``max`` — which a
    standalone CAM job (cam_new_job carries no model) never has. Measured live 2026-07-30:
    a setup created with the tool's own default stock simulated as
    ``{ok: false, error: "stock has zero extent", stock_volume_mm3: 0}`` while the SAME job
    with an explicit box gave stock 120000.0 / removed 41936.0 / machined 0.349. So the
    documented default ("a box-offset of the model bbox") silently produced an unsimulatable
    setup. Resolve the bbox here, which is exactly what the docstring already promises.
    """
    kind = str(st.get("type") or "").strip().lower()
    if kind not in ("box_offset", "cylinder_offset"):
        return st, None
    if st.get("min") is not None and st.get("max") is not None:
        return st, None            # the caller already pinned the extent
    try:
        path = _current_part_step()
        if not path:
            return st, ("stock type 'box_offset' needs a part to offset and there is no "
                        "current part — simulation will report 'stock has zero extent'. "
                        "Build/open the part first, or pass an explicit "
                        "stock={'type':'box','min':[...],'max':[...]}.")
        box = ((cb.call("cam_extract", {"path": path, "kind": "stock"})
                .get("stock") or {}).get("box") or {})
        lo, hi = box.get("min"), box.get("max")
        if not (isinstance(lo, list) and isinstance(hi, list) and len(lo) == 3 == len(hi)):
            return st, None
    except Exception as e:  # noqa: BLE001
        return st, (f"could not measure the current part to resolve the '{kind}' stock "
                    f"({type(e).__name__}: {e}); simulation may report a zero-extent stock")
    oxy = float(st.get("off_xy") or 0.0)
    otop = float(st.get("off_ztop") or 0.0)
    obot = float(st.get("off_zbot") or 0.0)
    out = dict(st)
    out["type"] = "box"
    out["min"] = [lo[0] - oxy, lo[1] - oxy, lo[2] - obot]
    out["max"] = [hi[0] + oxy, hi[1] + oxy, hi[2] + otop]
    return out, (f"stock '{kind}' resolved against the current part's bbox to "
                 f"box min={out['min']} max={out['max']} (off_xy={oxy}, off_ztop={otop}, "
                 f"off_zbot={obot}) — an unresolved offset stock has no extent and "
                 f"simulates as 0 mm^3")


def _toolpath_digest(tp: Any) -> Any:
    """A toolpath without its per-move array.

    Measured live 2026-07-30: ONE pocket2d on a 100x60x20 block returned **894,116 chars**
    from cam_generate — 4,500 individual move objects — which is larger than most context
    windows and carries nothing a caller can act on that the digest below does not. The
    G-code is cam_post's job; cam_generate's job is to say whether paths exist and how big
    they are."""
    if not isinstance(tp, dict):
        return tp
    moves = tp.get("moves")
    if not isinstance(moves, list):
        return tp
    kinds: dict = {}
    for mv in moves:
        if isinstance(mv, dict):
            kinds[str(mv.get("kind", "?"))] = kinds.get(str(mv.get("kind", "?")), 0) + 1
    out = {k: v for k, v in tp.items() if k != "moves"}
    out["move_count"] = len(moves)
    out["moves_by_kind"] = dict(sorted(kinds.items()))
    return out


#: A posted line that commands machine motion: a G0/G1/G2/G3 word, or a bare axis word on a
#: modal-motion line (every dialect here emits `X1.234 Y5.678` continuation lines).
_MOTION_RE = re.compile(r"^\s*(?:N\d+\s+)?(?:G0*[0-3](?![\d.])|[XYZABCUVW]-?[\d.])")


def _motion_line_count(gcode: str) -> int:
    return sum(1 for ln in str(gcode or "").splitlines() if _MOTION_RE.match(ln))


def _cam_display(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_cam_display(item) for item in value) + "]"
    if isinstance(value, dict):
        return ", ".join(f"{key}={_cam_display(item)}" for key, item in value.items())
    return str(value)


def _wcs_display(wcs: Any) -> str:
    if not isinstance(wcs, dict):
        return _cam_display(wcs) if wcs not in (None, "") else ""
    offset = wcs.get("offset")
    label = f"G{int(offset)}" if isinstance(offset, (int, float)) else ""
    sub = wcs.get("offset_sub")
    if sub:
        label += f".1 P{int(sub)}"
    origin = wcs.get("origin")
    if origin is not None:
        label += (": " if label else "") + f"origin {_cam_display(origin)}"
    return label


def _stock_display(stock: Any) -> str:
    if not isinstance(stock, dict):
        return _cam_display(stock) if stock not in (None, "") else ""
    pieces = []
    stock_type = stock.get("type")
    if stock_type:
        pieces.append(str(stock_type))
    lo, hi = stock.get("min"), stock.get("max")
    if (isinstance(lo, (list, tuple)) and isinstance(hi, (list, tuple))
            and len(lo) >= 3 and len(hi) >= 3):
        try:
            size = [float(hi[i]) - float(lo[i]) for i in range(3)]
            pieces.append(" x ".join(f"{axis:g}" for axis in size) + " mm")
        except (TypeError, ValueError):
            pieces.append(f"min {_cam_display(lo)} / max {_cam_display(hi)}")
    elif stock.get("radius") is not None and stock.get("height") is not None:
        pieces.append(
            f"diameter {2 * float(stock['radius']):g} x {_cam_display(stock['height'])} mm")
    material = stock.get("material")
    if material:
        pieces.append(str(material))
    return " — ".join(pieces)


def _first_present(mapping: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", [], {}):
            return value
    for container in ("extra", "cutting_overrides"):
        nested = mapping.get(container)
        if not isinstance(nested, dict):
            continue
        for name in names:
            value = nested.get(name)
            if value not in (None, "", [], {}):
                return value
    return None

def _tool_holder_display(tool: dict) -> str:
    holder = _first_present(tool, ("holder", "holder_name", "holder_id"))
    if holder is not None:
        return _cam_display(holder)
    dims = []
    for key, label in (("holder_diameter", "diameter"), ("holder_length", "length")):
        value = tool.get(key)
        if isinstance(value, (int, float)) and value > 0:
            dims.append(f"{label} {_cam_display(value)} mm")
    return ", ".join(dims)


def _tool_stickout_display(tool: dict) -> str:
    value = _first_present(
        tool, ("stickout", "stickout_mm", "tool_stickout", "tool_stickout_mm"))
    return f"{_cam_display(value)} mm" if value is not None else ""


def _setup_sheet_details(tree: dict) -> tuple[str, str]:
    html_parts = [
        "<section class='operator-details'><h2>Setup, Stock &amp; Fixture Details</h2>"
    ]
    text_parts = ["", "SETUP, STOCK & FIXTURE DETAILS"]
    wrote = False
    for setup in tree.get("setups") or []:
        if not isinstance(setup, dict):
            continue
        name = str(setup.get("name") or setup.get("id") or "Setup")
        wcs = _wcs_display(setup.get("wcs"))
        datum = _first_present(setup, ("datum", "datum_notes", "datum_note"))
        fixture = _first_present(
            setup, ("fixturing", "fixture", "fixture_notes", "fixture_note"))
        stock = _stock_display(setup.get("stock"))
        html_parts.append(f"<h3>{html.escape(name)}</h3><dl>")
        text_parts.append(f"  {name}")
        for label, value in (
            ("WCS", wcs),
            ("Datum", datum),
            ("Stock", stock),
            ("Fixture notes", fixture),
        ):
            if value not in (None, ""):
                shown = _cam_display(value)
                html_parts.append(
                    f"<dt><b>{html.escape(label)}</b></dt><dd>{html.escape(shown)}</dd>")
                text_parts.append(f"    {label}: {shown}")
                wrote = True
        html_parts.append("</dl>")
        operations = [op for op in (setup.get("operations") or []) if isinstance(op, dict)]
        if operations:
            html_parts.append(
                "<table><tr><th>#</th><th>Operation</th><th>WCS</th><th>Datum</th>"
                "<th>Fixture notes</th></tr>")
            text_parts.append("    OPERATIONS — WCS / DATUM / FIXTURE")
            for index, operation in enumerate(operations, 1):
                op_name = str(operation.get("name") or operation.get("id") or f"Operation {index}")
                op_wcs = _wcs_display(operation.get("wcs") or setup.get("wcs"))
                op_datum = _first_present(
                    operation, ("datum", "datum_notes", "datum_note")) or datum or ""
                op_fixture = _first_present(
                    operation, ("fixturing", "fixture", "fixture_notes", "fixture_note"),
                ) or fixture or ""
                values = [str(index), op_name, op_wcs, _cam_display(op_datum),
                          _cam_display(op_fixture)]
                html_parts.append("<tr>" + "".join(
                    f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
                text_parts.append(
                    f"      {index}. {op_name} | WCS: {op_wcs} | Datum: "
                    f"{_cam_display(op_datum)} | Fixture: {_cam_display(op_fixture)}")
                wrote = True
            html_parts.append("</table>")
    tools = [tool for tool in (tree.get("tools") or []) if isinstance(tool, dict)]
    tool_rows = []
    for tool in tools:
        holder = _tool_holder_display(tool)
        stickout = _tool_stickout_display(tool)
        if holder or stickout:
            tool_rows.append((tool, holder, stickout))
    if tool_rows:
        html_parts.append(
            "<h2>Tool Holder &amp; Stickout</h2><table><tr><th>T#</th><th>Tool</th>"
            "<th>Holder</th><th>Stickout</th></tr>")
        text_parts.extend(["", "TOOL HOLDER & STICKOUT"])
        for tool, holder, stickout in tool_rows:
            number = _cam_display(tool.get("number", ""))
            name = str(tool.get("name") or tool.get("type") or "")
            html_parts.append("<tr>" + "".join(
                f"<td>{html.escape(value)}</td>"
                for value in (number, name, holder, stickout)) + "</tr>")
            text_parts.append(
                f"  T{number} {name} | Holder: {holder} | Stickout: {stickout}")
            wrote = True
        html_parts.append("</table>")
    html_parts.append("</section>")
    if not wrote:
        return "", ""
    return "".join(html_parts), "\n".join(text_parts) + "\n"


def _enrich_setup_sheet(result: Any, tree_response: Any) -> Any:
    if not isinstance(result, dict):
        return result
    tree = tree_response.get("tree") if isinstance(tree_response, dict) else None
    if not isinstance(tree, dict) and isinstance(tree_response, dict):
        tree = tree_response
    if not isinstance(tree, dict):
        return result
    html_detail, text_detail = _setup_sheet_details(tree)
    if not html_detail and not text_detail:
        return result
    enriched = dict(result)
    base_html = enriched.get("html")
    if isinstance(base_html, str) and html_detail:
        marker = "</body>"
        enriched["html"] = (
            base_html.replace(marker, html_detail + marker, 1)
            if marker in base_html else base_html + html_detail
        )
    base_text = enriched.get("text")
    if isinstance(base_text, str) and text_detail:
        enriched["text"] = base_text.rstrip() + "\n" + text_detail
    return enriched


def register_cam_tools(mcp: FastMCP) -> None:
    """Attach the CAM toolpath/post/sim tools to the FastMCP server."""

    # Point the core's CuttingDB at the bundled materials registry before any cam RPC
    # triggers the lazy CuttingDB::builtin() singleton, so cam_materials reflects the registry.
    _wire_materials_registry()

    # ------------------------------------------------------------------ job lifecycle
    @mcp.tool()
    def cam_new_job(name: str = "CAM Job", post: str = "grbl") -> str:
        """Create a new CAM machining job and make it current. `post` is the default
        controller dialect (grbl|linuxcnc|fanuc|haas|mach3|marlin|siemens|centroid;
        see cam_post_dialects). Returns the job tree + doc_id."""
        try:
            r = cb.call("cam_new", {"name": name, "post": post})
            _CAM["doc_id"] = r.get("doc_id")
            r["changes"] = f"created CAM job '{name}' (post={post}, doc_id={r.get('doc_id')})"
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_job_tree() -> str:
        """The current CAM job tree: setups, their stock/wcs/machine, operations, and the
        tool library, plus counts. Read this to see the job state."""
        try:
            return _dumps(cb.call("cam_tree", {"doc_id": _job()}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_undo() -> str:
        """Undo the last CAM edit on the active job (ops, tools, setups, stock)."""
        try:
            r = cb.call("cam_undo", {"doc_id": _job()})
            stats = _job_stats(r.pop("tree", None))
            if stats is not None:
                r["job_stats"] = stats
            r["changes"] = ("nothing to undo" if r.get("noop")
                             else f"undid last CAM edit -> revision {r.get('revision')}")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_redo() -> str:
        """Redo the last undone CAM edit on the active job."""
        try:
            r = cb.call("cam_redo", {"doc_id": _job()})
            stats = _job_stats(r.pop("tree", None))
            if stats is not None:
                r["job_stats"] = stats
            r["changes"] = ("nothing to redo" if r.get("noop")
                             else f"redid last undone CAM edit -> revision {r.get('revision')}")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ tools / stock / setup
    @mcp.tool()
    def cam_add_tool(number: int, type: str = "endmill_flat", diameter: float = 6.0,
                     flutes: int = 2, corner_radius: float = 0.0, name: str = "",
                     tool_material: str = "carbide", tip_angle: float = 0.0,
                     thread_pitch: float = 0.0) -> str:
        """Add (or replace by number) a tool in the library. type: endmill_flat|endmill_ball|
        endmill_bull|chamfer_mill|face_mill|vbit|slot_mill|dovetail_mill|thread_mill|drill|
        spot_drill|center_drill|tap|reamer|boring_bar|boring_bar_id|turning_general|
        grooving_tool|threading_tool|parting_tool|laser_head|plasma_torch|waterjet_head|
        probe (cam_tool_types lists all, with each one's cutting-data class).

        An unknown type is REJECTED, naming the vocabulary: the engine would otherwise
        substitute a flat endmill without a word, and every feed/speed derived from the
        tool would be a flat endmill's. Plain short forms are mapped (ball|ballnose ->
        endmill_ball, flat|endmill -> endmill_flat, chamfer -> chamfer_mill, ...)."""
        try:
            tool = {"number": number, "type": type, "diameter": diameter, "flutes": flutes,
                    "corner_radius": corner_radius, "name": name,
                    "tool_material": tool_material, "tip_angle": tip_angle, "thread_pitch": thread_pitch}
            return _dumps(add_tool_from_dict(tool))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_add_setup(name: str = "Setup1", machine: str = "mill3", material: str = "aluminum",
                      stock: Optional[dict] = None, wcs: Optional[dict] = None,
                      machine_profile: str = "") -> str:
        """Add a machining setup (a stock + WCS + machine/platform). machine: mill3|mill4|mill5|
        router|lathe|millturn|laser|plasma|waterjet. `material` accepts any engine materials
        registry ID/alias (e.g. "aluminum_6061_t6", "6061") or a CuttingDB group id ("aluminum",
        "steel_mild") — a registry id is translated to the group whose feeds/speeds the calculator
        uses. `stock` is a stock dict (e.g. from cam_extract(kind="stock")["stock"]["box"]
        -> {"type":"box","min":[...],"max":[...]}), defaulting to a box-offset of the model bbox.
        `machine_profile` (optional): id from cam_machines() (e.g. "haas_vf2") — merges the full
        machine envelope (travel, rapids, rpm/feed clamps, tool_change_s, accel) into the setup
        machine payload. Returns the new setup_id (+ a material_note when the id has no dedicated
        feeds row)."""
        try:
            resolved, note = _resolve_cam_material(material)
            # LIVE-DIALECT guard: `dict(stock or {...})` crashes on a stringified stock dict.
            stock = require_mapping(stock, "stock", "a stock dict, e.g. {'type':'box_offset'}") if stock else None
            st = dict(stock or {"type": "box_offset", "off_ztop": 1.0})
            st["material"] = resolved
            mach: dict = {"kind": machine}
            mp = str(machine_profile or "").strip()
            if mp:
                try:
                    mr = cb.call("cam_machines", {})
                except Exception as e:  # noqa: BLE001
                    raise ValueError(
                        f"machine profile '{mp}' unavailable — cam_machines failed: {e}"
                    ) from e
                machines = mr.get("machines") if isinstance(mr, dict) else None
                if not isinstance(machines, list):
                    raise ValueError(
                        f"machine profile '{mp}' unavailable — cam_machines returned no list"
                    )
                hit = None
                for m in machines:
                    if isinstance(m, dict) and str(m.get("id", "")) == mp:
                        hit = m
                        break
                if hit is None:
                    ids = sorted(str(m.get("id", "")) for m in machines if isinstance(m, dict))
                    raise ValueError(
                        f"unknown machine profile '{mp}'; valid ids: {ids}"
                    )
                # Map aicad.machines/1 → core Machine wire keys; drop catalog-only fields.
                if hit.get("kind"):
                    mach["kind"] = hit["kind"]
                if hit.get("label"):
                    mach["name"] = hit["label"]
                if "travel_mm" in hit:
                    mach["travel"] = hit["travel_mm"]
                elif "travel" in hit:
                    mach["travel"] = hit["travel"]
                if "rapid_xy_mm_min" in hit:
                    mach["rapid_xy"] = hit["rapid_xy_mm_min"]
                elif "rapid_xy" in hit:
                    mach["rapid_xy"] = hit["rapid_xy"]
                if "rapid_z_mm_min" in hit:
                    mach["rapid_z"] = hit["rapid_z_mm_min"]
                elif "rapid_z" in hit:
                    mach["rapid_z"] = hit["rapid_z"]
                for src, dst in (
                    ("max_rpm", "max_rpm"),
                    ("min_rpm", "min_rpm"),
                    ("max_feed_mm_min", "max_feed"),
                    ("max_feed", "max_feed"),
                    ("tool_change_s", "tool_change_s"),
                    ("accel_mm_s2", "accel"),
                    ("accel", "accel"),
                ):
                    if src in hit and dst not in mach:
                        mach[dst] = hit[src]
            st, stock_note = _materialize_offset_stock(st)
            setup = {"name": name, "machine": mach, "stock": st}
            if wcs:
                # LIVE-DIALECT guard: a stringified `wcs` would otherwise pass through
                # untouched to the engine and produce a contentless engine-side rejection.
                setup["wcs"] = require_mapping(wcs, "wcs", "a WCS dict, e.g. {'origin':[x,y,z]}")
            r = _apply([{"op": "add_setup", "setup": setup, "ref": "_s"}])
            r["setup_id"] = r.get("id_map", {}).get("_s")
            if note:
                r["material_note"] = note
            if stock_note:
                r["stock_note"] = stock_note
            stats = r.get("job_stats") or {}
            r["changes"] = (f"setup '{name}' added (machine={mach.get('kind')}, "
                             f"material={resolved}, stock={st.get('type')}); "
                             f"job now has {stats.get('setups', '?')} setup(s)")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_set_stock(setup_id: str, stock: dict) -> str:
        """Set/replace a setup's stock. stock types: box | box_offset | cylinder |
        cylinder_offset | from_solid | from_sim (see cad_guide('cam')).
        from_sim form: {"type":"from_sim","setup_id":"<id>"} uses the remaining
        stock mesh from a prior cam_simulate of that setup."""
        try:
            # LIVE-CRASH guard (identical shape to run-97813015264134's `'str' object has
            # no attribute 'get'`): `stock.get(...)` below blows up on a stringified stock
            # dict with a bare AttributeError. Coerce/type-check first.
            stock = require_mapping(stock, "stock", "a stock dict, e.g. {'type':'box',...}")
            stock, stock_note = _materialize_offset_stock(stock)
            r = _apply([{"op": "set_stock", "setup_id": setup_id, "stock": stock}])
            if stock_note:
                r["stock_note"] = stock_note
            r["changes"] = f"stock on setup {setup_id} set to type={stock.get('type', '?')}"
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ operations
    @mcp.tool()
    def cam_add_operation(type: str, tool_number: int, setup_id: Optional[str] = None,
                          name: str = "", geometry: Optional[dict] = None,
                          heights: Optional[dict] = None, params: Optional[dict] = None) -> str:
        """Add a machining operation to a setup. type: face|contour2d|pocket2d|slot|chamfer2d|
        bore2d|engrave|adaptive2d|drill|thread_mill|parallel3d|contour3d|scallop3d|pencil3d|
        pocket3d|adaptive_clear3d|radial3d|spiral_finish3d|turn_face|turn_rough|turn_finish|
        turn_groove|turn_thread|turn_part|turn_drill|cut2d|probe (cam_op_types lists all).
        `geometry`: {regions|contours|points|profile|mesh} (use cam_extract to get it).
        `heights`: {clearance,retract,top,bottom}. `params`: any extra op fields (stepover,
        stepdown, stock_to_leave, direction, lead_in/out, cycle, peck, comp_side, ...).
        `params.extra` may carry `datum` and `fixture_notes`; cam_setup_sheet prints them beside
        this operation's effective WCS. Feeds/speeds auto-resolve from material+tool unless you
        pin rpm/feed/etc in params.
        """
        try:
            # LIVE-DIALECT guard: `geometry.get(k)` below (and the bare `op.update(params)`)
            # crash on stringified dicts — same shape as the run-97813015264134 'str' object
            # has no attribute 'get' failure. Coerce each optional structured arg up front.
            geometry = require_mapping(geometry, "geometry", "a geometry dict") if geometry else None
            heights = require_mapping(heights, "heights", "a heights dict, e.g. {'clearance':5}") if heights else None
            params = require_mapping(params, "params", "an op-params dict, e.g. {'stepover':2}") if params else None
            # An operation whose geometry dict names its slots but leaves every one EMPTY
            # is guaranteed to cut nothing. Measured live 2026-07-30: geometry={"regions":[]}
            # (what cam_extract returns when the section Z misses the part) was accepted with
            # "pocket2d operation added (tool #1)", cam_generate then warned "pocket: no
            # regions", and cam_post still answered ok:true with a 9-line header-only
            # program. Reject at the point the mistake is still cheap and locatable.
            named = sorted(k for k in _GEOMETRY_SLOTS if k in geometry) if geometry else []
            if named and not any(geometry.get(k) for k in named):
                raise ValueError(
                    f"{type} operation NOT added: `geometry` carries no geometry — "
                    f"{'/'.join(named)} is empty. An operation with empty geometry generates "
                    "zero toolpaths and posts a header-only program. Get real geometry first: "
                    "cam_extract(kind='section', z=<a Z INSIDE the part>) for regions, "
                    "kind='holes' for drill points, kind='profile' for a turning profile — "
                    "cam_extract(kind='stock') reports the part's bbox so you can pick a Z "
                    "inside it.")
            op: dict = {"type": type, "tool_number": tool_number}
            if name:
                op["name"] = name
            if geometry:
                op["geometry"] = geometry
            if heights:
                op["heights"] = heights
            if params:
                op.update(params)
            o = {"op": "add_operation", "operation": op, "ref": "_o"}
            if setup_id:
                o["setup_id"] = setup_id
            r = _apply([o])
            r["op_id"] = r.get("id_map", {}).get("_o")
            stats = r.get("job_stats") or {}
            geo_kinds = sorted(k for k in ("regions", "contours", "points", "profile", "mesh")
                                if geometry and geometry.get(k)) if geometry else []
            r["changes"] = (f"{type} operation added (tool #{tool_number}"
                             + (f", geometry={'+'.join(geo_kinds)}" if geo_kinds else "")
                             + f"); job now has {stats.get('operations', '?')} operation(s)")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_update_operation(op_id: str, params: dict) -> str:
        """Update fields on an existing operation (merge). e.g. {"stepover":2,"feed":900}."""
        try:
            # LIVE-CRASH guard: `params.keys()` blows up on a stringified params dict.
            params = require_mapping(params, "params", "an op-params dict, e.g. {'stepover':2}")
            r = _apply([{"op": "update_operation", "op_id": op_id, "operation": params}])
            keys = ", ".join(sorted(params.keys())) if params else "(no fields given)"
            r["changes"] = f"operation {op_id} updated: {keys}"
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_remove_operation(op_id: str) -> str:
        """Remove an operation from the active CAM job by *op_id*."""
        try:
            r = _apply([{"op": "remove_operation", "op_id": op_id}])
            stats = r.get("job_stats") or {}
            r["changes"] = (f"operation {op_id} removed; job now has "
                             f"{stats.get('operations', '?')} operation(s)")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ extraction
    @mcp.tool()
    def cam_extract(kind: str = "all", z: Optional[float] = None,
                    z_levels: Optional[list] = None, path: Optional[str] = None,
                    deflection: float = 0.1) -> str:
        """Extract CAM geometry FROM THE CURRENT PART (or a `path` to a STEP/IGES/BREP/STL).
        kind: all | stock | mesh | section (at z) | sections (at z_levels) | holes | profile.
        An unknown kind is REJECTED rather than silently answered with the "all" payload
        (which carries no `regions` and no `profile`); plain equivalents are mapped
        (regions -> section, levels|waterline -> sections, drill_points -> holes).
        Returns stock bounds, a mesh, planar Z-section regions, Z-axis drill points, and/or a
        turning profile — feed these straight into cam_add_setup/cam_add_operation geometry."""
        try:
            # Validate BEFORE the STEP export below: a bad kind should cost nothing.
            kind = _resolve_extract_kind(kind)
            p = path
            if not p:
                # export the current part to a temp STEP, then extract
                doc = _CUR.get("doc_id")
                if not doc:
                    raise ValueError("No current part and no path=. Build/open a part first, or pass path=.")
                p = os.path.join(tempfile.gettempdir(), f"chudware_cam_extract_{os.getpid()}.step")
                cb.call("export_file", {"doc_id": doc, "path": p, "format": "step"})
            params: dict = {"path": p, "kind": kind, "deflection": deflection}
            if z is not None:
                params["z"] = z
            if z_levels is not None:
                params["z_levels"] = z_levels
            return _dumps(cb.call("cam_extract", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ generate / sim / post
    @mcp.tool()
    def cam_generate(setup_id: Optional[str] = None, op_id: Optional[str] = None) -> str:
        """Generate toolpaths for the job (or one setup / one op). Returns a per-toolpath
        DIGEST — bbox, cut length, cycle time, move count and a per-kind move breakdown —
        plus any warnings (e.g. a tool too large for a pocket). The individual moves are not
        echoed: one pocket on a 100x60 block is ~4,500 of them. Post the job for the actual
        motion (cam_post)."""
        try:
            params: dict = {"doc_id": _job()}
            if setup_id:
                params["setup_id"] = setup_id
            if op_id:
                params["op_id"] = op_id
            r = cb.call("cam_generate", params)
            paths = r.get("toolpaths") if isinstance(r, dict) else None
            if isinstance(paths, list):
                r["toolpaths"] = [_toolpath_digest(tp) for tp in paths]
                moves = sum(int(tp.get("move_count") or 0) for tp in r["toolpaths"]
                            if isinstance(tp, dict))
                warns = r.get("warnings") or []
                # A generate that produced NOTHING is the head of the false-green chain that
                # ends in a header-only posted program — say so here, in the summary the
                # caller reads, not only in a `warnings` list that is easy to skim past.
                r["changes"] = (
                    f"generated {len(paths)} toolpath(s), {moves} move(s)"
                    if paths and moves else
                    "generated NO toolpaths — nothing will be posted"
                ) + (f"; {len(warns)} warning(s): "
                     + "; ".join(str(w.get("error") if isinstance(w, dict) else w)
                                 for w in warns[:5]) if warns else "")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_simulate(setup_id: Optional[str] = None, resolution: float = 0.5,
                     check_gouge: bool = True, check_rapid: bool = True,
                     check_holder: bool = False) -> str:
        """Material-removal simulation: removed/remaining volume, machined fraction, gouge,
        rapid-through-stock, and holder/shank collision checks, and estimated cycle time per
        setup. check_holder is off by default (needs tool shank/holder geometry to flag)."""
        try:
            params: dict = {"doc_id": _job(),
                            "options": {"resolution": resolution, "check_gouge": check_gouge,
                                        "check_rapid": check_rapid, "check_holder": check_holder}}
            if setup_id:
                params["setup_id"] = setup_id
            return _dumps(cb.call("cam_simulate", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_post(dialect: Optional[str] = None, setup_id: Optional[str] = None,
                 program_name: str = "Chudware", program_number: int = 1000,
                 sequence_numbers: bool = False) -> str:
        """Post-process to controller G-code. `dialect` overrides the job default. Returns the
        NC program text + line count, MOTION line count, tool changes, and estimated time.

        A program with no motion at all is reported as a FAILURE, not a success: the posted
        text would be a header and an M30 with nothing between them."""
        try:
            params: dict = {"doc_id": _job(),
                            "options": {"program_name": program_name,
                                        "program_number": program_number,
                                        "sequence_numbers": sequence_numbers}}
            if dialect:
                params["dialect"] = dialect
            if setup_id:
                params["setup_id"] = setup_id
            r = cb.call("cam_post", params)
            gcode = r.get("gcode") if isinstance(r, dict) else None
            if isinstance(gcode, str):
                motion = _motion_line_count(gcode)
                r["motion_line_count"] = motion
                if motion == 0:
                    # THE classic false green. Measured live 2026-07-30: an operation whose
                    # geometry produced no regions posted ok:true / line_count:9 /
                    # warnings:[] and a program that is G21 G90 G17 G40 M5 M9 M30 — a valid
                    # NC file that machines nothing. cam_generate DID warn ("pocket: no
                    # regions"); the post swallowed it and answered success.
                    r["ok"] = False
                    r["error"] = (
                        f"posted program has {r.get('line_count', len(gcode.splitlines()))} "
                        "line(s) but NOT ONE motion command — it is a header and an end-of-"
                        "program with nothing between them, and it machines nothing. The "
                        "toolpaths are empty: run cam_generate and read its warnings (a "
                        "pocket with no regions, a tool larger than the pocket, an operation "
                        "with no geometry). Fix the operation, regenerate, then post again.")
                r["changes"] = (f"posted {r.get('line_count', '?')} line(s) of "
                                f"{dialect or 'job-default'} G-code, {motion} motion line(s), "
                                f"{r.get('tool_changes', '?')} tool change(s), "
                                f"est {round(float(r.get('est_time_s') or 0.0), 1)} s")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_setup_sheet(dialect: Optional[str] = None) -> str:
        """Generate the operator setup sheet as backward-compatible HTML + text.

        In addition to the established tool list, per-operation cut length/time, and total time,
        the sheet includes each setup/operation's available WCS and datum, stock and fixture notes,
        plus holder/stickout columns when those fields exist in the CAM job.
        """
        try:
            doc_id = _job()
            params: dict = {"doc_id": doc_id}
            if dialect:
                params["dialect"] = dialect
            result = cb.call("cam_setup_sheet", params)
            try:
                tree = cb.call("cam_tree", {"doc_id": doc_id})
                enriched = _enrich_setup_sheet(result, tree)
            except Exception:  # noqa: BLE001 - enrichment must never erase a valid legacy sheet
                return _dumps(result)
            return _dumps(enriched)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ persistence
    @mcp.tool()
    def cam_save(path: str) -> str:
        """Save the active CAM job to a .cam+ file at *path*. Relative paths use the
        selected project root, never the worker or daemon cwd; without a project root,
        pass an absolute path. The response confirms the resolved path, file existence,
        and size."""
        try:
            out_path = _sanitize_output_path(path)
            r = cb.call("cam_save", {"doc_id": _job(), "path": out_path})
            exists = os.path.exists(out_path)
            size = os.path.getsize(out_path) if exists else 0
            r["path"] = out_path
            r["exists"] = exists
            r["size_bytes"] = size
            r["changes"] = (f"CAM job saved to {out_path} ({size} bytes)" if exists else
                             f"cam_save reported ok but {out_path} was not found on disk after write")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_open(path: str) -> str:
        """Open a .cam+ file and make it the current job."""
        try:
            in_path = _sanitize_output_path(path)
            r = cb.call("cam_open", {"path": in_path})
            _CAM["doc_id"] = r.get("doc_id")
            stats = _job_stats(r.pop("tree", None))
            r["path"] = in_path
            if stats is not None:
                r["job_stats"] = stats
                r["changes"] = (f"opened CAM job from {in_path} ({stats['setups']} setup(s), "
                                 f"{stats['tools']} tool(s), {stats['operations']} operation(s))")
            else:
                r["changes"] = f"opened CAM job from {in_path}"
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ reference data
    @mcp.tool()
    def cam_post_dialects() -> str:
        """List the available post-processor controller dialects."""
        try:
            return _dumps(cb.call("cam_post_dialects", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_materials() -> str:
        """List the cutting-data material database (the feeds & speeds priors)."""
        try:
            return _dumps(cb.call("cam_materials", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ feeds/speeds tools

    @mcp.tool()
    def feeds_speeds(
        material: str,
        tool_type: str,
        tool_dia: float,
        operation: str,
        max_rpm: Optional[float] = None,
        min_rpm: Optional[float] = None,
        max_feed: Optional[float] = None,
        min_feed: Optional[float] = None,
        thread: str = "",
        machine: str = "",
    ) -> str:
        """Compute table-driven rpm / feed / stepdown / stepover envelope for a
        material + tool type + operation (aicad.calc/1, contracts/advisor.md §7).

        Returns a recommended midpoint and the full [min, max] envelope so the
        advisory engine can range-check caller-supplied params. Use
        ``cam_check_feeds_envelope`` to get an advisory item when params are outside
        this range.

        Parameters
        ----------
        material : str
            Material id or alias — any id from ``cam_materials()`` or the simulation
            materials list (``alu_6061``, ``steel_1018``, ``ti_6al4v``, ``abs``,
            ``pla``, ``nylon_pa12``, ``cast_iron_gray``, ``inconel_718``, …).
        tool_type : str
            One of: ``endmill_flat``, ``endmill_ball``, ``drill``, ``tap``,
            ``face_mill``.
        tool_dia : float
            Tool diameter in mm.
        operation : str
            One of: ``roughing``, ``finishing``, ``slotting``, ``drilling``.
        max_rpm, min_rpm, max_feed, min_feed : float, optional
            Machine envelope overrides (mm/min for feeds, RPM for speed). Unset fields
            use conservative benchtop-VMC defaults (max_rpm=15000, max_feed=5000), or
            the selected ``machine`` profile when given.
        thread : str, optional
            When non-empty, include ``answer.tap_drill_mm`` from the tap-drill table
            (e.g. ``M10x1.5`` → 8.5). Prefer this over a separate tap_drill call.
        machine : str, optional
            Machine profile id (``haas_vf2``, ``tormach_770``, …) — clamps rpm/feed
            to that machine's envelope from machines.json / cam_machines().
        """
        from services.research import machining as _m  # noqa: PLC0415
        lim: dict = {}
        if max_rpm  is not None: lim["max_rpm"]  = max_rpm
        if min_rpm  is not None: lim["min_rpm"]  = min_rpm
        if max_feed is not None: lim["max_feed"] = max_feed
        if min_feed is not None: lim["min_feed"] = min_feed
        mat, note = _resolve_cam_material(material)
        result = _m.feeds_speeds(
            mat, tool_type, tool_dia, operation, lim or None,
            thread=thread or "", machine=machine or "",
        )
        if note:
            result.setdefault("notes", []).append(note)
        return _dumps(result)

    @mcp.tool()
    def cam_check_feeds_envelope(
        material: str,
        tool_type: str,
        tool_dia: float,
        operation: str,
        rpm: Optional[float] = None,
        feed: Optional[float] = None,
        stepdown: Optional[float] = None,
        stepover: Optional[float] = None,
        max_rpm: Optional[float] = None,
        min_rpm: Optional[float] = None,
        max_feed: Optional[float] = None,
        min_feed: Optional[float] = None,
    ) -> str:
        """Check whether supplied cutting params fall within the feeds/speeds table
        envelope. Returns an ``aicad.advisory/1`` item (contracts/advisor.md §3) with
        ``severity=hint`` and clamped ``candidate_fixes`` when any param is out of range,
        or ``{ok: true, in_envelope: true}`` when everything is within bounds.

        This is the CAM advisor seam: the CAM stage check battery calls this after
        ``cam_add_operation``; out-of-envelope params are surfaced as advisory hints
        before simulation discovers gouges.
        """
        from services.research import machining as _m  # noqa: PLC0415
        lim: dict = {}
        if max_rpm  is not None: lim["max_rpm"]  = max_rpm
        if min_rpm  is not None: lim["min_rpm"]  = min_rpm
        if max_feed is not None: lim["max_feed"] = max_feed
        if min_feed is not None: lim["min_feed"] = min_feed
        mat, _note = _resolve_cam_material(material)
        adv = _m.check_envelope(
            mat, tool_type, tool_dia, operation, lim or None,
            rpm=rpm, feed=feed, stepdown=stepdown, stepover=stepover,
        )
        if adv is None:
            return _dumps({"ok": True, "in_envelope": True,
                           "message": "All supplied params are within the feeds/speeds table envelope."})
        return _dumps(adv)

    @mcp.tool()
    def tap_drill(thread: str) -> str:
        """Return the standard tap drill diameter for a thread designation
        (aicad.calc/1, contracts/advisor.md §7).

        Accepts metric coarse (``M6x1.0``, ``M8``), metric fine, UNC
        (``1/4-20``, ``#10-24``) and UNF designations. Returns
        ``answer.tap_drill_mm`` (mm) and ``answer.pct_thread`` (typically 75%).
        Use ``research_thread_spec`` for full thread geometry (minor/pitch diameter,
        tensile stress area, etc.).
        """
        from services.research import machining as _m  # noqa: PLC0415
        return _dumps(_m.tap_drill(thread))

    @mcp.tool()
    def cam_tool_types() -> str:
        """List the supported tool types and their cutting-data class."""
        try:
            return _dumps(cb.call("cam_tool_types", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_op_types() -> str:
        """List the operation types the CAM engine supports for cam_add_operation."""
        try:
            return _dumps(cb.call("cam_op_types", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)
