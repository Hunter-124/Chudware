"""Deterministic ASCII / plaintext 2D representations for non-vision models.

Owner item 5 (2D translation half) — PCB layer grids, schematic sheets, sketch
polylines, SPICE/sim decks, and an experimental single-view 3D silhouette. Every
render returns an ASCII block + English summary + structured ``findings`` plus a
stable ``grid`` contract (origin, pitch, y-flip) so models can address cells and
apply targeted edits via :func:`apply_ascii_edits` / the MCP tool of the same name.

Pure helpers accept plain dicts so tests run offline; MCP wrappers optionally
load live ECAD / sketch sessions when a doc_id is given.

Mutation tools (``add_features`` sketch ops, board place/route/via, schematic
place/wire/label) auto-append a **compact** readback so non-vision models get an
immediate ASCII + NL loop without a second tool call. Full renders stay on the
explicit tools. Edit tools accept cell coords from those grids and emit engine
ops (sch_*/pcb_*/sketch_*) for surgical component swaps and section replaces.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Optional, Sequence

from mcp.server.fastmcp import FastMCP

# Compact auto-readback caps (mutation tool responses). Full render tools keep
# their own larger defaults (60/80). Keep context cheap for the agent loop.
READBACK_MAX_H = 40
READBACK_MAX_W = 120
READBACK_MAX_FINDINGS = 8
READBACK_MAX_ENTITIES_NL = 24

# Ops that mutate 2D sketch content inside add_features / doc_apply_ops batches.
SKETCH_MUTATION_OPS: frozenset[str] = frozenset({
    "sketch_add_entity",
    "sketch_edit_entity",
    "sketch_delete_entity",
    "sketch_add_constraint",
    "sketch_delete_constraint",
    "sketch_set_dim",
    "sketch_fully_define",
    "sketch_mirror_entities",
    "sketch_pattern_entities",
    "sketch_convert",
    "sketch_move",
    "sketch_fillet",
    "sketch_chamfer",
    "sketch_paste_entities",
    "sketch_break_external_ref",
})

# --------------------------------------------------------------------------- util


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=False)


def _err(reason: str, hint: str = "", **extra: Any) -> str:
    out = {"ok": False, "error": str(reason), "hint": hint or str(reason)}
    out.update(extra)
    return _dumps(out)


def _xy(p: Any) -> tuple[float, float]:
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return float(p[0]), float(p[1])
    if isinstance(p, dict):
        if "x" in p and "y" in p:
            return float(p["x"]), float(p["y"])
        if "u" in p and "v" in p:
            return float(p["u"]), float(p["v"])
    raise ValueError(f"not a 2D point: {p!r}")


def _clamp_int(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def make_grid_meta(*, xmin: float, ymin: float, xmax: float, ymax: float,
                   pitch: float, width: int, height: int,
                   unit: str = "mm", y_up: bool = True,
                   kind: str = "", layer: str = "") -> dict:
    """Stable cell<->world contract for render + apply-edit round-trips.

    Every rasterizer in this module addresses a cell as the NEAREST LATTICE POINT
    (``_clamp_int((x - xmin) / pitch)``), and sizes the raster as
    ``ceil(span / pitch) + 1`` — a lattice-point count, not a box count. So cell
    (col,row) SAMPLES the world point::

        x = xmin + col * pitch
        y = ymax - row * pitch   when y_up (row 0 is the top of the printed block)

    Live 2026-07-30: this pair used to be documented (and implemented) with a
    half-pitch box-centre offset — ``xmin + (col + 0.5) * pitch`` — which does NOT
    invert the rasterizer. Measured on the 3-rectangle F sketch: the raster placed
    e1.p1 = (10,-6) in cell (1,19), and cell_to_world(1,19) answered (10.5,-6.5),
    while world_to_cell(10,-6) answered (0,18) — a full cell off. apply_ascii_edits
    resolves `to_cell` through cell_to_world, so every cell-addressed edit landed
    half a pitch away from the cell the model was looking at.
    """
    meta = {
        "kind": kind or None,
        "unit": unit,
        "pitch": float(pitch),
        "width": int(width),
        "height": int(height),
        "origin_world": [float(xmin), float(ymin)],
        "bbox": [float(xmin), float(ymin), float(xmax), float(ymax)],
        "y_up": bool(y_up),
        "y_flip": True,  # row 0 is north/top of the printed block
        "cell_addressing": "col_row_0_based",
        "cell_to_world": (
            "x = bbox[0] + col * pitch; "
            "y = bbox[3] - row * pitch  # y_up, row0=top; cell = lattice point, not box centre"
        ),
        "world_to_cell": (
            "col = round((x - bbox[0]) / pitch) clamped 0..width-1; "
            "row = round((bbox[3] - y) / pitch) clamped 0..height-1"
        ),
    }
    if layer:
        meta["layer"] = layer
    return meta


def cell_to_world(col: int, row: int, grid: dict) -> tuple[float, float]:
    """Map ASCII cell (col,row) -> world (x,y) using a render ``grid`` dict.

    Exact inverse of every rasterizer's ``cell()`` in this module — see
    :func:`make_grid_meta` for why the half-pitch variant was wrong."""
    pitch = float(grid.get("pitch") or grid.get("pitch_mm") or 1.0)
    bbox = grid.get("bbox") or grid.get("bbox_mm") or [0, 0, 10, 10]
    xmin, ymin, xmax, ymax = map(float, bbox[:4])
    x = xmin + float(col) * pitch
    y = ymax - float(row) * pitch
    return x, y


def world_to_cell(x: float, y: float, grid: dict) -> tuple[int, int]:
    """Map world (x,y) -> ASCII cell (col,row) using a render ``grid`` dict."""
    pitch = float(grid.get("pitch") or grid.get("pitch_mm") or 1.0)
    bbox = grid.get("bbox") or grid.get("bbox_mm") or [0, 0, 10, 10]
    xmin, ymin, xmax, ymax = map(float, bbox[:4])
    w = int(grid.get("width") or 1)
    h = int(grid.get("height") or 1)
    col = _clamp_int((float(x) - xmin) / pitch, 0, max(0, w - 1))
    row = _clamp_int((ymax - float(y)) / pitch, 0, max(0, h - 1))
    return col, row


def annotate_ascii_with_rulers(ascii_block: str, *, every: int = 10,
                              max_w_label: int = 200) -> str:
    """Prefix a 0-based col ruler + row numbers so models can address cells.

    Example first lines::

        col 0    5    10
        0 | ....R1....
        1 | ..........
    """
    rows = (ascii_block or "").split("\n")
    if not rows or (len(rows) == 1 and rows[0] == ""):
        return ascii_block or ""
    w = max(len(r) for r in rows)
    if w > max_w_label:
        # still label, but keep cheap
        every = max(every, 20)
    # tens ruler
    tens = []
    ones = []
    for i in range(w):
        if i % every == 0:
            label = str(i)
            tens.append(label[0] if len(label) > 1 else " ")
            # multi-digit: write remaining digits into following ones row only for ones
            ones.append(label[-1])
        else:
            tens.append(" ")
            ones.append(str(i % 10) if (i % every) < every else " ")
    # simpler ones: mark every cell's ones digit sparsely
    ones = [("." if i % every else str(i % 10)) for i in range(w)]
    for i in range(0, w, every):
        s = str(i)
        for k, ch in enumerate(s):
            if i + k < w:
                ones[i + k] = ch
    row_w = max(2, len(str(max(0, len(rows) - 1))))
    out = [f"{'':>{row_w}}  {''.join(ones)}", f"{'':>{row_w}} +{'-' * w}"]
    for ri, r in enumerate(rows):
        out.append(f"{ri:>{row_w}} |{r}")
    return "\n".join(out)


# ==================================================================== HIGHLIGHT
# Shared `highlight` target vocabulary + marker / legend / callout plumbing.
#
# ONE vocabulary across every 2D render (mirrors the 3D render tools):
#   "R1"                 bare token — matched against every id/ref/name the
#                        domain knows (sketch entity id, board refdes, net,
#                        schematic ref, SPICE device, node, …)
#   "net:GND"            qualified token — restrict the match to one class
#   "e3,e4"              comma / semicolon separated list
#   '["e3", {"id":"e4"}]' JSON list (or a real Python list) of any of the above
#   {"net": "GND"}       geometry-query style dict → "net:GND"
#
# Markers are single letters A,B,C… (I and O skipped — they read as 1 and 0).
# A highlighted element is drawn with its marker letter in place of its normal
# glyph, and its anchor cell is bracketed:  >A<

_HL_MARKERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_HL_NONE = "-"            # callout marker for "notable but not drawn marked"
_HL_BRACKET_L = ">"
_HL_BRACKET_R = "<"

_HL_QUALIFIERS: frozenset[str] = frozenset({
    "id", "ref", "net", "type", "name", "pin", "pad", "node", "device",
    "label", "power", "wire", "track", "via", "symbol", "entity", "component",
    "junction", "footprint", "poly", "probe", "value", "libid", "box",
})

_HL_DICT_KEYS = ("id", "ref", "net", "name", "type", "pin", "pad", "node",
                 "device", "entity", "target", "sid", "poly", "box")


def parse_highlight(highlight: Any) -> list[str]:
    """Normalize the shared ``highlight`` argument to ordered, de-duped tokens.

    Accepts a bare string, a comma/semicolon list, a JSON list/dict string, a
    real list/tuple, or a geometry-query style dict. Order is preserved because
    marker letters are assigned in token order.
    """
    out: list[str] = []

    def push(tok: str) -> None:
        tok = str(tok).strip()
        if tok and tok not in out:
            out.append(tok)

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, (list, tuple, set)):
            for sub in item:
                walk(sub)
            return
        if isinstance(item, dict):
            for key in _HL_DICT_KEYS:
                val = item.get(key)
                if val is None:
                    continue
                if isinstance(val, (list, tuple)):
                    push(f"{key}:" + ",".join(str(v) for v in val))
                else:
                    push(f"{key}:{val}")
                return
            push(json.dumps(item, sort_keys=True, separators=(",", ":")))
            return
        text = str(item).strip()
        if not text:
            return
        if text[0] in "[{":
            try:
                walk(json.loads(text))
                return
            except Exception:
                pass
        # box:x0,y0,x1,y1 carries its own commas — never split it
        if text.lower().startswith("box:"):
            push(text)
            return
        if "," in text or ";" in text:
            for part in text.replace(";", ",").split(","):
                push(part)
            return
        push(text)

    walk(highlight)
    return out


def _hl_parts(token: str) -> tuple[str, str]:
    """Split ``"qual:value"`` → ``(qual, value)``; bare tokens → ``("", value)``."""
    if ":" in token:
        q, _, v = token.partition(":")
        ql = q.strip().lower()
        if ql in _HL_QUALIFIERS:
            return ql, v.strip()
    return "", token.strip()


class HighlightPlan:
    """Marker assignment + hit recording for one render.

    Domains build ``(qualifier, value)`` candidate pairs per drawable object and
    ask :meth:`marker`; a returned letter means "draw this one emphasised".
    """

    __slots__ = ("tokens", "markers", "callouts", "_matched")

    def __init__(self, highlight: Any = None) -> None:
        self.tokens: list[str] = parse_highlight(highlight)
        self.markers: dict[str, str] = {
            tok: _HL_MARKERS[i % len(_HL_MARKERS)]
            for i, tok in enumerate(self.tokens)
        }
        self.callouts: list[dict] = []
        self._matched: set[str] = set()

    def __bool__(self) -> bool:
        return bool(self.tokens)

    def marker(self, cands: Sequence[tuple[str, Any]]) -> Optional[str]:
        """First requested token matching any ``(qualifier, value)`` candidate."""
        if not self.tokens:
            return None
        low = [(str(q).lower(), str(v).strip().lower())
               for q, v in cands if v is not None and str(v).strip() != ""]
        if not low:
            return None
        for tok in self.tokens:
            q, v = _hl_parts(tok)
            vlow = v.lower()
            for cq, cv in low:
                if cv == vlow and (not q or q == cq):
                    return self.markers[tok]
        return None

    def note(self, marker: str, ident: str, desc: str, *,
             anchor: Optional[tuple[int, int]] = None,
             world: Optional[Sequence[float]] = None,
             cells: int = 0) -> None:
        """Record one callout row: ``marker -> id -> description``."""
        self._matched.add(marker)
        row: dict[str, Any] = {"marker": marker, "id": str(ident), "desc": desc}
        if anchor is not None:
            row["anchor_cell"] = [int(anchor[0]), int(anchor[1])]
        if world is not None:
            row["anchor_world"] = [round(float(v), 4) for v in world]
        if cells:
            row["cells_painted"] = int(cells)
        self.callouts.append(row)

    @property
    def unmatched(self) -> list[str]:
        """Requested tokens that matched nothing in this render."""
        return [t for t in self.tokens if self.markers[t] not in self._matched]

    def report(self) -> dict:
        """Structured highlight state for the render payload."""
        return {
            "requested": list(self.tokens),
            "markers": {t: self.markers[t] for t in self.tokens},
            "matched": sorted(self._matched),
            "unmatched": self.unmatched,
        }


def hl_stamp_anchor(grid: list[list[str]], cx: int, cy: int, marker: str,
                    empty: str = ".") -> None:
    """Force *marker* at (cx,cy) and bracket it ``>A<`` where the cells are free."""
    h = len(grid)
    w = len(grid[0]) if h else 0
    if not (0 <= cy < h and 0 <= cx < w):
        return
    grid[cy][cx] = marker
    if cx - 1 >= 0 and grid[cy][cx - 1] in (empty, " "):
        grid[cy][cx - 1] = _HL_BRACKET_L
    if cx + 1 < w and grid[cy][cx + 1] in (empty, " "):
        grid[cy][cx + 1] = _HL_BRACKET_R


def scale_origin_lines(grid_meta: dict, *, note: str = "") -> list[str]:
    """SCALE / ORIGIN / CONVERT lines so a glyph position converts back to world."""
    if not grid_meta:
        return ["SCALE: unknown (no grid contract for this view).",
                "ORIGIN: unknown."]
    pitch = float(grid_meta.get("pitch") or 1.0)
    unit = str(grid_meta.get("unit") or "mm")
    bbox = list(grid_meta.get("bbox") or [0.0, 0.0, 0.0, 0.0])
    xmin, ymin, xmax, ymax = (float(bbox[0]), float(bbox[1]),
                              float(bbox[2]), float(bbox[3]))
    w = int(grid_meta.get("width") or 0)
    h = int(grid_meta.get("height") or 0)
    x0 = xmin
    y0 = ymax
    lines = [
        f"SCALE: 1 character cell = {pitch:.4g} {unit} square "
        f"(+col = +x / east, +row = -y / south; y is flipped so north is up).",
        f"ORIGIN: cell (col=0,row=0) = world "
        f"({x0:.4g}, {y0:.4g}) {unit}. Grid {w}x{h} cells covers world "
        f"x[{xmin:.4g},{xmax:.4g}] y[{ymin:.4g},{ymax:.4g}] {unit}.",
        f"CONVERT: x = {xmin:.4g} + col*{pitch:.4g} ; "
        f"y = {ymax:.4g} - row*{pitch:.4g}",
    ]
    if note:
        lines.append(note)
    return lines


def highlight_legend_lines(plan: HighlightPlan, mode: str = "grid") -> list[str]:
    """Legend rows explaining the emphasis glyphs.

    ``mode``: ``grid`` (raster with text labels — sketch/pcb/schematic),
    ``plain`` (raster without text labels — the 3D silhouette), or ``rows``
    (line-oriented views such as the simulation roster).
    """
    if not plan:
        return ["  (no highlight requested — pass highlight='<id>' to emphasise "
                "one element and get it named in CALLOUTS)"]
    letters = ", ".join(sorted({plan.markers[t] for t in plan.tokens}))
    if mode == "rows":
        return [
            f"  {_HL_BRACKET_L}{letters}{_HL_BRACKET_R} in the 4-character gutter "
            f"at the start of a row = that row is HIGHLIGHTED under that marker",
            "  every marker maps to exactly one requested target — see CALLOUTS "
            "for the marker -> id map; one target can mark several rows",
        ]
    out = [
        f"  {letters} = HIGHLIGHTED element body — every cell of that element is "
        f"drawn with its marker letter instead of its normal glyph",
        f"  {_HL_BRACKET_L}X{_HL_BRACKET_R} = anchor cell of highlighted element X "
        f"(brackets sit in the two neighbouring cells when free)",
    ]
    if mode == "grid":
        out.append("  marker letters are single characters ON the geometry; "
                   "multi-character runs of text are id/ref labels, not markers "
                   "— see CALLOUTS for the marker -> id map")
    else:
        out.append("  see CALLOUTS for the marker -> id map")
    return out


def callout_lines(plan: HighlightPlan, extra: Sequence[dict] = ()) -> list[str]:
    """``marker -> id -> description`` rows (the ASCII analogue of leader lines)."""
    rows = list(plan.callouts) + list(extra)
    if not rows:
        return ["  (nothing highlighted and nothing flagged in this view)"]
    out: list[str] = []
    for r in rows:
        bits = [f"  {r.get('marker') or _HL_NONE} -> {r.get('id') or '?'} -> "
                f"{r.get('desc') or ''}"]
        if r.get("anchor_cell"):
            c = r["anchor_cell"]
            anchor = f"[anchor cell col={c[0]},row={c[1]}"
            if r.get("anchor_world"):
                wpt = r["anchor_world"]
                anchor += f" = world ({wpt[0]:g},{wpt[1]:g})"
            bits.append(anchor + "]")
        elif r.get("anchor_world"):
            wpt = r["anchor_world"]
            bits.append(f"[world ({wpt[0]:g},{wpt[1]:g})]")
        if r.get("cells_painted"):
            bits.append(f"({r['cells_painted']} cell(s) marked)")
        out.append(" ".join(bits))
    return out


def annotated_ascii(*, header: str, scale_lines: Sequence[str], drawing: str,
                    legend: Sequence[str], callouts: Sequence[str]) -> str:
    """Assemble the self-sufficient plate: header + scale + drawing + legend + callouts."""
    parts: list[str] = []
    if header:
        parts.append(header)
    parts.extend(scale_lines)
    parts.append("")
    parts.append(drawing)
    parts.append("")
    parts.extend(legend)
    parts.append("")
    parts.append("CALLOUTS (marker -> id -> what it is, with its key numbers):")
    parts.extend(callouts)
    return "\n".join(parts)


def unmatched_note(plan: HighlightPlan) -> list[dict]:
    """Honest 'you asked for X and it is not here' rows for the callout list."""
    return [{"marker": _HL_NONE, "id": tok,
             "desc": "REQUESTED BUT NOT FOUND in this view — no element with that "
                     "id/ref/net; check the id list in the summary"}
            for tok in plan.unmatched]


def finding_callouts(findings: Sequence[dict], limit: int = 6) -> list[dict]:
    """Notable-without-highlight rows: whatever the checkers flagged."""
    rows: list[dict] = []
    for f in list(findings)[:limit]:
        ident = (f.get("net")
                 or next(iter(f.get("refs") or []), None)
                 or next(iter(f.get("tracks") or []), None)
                 or next(iter(f.get("entities") or []), None)
                 or f.get("id") or f.get("ref"))
        tail = (f" (NOT marked in the drawing — re-render with "
                f"highlight='{ident}' to see where it is)" if ident else
                " (sketch/board-wide finding — it has no single element to mark)")
        rows.append({
            "marker": _HL_NONE,
            "id": str(ident or f.get("code") or "?"),
            "desc": f"[{f.get('severity', '?')}] {f.get('code')}: "
                    f"{f.get('message')}{tail}",
        })
    return rows


def summary_with_annotations(summary: str, scale_lines: Sequence[str],
                             legend: Sequence[str],
                             callouts: Sequence[str]) -> str:
    """Append the self-sufficient scale/legend/callout block to a narrative summary."""
    parts = [summary.rstrip(), ""]
    parts.extend(scale_lines)
    parts.append("")
    parts.extend(legend)
    parts.append("")
    parts.append("CALLOUTS (marker -> id -> what it is, with its key numbers):")
    parts.extend(callouts)
    return "\n".join(parts)


# =========================================================================== PCB
# Layer ASCII grid: components as refdes boxes, traces as path chars, vias, pads.

_PCB_EMPTY = "."
_PCB_VIA = "o"
_PCB_PAD = "#"
_PCB_H = "-"
_PCB_V = "|"
_PCB_D1 = "/"   # rising (y grows with x in image coords → drawn inverted)
_PCB_D2 = "\\"
_PCB_CROSS = "X"
_PCB_JUNCT = "+"


def _board_bbox(board: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    outline = board.get("outline") or {}
    for sh in outline.get("shapes") or []:
        for key in ("p1", "p2", "start", "end"):
            p = sh.get(key) if isinstance(sh, dict) else None
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
        for p in (sh.get("points") or []) if isinstance(sh, dict) else []:
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
    for fp in board.get("footprints") or []:
        pos = fp.get("pos") or [0.0, 0.0]
        xs.append(float(pos[0])); ys.append(float(pos[1]))
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
                xs.append(float(pos[0]) + px); ys.append(float(pos[1]) + py)
            except Exception:
                pass
    for tr in board.get("tracks") or []:
        for key in ("p1", "p2"):
            p = tr.get(key)
            if p and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if p and len(p) >= 2:
            xs.append(float(p[0])); ys.append(float(p[1]))
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    pad = 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _copper_layers(board: dict) -> list[str]:
    stack = board.get("stackup") or {}
    layers = []
    for L in stack.get("layers") or []:
        if isinstance(L, dict) and L.get("type") == "copper" and L.get("id"):
            layers.append(str(L["id"]))
    if not layers:
        # fall back to layers seen on tracks
        seen: list[str] = []
        for tr in board.get("tracks") or []:
            ly = tr.get("layer") or "F.Cu"
            if ly not in seen:
                seen.append(ly)
        layers = seen or ["F.Cu"]
    return layers


def _world_fp_aabb(fp: dict) -> tuple[float, float, float, float]:
    """World AABB of courtyard (graphics) or pads; mirrors place._fp_extent_local."""
    pos = fp.get("pos") or [0.0, 0.0]
    ox, oy = float(pos[0]), float(pos[1])
    xs: list[float] = []
    ys: list[float] = []
    for g in fp.get("graphics") or []:
        if g.get("layer") not in ("F.CrtYd", "B.CrtYd", None):
            # only courtyard layers when present; if graphics lack layer, still use
            if g.get("layer") not in ("F.CrtYd", "B.CrtYd"):
                continue
        gtype = g.get("type")
        if gtype == "line":
            for key in ("p1", "p2"):
                p = g.get(key)
                if p and len(p) >= 2:
                    xs.append(ox + float(p[0])); ys.append(oy + float(p[1]))
        elif gtype == "rect":
            p1 = g.get("p1") or g.get("start")
            p2 = g.get("p2") or g.get("end")
            if p1 and p2:
                xs.extend([ox + float(p1[0]), ox + float(p2[0])])
                ys.extend([oy + float(p1[1]), oy + float(p2[1])])
        elif gtype == "circle":
            c = g.get("center") or [0.0, 0.0]
            r = float(g.get("radius", 0.0))
            xs.extend([ox + float(c[0]) - r, ox + float(c[0]) + r])
            ys.extend([oy + float(c[1]) - r, oy + float(c[1]) + r])
        elif gtype == "poly" and g.get("points"):
            for p in g["points"]:
                xs.append(ox + float(p[0])); ys.append(oy + float(p[1]))
    if not xs:
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            size = pad.get("size") or [1.0, 1.0]
            w = float(size[0]); h = float(size[1] if len(size) > 1 else size[0])
            xs.extend([ox + px - w / 2.0, ox + px + w / 2.0])
            ys.extend([oy + py - h / 2.0, oy + py + h / 2.0])
    if not xs:
        return (ox - 1.0, oy - 1.0, ox + 1.0, oy + 1.0)
    # small courtyard margin so bare pads still occupy a cell
    m = 0.25
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _aabb_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


def _on_segment(ax: float, ay: float, bx: float, by: float, cx: float, cy: float,
                eps: float = 1e-9) -> bool:
    return (min(ax, cx) - eps <= bx <= max(ax, cx) + eps and
            min(ay, cy) - eps <= by <= max(ay, cy) + eps)


def segments_cross(a1: tuple[float, float], a2: tuple[float, float],
                   b1: tuple[float, float], b2: tuple[float, float],
                   *, share_endpoint_ok: bool = True) -> bool:
    """Proper intersection of open segments (shared endpoint alone is not a cross)."""
    ax, ay = a1; bx, by = a2; cx, cy = b1; dx, dy = b2
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)

    def _z(v: float) -> bool:
        return abs(v) < 1e-9

    # shared endpoint (or collinear endpoint touch) — not an "unintended cross"
    if share_endpoint_ok:
        for p in (a1, a2):
            for q in (b1, b2):
                if abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) < 1e-9:
                    return False

    if ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0)) and \
       ((o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0)):
        return True

    # collinear overlaps count as crossings for different nets
    if _z(o1) and _on_segment(ax, ay, cx, cy, bx, by):
        return True
    if _z(o2) and _on_segment(ax, ay, dx, dy, bx, by):
        return True
    if _z(o3) and _on_segment(cx, cy, ax, ay, dx, dy):
        return True
    if _z(o4) and _on_segment(cx, cy, bx, by, dx, dy):
        return True
    return False


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    pts: list[tuple[int, int]] = []
    dx = abs(x1 - x0); sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0); sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x += sx
        if e2 <= dx:
            err += dx; y += sy
    return pts


def _trace_char(x0: int, y0: int, x1: int, y1: int) -> str:
    dx = x1 - x0; dy = y1 - y0
    if dx == 0 and dy == 0:
        return _PCB_JUNCT
    if abs(dx) >= 2 * abs(dy):
        return _PCB_H
    if abs(dy) >= 2 * abs(dx):
        return _PCB_V
    # image y increases downward; world y increases upward — map after flip
    # in grid, y grows down so rising world slope appears as '\'
    if (dx > 0 and dy > 0) or (dx < 0 and dy < 0):
        return _PCB_D2
    return _PCB_D1


def _paint(grid: list[list[str]], x: int, y: int, ch: str,
           net_grid: list[list[Optional[str]]], net: Optional[str]) -> None:
    h = len(grid); w = len(grid[0]) if h else 0
    if not (0 <= x < w and 0 <= y < h):
        return
    prev = grid[y][x]
    prev_net = net_grid[y][x]
    if prev == _PCB_EMPTY:
        grid[y][x] = ch
        net_grid[y][x] = net
        return
    # same-net junction
    if prev_net is not None and net is not None and prev_net == net:
        if prev != ch and prev not in (_PCB_PAD, _PCB_VIA) and ch not in (_PCB_PAD, _PCB_VIA):
            grid[y][x] = _PCB_JUNCT
        elif ch in (_PCB_PAD, _PCB_VIA):
            grid[y][x] = ch
        return
    # different nets → mark cross (unless one is pad/via of the same cell)
    if prev_net is not None and net is not None and prev_net != net:
        grid[y][x] = _PCB_CROSS
        return
    # overwrite empty-ish with stronger glyph
    priority = {_PCB_EMPTY: 0, _PCB_H: 1, _PCB_V: 1, _PCB_D1: 1, _PCB_D2: 1,
                _PCB_JUNCT: 2, _PCB_VIA: 3, _PCB_PAD: 4, _PCB_CROSS: 5}
    if priority.get(ch, 0) >= priority.get(prev, 0):
        grid[y][x] = ch
        if net is not None:
            net_grid[y][x] = net


def _to_cell(x: float, y: float, xmin: float, ymax: float, pitch: float,
             w: int, h: int) -> tuple[int, int]:
    # flip Y so north is up in the printed block
    cx = _clamp_int((x - xmin) / pitch, 0, w - 1)
    cy = _clamp_int((ymax - y) / pitch, 0, h - 1)
    return cx, cy


_PCB_LEGEND_BASE = (
    "  .   = bare board (no copper on this layer in that cell)",
    "  -   = track running east-west      |   = track running north-south",
    "  /   = track running SW->NE         \\   = track running NW->SE",
    "  +   = track corner/junction, or component courtyard border",
    "  o   = via (spans layers)            #   = pad (copper land)",
    "  X   = two DIFFERENT nets painted into one cell (clearance collision)",
    "  ABC (multi-char run on a courtyard top border) = component refdes label",
)


def _fp_pad_ident(ref: str, pad: dict) -> str:
    num = str(pad.get("number") or pad.get("pin") or pad.get("id") or "?")
    return f"{ref}.{num}"


def render_board_layer_ascii(board: dict, layer: str = "F.Cu",
                             pitch_mm: float = 1.0,
                             max_dim: int = 80,
                             highlight: Any = None,
                             plan: Optional[HighlightPlan] = None) -> dict:
    """Render one copper layer of *board* to a deterministic ASCII grid.

    ``highlight`` accepts the shared target vocabulary: a refdes ("R1"), a
    footprint id ("fp1"), a net name ("GND" / "net:GND"), a track/via id
    ("tA", "v1"), a pad ("R1.1"), or a list of any of those. Matched objects are
    painted with a marker letter and named in the returned ``callouts``.
    """
    if pitch_mm <= 0:
        pitch_mm = 1.0
    hl = plan if plan is not None else HighlightPlan(highlight)
    xmin, ymin, xmax, ymax = _board_bbox(board)
    bw = max(xmax - xmin, pitch_mm)
    bh = max(ymax - ymin, pitch_mm)
    # auto-coarsen pitch so the grid stays readable
    pitch = pitch_mm
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)

    grid = [[_PCB_EMPTY for _ in range(w)] for _ in range(h)]
    net_grid: list[list[Optional[str]]] = [[None for _ in range(w)] for _ in range(h)]

    def force(cx: int, cy: int, ch: str) -> bool:
        if 0 <= cx < w and 0 <= cy < h:
            grid[cy][cx] = ch
            return True
        return False

    # tracks on this layer
    for tr in board.get("tracks") or []:
        if (tr.get("layer") or "F.Cu") != layer:
            continue
        p1 = tr.get("p1"); p2 = tr.get("p2")
        if not (p1 and p2):
            continue
        x0, y0 = _to_cell(float(p1[0]), float(p1[1]), xmin, ymax, pitch, w, h)
        x1, y1 = _to_cell(float(p2[0]), float(p2[1]), xmin, ymax, pitch, w, h)
        ch = _trace_char(x0, y0, x1, y1)
        net = tr.get("net") or None
        cells = _bresenham(x0, y0, x1, y1)
        for cx, cy in cells:
            _paint(grid, cx, cy, ch, net_grid, net)
        mk = hl.marker([("track", tr.get("id")), ("id", tr.get("id")),
                        ("net", net)])
        if mk:
            painted = sum(1 for cx, cy in cells if force(cx, cy, mk))
            hl_stamp_anchor(grid, x0, y0, mk, empty=_PCB_EMPTY)
            length = math.hypot(float(p2[0]) - float(p1[0]),
                                float(p2[1]) - float(p1[1]))
            hl.note(mk, str(tr.get("id") or "track"),
                    f"track on {layer} net={net or '(none)'} "
                    f"({float(p1[0]):g},{float(p1[1]):g})->"
                    f"({float(p2[0]):g},{float(p2[1]):g})mm "
                    f"len={length:.3g}mm width={tr.get('width', '?')}mm",
                    anchor=(x0, y0), world=(float(p1[0]), float(p1[1])),
                    cells=painted)

    # vias (all layers)
    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if not p:
            continue
        cx, cy = _to_cell(float(p[0]), float(p[1]), xmin, ymax, pitch, w, h)
        _paint(grid, cx, cy, _PCB_VIA, net_grid, v.get("net"))
        mk = hl.marker([("via", v.get("id")), ("id", v.get("id")),
                        ("net", v.get("net"))])
        if mk:
            hl_stamp_anchor(grid, cx, cy, mk, empty=_PCB_EMPTY)
            hl.note(mk, str(v.get("id") or "via"),
                    f"via net={v.get('net') or '(none)'} at "
                    f"({float(p[0]):g},{float(p[1]):g})mm "
                    f"dia={v.get('diameter', '?')}mm",
                    anchor=(cx, cy), world=(float(p[0]), float(p[1])), cells=1)

    # pads + component boxes on this copper layer (top side → F.Cu, bottom → B.Cu)
    side_ok = {"F.Cu": "top", "B.Cu": "bottom"}
    want_side = side_ok.get(layer)
    for fp in board.get("footprints") or []:
        side = (fp.get("side") or "top").lower()
        if want_side and side not in (want_side, "both"):
            # still draw if pad layers include this copper
            pass
        pos = fp.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        ref = str(fp.get("ref") or fp.get("id") or "?")
        fp_mk = hl.marker([("ref", ref), ("footprint", ref),
                           ("id", fp.get("id")), ("footprint", fp.get("id")),
                           ("libid", fp.get("fpid")), ("value", fp.get("value"))])
        n_pads = 0
        for pad in fp.get("pads") or []:
            layers = pad.get("layers") or [layer]
            if layer not in layers and "F.Cu" not in layers and "B.Cu" not in layers:
                # permissive: if layers empty, treat as this layer
                if pad.get("layers"):
                    continue
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            cx, cy = _to_cell(ox + px, oy + py, xmin, ymax, pitch, w, h)
            _paint(grid, cx, cy, _PCB_PAD, net_grid, pad.get("net"))
            n_pads += 1
            ident = _fp_pad_ident(ref, pad)
            pad_mk = hl.marker([("pad", ident), ("pin", ident),
                                ("pad", ident.replace(".", ":")),
                                ("id", ident), ("net", pad.get("net"))])
            if pad_mk and pad_mk != fp_mk:
                force(cx, cy, pad_mk)
                hl.note(pad_mk, ident,
                        f"pad {ident} net={pad.get('net') or '(none)'} on {layer} "
                        f"at ({ox + px:g},{oy + py:g})mm "
                        f"size={pad.get('size') or '?'}mm",
                        anchor=(cx, cy), world=(ox + px, oy + py), cells=1)
            elif fp_mk:
                force(cx, cy, fp_mk)

        # stamp refdes into courtyard AABB (deterministic: top-left cell)
        ax0, ay0, ax1, ay1 = _world_fp_aabb(fp)
        c0x, c0y = _to_cell(ax0, ay1, xmin, ymax, pitch, w, h)  # top-left in image
        c1x, c1y = _to_cell(ax1, ay0, xmin, ymax, pitch, w, h)  # bottom-right
        x_lo, x_hi = min(c0x, c1x), max(c0x, c1x)
        y_lo, y_hi = min(c0y, c1y), max(c0y, c1y)
        # draw box border with refdes chars inside top edge
        for x in range(x_lo, x_hi + 1):
            for y in range(y_lo, y_hi + 1):
                edge = (x in (x_lo, x_hi) or y in (y_lo, y_hi))
                if not edge:
                    continue
                if not (0 <= x < w and 0 <= y < h):
                    continue
                if fp_mk:
                    grid[y][x] = fp_mk
                elif grid[y][x] == _PCB_EMPTY:
                    grid[y][x] = "+"
        # write refdes on the top border (left-justified, truncate)
        for i, ch in enumerate(ref[: max(1, x_hi - x_lo)]):
            x = x_lo + i
            y = y_lo
            if 0 <= x < w and 0 <= y < h:
                grid[y][x] = ch
        if fp_mk:
            ccx, ccy = _to_cell(ox, oy, xmin, ymax, pitch, w, h)
            hl_stamp_anchor(grid, ccx, ccy, fp_mk, empty=_PCB_EMPTY)
            hl.note(fp_mk, ref,
                    f"footprint {ref} fpid={fp.get('fpid') or '?'} "
                    f"value={fp.get('value')!r} side={side} "
                    f"origin=({ox:g},{oy:g})mm angle={fp.get('angle', 0)}deg "
                    f"{n_pads} pad(s) on {layer}, courtyard "
                    f"{ax1 - ax0:.3g}x{ay1 - ay0:.3g}mm",
                    anchor=(ccx, ccy), world=(ox, oy),
                    cells=max(0, (x_hi - x_lo + 1) * 2 + (y_hi - y_lo - 1) * 2))

    rows = ["".join(r) for r in grid]
    ascii_block = "\n".join(rows)
    gmeta = make_grid_meta(
        xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
        pitch=pitch, width=w, height=h, unit="mm",
        kind="pcb", layer=layer,
    )
    ruled = annotate_ascii_with_rulers(ascii_block)
    scale_lines = scale_origin_lines(
        gmeta, note=f"LAYER: {layer} — only copper on THIS layer is drawn; "
                    f"vias are drawn on every layer. Other layers are not visible here.")
    legend = ([f"LEGEND (PCB layer {layer}, one character = one grid cell):"]
              + list(_PCB_LEGEND_BASE) + highlight_legend_lines(hl))
    callouts = callout_lines(hl, unmatched_note(hl) if plan is None else ())
    return {
        "layer": layer,
        "pitch_mm": pitch,
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox_mm": [xmin, ymin, xmax, ymax],
        "bbox": [xmin, ymin, xmax, ymax],
        "ascii": ascii_block,
        "ascii_ruled": ruled,
        "ascii_annotated": annotated_ascii(
            header=f"[pcb layer {layer} {w}x{h} cells pitch={pitch:.4g}mm]",
            scale_lines=scale_lines, drawing=ruled,
            legend=legend, callouts=callouts),
        "scale": {"pitch_mm": pitch, "unit": "mm", "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts),
        "highlight": hl.report(),
        "grid": gmeta,
        "index": _board_cell_index(board, layer, xmin, ymax, pitch, w, h),
    }



def _board_cell_index(board: dict, layer: str, xmin: float, ymax: float,
                      pitch: float, w: int, h: int) -> dict:
    """Map cell "col,row" -> object ids on *layer* for targeted edits."""
    cells: dict[str, list[dict]] = {}

    def add(cx: int, cy: int, item: dict) -> None:
        if not (0 <= cx < w and 0 <= cy < h):
            return
        key = f"{cx},{cy}"
        cells.setdefault(key, []).append(item)

    for tr in board.get("tracks") or []:
        if (tr.get("layer") or "F.Cu") != layer:
            continue
        p1 = tr.get("p1"); p2 = tr.get("p2")
        if not (p1 and p2):
            continue
        x0, y0 = _to_cell(float(p1[0]), float(p1[1]), xmin, ymax, pitch, w, h)
        x1, y1 = _to_cell(float(p2[0]), float(p2[1]), xmin, ymax, pitch, w, h)
        for cx, cy in _bresenham(x0, y0, x1, y1):
            add(cx, cy, {
                "kind": "track",
                "id": tr.get("id"),
                "net": tr.get("net"),
                "layer": layer,
                "p1": list(p1),
                "p2": list(p2),
            })
    for v in board.get("vias") or []:
        pos = v.get("pos") or v.get("at")
        if not pos:
            continue
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "via",
            "id": v.get("id"),
            "net": v.get("net"),
            "pos": list(pos),
        })
    for fp in board.get("footprints") or []:
        pos = fp.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        cx, cy = _to_cell(ox, oy, xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "footprint",
            "id": fp.get("id"),
            "ref": fp.get("ref"),
            "fpid": fp.get("fpid"),
            "pos": [ox, oy],
            "side": fp.get("side") or "top",
            "value": fp.get("value"),
        })
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            pcx, pcy = _to_cell(ox + px, oy + py, xmin, ymax, pitch, w, h)
            add(pcx, pcy, {
                "kind": "pad",
                "footprint_id": fp.get("id"),
                "ref": fp.get("ref"),
                "pad": str(pad.get("number") or pad.get("pin") or pad.get("id") or "?"),
                "net": pad.get("net"),
                "pos": [ox + px, oy + py],
            })
    return {k: v for k, v in sorted(cells.items())}


def _pad_nodes(board: dict) -> dict[str, list[str]]:
    """net → list of pad node ids (ref.pin)."""
    out: dict[str, list[str]] = {}
    for fp in board.get("footprints") or []:
        ref = fp.get("ref") or fp.get("id") or "?"
        for pad in fp.get("pads") or []:
            net = pad.get("net") or ""
            if not net:
                continue
            num = str(pad.get("number") or pad.get("pin") or pad.get("id") or "?")
            out.setdefault(net, []).append(f"{ref}.{num}")
    return out


def check_board(board: dict) -> list[dict]:
    """Structured findings: unconnected nets, same-layer crossings, courtyard overlaps."""
    findings: list[dict] = []

    # --- unconnected named nets (multi-pad with no copper path) ---------------
    # Build undirected graph of copper endpoints (pad centers + track ends + vias)
    # keyed by rounded mm so floating noise does not split nodes.
    def _key(x: float, y: float) -> tuple[float, float]:
        return (round(x, 3), round(y, 3))

    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def _find(a: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _union(a: tuple[float, float], b: tuple[float, float]) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    pad_pos: dict[str, list[tuple[str, tuple[float, float]]]] = {}  # net → [(node, key)]
    for fp in board.get("footprints") or []:
        ref = fp.get("ref") or fp.get("id") or "?"
        pos = fp.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        for pad in fp.get("pads") or []:
            net = pad.get("net") or ""
            if not net:
                continue
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
            except Exception:
                px = py = 0.0
            k = _key(ox + px, oy + py)
            parent.setdefault(k, k)
            num = str(pad.get("number") or pad.get("pin") or pad.get("id") or "?")
            pad_pos.setdefault(net, []).append((f"{ref}.{num}", k))

    for tr in board.get("tracks") or []:
        p1 = tr.get("p1"); p2 = tr.get("p2")
        if not (p1 and p2):
            continue
        a = _key(float(p1[0]), float(p1[1]))
        b = _key(float(p2[0]), float(p2[1]))
        parent.setdefault(a, a); parent.setdefault(b, b)
        _union(a, b)
        # also attach to net via endpoint proximity is implicit through pad keys

    for v in board.get("vias") or []:
        p = v.get("pos") or v.get("at")
        if not p:
            continue
        k = _key(float(p[0]), float(p[1]))
        parent.setdefault(k, k)

    # For each net with ≥2 pads: if pad keys not all in one component via tracks that
    # carry that net, flag unconnected. Restrict unions to same-net tracks.
    # Rebuild per-net connectivity:
    named_nets = []
    for n in board.get("nets") or []:
        if isinstance(n, dict) and n.get("name"):
            named_nets.append(str(n["name"]))
        elif isinstance(n, str):
            named_nets.append(n)
    # include nets only present on pads
    for net in pad_pos:
        if net not in named_nets:
            named_nets.append(net)

    for net in sorted(named_nets):
        pads = pad_pos.get(net) or []
        if len(pads) < 2:
            # single-pad named net is a stub — flag as unconnected only if named & multi? skip stubs
            continue
        # per-net UF
        pmap: dict[tuple[float, float], tuple[float, float]] = {}

        def find(a: tuple[float, float]) -> tuple[float, float]:
            pmap.setdefault(a, a)
            while pmap[a] != a:
                pmap[a] = pmap[pmap[a]]
                a = pmap[a]
            return a

        def union(a: tuple[float, float], b: tuple[float, float]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                pmap[rb] = ra

        for _, k in pads:
            pmap.setdefault(k, k)
        for tr in board.get("tracks") or []:
            if (tr.get("net") or "") != net:
                continue
            p1 = tr.get("p1"); p2 = tr.get("p2")
            if not (p1 and p2):
                continue
            a = _key(float(p1[0]), float(p1[1])); b = _key(float(p2[0]), float(p2[1]))
            union(a, b)
            # snap near pads (within 0.5 mm) onto the track ends
            for _, pk in pads:
                if math.hypot(pk[0] - a[0], pk[1] - a[1]) <= 0.5:
                    union(pk, a)
                if math.hypot(pk[0] - b[0], pk[1] - b[1]) <= 0.5:
                    union(pk, b)
        for v in board.get("vias") or []:
            if (v.get("net") or "") != net:
                continue
            p = v.get("pos") or v.get("at")
            if not p:
                continue
            vk = _key(float(p[0]), float(p[1]))
            pmap.setdefault(vk, vk)
            for _, pk in pads:
                if math.hypot(pk[0] - vk[0], pk[1] - vk[1]) <= 0.5:
                    union(pk, vk)

        roots = {find(k) for _, k in pads}
        if len(roots) > 1:
            findings.append({
                "code": "unconnected_net",
                "severity": "error",
                "net": net,
                "pads": [n for n, _ in pads],
                "components": len(roots),
                "message": (f"Named net {net!r} has {len(pads)} pads in "
                            f"{len(roots)} disconnected copper islands"),
            })

    # --- same-layer unintended crossings (different nets) --------------------
    tracks = board.get("tracks") or []
    for i in range(len(tracks)):
        ti = tracks[i]
        li = ti.get("layer") or "F.Cu"
        ni = ti.get("net") or ""
        p1 = ti.get("p1"); p2 = ti.get("p2")
        if not (p1 and p2):
            continue
        a1 = (float(p1[0]), float(p1[1])); a2 = (float(p2[0]), float(p2[1]))
        for j in range(i + 1, len(tracks)):
            tj = tracks[j]
            if (tj.get("layer") or "F.Cu") != li:
                continue
            nj = tj.get("net") or ""
            if ni and nj and ni == nj:
                continue
            q1 = tj.get("p1"); q2 = tj.get("p2")
            if not (q1 and q2):
                continue
            b1 = (float(q1[0]), float(q1[1])); b2 = (float(q2[0]), float(q2[1]))
            if segments_cross(a1, a2, b1, b2):
                findings.append({
                    "code": "trace_crossing",
                    "severity": "error",
                    "layer": li,
                    "nets": sorted({n for n in (ni, nj) if n}),
                    "tracks": [ti.get("id") or f"t{i}", tj.get("id") or f"t{j}"],
                    "message": (f"Unintended crossing on {li}: "
                                f"net {ni or '?'} track {ti.get('id') or i} crosses "
                                f"net {nj or '?'} track {tj.get('id') or j}"),
                })

    # --- overlapping courtyards ----------------------------------------------
    fps = board.get("footprints") or []
    boxes = []
    for fp in fps:
        boxes.append((fp.get("ref") or fp.get("id") or "?", _world_fp_aabb(fp)))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _aabb_overlap(boxes[i][1], boxes[j][1]):
                findings.append({
                    "code": "courtyard_overlap",
                    "severity": "warning",
                    "refs": [boxes[i][0], boxes[j][0]],
                    "message": (f"Overlapping component courtyards: "
                                f"{boxes[i][0]} and {boxes[j][0]}"),
                })

    # stable order
    findings.sort(key=lambda f: (f.get("code", ""), f.get("message", "")))
    return findings


def summarize_board(board: dict, findings: list[dict],
                    layers_rendered: list[str]) -> str:
    fps = board.get("footprints") or []
    tracks = board.get("tracks") or []
    vias = board.get("vias") or []
    nets = board.get("nets") or []
    net_names = []
    for n in nets:
        if isinstance(n, dict) and n.get("name"):
            net_names.append(str(n["name"]))
        elif isinstance(n, str):
            net_names.append(n)
    pad_nets = _pad_nodes(board)
    lines = [
        f"Board {board.get('name') or board.get('id') or '(unnamed)'}: "
        f"{len(fps)} footprint(s), {len(tracks)} track(s), {len(vias)} via(s), "
        f"{len(net_names)} named net(s).",
        f"Copper layers rendered: {', '.join(layers_rendered) or '(none)'}.",
    ]
    if pad_nets:
        lines.append("Net connectivity:")
        for net in sorted(pad_nets):
            pads = pad_nets[net]
            lines.append(f"  {net}: {len(pads)} pad(s) — {', '.join(pads[:8])}"
                         + (f" … +{len(pads)-8}" if len(pads) > 8 else ""))
    else:
        lines.append("Net connectivity: no netted pads.")

    n_cross = sum(1 for f in findings if f.get("code") == "trace_crossing")
    n_unc = sum(1 for f in findings if f.get("code") == "unconnected_net")
    n_ov = sum(1 for f in findings if f.get("code") == "courtyard_overlap")
    if not findings:
        lines.append("Checks: clean — no unconnected multi-pad nets, no same-layer "
                     "crossings, no courtyard overlaps.")
    else:
        lines.append(f"Checks: {n_unc} unconnected net(s), {n_cross} same-layer "
                     f"crossing(s), {n_ov} courtyard overlap(s).")
        for f in findings[:12]:
            lines.append(f"  [{f.get('severity','?')}] {f.get('message')}")
        if len(findings) > 12:
            lines.append(f"  … and {len(findings) - 12} more finding(s)")
    return "\n".join(lines)


def board_ascii_report(board: dict, layer: Optional[str] = None,
                       pitch_mm: float = 1.0,
                       highlight: Any = None) -> dict:
    """Full PCB report: per-layer ascii + summary + findings.

    ``highlight``: refdes ("R1"), footprint id, net ("net:GND"), track/via id,
    pad ("R1.1"), or a list. One marker letter per requested token, shared
    across every rendered layer.
    """
    layers = [layer] if layer else _copper_layers(board)
    # de-dupe preserve order
    seen: set[str] = set()
    layers = [L for L in layers if not (L in seen or seen.add(L))]  # type: ignore[func-returns-value]
    hl = HighlightPlan(highlight)
    layer_blocks = []
    ascii_parts = []
    for L in layers:
        block = render_board_layer_ascii(board, layer=L, pitch_mm=pitch_mm, plan=hl)
        layer_blocks.append(block)
        ascii_parts.append(f"[layer {L} pitch={block['pitch_mm']:.3g}mm "
                           f"{block['width']}x{block['height']}]\n{block['ascii']}")
    findings = check_board(board)
    summary = summarize_board(board, findings, layers)
    ascii_all = "\n\n".join(ascii_parts)
    primary = layer_blocks[0] if layer_blocks else {}
    ruled = primary.get("ascii_ruled") or annotate_ascii_with_rulers(
        primary.get("ascii") or "")
    gmeta = primary.get("grid") or {}
    eff_pitch = float(primary.get("pitch_mm") or pitch_mm)
    scale_lines = scale_origin_lines(
        gmeta,
        note=(f"LAYERS: {', '.join(layers) or '(none)'} — each layer block above "
              f"shares this grid; only copper on that layer is drawn (vias appear "
              f"on all layers). Requested pitch {pitch_mm:.4g}mm, effective "
              f"{eff_pitch:.4g}mm after auto-coarsening to fit the width cap."))
    legend = (["LEGEND (PCB, one character = one grid cell):"]
              + list(_PCB_LEGEND_BASE) + highlight_legend_lines(hl))
    extra = unmatched_note(hl) + (finding_callouts(findings) if not hl else [])
    callouts = callout_lines(hl, extra)
    return {
        "ok": True,
        "kind": "pcb",
        "ascii": ascii_all,
        "ascii_ruled": ruled,
        "ascii_annotated": annotated_ascii(
            header=f"[pcb {'+'.join(layers) or '(no layers)'} "
                   f"{primary.get('width')}x{primary.get('height')} cells]",
            scale_lines=scale_lines, drawing=ascii_all,
            legend=legend, callouts=callouts),
        "summary": summary_with_annotations(summary, scale_lines, legend, callouts),
        "findings": findings,
        "layers": layer_blocks,
        "pitch_mm": eff_pitch,
        "pitch_mm_requested": pitch_mm,
        "scale": {"pitch_mm": eff_pitch, "unit": "mm", "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts) + extra,
        "highlight": hl.report(),
        "grid": gmeta or None,
        "index": primary.get("index") or {},
        "width": primary.get("width"),
        "height": primary.get("height"),
        "bbox": primary.get("bbox") or primary.get("bbox_mm"),
        "hint": (
            "Use grid + index for cell-addressed edits via apply_ascii_edits "
            "(kind='pcb'). Col/row are 0-based; row 0 is top of the ASCII block. "
            "Pass highlight='R1' / 'net:GND' / ['R1','v1'] to mark one element "
            "with a letter and get it named in callouts."
        ),
    }


# ========================================================================= SKETCH


def _entity_polyline(ent: dict, n_arc: int = 24) -> list[tuple[float, float]]:
    """Control-point / sampled polyline for one sketch entity (deterministic)."""
    etype = (ent.get("type") or "").lower()
    # support both authored (p1/p2) and solved-coords shapes
    coords = ent.get("coords") or ent.get("solved_coords") or {}

    def g(*keys, default=None):
        for k in keys:
            if k in ent and ent[k] is not None:
                return ent[k]
            if k in coords and coords[k] is not None:
                return coords[k]
        return default

    if etype == "line":
        p1 = g("p1", "start")
        p2 = g("p2", "end")
        if p1 is not None and p2 is not None:
            return [_xy(p1), _xy(p2)]
        x1 = g("start_x", "x1", default=0.0)
        y1 = g("start_y", "y1", default=0.0)
        x2 = g("end_x", "x2", default=0.0)
        y2 = g("end_y", "y2", default=0.0)
        return [(float(x1), float(y1)), (float(x2), float(y2))]

    if etype == "circle":
        c = g("center", "ctr")
        if c is not None:
            cx, cy = _xy(c)
        else:
            cx = float(g("center_x", "cx", default=0.0))
            cy = float(g("center_y", "cy", default=0.0))
        r = float(g("radius", "r", default=1.0))
        pts = []
        for i in range(n_arc + 1):
            a = 2.0 * math.pi * i / n_arc
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    if etype == "arc":
        c = g("center", "ctr")
        if c is not None:
            cx, cy = _xy(c)
        else:
            cx = float(g("center_x", "cx", default=0.0))
            cy = float(g("center_y", "cy", default=0.0))
        r = float(g("radius", "r", default=1.0))
        a0 = g("start", "start_angle", "a1", default=0.0)
        a1 = g("end", "end_angle", "a2", default=math.pi)
        a0 = float(a0); a1 = float(a1)
        # degrees if |a| > 2π roughly and looks like degrees
        if abs(a0) > 2 * math.pi + 0.1 or abs(a1) > 2 * math.pi + 0.1:
            a0 = math.radians(a0); a1 = math.radians(a1)
        # sweep CCW from a0 to a1
        sweep = a1 - a0
        if sweep <= 0:
            sweep += 2 * math.pi
        n = max(4, int(round(n_arc * abs(sweep) / (2 * math.pi))))
        pts = []
        for i in range(n + 1):
            a = a0 + sweep * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    if etype in ("spline", "polyline", "eq_curve"):
        pts = g("points", "control_points", "pts", default=[]) or []
        out = []
        for p in pts:
            try:
                out.append(_xy(p))
            except Exception:
                continue
        return out

    if etype == "point":
        p = g("p", "pos")
        if p is not None:
            return [_xy(p)]
        return [(float(g("x", default=0.0)), float(g("y", default=0.0)))]

    return []


def _sketch_bbox(polylines: list[list[tuple[float, float]]]
                 ) -> tuple[float, float, float, float]:
    xs: list[float] = []; ys: list[float] = []
    for pl in polylines:
        for x, y in pl:
            xs.append(x); ys.append(y)
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    pad = 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


_SKETCH_LEGEND_BASE = (
    "  .   = empty sketch plane (no geometry in that cell)",
    "  -   = entity segment running east-west   |   = running north-south",
    "  /   = entity segment running SW->NE      \\   = running NW->SE",
    "  +   = two entities crossing in one cell, a corner, or a point entity",
    "  e1 e2 … (multi-character run one row ABOVE an entity's start cell)"
    " = entity id label, first 4 characters",
)


def _sketch_hl_cands(ent: dict) -> list[tuple[str, Any]]:
    eid = ent.get("id")
    etype = ent.get("type")
    cands: list[tuple[str, Any]] = [("entity", eid), ("id", eid),
                                    ("type", etype)]
    if eid:
        # health/closure talks in "e1.start" / "e1.end" — accept those too
        cands += [("entity", f"{eid}.start"), ("entity", f"{eid}.end"),
                  ("id", f"{eid}.start"), ("id", f"{eid}.end")]
    return cands


def render_sketch_ascii(entities: list[dict], pitch: float = 0.5,
                        max_dim: int = 60,
                        highlight: Any = None,
                        plan: Optional[HighlightPlan] = None) -> dict:
    """ASCII render of sketch entities as control-point polylines.

    ``highlight`` accepts the shared target vocabulary as it applies to a
    sketch: an entity id ("e3"), an endpoint handle ("e3.end"), an entity type
    ("circle" — marks every circle), or a list of any of those.
    """
    if pitch <= 0:
        pitch = 0.5
    hl = plan if plan is not None else HighlightPlan(highlight)
    drawn: list[dict] = []
    polys = []
    labels: list[tuple[int, int, str]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if ent.get("construction"):
            continue
        pl = _entity_polyline(ent)
        if not pl:
            continue
        polys.append(pl)
        drawn.append(ent)

    xmin, ymin, xmax, ymax = _sketch_bbox(polys)
    bw = max(xmax - xmin, pitch); bh = max(ymax - ymin, pitch)
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)

    grid = [[" " for _ in range(w)] for _ in range(h)]

    def cell(x: float, y: float) -> tuple[int, int]:
        return (_clamp_int((x - xmin) / pitch, 0, w - 1),
                _clamp_int((ymax - y) / pitch, 0, h - 1))

    for ent, pl in zip(drawn, polys):
        mk = hl.marker(_sketch_hl_cands(ent))
        touched: set[tuple[int, int]] = set()
        if len(pl) == 1:
            cx, cy = cell(pl[0][0], pl[0][1])
            grid[cy][cx] = mk or "+"
            touched.add((cx, cy))
        else:
            for i in range(len(pl) - 1):
                x0, y0 = cell(pl[i][0], pl[i][1])
                x1, y1 = cell(pl[i + 1][0], pl[i + 1][1])
                ch = _trace_char(x0, y0, x1, y1)
                for cx, cy in _bresenham(x0, y0, x1, y1):
                    touched.add((cx, cy))
                    if mk:
                        grid[cy][cx] = mk
                    elif grid[cy][cx] in (" ", "."):
                        grid[cy][cx] = ch
                    elif grid[cy][cx] != ch:
                        grid[cy][cx] = "+"
        # label near first point
        eid = str(ent.get("id") or "")
        acx, acy = cell(pl[0][0], pl[0][1])
        if eid and pl:
            labels.append((acx, max(0, acy - 1), eid[:4]))
        if mk:
            hl_stamp_anchor(grid, acx, acy, mk, empty=" ")
            hl.note(mk, eid or (ent.get("type") or "entity"),
                    _entity_nl(ent), anchor=(acx, acy),
                    world=pl[0], cells=len(touched))

    for cx, cy, lab in labels:
        for i, ch in enumerate(lab):
            x = cx + i
            if 0 <= x < w and 0 <= cy < h and grid[cy][x] == " ":
                grid[cy][x] = ch

    # empty → '.'
    for y in range(h):
        for x in range(w):
            if grid[y][x] == " ":
                grid[y][x] = "."

    ascii_block = "\n".join("".join(r) for r in grid)
    gmeta = make_grid_meta(
        xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
        pitch=pitch, width=w, height=h, unit="mm", kind="sketch",
    )
    # entity index at first-point cells
    index: dict[str, list[dict]] = {}
    for ent in drawn:
        pl = _entity_polyline(ent)
        if not pl:
            continue
        cx, cy = cell(pl[0][0], pl[0][1])
        key = f"{cx},{cy}"
        index.setdefault(key, []).append({
            "kind": "entity",
            "id": ent.get("id"),
            "type": ent.get("type"),
            "p1": list(pl[0]),
            "p2": list(pl[-1]) if len(pl) > 1 else list(pl[0]),
        })
    n_constr = sum(1 for e in entities
                   if isinstance(e, dict) and e.get("construction"))
    ruled = annotate_ascii_with_rulers(ascii_block)
    scale_lines = scale_origin_lines(
        gmeta,
        note=(f"HIDDEN: construction geometry is NOT drawn "
              f"({n_constr} construction entit(y/ies) skipped); curves are drawn "
              f"as sampled polylines, so a small radius can collapse to one cell."))
    legend = (["LEGEND (sketch, one character = one grid cell):"]
              + list(_SKETCH_LEGEND_BASE) + highlight_legend_lines(hl))
    callouts = callout_lines(hl, unmatched_note(hl) if plan is None else ())
    return {
        "ascii": ascii_block,
        "ascii_ruled": ruled,
        "ascii_annotated": annotated_ascii(
            header=f"[sketch {w}x{h} cells pitch={pitch:.4g}mm]",
            scale_lines=scale_lines, drawing=ruled,
            legend=legend, callouts=callouts),
        "scale": {"pitch": pitch, "unit": "mm", "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts),
        "highlight": hl.report(),
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox": [xmin, ymin, xmax, ymax],
        "entity_count": len(polys),
        "grid": gmeta,
        "index": index,
    }


def _endpoints(ent: dict) -> list[tuple[float, float]]:
    etype = (ent.get("type") or "").lower()
    pl = _entity_polyline(ent)
    if not pl:
        return []
    if etype == "circle":
        return []  # closed by definition
    if etype == "point":
        return [pl[0]]
    if len(pl) == 1:
        return [pl[0]]
    return [pl[0], pl[-1]]


# Endpoints this close are one point: core/sketch/sketch_geom.cpp TOL (OCCT
# Precision::Confusion), in sketch units (mm). Only a larger gap can be near-coincident.
_COINCIDENT_TOL_MM = 1e-7


SKETCH_DIMENSION_TYPES = frozenset({
    "distance", "length", "angle", "radius", "diameter", "arc_length",
    "horizontal_distance", "vertical_distance",
})


def sketch_feeds_feature(features: list[dict], sketch_id: str) -> bool:
    """Whether another feature consumes this sketch (including nested profile refs)."""
    def references(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("sketch") == sketch_id or any(
                references(item) for item in value.values())
        if isinstance(value, list):
            return any(references(item) for item in value)
        return isinstance(value, str) and value == sketch_id

    return any(isinstance(feature, dict) and feature.get("id") != sketch_id
               and not feature.get("suppressed") and not feature.get("deleted")
               and (references(feature.get("refs")) or
                    (feature.get("params") or {}).get("sketch") == sketch_id)
               for feature in features)


def sketch_design_intent(entities: list[dict], constraints: list[dict], *,
                         health: Optional[dict] = None,
                         feeds_feature: bool = False) -> dict:
    """Grade authored intent without mistaking fixed external references for pinned profiles."""
    health = health or {}
    external = {str(eid) for row in health.get("external_refs") or []
                if isinstance(row, dict) for eid in row.get("entity_ids") or []}
    external.update(str(e["id"]) for e in entities if isinstance(e, dict)
                    and e.get("id") and (e.get("external") or e.get("source")))
    authored_ids = {str(e["id"]) for e in entities if isinstance(e, dict) and e.get("id")} - external
    profile = {str(e["id"]) for e in entities if isinstance(e, dict) and e.get("id")
               and not e.get("construction") and str(e["id"]) not in external}
    graph: dict[str, set[str]] = {}
    pinned: set[str] = set()
    dimensions = 0
    skipped = {row.get("id") for row in health.get("skipped_constraints") or []
               if isinstance(row, dict)}
    for constraint in constraints:
        if not isinstance(constraint, dict) or constraint.get("id") in skipped:
            continue
        kind = constraint.get("type")
        refs = [ref if ref in ("axis.h", "axis.v") else ref.partition(".")[0]
                for ref in constraint.get("refs") or [] if isinstance(ref, str)]
        if kind == "fix":
            pinned.update(ref for ref in refs if ref in authored_ids)
            continue
        if constraint.get("driven"):
            continue
        if kind in SKETCH_DIMENSION_TYPES and any(ref in profile for ref in refs):
            dimensions += 1
        for ref in refs:
            graph.setdefault(ref, set()).update(other for other in refs if other != ref)

    def reachable(roots: set[str]) -> set[str]:
        reached = set(roots)
        queue = list(roots)
        while queue:
            for other in graph.get(queue.pop(), ()):
                if other not in reached:
                    reached.add(other)
                    queue.append(other)
        return reached

    anchored = not profile or profile <= reachable(external | {"origin", "axis.h", "axis.v"})
    # "Pinned only" means the sketch reaches DOF 0 because of fix pins and carries no driving
    # dimension; an anchored-but-under-defined sketch is reported as under-dimensioned instead.
    pinned_only = bool(profile and pinned and not dimensions and health.get("dof") == 0)
    findings = []

    def finding(code: str, message: str, ids: Optional[set[str]] = None) -> None:
        row = {"code": code, "kind": code, "severity": "warning", "message": message}
        if ids:
            row["entities"] = sorted(ids)
        findings.append(row)

    if pinned:
        finding("pinned_instead_of_constrained",
                "Authored geometry is pinned instead of constrained: remove its fix relations; "
                "use horizontal/vertical, coincident, tangent or equal relations and driving "
                "length/radius dimensions, then sketch_fully_define for the remaining DOF.", pinned)
    if profile and feeds_feature and not dimensions:
        finding("no_driving_dimensions",
                "This sketch feeds a feature but has no driving dimensions. Add length "
                'refs:["e1"] or radius refs:["e2"] with value:{"value":N,"expr":null}, '
                "then sketch_fully_define; driven:true dimensions only measure.")
    if not anchored:
        finding("unreferenced_profile",
                "Profile is not referenced to origin/model geometry. Add coincident "
                'refs:["e1.p1","origin"], symmetric refs:["e1","e2","axis.v"], or '
                "point_on to a converted construction edge; sketch_fully_define(anchor='origin') "
                "can dimension the remaining offsets.")
    dof = health.get("dof")
    if isinstance(dof, (int, float)) and dof > 0:
        finding("under_dimensioned",
                f"Sketch has {dof} remaining DOF. Add design-intent relations and driving "
                "dimensions, then sketch_fully_define and check_sketch: dof 0, not pinned_only.")
    return {"pinned_only": pinned_only, "driving_dimensions": dimensions,
            "pinned_entities": sorted(pinned), "anchored": anchored, "findings": findings}


def sketch_define_additions(id_map: dict, op_index: int, constraints: list[dict]) -> dict:
    """Count precisely the constraints minted by one fully-define op, not all sketch intent."""
    key = f"${op_index}"
    ids = [value for name, value in id_map.items()
           if name == key or name.startswith(key + "#")]
    types = {constraint.get("id"): constraint.get("type") for constraint in constraints
             if isinstance(constraint, dict)}
    dimensions = sum(types.get(cid) in SKETCH_DIMENSION_TYPES for cid in ids)
    return {"constraint_ids": ids, "relations_added": len(ids) - dimensions,
            "dimensions_added": dimensions}


def live_sketch_design_intent(doc_id: str, sketch_id: str, health: dict) -> dict:
    """Enrich native solver health with authored constraints and feature-consumer context."""
    from services.mcp import core_bridge as cb

    features = (cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}).get("features") or []
    feature = next((row for row in features if row.get("id") == sketch_id), {})
    params = feature.get("params") or {}
    return sketch_design_intent(params.get("entities") or [], params.get("constraints") or [],
                                health=health,
                                feeds_feature=sketch_feeds_feature(features, sketch_id))


def check_sketch_geometry(entities: list[dict],
                          *, near_tol: float = 1e-2,
                          health: Optional[dict] = None,
                          constraints: Optional[list[dict]] = None,
                          feeds_feature: bool = False) -> list[dict]:
    """Open profiles, near-coincident endpoints, under-dimensioned entities.

    When *health* (sketch_health result) is provided, open-profile and under-
    dimensioned findings prefer that source (wrap, don't re-solve).
    """
    findings: list[dict] = []
    ents = [e for e in entities if isinstance(e, dict) and not e.get("construction")]

    # --- open profiles / dangling endpoints ----------------------------------
    if health and isinstance(health.get("closure"), dict):
        open_eps = health["closure"].get("open_endpoints") or []
        if open_eps:
            findings.append({
                "code": "open_profile",
                "severity": "error",
                "open_endpoints": list(open_eps),
                "message": (f"Open profile: {len(open_eps)} dangling endpoint(s) "
                            f"at {', '.join(map(str, open_eps[:6]))}"),
                "source": "sketch_health",
            })
    else:
        # chain endpoints with union-find; degree-1 nodes are open
        keys: list[tuple[float, float]] = []
        ep_owner: list[tuple[str, tuple[float, float]]] = []
        for e in ents:
            eid = str(e.get("id") or "?")
            for p in _endpoints(e):
                k = (round(p[0], 4), round(p[1], 4))
                keys.append(k)
                ep_owner.append((eid, k))
        parent: dict[tuple[float, float], tuple[float, float]] = {}

        def find(a: tuple[float, float]) -> tuple[float, float]:
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: tuple[float, float], b: tuple[float, float]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for k in keys:
            parent.setdefault(k, k)
        # merge near-equal keys
        uniq = sorted(set(keys))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                if math.hypot(uniq[i][0] - uniq[j][0],
                              uniq[i][1] - uniq[j][1]) <= near_tol:
                    union(uniq[i], uniq[j])
        degree: dict[tuple[float, float], int] = {}
        for _, k in ep_owner:
            r = find(k)
            degree[r] = degree.get(r, 0) + 1
        dangling = [k for k, d in degree.items() if d == 1]
        # circles alone don't create dangling; pure closed loops have degree 2
        if dangling and ents:
            findings.append({
                "code": "open_profile",
                "severity": "error",
                "open_endpoints": [list(k) for k in sorted(dangling)],
                "message": (f"Open profile: {len(dangling)} dangling endpoint(s) "
                            f"(near_tol={near_tol})"),
                "source": "geometry",
            })

    # --- near-coincident endpoints (distinct but within 10x near_tol, not merged) --
    # A gap at or below the kernel's coincidence tolerance is float noise, not a gap: the
    # 2026-10-10 6-axis run got 9 warnings per sketch for d=1e-11 mm, each one asking for
    # sketch_auto_constrain on endpoints that were already coincident.
    eps_list: list[tuple[str, float, float]] = []
    for e in ents:
        eid = str(e.get("id") or "?")
        for p in _endpoints(e):
            eps_list.append((eid, p[0], p[1]))
    near_pairs = []
    for i in range(len(eps_list)):
        for j in range(i + 1, len(eps_list)):
            ei, xi, yi = eps_list[i]
            ej, xj, yj = eps_list[j]
            if ei == ej:
                continue
            d = math.hypot(xi - xj, yi - yj)
            if _COINCIDENT_TOL_MM < d <= near_tol * 10:
                pair = tuple(sorted([ei, ej]))
                near_pairs.append((pair, d, (xi, yi), (xj, yj)))
    # de-dupe by entity pair
    seen_pairs: set[tuple[str, str]] = set()
    for pair, d, p, q in sorted(near_pairs, key=lambda t: (t[0], t[1])):
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        findings.append({
            "code": "near_coincident",
            "severity": "warning",
            "entities": list(pair),
            "distance": round(d, 6),
            "message": (f"Near-coincident endpoints between {pair[0]} and {pair[1]} "
                        f"(d={d:.4g}); run sketch_auto_constrain"),
        })

    # --- under-dimensioned ---------------------------------------------------
    # ONE finding, not two. Live 2026-07-30: `under_dimensioned` re-printed the entire
    # dof_narration that summarize_sketch had already printed via sketch_health_text,
    # and `under_dimensioned_entities` then listed the same 12 entity ids a third time.
    # The structured payload (dof, status, entities[]) answers "is it under-constrained,
    # by how much, and which entities" on its own; WHICH coordinate slots are free stays
    # readable on the summary's DOF line (bounded sample) and in full from sketch_health.
    if health is not None:
        dof = health.get("dof")
        status = health.get("status")
        statuses = health.get("statuses") or {}
        under_ents = [k for k, v in statuses.items() if v == "under"]
        if dof is not None and (dof > 0 or status in ("under", "unknown")):
            shown = ", ".join(map(str, under_ents[:8]))
            more = f" … +{len(under_ents) - 8}" if len(under_ents) > 8 else ""
            who = (f" {len(under_ents)} entity(ies) free: {shown}{more}."
                   if under_ents else "")
            findings.append({
                "code": "under_dimensioned",
                "severity": "warning",
                "dof": dof,
                "status": status,
                "entities": under_ents,
                "message": (f"Sketch under-dimensioned: dof={dof}, status={status!r}.{who}"
                            " Add horizontal/vertical/coincident relations and driving dimensions,"
                            " then sketch_fully_define until dof is 0 and not pinned_only."),
                "source": "sketch_health",
            })
        elif under_ents:
            findings.append({
                "code": "under_dimensioned",
                "severity": "info",
                "entities": under_ents,
                "message": (f"{len(under_ents)} entity(ies) report status 'under' while "
                            f"dof={dof}: {', '.join(map(str, under_ents[:8]))}"),
                "source": "sketch_health",
            })
    else:
        # offline: any non-construction non-point entity without a matching dimension
        # is reported as potentially free — weak heuristic only
        dims = []  # caller may pass via entities container
        _ = dims
        free = [str(e.get("id") or "?") for e in ents
                if (e.get("type") or "").lower() not in ("point",)]
        if free:
            findings.append({
                "code": "under_dimensioned",
                "severity": "info",
                "entities": free,
                "message": (f"{len(free)} entity(ies) present without sketch_health; "
                            "pass health or call sketch_health for DOF"),
                "source": "heuristic",
            })
    if constraints is not None:
        intent = sketch_design_intent(entities, constraints, health=health,
                                      feeds_feature=feeds_feature)
        existing = {row.get("code") for row in findings}
        findings.extend(row for row in intent["findings"] if row["code"] not in existing)

    findings.sort(key=lambda f: (f.get("code", ""), f.get("message", "")))
    return findings


def _entity_nl(ent: dict) -> str:
    """One-line plain-English entity callout for non-vision models."""
    eid = str(ent.get("id") or "?")
    et = (ent.get("type") or "?").lower()
    bits: list[str] = [f"{eid}:{et}"]
    if et == "line":
        try:
            p1 = _xy(ent.get("p1")); p2 = _xy(ent.get("p2"))
            bits.append(f"({p1[0]:g},{p1[1]:g})->({p2[0]:g},{p2[1]:g})")
        except Exception:
            pass
    elif et == "circle":
        try:
            c = _xy(ent.get("center") or ent.get("c") or [0, 0])
            r = ent.get("radius")
            if isinstance(r, dict):
                r = r.get("value", r)
            bits.append(f"center=({c[0]:g},{c[1]:g}) r={r}")
        except Exception:
            pass
    elif et == "arc":
        try:
            c = _xy(ent.get("center") or ent.get("c") or [0, 0])
            r = ent.get("radius")
            if isinstance(r, dict):
                r = r.get("value", r)
            bits.append(f"center=({c[0]:g},{c[1]:g}) r={r}")
        except Exception:
            pass
    elif et == "point":
        try:
            p = _xy(ent.get("p") or ent.get("pos") or ent.get("at") or [0, 0])
            bits.append(f"({p[0]:g},{p[1]:g})")
        except Exception:
            pass
    if ent.get("construction"):
        bits.append("construction")
    return " ".join(str(b) for b in bits)


def _constraint_nl(c: dict) -> str:
    """One-line constraint / dimension callout with names and values."""
    cid = str(c.get("id") or "?")
    ct = str(c.get("type") or "?")
    refs = c.get("refs") or c.get("entities") or c.get("handles") or []
    if isinstance(refs, (list, tuple)):
        ref_s = ",".join(str(r) for r in refs[:6])
    else:
        ref_s = str(refs)
    val = c.get("value")
    if isinstance(val, dict):
        val = val.get("value", val)
    driven = c.get("driven")
    parts = [f"{cid}:{ct}"]
    if ref_s:
        parts.append(f"refs=[{ref_s}]")
    if val is not None:
        parts.append(f"value={val}")
    if driven:
        parts.append("driven")
    return " ".join(parts)


def summarize_sketch(entities: list[dict], constraints: list[dict],
                     findings: list[dict], health: Optional[dict] = None) -> str:
    type_counts: dict[str, int] = {}
    for e in entities:
        if not isinstance(e, dict):
            continue
        t = (e.get("type") or "?").lower()
        type_counts[t] = type_counts.get(t, 0) + 1
    rel_counts: dict[str, int] = {}
    dims: list[str] = []
    rel_details: list[str] = []
    for c in constraints or []:
        if not isinstance(c, dict):
            continue
        ct = c.get("type") or "?"
        if ct in SKETCH_DIMENSION_TYPES:
            dims.append(_constraint_nl(c))
        else:
            rel_counts[ct] = rel_counts.get(ct, 0) + 1
            rel_details.append(_constraint_nl(c))

    lines = [
        f"Sketch: {sum(type_counts.values())} entit(y/ies) "
        f"({', '.join(f'{n} {t}' for t, n in sorted(type_counts.items())) or 'none'}).",
    ]
    # Named entity callouts (capped) so text-only models can address geometry.
    ent_lines = [_entity_nl(e) for e in entities
                 if isinstance(e, dict)][:READBACK_MAX_ENTITIES_NL]
    if ent_lines:
        lines.append("Entities:")
        for el in ent_lines:
            lines.append(f"  {el}")
        n_more = sum(1 for e in entities if isinstance(e, dict)) - len(ent_lines)
        if n_more > 0:
            lines.append(f"  … +{n_more} more")

    if dims:
        lines.append("Dimensions: " + "; ".join(dims[:12])
                     + (f" … +{len(dims)-12}" if len(dims) > 12 else ""))
    else:
        lines.append("Dimensions: (none listed)")
    if rel_counts:
        lines.append("Constraints: " + ", ".join(f"{n}× {t}" for t, n in sorted(rel_counts.items())))
        for rd in rel_details[:12]:
            lines.append(f"  {rd}")
        if len(rel_details) > 12:
            lines.append(f"  … +{len(rel_details)-12} more")
    else:
        lines.append("Constraints: (none listed)")

    # Endpoint / open-closed status from geometry + health.
    open_eps: list[str] = []
    if health is not None:
        closure = health.get("closure") or {}
        raw_open = closure.get("open_endpoints") or []
        open_eps = [str(x) for x in raw_open]
        closed_ok = closure.get("closed_chains_ok")
        if closed_ok is True and not open_eps:
            lines.append("Profile: CLOSED (no dangling endpoints).")
        elif open_eps:
            lines.append("Profile: OPEN — dangling endpoints: "
                         + ", ".join(open_eps[:12])
                         + (f" … +{len(open_eps)-12}" if len(open_eps) > 12 else ""))
        elif closed_ok is False:
            lines.append("Profile: OPEN (health reports unclosed chains).")
    # near-coincident notes from findings
    near = [f for f in findings if f.get("code") == "near_coincident"]
    if near:
        lines.append("Endpoint coincidence notes:")
        for f in near[:6]:
            lines.append(f"  {f.get('message')}")

    if health is not None:
        try:
            from services.mcp.gsl_tools import sketch_health_text  # type: ignore
            lines.append("")
            lines.append(sketch_health_text(health))
        except Exception:
            lines.append(
                f"Health: status={health.get('status')!r} dof={health.get('dof')}"
                + (f" — {health.get('dof_narration')}" if health.get("dof_narration") else ""))

    if findings:
        lines.append(f"Findings ({len(findings)}):")
        for f in findings[:12]:
            lines.append(f"  [{f.get('severity','?')}] {f.get('message')}")
    else:
        lines.append("Findings: none")
    return "\n".join(lines)


def _cap_ascii(ascii_block: str, max_h: int = READBACK_MAX_H,
               max_w: int = READBACK_MAX_W) -> tuple[str, int, int, bool]:
    """Clamp an ASCII grid to max_h rows × max_w cols. Returns (text, h, w, truncated)."""
    rows = (ascii_block or "").split("\n")
    truncated = False
    if len(rows) > max_h:
        rows = rows[:max_h]
        truncated = True
    out_rows: list[str] = []
    for r in rows:
        if len(r) > max_w:
            out_rows.append(r[:max_w])
            truncated = True
        else:
            out_rows.append(r)
    h = len(out_rows)
    w = max((len(r) for r in out_rows), default=0)
    return "\n".join(out_rows), h, w, truncated


def _clip_grid_index_to_picture(grid: dict, index: dict, *, w: int, h: int
                                ) -> tuple[dict, dict, list[str]]:
    """Re-scope a render's ``grid``/``index`` to the picture that is actually shipped.

    ``_cap_ascii`` clamps the printed block to max_h x max_w, but the render's grid
    and index describe the FULL raster. Measured 2026-07-30 on a 30x200 mm profile:
    the readback shipped 40 rows and reported ``truncated: True``, while ``grid.height``
    still said 41 and ``index`` still advertised entities at cell "0,40" — a row the
    model cannot see and ``world_to_cell`` can never return. A picture whose index
    claims coverage it does not have is worse than a smaller picture, so the index is
    clipped to the visible lattice window and whatever fell off is NAMED (never dropped
    silently) so the model knows to re-render instead of trusting the crop.
    """
    pitch = float(grid.get("pitch") or 1.0)
    bbox = [float(v) for v in (grid.get("bbox") or [0.0, 0.0, 0.0, 0.0])[:4]]
    out_grid = dict(grid)
    out_grid["width"] = int(w)
    out_grid["height"] = int(h)
    # Cells are lattice points: col 0..w-1 spans x[xmin, xmin+(w-1)*pitch] and row 0
    # is ymax, so clipping rows raises ymin and clipping cols lowers xmax.
    out_grid["bbox"] = [bbox[0], bbox[3] - (h - 1) * pitch,
                        bbox[0] + (w - 1) * pitch, bbox[3]]
    out_grid["origin_world"] = [out_grid["bbox"][0], out_grid["bbox"][1]]
    out_index: dict[str, list[dict]] = {}
    clipped: list[str] = []
    for key, hits in (index or {}).items():
        try:
            col, row = (int(t) for t in str(key).split(",", 1))
        except (TypeError, ValueError):
            continue
        if 0 <= col < w and 0 <= row < h:
            out_index[key] = hits
        else:
            clipped += [str(hit.get("id")) for hit in hits
                        if isinstance(hit, dict) and hit.get("id")]
    return out_grid, out_index, clipped


def sketch_mutation_readback(sketch: dict, *, pitch: float = 1.0,
                             health: Optional[dict] = None,
                             near_tol: float = 1e-2,
                             max_h: int = READBACK_MAX_H,
                             max_w: int = READBACK_MAX_W,
                             sketch_id: str = "") -> dict:
    """Compact deterministic ASCII + NL block for sketch mutation tool responses.

    Caps the grid (default 40x120) so agent context stays cheap; full render remains
    available via ``render_sketch_ascii``. Byte-stable for fixed inputs.

    Ships exactly ONE picture, ``ascii_ruled`` (header line + 0-based col/row rulers).
    Measured 2026-07-30 on the 3-rectangle F profile: ``ascii`` (bare grid, 567 chars),
    ``ascii_block`` (header + the same grid, 598) and ``ascii_ruled`` (713) were the
    same drawing three times with different chrome, and ``ascii_ruled`` is a strict
    superset — it is the only one whose col/row numbers a model can read a cell address
    off without counting characters, and cell addresses are the contract
    ``apply_ascii_edits(kind='sketch')`` resolves edits against.
    """
    entities = list(sketch.get("entities") or [])
    constraints = list(sketch.get("constraints") or [])
    if health is None and isinstance(sketch.get("health"), dict):
        health = sketch["health"]
    # Render with a hard max_dim so the raster itself never exceeds the cap.
    max_dim = max(8, min(max_h, max_w))
    rendered = render_sketch_ascii(entities, pitch=pitch, max_dim=max_dim)
    ascii_txt, h, w, truncated = _cap_ascii(rendered["ascii"], max_h=max_h, max_w=max_w)
    grid, index, clipped = _clip_grid_index_to_picture(
        rendered.get("grid") or {}, rendered.get("index") or {}, w=w, h=h)
    findings = check_sketch_geometry(
        entities, near_tol=near_tol, health=health, constraints=constraints,
        feeds_feature=bool(sketch.get("feeds_feature")))
    summary = summarize_sketch(entities, constraints, findings, health=health)
    # compact findings payload
    compact_findings = [
        {"code": f.get("code"), "severity": f.get("severity"), "message": f.get("message")}
        for f in findings[:READBACK_MAX_FINDINGS]
    ]
    sid = sketch_id or sketch.get("id") or ""
    header = f"[sketch_readback id={sid or '?'} {w}x{h}"
    if truncated:
        header += " truncated=1"
    header += "]"
    hint = ("READ this ASCII + summary before the next sketch edit. "
            "Use apply_ascii_edits(kind='sketch', sketch_id=..., edits=[...]) "
            "for cell-targeted entity add/delete. Call render_sketch_ascii + "
            "sketch_health before declaring the sketch done.")
    out = {
        "ok": True,
        "kind": "sketch_readback",
        "sketch_id": sid or None,
        "ascii_ruled": f"{header}\n{annotate_ascii_with_rulers(ascii_txt)}",
        "summary": summary,
        "findings": compact_findings,
        "findings_total": len(findings),
        "width": w,
        "height": h,
        "pitch": grid.get("pitch", rendered.get("pitch")),
        "grid": grid,
        "index": index,
        "truncated": truncated,
        "caps": {"max_h": max_h, "max_w": max_w},
    }
    intent = sketch_design_intent(entities, constraints, health=health,
                                  feeds_feature=bool(sketch.get("feeds_feature")))
    out.update({key: value for key, value in intent.items() if key != "findings"})
    out["dof"] = (health or {}).get("dof")
    if clipped:
        out["clipped_entities"] = sorted(set(clipped))
        hint = (f"PICTURE IS CROPPED to {w}x{h} cells — grid/index describe only what "
                f"is drawn; {len(out['clipped_entities'])} entity(ies) start outside it "
                f"({', '.join(out['clipped_entities'][:8])}). Call render_sketch_ascii "
                f"with a coarser pitch to see them. " + hint)
    out["hint"] = hint
    return out


def board_mutation_readback(board: dict, *, layer: Optional[str] = None,
                            pitch_mm: float = 1.0,
                            max_h: int = READBACK_MAX_H,
                            max_w: int = READBACK_MAX_W,
                            focus: Optional[Sequence[float]] = None,
                            focus_radius_mm: float = 25.0) -> dict:
    """Compact per-layer ASCII + NL connectivity delta for board mutation responses.

    Renders one copper layer (default first / F.Cu), optionally cropped around
    ``focus`` (x,y mm) so place/route/via changes stay local. Caps 40×120.
    """
    layers = [layer] if layer else _copper_layers(board)
    if not layers:
        layers = ["F.Cu"]
    L = layers[0]
    # Optional focus crop: build a shallow board view with only nearby geometry so
    # the ASCII excerpt centres on the mutation without changing check_board.
    view_board = board
    if focus is not None and len(focus) >= 2:
        fx, fy = float(focus[0]), float(focus[1])
        r = float(focus_radius_mm)
        view_board = _board_focus_crop(board, fx, fy, r)

    max_dim = max(8, min(max_h, max_w))
    block = render_board_layer_ascii(view_board, layer=L, pitch_mm=pitch_mm,
                                     max_dim=max_dim)
    ascii_txt, h, w, truncated = _cap_ascii(block["ascii"], max_h=max_h, max_w=max_w)
    findings = check_board(board)  # full-board findings (not cropped)
    summary = summarize_board(board, findings, [L])
    compact_findings = [
        {"code": f.get("code"), "severity": f.get("severity"), "message": f.get("message")}
        for f in findings[:READBACK_MAX_FINDINGS]
    ]
    # Net/connectivity delta-friendly counts for the model.
    pad_nets = _pad_nodes(board)
    net_delta = {
        "named_nets": len(board.get("nets") or []),
        "tracks": len(board.get("tracks") or []),
        "vias": len(board.get("vias") or []),
        "footprints": len(board.get("footprints") or []),
        "pad_net_count": len(pad_nets),
        "unconnected_multi_pad": sum(
            1 for f in findings if f.get("code") == "unconnected_net"),
        "crossings": sum(1 for f in findings if f.get("code") == "trace_crossing"),
        "courtyard_overlaps": sum(
            1 for f in findings if f.get("code") == "courtyard_overlap"),
    }
    header = f"[board_readback layer={L} {w}x{h}"
    if truncated:
        header += " truncated=1"
    if focus is not None and len(focus) >= 2:
        header += f" focus=({float(focus[0]):g},{float(focus[1]):g})"
    header += "]"
    return {
        "ok": True,
        "kind": "board_readback",
        "layer": L,
        "ascii_ruled": f"{header}\n{annotate_ascii_with_rulers(ascii_txt)}",
        "summary": summary,
        "findings": compact_findings,
        "findings_total": len(findings),
        "net_delta": net_delta,
        "width": w,
        "height": h,
        "pitch_mm": block.get("pitch_mm"),
        "grid": block.get("grid"),
        "index": block.get("index") or {},
        "truncated": truncated,
        "caps": {"max_h": max_h, "max_w": max_w},
        "hint": ("READ this ASCII + net_delta before the next board edit. "
                 "Use apply_ascii_edits(kind='pcb', edits=[...]) for cell-targeted "
                 "moves/swaps/section replaces. Call render_board_ascii before done."),
    }


def _board_focus_crop(board: dict, fx: float, fy: float, radius: float) -> dict:
    """Shallow copy of board keeping geometry near (fx,fy) for local ASCII excerpt."""
    r2 = radius * radius

    def _near_pt(p: Any) -> bool:
        try:
            x, y = _xy(p)
        except Exception:
            return False
        dx, dy = x - fx, y - fy
        return dx * dx + dy * dy <= r2

    def _near_seg(p1: Any, p2: Any) -> bool:
        return _near_pt(p1) or _near_pt(p2)

    tracks = [t for t in (board.get("tracks") or [])
              if isinstance(t, dict) and _near_seg(t.get("p1"), t.get("p2"))]
    vias = [v for v in (board.get("vias") or [])
            if isinstance(v, dict) and _near_pt(v.get("pos") or v.get("at"))]
    fps = []
    for fp in board.get("footprints") or []:
        if not isinstance(fp, dict):
            continue
        pos = fp.get("pos") or [0, 0]
        if _near_pt(pos):
            fps.append(fp)
            continue
        # also keep if any pad is near
        for pad in fp.get("pads") or []:
            try:
                px, py = _xy(pad.get("pos") or [0, 0])
                ox, oy = float(pos[0]), float(pos[1])
                if _near_pt([ox + px, oy + py]):
                    fps.append(fp)
                    break
            except Exception:
                continue
    out = dict(board)
    out["tracks"] = tracks
    out["vias"] = vias
    out["footprints"] = fps
    # synthetic tight outline so bbox shrinks to the focus window
    out["outline"] = {
        "closed": True,
        "shapes": [
            {"type": "line", "p1": [fx - radius, fy - radius],
             "p2": [fx + radius, fy - radius]},
            {"type": "line", "p1": [fx + radius, fy - radius],
             "p2": [fx + radius, fy + radius]},
            {"type": "line", "p1": [fx + radius, fy + radius],
             "p2": [fx - radius, fy + radius]},
            {"type": "line", "p1": [fx - radius, fy + radius],
             "p2": [fx - radius, fy - radius]},
        ],
    }
    return out


def attach_sketch_readback(response: dict, sketch: dict, **kwargs: Any) -> dict:
    """Mutate *response* in place with the ``ascii_readback`` block.

    Says each thing ONCE. This used to set ``ascii``, ``ascii_block``,
    ``sketch_summary``, ``summary``, ``findings``, ``readback_hint`` AND the whole
    ``ascii_readback`` object, so the same picture shipped three times and the same
    3k-char summary twice in every sketch-mutation reply. ``summary`` is still filled
    when the tool has none of its own (it is the reply's headline), and everything else
    is one dereference away under ``ascii_readback``.
    """
    try:
        rb = sketch_mutation_readback(sketch, **kwargs)
        response["ascii_readback"] = rb
        if not response.get("summary"):
            response["summary"] = rb["summary"]
    except Exception as e:  # noqa: BLE001 — never fail the mutation on readback
        response["ascii_readback"] = {
            "ok": False, "error": f"sketch readback unavailable: {e}",
        }
    return response


def attach_board_readback(response: dict, board: dict, **kwargs: Any) -> dict:
    """Mutate *response* in place with compact board ASCII + net_delta.

    Says each thing ONCE, matching ``attach_sketch_readback``. Measured 2026-07-30 on a
    nine-footprint ESP32 board: this used to copy ``ascii`` (1519), ``ascii_block`` (1553)
    and ``summary`` (879) up beside an ``ascii_readback`` that already carried them plus a
    third rendering ``ascii_ruled`` (1763) — 21.5k of readback inside a 28.1k reply, the
    same drawing four times. Only ``ascii_ruled`` survives: it is a strict superset whose
    col/row rulers are what ``apply_ascii_edits(kind='pcb')`` resolves cells against.
    ``summary`` is still filled when the tool has none of its own (it is the headline).
    """
    try:
        rb = board_mutation_readback(board, **kwargs)
        response["ascii_readback"] = rb
        if not response.get("summary"):
            response["summary"] = rb["summary"]
    except Exception as e:  # noqa: BLE001
        response["ascii_readback"] = {
            "ok": False, "error": f"board readback unavailable: {e}",
        }
    return response


def sketch_ids_from_ops(ops: Sequence[Any]) -> list[str]:
    """Collect sketch feature ids touched by a batch of doc ops (deterministic order)."""
    seen: set[str] = set()
    out: list[str] = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        name = op.get("op") or ""
        if name not in SKETCH_MUTATION_OPS:
            # also: add_feature of type sketch — no entity mutation yet, skip
            continue
        sid = op.get("sketch")
        if not isinstance(sid, str) or not sid or sid.startswith("$"):
            continue
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def live_sketch_readback(doc_id: str, sketch_id: str, **kwargs: Any) -> Optional[dict]:
    """Best-effort live sketch load + compact readback (returns None on failure)."""
    try:
        data, health = _load_sketch(sketch_id, doc_id)
        return sketch_mutation_readback(data, health=health, sketch_id=sketch_id, **kwargs)
    except Exception:
        return None


def live_board_readback(doc_id: str = "", **kwargs: Any) -> Optional[dict]:
    """Best-effort live board load + compact readback."""
    try:
        board = _load_board(doc_id or "")
        return board_mutation_readback(board, **kwargs)
    except Exception:
        return None



def sketch_ascii_report(sketch: dict, *, pitch: float = 0.5,
                        health: Optional[dict] = None,
                        near_tol: float = 1e-2,
                        highlight: Any = None) -> dict:
    """Full sketch report from a dict with entities/constraints (and optional health).

    ``highlight``: entity id ("e3"), endpoint handle ("e3.end"), entity type
    ("circle"), or a list of any of those.
    """
    entities = list(sketch.get("entities") or [])
    constraints = list(sketch.get("constraints") or [])
    if health is None and isinstance(sketch.get("health"), dict):
        health = sketch["health"]
    hl = HighlightPlan(highlight)
    rendered = render_sketch_ascii(entities, pitch=pitch, plan=hl)
    findings = check_sketch_geometry(
        entities, near_tol=near_tol, health=health, constraints=constraints,
        feeds_feature=bool(sketch.get("feeds_feature")))
    summary = summarize_sketch(entities, constraints, findings, health=health)
    scale_lines = rendered["scale"]["lines"]
    legend = rendered["legend"]
    extra = unmatched_note(hl) + (finding_callouts(findings) if not hl else [])
    callout_txt = callout_lines(hl, extra)
    return {
        "ok": True,
        "kind": "sketch",
        "ascii": rendered["ascii"],
        "ascii_ruled": rendered.get("ascii_ruled") or annotate_ascii_with_rulers(
            rendered["ascii"]),
        "ascii_annotated": annotated_ascii(
            header=f"[sketch {rendered['width']}x{rendered['height']} cells "
                   f"pitch={rendered['pitch']:.4g}mm]",
            scale_lines=scale_lines, drawing=rendered["ascii_ruled"],
            legend=legend, callouts=callout_txt),
        "summary": summary_with_annotations(summary, scale_lines, legend,
                                            callout_txt),
        "findings": findings,
        "pitch": rendered["pitch"],
        "pitch_requested": pitch,
        "scale": rendered["scale"],
        "legend": legend,
        "callouts": list(hl.callouts) + extra,
        "highlight": hl.report(),
        "width": rendered["width"],
        "height": rendered["height"],
        "bbox": rendered["bbox"],
        "grid": rendered.get("grid"),
        "index": rendered.get("index") or {},
        "hint": (
            "Use grid + index for cell-addressed sketch edits via apply_ascii_edits "
            "(kind='sketch'). Prefer entity ids from the summary when available. "
            "Pass highlight='e3' (or a list) to mark one entity with a letter and "
            "get it named in callouts."
        ),
    }



# ===================================================================== SCHEMATIC
# Sheet ASCII grid: symbols as refdes boxes, wires as path chars, labels/power.

_SCH_EMPTY = "."
_SCH_WIRE_H = "-"
_SCH_WIRE_V = "|"
_SCH_WIRE_J = "+"
_SCH_POWER = "P"
_SCH_LABEL = "L"
_SCH_JUNCT = "*"
_SCH_PIN = "o"


def _sch_bbox(sch: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for sym in sch.get("symbols") or []:
        pos = sym.get("pos") or [0.0, 0.0]
        if len(pos) >= 2:
            xs.append(float(pos[0])); ys.append(float(pos[1]))
        # pin extents if available on instance (rare) — else fixed half-box
        xs.extend([float(pos[0]) - 5.0, float(pos[0]) + 5.0])
        ys.extend([float(pos[1]) - 5.0, float(pos[1]) + 5.0])
    for w in sch.get("wires") or []:
        for pt in w.get("pts") or []:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                xs.append(float(pt[0])); ys.append(float(pt[1]))
    for lb in sch.get("labels") or []:
        pos = lb.get("pos") or [0, 0]
        if len(pos) >= 2:
            xs.append(float(pos[0])); ys.append(float(pos[1]))
    for pw in sch.get("power") or []:
        pos = pw.get("pos") or [0, 0]
        if len(pos) >= 2:
            xs.append(float(pos[0])); ys.append(float(pos[1]))
    for j in sch.get("junctions") or []:
        pos = j.get("pos") or [0, 0]
        if len(pos) >= 2:
            xs.append(float(pos[0])); ys.append(float(pos[1]))
    if not xs:
        return (0.0, 0.0, 40.0, 30.0)
    pad = 2.54  # one pin grid unit
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _sch_cell_index(sch: dict, xmin: float, ymax: float, pitch: float,
                    w: int, h: int) -> dict:
    cells: dict[str, list[dict]] = {}

    def add(cx: int, cy: int, item: dict) -> None:
        if not (0 <= cx < w and 0 <= cy < h):
            return
        key = f"{cx},{cy}"
        cells.setdefault(key, []).append(item)

    for sym in sch.get("symbols") or []:
        pos = sym.get("pos") or [0.0, 0.0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "symbol",
            "id": sym.get("id"),
            "ref": sym.get("ref"),
            "libid": sym.get("libid"),
            "value": sym.get("value"),
            "pos": [float(pos[0]), float(pos[1])],
            "angle": sym.get("angle", 0.0),
            "footprint": sym.get("footprint"),
        })
    for wire in sch.get("wires") or []:
        pts = wire.get("pts") or []
        if len(pts) < 2:
            continue
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            x0, y0 = _to_cell(float(a[0]), float(a[1]), xmin, ymax, pitch, w, h)
            x1, y1 = _to_cell(float(b[0]), float(b[1]), xmin, ymax, pitch, w, h)
            for cx, cy in _bresenham(x0, y0, x1, y1):
                add(cx, cy, {
                    "kind": "wire",
                    "id": wire.get("id"),
                    "pts": [[float(p[0]), float(p[1])] for p in pts
                            if isinstance(p, (list, tuple)) and len(p) >= 2],
                })
    for lb in sch.get("labels") or []:
        pos = lb.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "label",
            "id": lb.get("id"),
            "text": lb.get("text"),
            "scope": lb.get("scope", "local"),
            "pos": [float(pos[0]), float(pos[1])],
        })
    for pw in sch.get("power") or []:
        pos = pw.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "power",
            "id": pw.get("id"),
            "net": pw.get("net"),
            "pos": [float(pos[0]), float(pos[1])],
        })
    for j in sch.get("junctions") or []:
        pos = j.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        add(cx, cy, {
            "kind": "junction",
            "id": j.get("id"),
            "pos": [float(pos[0]), float(pos[1])],
        })
    return {k: v for k, v in sorted(cells.items())}


_SCH_LEGEND_BASE = (
    "  .   = empty sheet",
    "  -   = wire running east-west        |   = wire running north-south",
    "  /   = wire running SW->NE           \\   = wire running NW->SE",
    "  +   = wire crossing/corner, or the border of a symbol box",
    "  *   = junction (wires electrically joined here)",
    "  L   = net label anchor, followed by up to 6 characters of its text",
    "  P   = power port anchor, followed by up to 4 characters of its net name",
    "  R1 U2 … on a symbol box top border = refdes; the row below it = value",
)


def render_schematic_ascii(sch: dict, pitch_mm: float = 2.54,
                           max_dim: int = 80,
                           highlight: Any = None,
                           plan: Optional[HighlightPlan] = None) -> dict:
    """Deterministic schematic sheet ASCII grid with cell index + rulers.

    ``highlight``: symbol ref ("R1") or id ("s1"), wire id ("w2"), junction id,
    label text ("MID"), power/net name ("GND" / "net:GND"), or a list.
    """
    if pitch_mm <= 0:
        pitch_mm = 2.54
    hl = plan if plan is not None else HighlightPlan(highlight)
    xmin, ymin, xmax, ymax = _sch_bbox(sch)
    bw = max(xmax - xmin, pitch_mm)
    bh = max(ymax - ymin, pitch_mm)
    pitch = pitch_mm
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)

    grid = [[_SCH_EMPTY for _ in range(w)] for _ in range(h)]

    def paint(cx: int, cy: int, ch: str, force: bool = False) -> None:
        if not (0 <= cx < w and 0 <= cy < h):
            return
        prev = grid[cy][cx]
        if force or prev == _SCH_EMPTY:
            grid[cy][cx] = ch
        elif prev != ch and prev not in (_SCH_JUNCT,):
            if ch in (_SCH_WIRE_H, _SCH_WIRE_V) and prev in (_SCH_WIRE_H, _SCH_WIRE_V):
                grid[cy][cx] = _SCH_WIRE_J
            # keep stronger markers (symbols/labels/power)

    # wires first
    for wire in sch.get("wires") or []:
        pts = wire.get("pts") or []
        if len(pts) < 2:
            continue
        mk = hl.marker([("wire", wire.get("id")), ("id", wire.get("id")),
                        ("net", wire.get("net"))])
        touched = 0
        first: Optional[tuple[int, int]] = None
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            x0, y0 = _to_cell(float(a[0]), float(a[1]), xmin, ymax, pitch, w, h)
            x1, y1 = _to_cell(float(b[0]), float(b[1]), xmin, ymax, pitch, w, h)
            if first is None:
                first = (x0, y0)
            ch = _trace_char(x0, y0, x1, y1)
            for cx, cy in _bresenham(x0, y0, x1, y1):
                paint(cx, cy, mk or ch, force=bool(mk))
                touched += 1
        if mk and first is not None:
            hl_stamp_anchor(grid, first[0], first[1], mk, empty=_SCH_EMPTY)
            ends = f"({float(pts[0][0]):g},{float(pts[0][1]):g})->" \
                   f"({float(pts[-1][0]):g},{float(pts[-1][1]):g})mm"
            hl.note(mk, str(wire.get("id") or "wire"),
                    f"wire {ends} {len(pts)} point(s)"
                    + (f" net={wire.get('net')}" if wire.get("net") else ""),
                    anchor=first,
                    world=(float(pts[0][0]), float(pts[0][1])), cells=touched)

    for j in sch.get("junctions") or []:
        pos = j.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        mk = hl.marker([("junction", j.get("id")), ("id", j.get("id"))])
        paint(cx, cy, mk or _SCH_JUNCT, force=True)
        if mk:
            hl_stamp_anchor(grid, cx, cy, mk, empty=_SCH_EMPTY)
            hl.note(mk, str(j.get("id") or "junction"),
                    f"junction at ({float(pos[0]):g},{float(pos[1]):g})mm",
                    anchor=(cx, cy), world=(float(pos[0]), float(pos[1])), cells=1)

    for lb in sch.get("labels") or []:
        pos = lb.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        mk = hl.marker([("label", lb.get("id")), ("id", lb.get("id")),
                        ("label", lb.get("text")), ("net", lb.get("text")),
                        ("name", lb.get("text"))])
        paint(cx, cy, mk or _SCH_LABEL, force=True)
        text = str(lb.get("text") or "")[:6]
        for i, ch in enumerate(text):
            if 0 <= cx + 1 + i < w and grid[cy][cx + 1 + i] == _SCH_EMPTY:
                grid[cy][cx + 1 + i] = ch
        if mk:
            hl.note(mk, str(lb.get("id") or lb.get("text") or "label"),
                    f"net label {lb.get('text')!r} scope={lb.get('scope', 'local')} "
                    f"at ({float(pos[0]):g},{float(pos[1]):g})mm",
                    anchor=(cx, cy), world=(float(pos[0]), float(pos[1])), cells=1)

    for pw in sch.get("power") or []:
        pos = pw.get("pos") or [0, 0]
        cx, cy = _to_cell(float(pos[0]), float(pos[1]), xmin, ymax, pitch, w, h)
        mk = hl.marker([("power", pw.get("id")), ("id", pw.get("id")),
                        ("power", pw.get("net")), ("net", pw.get("net")),
                        ("name", pw.get("net"))])
        paint(cx, cy, mk or _SCH_POWER, force=True)
        net = str(pw.get("net") or "")[:4]
        for i, ch in enumerate(net):
            if 0 <= cx + 1 + i < w and grid[cy][cx + 1 + i] == _SCH_EMPTY:
                grid[cy][cx + 1 + i] = ch
        if mk:
            hl.note(mk, str(pw.get("id") or pw.get("net") or "power"),
                    f"power port net={pw.get('net')!r} at "
                    f"({float(pos[0]):g},{float(pos[1]):g})mm",
                    anchor=(cx, cy), world=(float(pos[0]), float(pos[1])), cells=1)

    # symbols as small boxes with refdes
    for sym in sch.get("symbols") or []:
        pos = sym.get("pos") or [0.0, 0.0]
        ox, oy = float(pos[0]), float(pos[1])
        # half-size ~2.5 mm each side in world → cells
        half = max(1, int(round(2.54 / pitch)))
        cx, cy = _to_cell(ox, oy, xmin, ymax, pitch, w, h)
        x_lo, x_hi = max(0, cx - half), min(w - 1, cx + half)
        y_lo, y_hi = max(0, cy - half), min(h - 1, cy + half)
        ref = str(sym.get("ref") or sym.get("id") or "?")
        mk = hl.marker([("ref", ref), ("symbol", ref), ("id", sym.get("id")),
                        ("symbol", sym.get("id")), ("libid", sym.get("libid")),
                        ("value", sym.get("value"))])
        for x in range(x_lo, x_hi + 1):
            for y in range(y_lo, y_hi + 1):
                edge = x in (x_lo, x_hi) or y in (y_lo, y_hi)
                if not edge:
                    continue
                if mk:
                    grid[y][x] = mk
                elif grid[y][x] == _SCH_EMPTY:
                    grid[y][x] = "+"
        for i, ch in enumerate(ref[: max(1, x_hi - x_lo)]):
            x = x_lo + i
            if 0 <= x < w:
                grid[y_lo][x] = ch
        val = str(sym.get("value") or "")
        if val:
            for i, ch in enumerate(val[: max(1, x_hi - x_lo)]):
                x = x_lo + i
                y = min(h - 1, y_lo + 1)
                if 0 <= x < w and grid[y][x] in (_SCH_EMPTY, "+", mk or "\0"):
                    grid[y][x] = ch
        if mk:
            hl_stamp_anchor(grid, cx, cy, mk, empty=_SCH_EMPTY)
            hl.note(mk, ref,
                    f"symbol {ref} libid={sym.get('libid') or '?'} "
                    f"value={sym.get('value')!r} at ({ox:g},{oy:g})mm "
                    f"angle={sym.get('angle', 0)}deg "
                    f"box cols {x_lo}..{x_hi} rows {y_lo}..{y_hi}",
                    anchor=(cx, cy), world=(ox, oy),
                    cells=max(0, (x_hi - x_lo + 1) * 2 + (y_hi - y_lo - 1) * 2))

    ascii_block = "\n".join("".join(r) for r in grid)
    gmeta = make_grid_meta(
        xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
        pitch=pitch, width=w, height=h, unit="mm", kind="schematic",
    )
    index = _sch_cell_index(sch, xmin, ymax, pitch, w, h)
    ruled = annotate_ascii_with_rulers(ascii_block)
    scale_lines = scale_origin_lines(
        gmeta,
        note=("HIDDEN: symbol pins and bodies are NOT drawn to shape — every "
              "symbol is a fixed ~2.54mm half-size box around its origin, so "
              "boxes may overlap even when the real symbols do not. Connectivity "
              "comes from the wire/junction geometry, not from the boxes."))
    legend = (["LEGEND (schematic sheet, one character = one grid cell):"]
              + list(_SCH_LEGEND_BASE) + highlight_legend_lines(hl))
    callouts = callout_lines(hl, unmatched_note(hl) if plan is None else ())
    return {
        "ascii": ascii_block,
        "ascii_ruled": ruled,
        "ascii_annotated": annotated_ascii(
            header=f"[schematic {w}x{h} cells pitch={pitch:.4g}mm]",
            scale_lines=scale_lines, drawing=ruled,
            legend=legend, callouts=callouts),
        "scale": {"pitch_mm": pitch, "unit": "mm", "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts),
        "highlight": hl.report(),
        "pitch_mm": pitch,
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox": [xmin, ymin, xmax, ymax],
        "bbox_mm": [xmin, ymin, xmax, ymax],
        "grid": gmeta,
        "index": index,
        "symbol_count": len(sch.get("symbols") or []),
        "wire_count": len(sch.get("wires") or []),
        "label_count": len(sch.get("labels") or []),
        "power_count": len(sch.get("power") or []),
    }


def check_schematic(sch: dict) -> list[dict]:
    """Lightweight geometry/ERC-ish findings without requiring a live session."""
    findings: list[dict] = []
    refs = {}
    for sym in sch.get("symbols") or []:
        ref = sym.get("ref") or ""
        if not ref:
            findings.append({
                "code": "missing_ref",
                "severity": "warning",
                "id": sym.get("id"),
                "message": f"Symbol {sym.get('id')} has no refdes",
            })
            continue
        if ref in refs:
            findings.append({
                "code": "duplicate_ref",
                "severity": "error",
                "refs": [ref],
                "ids": [refs[ref], sym.get("id")],
                "message": f"Duplicate refdes {ref!r}",
            })
        else:
            refs[ref] = sym.get("id")
        if not sym.get("libid"):
            findings.append({
                "code": "missing_libid",
                "severity": "error",
                "ref": ref,
                "message": f"{ref} has no libid",
            })
    # floating labels (not near any wire/symbol within 1 mm) — soft
    pts = []
    for w in sch.get("wires") or []:
        for pt in w.get("pts") or []:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                pts.append((float(pt[0]), float(pt[1])))
    for sym in sch.get("symbols") or []:
        pos = sym.get("pos") or [0, 0]
        pts.append((float(pos[0]), float(pos[1])))
    for lb in sch.get("labels") or []:
        pos = lb.get("pos") or [0, 0]
        lx, ly = float(pos[0]), float(pos[1])
        if pts and min(math.hypot(lx - x, ly - y) for x, y in pts) > 1.0:
            findings.append({
                "code": "floating_label",
                "severity": "warning",
                "text": lb.get("text"),
                "id": lb.get("id"),
                "message": (f"Label {lb.get('text')!r} is >1 mm from any wire/symbol "
                            f"— may not name a net"),
            })
    if not (sch.get("symbols") or []):
        findings.append({
            "code": "empty_schematic",
            "severity": "warning",
            "message": "Schematic has no symbols",
        })
    # optional netlist-backed unconnected pins
    try:
        from services.mcp.ecad_common import netlist_mod as _nl_mod  # noqa: PLC0415
        nl = _nl_mod.build_netlist(sch)
        by_pin = nl.get("by_pin") or {}
        # count pins that never appear in by_pin if components list available
        for comp in nl.get("components") or []:
            ref = comp.get("ref")
            for pin in comp.get("pins") or []:
                # pins may be dicts or numbers
                if isinstance(pin, dict):
                    num = str(pin.get("number") or pin.get("name") or "")
                else:
                    num = str(pin)
                if not num:
                    continue
                # component id may be in by_pin as id.num
                sid = comp.get("id") or ""
                if sid and f"{sid}.{num}" not in by_pin and f"{ref}.{num}" not in by_pin:
                    # only flag if nets empty for that pin — soft
                    pass
        nets = nl.get("nets") or {}
        single = [n for n, members in nets.items()
                  if isinstance(members, list) and len(members) == 1
                  and not str(n).upper().startswith(("GND", "VCC", "VDD", "+"))]
        for n in single[:12]:
            findings.append({
                "code": "single_pin_net",
                "severity": "hint",
                "net": n,
                "message": f"Net {n!r} has only one pin member",
            })
    except Exception:
        pass
    findings.sort(key=lambda f: (f.get("code", ""), f.get("message", "")))
    return findings


def summarize_schematic(sch: dict, findings: list[dict]) -> str:
    syms = sch.get("symbols") or []
    wires = sch.get("wires") or []
    labels = sch.get("labels") or []
    power = sch.get("power") or []
    lines = [
        f"Schematic: {len(syms)} symbol(s), {len(wires)} wire(s), "
        f"{len(labels)} label(s), {len(power)} power port(s).",
    ]
    if syms:
        lines.append("Symbols:")
        for s in syms[:24]:
            lines.append(
                f"  {s.get('ref') or '?'}  libid={s.get('libid') or '?'}  "
                f"value={s.get('value')!r}  pos={s.get('pos')}  id={s.get('id')}"
            )
        if len(syms) > 24:
            lines.append(f"  … +{len(syms) - 24} more")
    if labels:
        lines.append("Labels: " + ", ".join(
            f"{lb.get('text')}@{lb.get('pos')}" for lb in labels[:12]))
    if power:
        lines.append("Power: " + ", ".join(
            f"{pw.get('net')}@{pw.get('pos')}" for pw in power[:12]))
    if findings:
        lines.append(f"Findings ({len(findings)}):")
        for f in findings[:12]:
            lines.append(f"  [{f.get('severity','?')}] {f.get('message')}")
    else:
        lines.append("Findings: none")
    return "\n".join(lines)


def schematic_ascii_report(sch: dict, pitch_mm: float = 2.54,
                           highlight: Any = None) -> dict:
    """Full schematic report: sheet ASCII + cell index + checks.

    ``highlight``: symbol ref/id, wire id, junction id, label text, power net,
    or a list of any of those.
    """
    hl = HighlightPlan(highlight)
    rendered = render_schematic_ascii(sch, pitch_mm=pitch_mm, plan=hl)
    findings = check_schematic(sch)
    summary = summarize_schematic(sch, findings)
    scale_lines = rendered["scale"]["lines"]
    legend = rendered["legend"]
    extra = unmatched_note(hl) + (finding_callouts(findings) if not hl else [])
    callout_txt = callout_lines(hl, extra)
    return {
        "ok": True,
        "kind": "schematic",
        "ascii": rendered["ascii"],
        "ascii_ruled": rendered["ascii_ruled"],
        "ascii_annotated": annotated_ascii(
            header=f"[schematic {rendered['width']}x{rendered['height']} cells "
                   f"pitch={rendered['pitch_mm']:.4g}mm]",
            scale_lines=scale_lines, drawing=rendered["ascii_ruled"],
            legend=legend, callouts=callout_txt),
        "summary": summary_with_annotations(summary, scale_lines, legend,
                                            callout_txt),
        "findings": findings,
        "pitch_mm": rendered["pitch_mm"],
        "pitch_mm_requested": pitch_mm,
        "pitch": rendered["pitch"],
        "scale": rendered["scale"],
        "legend": legend,
        "callouts": list(hl.callouts) + extra,
        "highlight": hl.report(),
        "width": rendered["width"],
        "height": rendered["height"],
        "bbox": rendered["bbox"],
        "grid": rendered["grid"],
        "index": rendered["index"],
        "hint": (
            "READ the ruled ASCII + index before the next schematic edit. "
            "Use apply_ascii_edits(kind='schematic', edits=[...]) to move/swap "
            "symbols, delete wires, or place labels/power at cell coords. "
            "Call render_schematic_ascii before declaring the schematic done. "
            "Pass highlight='R1' / 'net:GND' / ['R1','w2'] to mark one element."
        ),
    }


def schematic_mutation_readback(sch: dict, *, pitch_mm: float = 2.54,
                                max_h: int = READBACK_MAX_H,
                                max_w: int = READBACK_MAX_W,
                                focus: Optional[Sequence[float]] = None,
                                focus_radius_mm: float = 30.0) -> dict:
    view = sch
    if focus is not None and len(focus) >= 2:
        fx, fy = float(focus[0]), float(focus[1])
        r = float(focus_radius_mm)
        r2 = r * r

        def near(p) -> bool:
            try:
                x, y = _xy(p)
            except Exception:
                return False
            return (x - fx) ** 2 + (y - fy) ** 2 <= r2

        view = dict(sch)
        view["symbols"] = [s for s in (sch.get("symbols") or [])
                           if near(s.get("pos") or [0, 0])]
        view["wires"] = [w for w in (sch.get("wires") or [])
                         if any(near(pt) for pt in (w.get("pts") or []))]
        view["labels"] = [lb for lb in (sch.get("labels") or [])
                          if near(lb.get("pos") or [0, 0])]
        view["power"] = [pw for pw in (sch.get("power") or [])
                         if near(pw.get("pos") or [0, 0])]
        view["junctions"] = [j for j in (sch.get("junctions") or [])
                             if near(j.get("pos") or [0, 0])]
    max_dim = max(8, min(max_h, max_w))
    # temporarily render with max_dim via pitch coarsen inside renderer
    rendered = render_schematic_ascii(view, pitch_mm=pitch_mm, max_dim=max_dim)
    ascii_txt, h, w, truncated = _cap_ascii(rendered["ascii"], max_h=max_h, max_w=max_w)
    findings = check_schematic(sch)
    summary = summarize_schematic(sch, findings)
    compact = [{"code": f.get("code"), "severity": f.get("severity"),
                "message": f.get("message")} for f in findings[:READBACK_MAX_FINDINGS]]
    header = f"[schematic_readback {w}x{h}"
    if truncated:
        header += " truncated=1"
    if focus is not None and len(focus) >= 2:
        header += f" focus=({float(focus[0]):g},{float(focus[1]):g})"
    header += "]"
    return {
        "ok": True,
        "kind": "schematic_readback",
        "ascii_ruled": f"{header}\n{annotate_ascii_with_rulers(ascii_txt)}",
        "summary": summary,
        "findings": compact,
        "findings_total": len(findings),
        "width": w,
        "height": h,
        "pitch_mm": rendered.get("pitch_mm"),
        "grid": rendered.get("grid"),
        "index": rendered.get("index") or {},
        "truncated": truncated,
        "caps": {"max_h": max_h, "max_w": max_w},
        "hint": ("READ this ASCII + summary before the next schematic edit. "
                 "Call render_schematic_ascii before declaring the schematic done. "
                 "Targeted edits: apply_ascii_edits(kind='schematic', ...)."),
    }


def attach_schematic_readback(response: dict, sch: dict, **kwargs: Any) -> dict:
    """Mutate *response* in place with the schematic ``ascii_readback`` block.

    Says each thing ONCE (see ``attach_sketch_readback``). Measured 2026-07-30 on a
    nine-symbol ESP32 sheet: ``ascii`` (671) and ``ascii_block`` (698) were the same
    drawing twice inside the block and copied AGAIN to the top level alongside a second
    copy of the 871-char summary and of the 22k ``index``. One ruled picture remains —
    its col/row rulers are the addressing ``apply_ascii_edits(kind='schematic')`` reads.
    """
    try:
        rb = schematic_mutation_readback(sch, **kwargs)
        response["ascii_readback"] = rb
        if not response.get("summary"):
            response["summary"] = rb["summary"]
    except Exception as e:  # noqa: BLE001
        response["ascii_readback"] = {
            "ok": False, "error": f"schematic readback unavailable: {e}",
        }
    return response


# =========================================================================== SIM
# SPICE deck + circuit device roster as deterministic plaintext (not a 2D grid).


_SIM_LEGEND_BASE = (
    "  [circuit devices=N nodes=M] = section header: SPICE device roster",
    "  [spice_deck]                = section header: raw deck text as solved",
    "  [netlist components=N nets=M] = section header: components + net membership",
    "  [results]                   = section header: solved values",
    "  <kind> <name> nodes=[a,b] k=v = one device: element type, instance name,",
    "        ORDERED node list (order is the SPICE pin order), then parameters",
    "  net NAME: R1.1,R2.2         = net membership written as refdes.pin",
    "  key = value                 = one solved probe (volts unless noted)",
)


def _sim_probe_cands(key: str) -> list[tuple[str, Any]]:
    k = str(key)
    cands: list[tuple[str, Any]] = [("probe", k), ("node", k), ("name", k),
                                    ("id", k)]
    low = k.lower()
    if low.startswith(("v(", "i(")) and low.endswith(")"):
        inner = k[2:-1]
        cands += [("probe", inner), ("node", inner), ("name", inner),
                  ("id", inner)]
    return cands


def render_sim_ascii(circuit: Optional[dict] = None,
                     spice_text: str = "",
                     netlist: Optional[dict] = None,
                     results: Optional[dict] = None,
                     highlight: Any = None) -> dict:
    """Deterministic plaintext of a SPICE circuit / deck for non-vision models.

    Not a spatial grid — simulation state is topological. Returns a stable
    multi-section ASCII block + summary + findings.

    ``highlight``: device/instance name ("R1"), a node ("n1" / "node:GND"), a
    netlist component ref, a net name, a probe key ("V(mid)"), or a list. Marked
    rows carry a ``>A<`` gutter; every row has a 4-character gutter so columns
    line up whether or not anything is highlighted.
    """
    hl = HighlightPlan(highlight)
    lines: list[str] = []
    findings: list[dict] = []
    devices = []
    nodes: list[str] = []

    def row(text: str, marker: Optional[str] = None) -> None:
        gut = f"{_HL_BRACKET_L}{marker}{_HL_BRACKET_R} " if marker else "    "
        lines.append(gut + text)

    if circuit and isinstance(circuit, dict):
        devices = list(circuit.get("devices") or [])
        nodes = list(circuit.get("nodes") or [])
        lines.append(f"[circuit devices={len(devices)} nodes={len(nodes)}]")
        for d in devices:
            if not isinstance(d, dict):
                continue
            kind = d.get("kind") or d.get("type") or "?"
            name = d.get("name") or d.get("ref") or "?"
            nlist = d.get("nodes") or d.get("pins") or []
            params = d.get("params") or {}
            # stable param order
            if isinstance(params, dict):
                ptxt = " ".join(f"{k}={params[k]}" for k in sorted(params))
            else:
                ptxt = str(params)
            ntxt = ",".join(str(n) for n in nlist)
            cands: list[tuple[str, Any]] = [
                ("device", name), ("name", name), ("id", name), ("ref", name),
                ("type", kind),
            ]
            cands += [("node", n) for n in nlist]
            mk = hl.marker(cands)
            row(f"{kind} {name} nodes=[{ntxt}] {ptxt}".rstrip(), mk)
            if mk:
                hl.note(mk, str(name),
                        f"{kind} device {name} on nodes [{ntxt}]"
                        + (f" with {ptxt}" if ptxt else "")
                        + f" [circuit view line {len(lines)}]")
        if not devices:
            findings.append({
                "code": "no_devices",
                "severity": "error",
                "message": "Circuit has no simulatable devices",
            })
    if spice_text:
        lines.append("[spice_deck]")
        # strip blank trailing lines, keep deterministic
        deck_lines = [ln.rstrip() for ln in str(spice_text).splitlines()]
        while deck_lines and not deck_lines[-1]:
            deck_lines.pop()
        for ln in (deck_lines or ["(empty deck)"]):
            head = ln.split()[0] if ln.split() else ""
            mk = hl.marker([("device", head), ("name", head), ("id", head),
                            ("ref", head)]) if head else None
            row(ln, mk)
            if mk:
                hl.note(mk, head, f"SPICE deck card: {ln.strip()!r} "
                                  f"[deck view line {len(lines)}]")
    if netlist and isinstance(netlist, dict):
        comps = netlist.get("components") or []
        nets = netlist.get("nets") or {}
        lines.append(f"[netlist components={len(comps)} nets={len(nets)}]")
        for c in comps[:40]:
            if not isinstance(c, dict):
                continue
            ref = c.get("ref") or "?"
            mk = hl.marker([("component", ref), ("ref", ref), ("id", ref),
                            ("name", ref), ("libid", c.get("libid")),
                            ("value", c.get("value"))])
            row(f"{ref} libid={c.get('libid') or '?'} value={c.get('value')!r}", mk)
            if mk:
                hl.note(mk, str(ref),
                        f"netlist component {ref} libid={c.get('libid') or '?'} "
                        f"value={c.get('value')!r} "
                        f"[netlist view line {len(lines)}]")
        for nname in sorted(nets)[:40]:
            members = nets[nname] or []
            mtxt = ",".join(
                f"{m.get('ref')}.{m.get('pin')}" if isinstance(m, dict) else str(m)
                for m in members[:8]
            )
            mk = hl.marker([("net", nname), ("name", nname), ("node", nname),
                            ("id", nname)])
            row(f"net {nname}: {mtxt}"
                + (f" +{len(members)-8}" if len(members) > 8 else ""), mk)
            if mk:
                hl.note(mk, str(nname),
                        f"net {nname} with {len(members)} member pin(s): {mtxt}"
                        f" [netlist view line {len(lines)}]")
    if results and isinstance(results, dict):
        lines.append("[results]")
        # common shapes: op voltages, probes
        probes = results.get("probes") or results.get("values") or results.get("op") or results
        if isinstance(probes, dict):
            for k in sorted(probes):
                v = probes[k]
                mk = hl.marker(_sim_probe_cands(k))
                row(f"{k} = {v:.6g}" if isinstance(v, (int, float))
                    else f"{k} = {v}", mk)
                if mk:
                    hl.note(mk, str(k),
                            f"solved probe {k} = {v} [results view line {len(lines)}]")
        elif isinstance(probes, list):
            for item in probes[:40]:
                row(str(item))
    ascii_block = "\n".join(lines) if lines else "(empty simulation view)"
    summary_bits = []
    if devices:
        summary_bits.append(f"{len(devices)} device(s)")
    if nodes:
        summary_bits.append(f"{len(nodes)} node(s)")
    if spice_text:
        summary_bits.append(f"deck {len(str(spice_text).splitlines())} line(s)")
    if results:
        summary_bits.append("results present")
    summary = "Simulation: " + (", ".join(summary_bits) if summary_bits else "empty") + "."
    if findings:
        summary += " " + "; ".join(f.get("message", "") for f in findings[:4])
    scale_lines = [
        "SCALE: TOPOLOGICAL view — this is NOT a spatial drawing and has no "
        "mm-per-character scale. One text row = one device, net, deck card or "
        "probe; row order is the section order below, alphabetical within nets "
        "and probes.",
        "ORIGIN: none — no coordinate frame exists here. Component positions "
        "live in the schematic; use render_schematic_ascii for geometry.",
        "GUTTER: every payload row starts with a 4-character marker gutter; "
        "'    ' = not highlighted, '>A< ' = highlighted with marker A. Section "
        "headers start at column 0. Payload text begins at column 4.",
    ]
    legend = (["LEGEND (simulation, plaintext topology):"]
              + list(_SIM_LEGEND_BASE) + highlight_legend_lines(hl, mode="rows"))
    extra = unmatched_note(hl) + (finding_callouts(findings) if not hl else [])
    callout_txt = callout_lines(hl, extra)
    return {
        "ok": True,
        "kind": "sim",
        "ascii": ascii_block,
        "ascii_annotated": annotated_ascii(
            header=f"[sim view {len(lines)} row(s)]",
            scale_lines=scale_lines, drawing=ascii_block,
            legend=legend, callouts=callout_txt),
        "summary": summary_with_annotations(summary, scale_lines, legend,
                                            callout_txt),
        "findings": findings,
        "scale": {"kind": "topological", "unit": None, "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts) + extra,
        "highlight": hl.report(),
        "device_count": len(devices),
        "node_count": len(nodes),
        "grid": None,  # topological, not spatial
        "hint": (
            "Simulation view is topological (device/node list + SPICE deck), not a "
            "2D grid. Edit the schematic (render_schematic_ascii / apply_ascii_edits "
            "kind='schematic') then re-run ecad_simulate. Pass highlight='R1' or "
            "'node:mid' to mark the rows you are reasoning about."
        ),
    }


# ============================================================== APPLY EDITS
# Cell-addressed / id-addressed surgical edits → engine op batches.


def _parse_cell(cell: Any) -> Optional[tuple[int, int]]:
    if cell is None:
        return None
    if isinstance(cell, (list, tuple)) and len(cell) >= 2:
        return int(cell[0]), int(cell[1])
    if isinstance(cell, str) and "," in cell:
        a, b = cell.split(",", 1)
        return int(a.strip()), int(b.strip())
    if isinstance(cell, dict) and "col" in cell and "row" in cell:
        return int(cell["col"]), int(cell["row"])
    return None


def _index_hits(index: dict, col: int, row: int,
                kinds: Optional[Sequence[str]] = None) -> list[dict]:
    hits = list(index.get(f"{col},{row}") or [])
    if kinds:
        want = set(kinds)
        hits = [h for h in hits if h.get("kind") in want]
    return hits


def plan_ascii_edits(kind: str, edits: Sequence[dict], *,
                     grid: Optional[dict] = None,
                     index: Optional[dict] = None,
                     doc: Optional[dict] = None,
                     layer: str = "F.Cu",
                     sketch_id: str = "") -> dict:
    """Compile high-level ASCII edits into engine ops (pure; does not apply).

    Edit object shapes (all fields optional except ``op``)::

        {"op": "move", "ref"|"id": "...", "to_cell": [col,row] | "to": [x,y]}
        {"op": "swap", "a": "R1", "b": "R2"}          # swap positions (and angles)
        {"op": "delete", "ref"|"id": "..." | "cell": [c,r], "kinds": ["track"]}
        {"op": "place_symbol", "libid": "Device:R", "cell": [c,r], "ref": "R3", "value": "10k"}
        {"op": "place_label", "text": "MID", "cell": [c,r], "scope": "local"}
        {"op": "place_power", "net": "GND", "cell": [c,r]}
        {"op": "add_track", "net": "A", "from_cell": [c,r], "to_cell": [c2,r2], "layer": "F.Cu"}
        {"op": "add_wire", "from_cell": [c,r], "to_cell": [c2,r2]}
        {"op": "add_via", "net": "A", "cell": [c,r]}
        {"op": "set_value", "ref": "R1", "value": "4.7k"}
        {"op": "replace_section", "cell_min": [c0,r0], "cell_max": [c1,r1],
         "clear": ["track","via"], "place": [ ... nested edits ... ]}
        {"op": "sketch_add_line", "from_cell":[c,r], "to_cell":[c2,r2]}
        {"op": "sketch_delete", "id": "e1" | "cell": [c,r]}

    Returns ``{ok, ops, planned, errors, kind}``.
    """
    kind = (kind or "").lower().strip()
    if kind in ("sch", "sheet"):
        kind = "schematic"
    if kind in ("board", "pcb_layout"):
        kind = "pcb"
    if kind not in ("schematic", "pcb", "sketch"):
        return {"ok": False, "error": f"unknown kind {kind!r}",
                "hint": "kind must be schematic|pcb|sketch",
                "ops": [], "planned": [], "errors": []}

    grid = dict(grid or {})
    index = dict(index or {})
    doc = doc or {}
    ops: list[dict] = []
    planned: list[dict] = []
    errors: list[str] = []

    def world_of(edit: dict, cell_key: str = "cell",
                 world_key: str = "to") -> Optional[list[float]]:
        if world_key in edit and edit[world_key] is not None:
            p = edit[world_key]
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                return [float(p[0]), float(p[1])]
        cell = _parse_cell(edit.get(cell_key) or edit.get("to_cell")
                           or edit.get("from_cell"))
        # for to_cell specifically
        if cell is None and cell_key != "to_cell":
            cell = _parse_cell(edit.get("to_cell"))
        if cell is None:
            return None
        if not grid:
            errors.append("grid metadata required to resolve cell coordinates")
            return None
        x, y = cell_to_world(cell[0], cell[1], grid)
        return [x, y]

    def find_symbol(token: str) -> Optional[dict]:
        for s in doc.get("symbols") or []:
            if s.get("ref") == token or s.get("id") == token:
                return s
        # index fallback
        for hits in index.values():
            for h in hits:
                if h.get("kind") == "symbol" and (
                        h.get("ref") == token or h.get("id") == token):
                    return h
        return None

    def find_footprint(token: str) -> Optional[dict]:
        for fp in doc.get("footprints") or []:
            if fp.get("ref") == token or fp.get("id") == token:
                return fp
        for hits in index.values():
            for h in hits:
                if h.get("kind") == "footprint" and (
                        h.get("ref") == token or h.get("id") == token):
                    return h
        return None

    for i, raw in enumerate(edits or []):
        if not isinstance(raw, dict):
            errors.append(f"edit[{i}]: not an object")
            continue
        eop = (raw.get("op") or raw.get("action") or "").lower().strip()
        if not eop:
            errors.append(f"edit[{i}]: missing op")
            continue

        # -------- replace_section: clear kinds in AABB then nested place ----
        if eop in ("replace_section", "section_replace", "clear_section"):
            cmin = _parse_cell(raw.get("cell_min") or raw.get("min") or raw.get("from_cell"))
            cmax = _parse_cell(raw.get("cell_max") or raw.get("max") or raw.get("to_cell"))
            if not cmin or not cmax:
                errors.append(f"edit[{i}]: replace_section needs cell_min + cell_max")
                continue
            col0, row0 = min(cmin[0], cmax[0]), min(cmin[1], cmax[1])
            col1, row1 = max(cmin[0], cmax[0]), max(cmin[1], cmax[1])
            clear_kinds = set(raw.get("clear") or raw.get("kinds")
                              or (["track", "via"] if kind == "pcb"
                                  else ["wire", "label"] if kind == "schematic"
                                  else ["entity"]))
            seen_ids: set[str] = set()
            for r in range(row0, row1 + 1):
                for c in range(col0, col1 + 1):
                    for h in _index_hits(index, c, r):
                        hk = h.get("kind")
                        if hk not in clear_kinds and not (
                                hk == "pad" and "footprint" in clear_kinds):
                            # map pad→footprint clear
                            if hk == "footprint" and "footprint" in clear_kinds:
                                pass
                            else:
                                if hk not in clear_kinds:
                                    continue
                        hid = h.get("id") or h.get("footprint_id")
                        if not hid or hid in seen_ids:
                            continue
                        seen_ids.add(str(hid))
                        if kind == "pcb":
                            if hk == "track":
                                ops.append({"op": "pcb_delete_track", "id": hid})
                            elif hk == "via":
                                ops.append({"op": "pcb_delete_via", "id": hid})
                            elif hk in ("footprint", "pad"):
                                fid = h.get("footprint_id") or h.get("id")
                                if fid and fid not in seen_ids:
                                    ops.append({"op": "pcb_delete_footprint", "id": fid})
                                    seen_ids.add(str(fid))
                        elif kind == "schematic":
                            if hk == "wire":
                                ops.append({"op": "sch_delete_wire", "id": hid})
                            elif hk == "symbol":
                                ops.append({"op": "sch_delete_symbol", "id": hid})
                            elif hk == "label":
                                ops.append({"op": "sch_delete_label", "id": hid})
                            elif hk == "junction":
                                ops.append({"op": "sch_delete_junction", "id": hid})
                        elif kind == "sketch":
                            ops.append({
                                "op": "sketch_delete_entity",
                                "sketch": sketch_id or raw.get("sketch") or "",
                                "id": hid,
                            })
            planned.append({"edit": i, "op": eop, "cleared_ids": sorted(seen_ids),
                            "cells": [[col0, row0], [col1, row1]]})
            # nested place edits
            for j, nested in enumerate(raw.get("place") or raw.get("edits") or []):
                if isinstance(nested, dict):
                    # recurse single by appending to a synthetic list handled below via re-entry
                    sub = plan_ascii_edits(kind, [nested], grid=grid, index=index,
                                           doc=doc, layer=layer, sketch_id=sketch_id)
                    ops.extend(sub.get("ops") or [])
                    planned.extend(sub.get("planned") or [])
                    errors.extend(sub.get("errors") or [])
            continue

        if eop == "swap":
            a = raw.get("a") or raw.get("ref_a") or raw.get("left")
            b = raw.get("b") or raw.get("ref_b") or raw.get("right")
            if not a or not b:
                errors.append(f"edit[{i}]: swap needs a + b refs")
                continue
            if kind == "schematic":
                sa, sb = find_symbol(str(a)), find_symbol(str(b))
                if not sa or not sb:
                    errors.append(f"edit[{i}]: swap cannot find {a!r} and/or {b!r}")
                    continue
                pa = list(sa.get("pos") or [0, 0])
                pb = list(sb.get("pos") or [0, 0])
                aa = sa.get("angle", 0.0); ab = sb.get("angle", 0.0)
                ops.append({"op": "sch_edit_symbol", "id": sa.get("id"),
                            "pos": pb, "angle": ab})
                ops.append({"op": "sch_edit_symbol", "id": sb.get("id"),
                            "pos": pa, "angle": aa})
                planned.append({"edit": i, "op": "swap", "a": a, "b": b,
                                "ids": [sa.get("id"), sb.get("id")]})
            elif kind == "pcb":
                fa, fb = find_footprint(str(a)), find_footprint(str(b))
                if not fa or not fb:
                    errors.append(f"edit[{i}]: swap cannot find {a!r} and/or {b!r}")
                    continue
                pa = list(fa.get("pos") or [0, 0])
                pb = list(fb.get("pos") or [0, 0])
                aa = fa.get("angle", 0.0); ab = fb.get("angle", 0.0)
                ops.append({"op": "pcb_edit_footprint", "id": fa.get("id"),
                            "pos": pb, "angle": ab})
                ops.append({"op": "pcb_edit_footprint", "id": fb.get("id"),
                            "pos": pa, "angle": aa})
                planned.append({"edit": i, "op": "swap", "a": a, "b": b,
                                "ids": [fa.get("id"), fb.get("id")]})
            else:
                errors.append(f"edit[{i}]: swap not supported for kind={kind}")
            continue

        if eop in ("move", "move_to", "relocate"):
            token = raw.get("ref") or raw.get("id") or raw.get("target")
            pos = world_of(raw, "to_cell", "to")
            if pos is None and raw.get("to") is None:
                # allow cell key
                pos = world_of(raw, "cell", "pos")
            if not token:
                # move whatever is at from_cell
                cell = _parse_cell(raw.get("from_cell") or raw.get("cell"))
                if cell:
                    hits = _index_hits(index, cell[0], cell[1],
                                       ["symbol", "footprint", "entity"])
                    if hits:
                        token = hits[0].get("ref") or hits[0].get("id")
            if not token or pos is None:
                errors.append(f"edit[{i}]: move needs ref/id + to_cell/to")
                continue
            if kind == "schematic":
                s = find_symbol(str(token))
                if not s:
                    errors.append(f"edit[{i}]: no symbol {token!r}")
                    continue
                op = {"op": "sch_edit_symbol", "id": s.get("id"), "pos": pos}
                if "angle" in raw:
                    op["angle"] = float(raw["angle"])
                ops.append(op)
            elif kind == "pcb":
                fp = find_footprint(str(token))
                if not fp:
                    errors.append(f"edit[{i}]: no footprint {token!r}")
                    continue
                op = {"op": "pcb_edit_footprint", "id": fp.get("id"), "pos": pos}
                if "angle" in raw:
                    op["angle"] = float(raw["angle"])
                if "side" in raw:
                    op["side"] = raw["side"]
                ops.append(op)
            else:
                errors.append(f"edit[{i}]: move not supported for sketch entities "
                              "(delete+add, or sketch_edit_entity via add_features)")
                continue
            planned.append({"edit": i, "op": "move", "target": token, "pos": pos})
            continue

        if eop in ("delete", "remove", "del"):
            token = raw.get("ref") or raw.get("id") or raw.get("target")
            kinds = raw.get("kinds")
            cell = _parse_cell(raw.get("cell") or raw.get("at_cell"))
            targets: list[dict] = []
            if token:
                if kind == "schematic":
                    s = find_symbol(str(token))
                    if s:
                        targets.append({"kind": "symbol", "id": s.get("id")})
                    # also allow wire/label id
                    for coll, kname in (("wires", "wire"), ("labels", "label"),
                                        ("junctions", "junction")):
                        for obj in doc.get(coll) or []:
                            if obj.get("id") == token:
                                targets.append({"kind": kname, "id": token})
                elif kind == "pcb":
                    fp = find_footprint(str(token))
                    if fp:
                        targets.append({"kind": "footprint", "id": fp.get("id")})
                    for coll, kname in (("tracks", "track"), ("vias", "via")):
                        for obj in doc.get(coll) or []:
                            if obj.get("id") == token:
                                targets.append({"kind": kname, "id": token})
                else:
                    targets.append({"kind": "entity", "id": token})
            elif cell:
                targets = _index_hits(index, cell[0], cell[1], kinds)
            if not targets:
                errors.append(f"edit[{i}]: delete found nothing")
                continue
            for t in targets:
                hid = t.get("id") or t.get("footprint_id")
                if not hid:
                    continue
                hk = t.get("kind")
                if kind == "schematic":
                    if hk == "symbol":
                        ops.append({"op": "sch_delete_symbol", "id": hid})
                    elif hk == "wire":
                        ops.append({"op": "sch_delete_wire", "id": hid})
                    elif hk == "label":
                        ops.append({"op": "sch_delete_label", "id": hid})
                    elif hk == "junction":
                        ops.append({"op": "sch_delete_junction", "id": hid})
                elif kind == "pcb":
                    if hk in ("footprint", "pad"):
                        ops.append({"op": "pcb_delete_footprint",
                                    "id": t.get("footprint_id") or hid})
                    elif hk == "track":
                        ops.append({"op": "pcb_delete_track", "id": hid})
                    elif hk == "via":
                        ops.append({"op": "pcb_delete_via", "id": hid})
                else:
                    ops.append({
                        "op": "sketch_delete_entity",
                        "sketch": sketch_id or raw.get("sketch") or "",
                        "id": hid,
                    })
            planned.append({"edit": i, "op": "delete",
                            "targets": [t.get("id") for t in targets]})
            continue

        if eop in ("set_value", "set_ref", "edit_symbol", "edit_footprint"):
            token = raw.get("ref") or raw.get("id")
            if not token:
                errors.append(f"edit[{i}]: set_value needs ref/id")
                continue
            if kind == "schematic":
                s = find_symbol(str(token))
                if not s:
                    errors.append(f"edit[{i}]: no symbol {token!r}")
                    continue
                op = {"op": "sch_edit_symbol", "id": s.get("id")}
                for k in ("value", "ref", "footprint", "angle", "libid"):
                    if k in raw and k != "libid":
                        op[k] = raw[k]
                if "value" in raw:
                    op["value"] = raw["value"]
                ops.append(op)
            elif kind == "pcb":
                fp = find_footprint(str(token))
                if not fp:
                    errors.append(f"edit[{i}]: no footprint {token!r}")
                    continue
                op = {"op": "pcb_edit_footprint", "id": fp.get("id")}
                for k in ("value", "ref", "angle", "side", "pos"):
                    if k in raw:
                        op[k] = raw[k]
                ops.append(op)
            else:
                errors.append(f"edit[{i}]: set_value not for sketch")
                continue
            planned.append({"edit": i, "op": eop, "target": token})
            continue

        if eop in ("place_symbol", "add_symbol"):
            if kind != "schematic":
                errors.append(f"edit[{i}]: place_symbol only for schematic")
                continue
            pos = world_of(raw, "cell", "pos")
            libid = raw.get("libid")
            if not libid or pos is None:
                errors.append(f"edit[{i}]: place_symbol needs libid + cell/pos")
                continue
            op = {"op": "sch_add_symbol", "libid": libid, "pos": pos}
            for k in ("ref", "value", "angle", "footprint"):
                if raw.get(k) not in (None, ""):
                    op[k] = raw[k]
            ops.append(op)
            planned.append({"edit": i, "op": eop, "libid": libid, "pos": pos})
            continue

        if eop in ("place_label", "add_label"):
            if kind != "schematic":
                errors.append(f"edit[{i}]: place_label only for schematic")
                continue
            pos = world_of(raw, "cell", "pos")
            text = raw.get("text") or raw.get("label") or raw.get("net")
            if not text or pos is None:
                errors.append(f"edit[{i}]: place_label needs text + cell/pos")
                continue
            ops.append({"op": "sch_add_label", "text": str(text), "pos": pos,
                        "scope": raw.get("scope", "local")})
            planned.append({"edit": i, "op": eop, "text": text, "pos": pos})
            continue

        if eop in ("place_power", "add_power"):
            if kind != "schematic":
                errors.append(f"edit[{i}]: place_power only for schematic")
                continue
            pos = world_of(raw, "cell", "pos")
            net = raw.get("net") or raw.get("text")
            if not net or pos is None:
                errors.append(f"edit[{i}]: place_power needs net + cell/pos")
                continue
            ops.append({"op": "sch_add_power", "net": str(net), "pos": pos})
            planned.append({"edit": i, "op": eop, "net": net, "pos": pos})
            continue

        if eop in ("add_wire", "place_wire"):
            if kind != "schematic":
                errors.append(f"edit[{i}]: add_wire only for schematic")
                continue
            # support from_cell/to_cell or pts
            if raw.get("pts"):
                pts = raw["pts"]
            else:
                p1 = world_of({"to_cell": raw.get("from_cell"),
                               "to": raw.get("from")}, "to_cell", "to")
                p2 = world_of({"to_cell": raw.get("to_cell"),
                               "to": raw.get("to")}, "to_cell", "to")
                if p1 is None or p2 is None:
                    errors.append(f"edit[{i}]: add_wire needs from_cell+to_cell or pts")
                    continue
                pts = [p1, p2]
            ops.append({"op": "sch_add_wire", "pts": pts})
            planned.append({"edit": i, "op": eop, "pts": pts})
            continue

        if eop in ("add_track", "place_track"):
            if kind != "pcb":
                errors.append(f"edit[{i}]: add_track only for pcb")
                continue
            p1 = world_of({"to_cell": raw.get("from_cell"),
                           "to": raw.get("from") or raw.get("p1")}, "to_cell", "to")
            p2 = world_of({"to_cell": raw.get("to_cell"),
                           "to": raw.get("to") or raw.get("p2")}, "to_cell", "to")
            net = raw.get("net")
            if p1 is None or p2 is None or not net:
                errors.append(f"edit[{i}]: add_track needs net + from_cell + to_cell")
                continue
            op = {"op": "pcb_add_track", "net": net, "layer": raw.get("layer") or layer,
                  "p1": p1, "p2": p2, "kind": raw.get("kind", "segment")}
            if raw.get("width") is not None:
                op["width"] = float(raw["width"])
            ops.append(op)
            planned.append({"edit": i, "op": eop, "net": net, "p1": p1, "p2": p2})
            continue

        if eop in ("add_via", "place_via"):
            if kind != "pcb":
                errors.append(f"edit[{i}]: add_via only for pcb")
                continue
            pos = world_of(raw, "cell", "at") or world_of(raw, "cell", "pos")
            net = raw.get("net")
            if pos is None or not net:
                errors.append(f"edit[{i}]: add_via needs net + cell")
                continue
            ops.append({"op": "pcb_add_via", "net": net, "at": pos})
            planned.append({"edit": i, "op": eop, "net": net, "at": pos})
            continue

        if eop in ("sketch_add_line", "add_line"):
            if kind != "sketch":
                errors.append(f"edit[{i}]: sketch_add_line only for sketch")
                continue
            p1 = world_of({"to_cell": raw.get("from_cell"),
                           "to": raw.get("from") or raw.get("p1")}, "to_cell", "to")
            p2 = world_of({"to_cell": raw.get("to_cell"),
                           "to": raw.get("to") or raw.get("p2")}, "to_cell", "to")
            if p1 is None or p2 is None:
                errors.append(f"edit[{i}]: sketch_add_line needs from_cell+to_cell")
                continue
            sid = sketch_id or raw.get("sketch") or ""
            ops.append({
                "op": "sketch_add_entity",
                "sketch": sid,
                "entity": {"type": "line", "p1": p1, "p2": p2},
            })
            planned.append({"edit": i, "op": eop, "p1": p1, "p2": p2})
            continue

        if eop in ("sketch_delete", "delete_entity"):
            if kind != "sketch":
                errors.append(f"edit[{i}]: sketch_delete only for sketch")
                continue
            token = raw.get("id") or raw.get("entity")
            if not token:
                cell = _parse_cell(raw.get("cell"))
                if cell:
                    hits = _index_hits(index, cell[0], cell[1], ["entity"])
                    if hits:
                        token = hits[0].get("id")
            if not token:
                errors.append(f"edit[{i}]: sketch_delete needs id or cell")
                continue
            ops.append({
                "op": "sketch_delete_entity",
                "sketch": sketch_id or raw.get("sketch") or "",
                "id": token,
            })
            planned.append({"edit": i, "op": eop, "id": token})
            continue

        errors.append(f"edit[{i}]: unsupported op {eop!r} for kind={kind}")

    return {
        "ok": not errors,
        "kind": kind,
        "ops": ops,
        "planned": planned,
        "errors": errors,
        "n_ops": len(ops),
        "n_edits": len(list(edits or [])),
    }


def apply_ascii_edits(kind: str, edits: Sequence[dict], *,
                      grid: Optional[dict] = None,
                      index: Optional[dict] = None,
                      doc: Optional[dict] = None,
                      doc_id: str = "",
                      layer: str = "F.Cu",
                      sketch_id: str = "",
                      dry_run: bool = False,
                      apply_live: bool = True) -> dict:
    """Plan (+ optionally apply) cell-addressed edits against a live/offline doc.

    When ``apply_live`` and not ``dry_run``:
      - schematic/pcb → C++ ecad_apply_ops via the ecad_common session facade
      - sketch → services.mcp.cad_tools add_features path via core bridge

    Offline: pass ``doc`` + ``grid`` + ``index`` from a prior render; with
    ``dry_run=True`` only the op list is returned (no session required).
    """
    kind_n = (kind or "").lower().strip()
    if kind_n in ("sch", "sheet"):
        kind_n = "schematic"
    if kind_n in ("board", "pcb_layout"):
        kind_n = "pcb"

    # Auto-fill grid/index/doc from a render when missing and doc provided
    if doc is not None and (not grid or not index):
        if kind_n == "schematic":
            rep = schematic_ascii_report(doc)
            grid = grid or rep.get("grid")
            index = index or rep.get("index")
        elif kind_n == "pcb":
            rep = board_ascii_report(doc, layer=layer or None)
            # primary layer block
            layers = rep.get("layers") or []
            primary = layers[0] if layers else {}
            grid = grid or rep.get("grid") or primary.get("grid")
            index = index or rep.get("index") or primary.get("index")
        elif kind_n == "sketch":
            rep = sketch_ascii_report(doc)
            grid = grid or rep.get("grid")
            index = index or rep.get("index")

    # Live load if needed
    session = None
    live_doc = doc
    if live_doc is None and apply_live and not dry_run:
        try:
            if kind_n in ("schematic", "pcb"):
                from services.mcp.ecad_common import ecad_session  # noqa: PLC0415
                if doc_id:
                    session = ecad_session.get_session(doc_id)
                else:
                    from services.mcp.ecad_tools import _ECAD  # noqa: PLC0415
                    did = (_ECAD.get("current_sch") if kind_n == "schematic"
                           else _ECAD.get("current_pcb")) or _ECAD.get("current")
                    if not did:
                        return {"ok": False,
                                "error": f"no active {kind_n} doc — pass doc_id",
                                "ops": [], "planned": [], "errors": []}
                    session = ecad_session.get_session(did)
                live_doc = session.doc
                doc_id = session.doc_id
            # sketch live path fills later
        except Exception as e:
            return {"ok": False, "error": f"could not load live doc: {e}",
                    "ops": [], "planned": [], "errors": []}

    if live_doc is not None and (not grid or not index):
        # re-render for contract
        if kind_n == "schematic":
            rep = schematic_ascii_report(live_doc)
            grid = grid or rep.get("grid"); index = index or rep.get("index")
        elif kind_n == "pcb":
            rep = board_ascii_report(live_doc, layer=layer or None)
            layers = rep.get("layers") or []
            primary = layers[0] if layers else {}
            grid = grid or rep.get("grid") or primary.get("grid")
            index = index or rep.get("index") or primary.get("index")
        elif kind_n == "sketch":
            rep = sketch_ascii_report(live_doc)
            grid = grid or rep.get("grid"); index = index or rep.get("index")

    plan = plan_ascii_edits(kind_n, edits, grid=grid, index=index,
                            doc=live_doc or doc or {}, layer=layer,
                            sketch_id=sketch_id)
    if dry_run or not apply_live:
        plan["dry_run"] = True
        plan["applied"] = False
        return plan
    if not plan.get("ops"):
        plan["applied"] = False
        plan["ok"] = bool(plan.get("ok")) and not plan.get("errors")
        return plan

    # Apply
    try:
        if kind_n in ("schematic", "pcb"):
            from services.mcp.ecad_common import ecad_session  # noqa: PLC0415
            if session is None:
                session = ecad_session.get_session(doc_id)
            resp = ecad_session.apply_ops(session, session.revision, plan["ops"])
            plan["applied"] = True
            plan["revision"] = session.revision
            plan["doc_id"] = session.doc_id
            plan["apply_response"] = {
                k: resp.get(k) for k in ("ok", "warnings", "id_map", "revision")
                if isinstance(resp, dict) and k in resp
            }
            # attach readback
            if kind_n == "schematic":
                attach_schematic_readback(plan, session.doc)
            else:
                attach_board_readback(plan, session.doc, layer=layer or "")
            plan["ok"] = True
            return plan
        if kind_n == "sketch":
            # sketch ops go through add_features
            from services.mcp.cad_tools import _doc  # noqa: PLC0415
            import services.mcp.core_bridge as cb  # noqa: PLC0415
            d = _doc(doc_id or None)
            # ensure sketch field present
            ops2 = []
            for op in plan["ops"]:
                op = dict(op)
                if not op.get("sketch"):
                    op["sketch"] = sketch_id
                ops2.append(op)
            result = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops2})
            plan["applied"] = True
            plan["doc_id"] = d
            plan["apply_response"] = result if isinstance(result, dict) else {"raw": result}
            plan["ok"] = True
            if sketch_id:
                try:
                    data, health = _load_sketch(sketch_id, d)
                    attach_sketch_readback(plan, data, health=health, sketch_id=sketch_id)
                except Exception:
                    pass
            return plan
    except Exception as e:
        plan["applied"] = False
        plan["ok"] = False
        plan["error"] = f"apply failed: {e}"
        plan["errors"] = list(plan.get("errors") or []) + [str(e)]
        return plan

    plan["applied"] = False
    return plan



# ============================================================== 3D experimental


def _project_polylines_ascii(polylines: list[list[tuple[float, float]]],
                             pitch: float = 1.0, max_dim: int = 70,
                             markers: Optional[dict[int, str]] = None) -> dict:
    xmin, ymin, xmax, ymax = _sketch_bbox(polylines)
    bw = max(xmax - xmin, pitch); bh = max(ymax - ymin, pitch)
    w = max(1, int(math.ceil(bw / pitch)) + 1)
    h = max(1, int(math.ceil(bh / pitch)) + 1)
    if max(w, h) > max_dim:
        scale = max(w, h) / max_dim
        pitch = pitch * scale
        w = max(1, int(math.ceil(bw / pitch)) + 1)
        h = max(1, int(math.ceil(bh / pitch)) + 1)
    grid = [["." for _ in range(w)] for _ in range(h)]
    marks = markers or {}
    anchors: list[tuple[int, int]] = []
    painted: list[int] = []

    def cell(x: float, y: float) -> tuple[int, int]:
        return (_clamp_int((x - xmin) / pitch, 0, w - 1),
                _clamp_int((ymax - y) / pitch, 0, h - 1))

    for pi, pl in enumerate(polylines):
        mk = marks.get(pi)
        if not pl:
            anchors.append((0, 0))
            painted.append(0)
            continue
        acx, acy = cell(pl[0][0], pl[0][1])
        anchors.append((acx, acy))
        touched: set[tuple[int, int]] = set()
        if len(pl) < 2:
            grid[acy][acx] = mk or "+"
            touched.add((acx, acy))
        else:
            for i in range(len(pl) - 1):
                x0, y0 = cell(pl[i][0], pl[i][1])
                x1, y1 = cell(pl[i + 1][0], pl[i + 1][1])
                ch = _trace_char(x0, y0, x1, y1)
                for cx, cy in _bresenham(x0, y0, x1, y1):
                    touched.add((cx, cy))
                    if mk:
                        grid[cy][cx] = mk
                    elif grid[cy][cx] == ".":
                        grid[cy][cx] = ch
                    elif grid[cy][cx] != ch:
                        grid[cy][cx] = "+"
        painted.append(len(touched))
    for pi, mk in marks.items():
        if 0 <= pi < len(anchors):
            hl_stamp_anchor(grid, anchors[pi][0], anchors[pi][1], mk, empty=".")
    return {
        "ascii": "\n".join("".join(r) for r in grid),
        "pitch": pitch,
        "width": w,
        "height": h,
        "bbox": [xmin, ymin, xmax, ymax],
        "anchors": anchors,
        "painted": painted,
        "grid": make_grid_meta(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
                               pitch=pitch, width=w, height=h, unit="mm",
                               kind="silhouette3d"),
    }


def _view_basis(view: str) -> tuple[tuple[float, float, float],
                                    tuple[float, float, float],
                                    tuple[float, float, float]]:
    """Return (view_dir, screen_x, screen_y) unit-ish axes for named views."""
    v = (view or "front").lower()
    # screen_y is "up" on the page
    if v == "front":
        return (0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    if v == "back":
        return (0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    if v == "right":
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    if v == "left":
        return (-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)
    if v == "top":
        return (0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    if v == "bottom":
        return (0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)
    # iso
    d = (1.0, -1.0, 1.0)
    # approximate screen axes
    return d, (1.0, 1.0, 0.0), (-0.5, 0.5, 1.0)


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def project_points_2d(pts3: Iterable[tuple[float, float, float]],
                      view: str = "front") -> list[tuple[float, float]]:
    vd, sx, sy = _view_basis(view)
    sx = _norm(sx); sy = _norm(sy)
    out = []
    for p in pts3:
        out.append((_dot(p, sx), _dot(p, sy)))
    return out


def silhouette_from_hlr(view_json: dict) -> list[list[tuple[float, float]]]:
    """Extract visible polylines from a project_view / doc_drawing_views entry."""
    polys: list[list[tuple[float, float]]] = []
    for key in ("visible", "outline", "OutLineV", "VCompound"):
        raw = view_json.get(key)
        if not raw:
            continue
        for pl in raw:
            if not isinstance(pl, (list, tuple)) or len(pl) < 2:
                continue
            pts = []
            for p in pl:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((float(p[0]), float(p[1])))
            if len(pts) >= 2:
                polys.append(pts)
    return polys


def silhouette_from_bbox(bbox: dict | list | tuple, view: str = "front"
                         ) -> list[list[tuple[float, float]]]:
    """Wireframe of an axis-aligned bbox projected to *view* (fallback)."""
    if isinstance(bbox, dict):
        mn = bbox.get("min") or bbox.get("mins") or [0, 0, 0]
        mx = bbox.get("max") or bbox.get("maxs") or [1, 1, 1]
        if "xmin" in bbox:
            mn = [bbox["xmin"], bbox["ymin"], bbox["zmin"]]
            mx = [bbox["xmax"], bbox["ymax"], bbox["zmax"]]
    else:
        # [xmin,ymin,zmin,xmax,ymax,zmax] or two corners
        if len(bbox) == 6:
            mn, mx = bbox[:3], bbox[3:]
        else:
            mn, mx = [0, 0, 0], [1, 1, 1]
    x0, y0, z0 = map(float, mn)
    x1, y1, z1 = map(float, mx)
    corners = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    proj = project_points_2d(corners, view)
    return [[proj[i], proj[j]] for i, j in edges]


_SIL_LEGEND_BASE = (
    "  .   = empty (nothing projected into that cell)",
    "  -   = edge running east-west on the page   |   = running north-south",
    "  /   = edge running SW->NE                  \\   = running NW->SE",
    "  +   = two edges in one cell, a corner, or a degenerate single-point edge",
    "  NOTE: this is a wireframe silhouette — hidden-line removal is only as good",
    "        as the source, and nothing encodes depth. A far edge and a near edge",
    "        look identical.",
)


def _sil_box_marker(plan: HighlightPlan,
                    pl: Sequence[tuple[float, float]]) -> Optional[str]:
    """Match a ``box:x0,y0,x1,y1`` token against a projected polyline."""
    for tok in plan.tokens:
        q, v = _hl_parts(tok)
        if q != "box":
            continue
        try:
            x0, y0, x1, y1 = (float(t) for t in v.split(","))
        except Exception:
            continue
        lo_x, hi_x = min(x0, x1), max(x0, x1)
        lo_y, hi_y = min(y0, y1), max(y0, y1)
        if any(lo_x <= px <= hi_x and lo_y <= py <= hi_y for px, py in pl):
            return plan.markers[tok]
    return None


def render_ascii_3d(polylines: Optional[list] = None,
                    bbox: Any = None,
                    view: str = "front",
                    pitch: float = 1.0,
                    source: str = "experimental",
                    highlight: Any = None) -> dict:
    """Experimental single-view silhouette ASCII for non-vision models.

    ``highlight`` for this view is a **polyline handle** — "p3" (or the bare
    index "3"), or a projected-plane region "box:x0,y0,x1,y1" which marks every
    polyline with a point inside it. Feature ids/sids do NOT survive hidden-line
    projection, so the callout list enumerates the p-handles instead.
    """
    if polylines:
        pls = []
        for pl in polylines:
            if not pl:
                continue
            if len(pl[0]) >= 3:
                pls.append(project_points_2d([tuple(p[:3]) for p in pl], view))  # type: ignore
            else:
                pls.append([(float(p[0]), float(p[1])) for p in pl])
    elif bbox is not None:
        pls = silhouette_from_bbox(bbox, view)
        source = source or "bbox_wireframe"
    else:
        return {
            "ok": False,
            "error": "render_ascii requires polylines or bbox",
            "experimental": True,
        }

    hl = HighlightPlan(highlight)
    marks: dict[int, str] = {}
    for i, pl in enumerate(pls):
        mk = hl.marker([("poly", f"p{i}"), ("id", f"p{i}"), ("poly", str(i)),
                        ("name", f"p{i}")]) or _sil_box_marker(hl, pl)
        if mk:
            marks[i] = mk

    rendered = _project_polylines_ascii(pls, pitch=pitch, markers=marks)
    anchors = rendered["anchors"]
    for i, mk in sorted(marks.items()):
        pl = pls[i]
        length = sum(math.hypot(pl[k + 1][0] - pl[k][0], pl[k + 1][1] - pl[k][1])
                     for k in range(len(pl) - 1))
        hl.note(mk, f"p{i}",
                f"silhouette polyline {i}: {len(pl)} point(s) "
                f"({pl[0][0]:g},{pl[0][1]:g})->({pl[-1][0]:g},{pl[-1][1]:g}) "
                f"on the {view} projection plane, projected length {length:.4g}",
                anchor=anchors[i], world=pl[0], cells=rendered["painted"][i])

    gmeta = rendered["grid"]
    summary = (
        f"[EXPERIMENTAL] 3D silhouette ASCII (view={view}, source={source}). "
        f"Grid {rendered['width']}x{rendered['height']} at pitch={rendered['pitch']:.3g}. "
        f"{len(pls)} polyline(s). Lossy — for 'is there a hole?' class questions, "
        f"not dimensions. Prefer describe_part / multi-view render when available."
    )
    scale_lines = scale_origin_lines(
        gmeta,
        note=(f"PROJECTION: coordinates are on the {view} PROJECTION PLANE "
              f"(screen x/y after projecting the model), not model x/y/z; depth "
              f"along the view direction is discarded. Source={source}: "
              + ("outline of the model's axis-aligned bounding box only — the "
                 "real shape is NOT drawn."
                 if source == "bbox_wireframe" else
                 "hidden-line-removed visible edges.")))
    legend = ([f"LEGEND (3D silhouette, view={view}, one char = one cell):"]
              + list(_SIL_LEGEND_BASE) + highlight_legend_lines(hl, mode="plain"))
    inventory: list[dict] = []
    if not hl:
        for i, pl in enumerate(pls[:8]):
            inventory.append({
                "marker": _HL_NONE, "id": f"p{i}",
                "desc": f"polyline {i}: ({pl[0][0]:g},{pl[0][1]:g})->"
                        f"({pl[-1][0]:g},{pl[-1][1]:g}) — re-render with "
                        f"highlight='p{i}' to mark it",
                "anchor_cell": list(anchors[i]),
            })
        if len(pls) > 8:
            inventory.append({"marker": _HL_NONE, "id": f"p8..p{len(pls) - 1}",
                              "desc": f"+{len(pls) - 8} more polyline(s) not listed"})
    extra = unmatched_note(hl) + inventory
    callout_txt = callout_lines(hl, extra)
    ruled = annotate_ascii_with_rulers(rendered["ascii"])
    return {
        "ok": True,
        "kind": "silhouette3d",
        "experimental": True,
        "view": view,
        "source": source,
        "ascii": rendered["ascii"],
        "ascii_ruled": ruled,
        "ascii_annotated": annotated_ascii(
            header=f"[silhouette3d view={view} source={source} "
                   f"{rendered['width']}x{rendered['height']} cells]",
            scale_lines=scale_lines, drawing=ruled,
            legend=legend, callouts=callout_txt),
        "summary": summary_with_annotations(summary, scale_lines, legend,
                                            callout_txt),
        "findings": [],
        "scale": {"pitch": rendered["pitch"], "unit": "mm",
                  "lines": scale_lines},
        "legend": legend,
        "callouts": list(hl.callouts) + extra,
        "highlight": hl.report(),
        "polyline_count": len(pls),
        "grid": gmeta,
        "pitch": rendered["pitch"],
        "width": rendered["width"],
        "height": rendered["height"],
        "bbox": rendered["bbox"],
    }


# ================================================================= MCP register



def _load_schematic(doc_id: str = "") -> dict:
    from services.mcp.ecad_common import ecad_session  # noqa: PLC0415
    if doc_id:
        session = ecad_session.get_session(doc_id)
    else:
        try:
            from services.mcp.ecad_tools import _ECAD  # noqa: PLC0415
            did = _ECAD.get("current_sch") or _ECAD.get("current")
            if not did:
                raise ValueError("no current schematic — pass doc_id or call "
                                 "ecad_new_schematic first")
            session = ecad_session.get_session(did)
        except Exception as e:
            raise ValueError(f"no schematic session: {e}") from e
    if session.kind != "sch":
        raise ValueError(f"document {session.doc_id!r} is {session.kind}, not sch")
    return session.doc


def _load_board(doc_id: str = "") -> dict:
    from services.mcp.ecad_common import ecad_session  # noqa: PLC0415
    if doc_id:
        session = ecad_session.get_session(doc_id)
    else:
        # current pcb if any
        try:
            from services.mcp.ecad_tools import _ECAD  # noqa: PLC0415
            did = _ECAD.get("current_pcb") or _ECAD.get("current")
            if not did:
                raise ValueError("no current PCB — pass doc_id or call ecad_new_pcb first")
            session = ecad_session.get_session(did)
        except Exception as e:
            raise ValueError(f"no PCB session: {e}") from e
    if session.kind != "pcb":
        raise ValueError(f"document {session.doc_id!r} is {session.kind}, not pcb")
    return session.doc


def _load_sketch(sketch: str, doc_id: Optional[str]) -> tuple[dict, Optional[dict]]:
    """Return (sketch_dict with entities/constraints, optional health)."""
    from services.mcp.cad_tools import _doc  # noqa: PLC0415
    import services.mcp.core_bridge as cb  # noqa: PLC0415
    d = _doc(doc_id)
    overview = cb.call("doc_sketch_overview", {"doc_id": d, "sketch": sketch})
    tree = cb.call("doc_tree", {"doc_id": d}).get("tree") or {}
    features = tree.get("features") or []
    feature = next((row for row in features if row.get("id") == sketch), {})
    params = feature.get("params") or {}
    entities = list(overview.get("entities") or params.get("entities") or [])
    constraints = list(params.get("constraints") or overview.get("constraints") or [])
    # prefer solved entity coords already on overview entities
    health = None
    try:
        health = cb.call("sketch_health", {"doc_id": d, "sketch": sketch})
    except Exception:
        health = None
    return {"id": sketch, "entities": entities, "constraints": constraints,
            "feeds_feature": sketch_feeds_feature(features, sketch)}, health


def register_ascii2d_tools(mcp: FastMCP) -> None:
    """Register ASCII render + apply-edit tools for sketch/PCB/schematic/sim."""
    # Capture module-level callables before nested @mcp.tool defs shadow the names.
    _apply_ascii_edits_impl = globals()["apply_ascii_edits"]
    _render_sim_ascii_impl = globals()["render_sim_ascii"]

    @mcp.tool()
    def render_board_ascii(doc_id: str = "", layer: str = "",
                           pitch_mm: float = 1.0,
                           board_json: str = "",
                           highlight: str = "") -> str:
        """Per-layer ASCII grid of a PCB + net connectivity summary + checks.

        Renders components as refdes boxes, traces as path characters (- | / \\),
        vias as 'o', pads as '#', and marks same-cell different-net collisions as 'X'.
        Every response carries a LEGEND (what each glyph means), a SCALE/ORIGIN
        block (mm per cell + the cell->world formula) and a CALLOUT list.

        Call this (full render) before declaring a board done. Board mutation tools
        (place/route/zone/autoplace/autoroute/board_from_schematic) already append a
        compact auto-readback — READ that first after every mutation; use this tool for
        the uncapped / multi-layer review.

        Args:
          doc_id: open PCB session id (default: current PCB).
          layer: copper layer id (e.g. 'F.Cu'); empty = all copper layers.
          pitch_mm: world-mm per ASCII cell (auto-coarsened to keep width ≤ 80).
          board_json: optional full board document JSON string — when set, skips
            the live session (useful for offline / unit tests).
          highlight: element(s) to emphasise — refdes ('R1'), footprint id ('fp1'),
            net ('GND' or 'net:GND'), track/via id ('tA', 'v1'), pad ('R1.1'), or a
            comma-separated / JSON list ('R1,net:GND'). Each matched element is
            painted with its own marker letter (A, B, C…), its anchor cell is
            bracketed '>A<', and it is named in ``callouts``.

        Returns JSON: {ok, ascii, ascii_ruled, ascii_annotated, summary, findings,
        layers, scale, legend, callouts, highlight, grid, index}.
        ``ascii_annotated`` is the self-sufficient plate: scale + drawing + legend
        + callouts. Findings codes: unconnected_net, trace_crossing,
        courtyard_overlap.
        """
        try:
            if board_json:
                board = json.loads(board_json) if isinstance(board_json, str) else board_json
            else:
                board = _load_board(doc_id)
            report = board_ascii_report(
                board, layer=(layer or None), pitch_mm=float(pitch_mm) or 1.0,
                highlight=highlight)
            return _dumps(report)
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass board_json=... or open a PCB with ecad_new_pcb / "

                             "board_from_schematic first.")

    @mcp.tool()
    def render_schematic_ascii(doc_id: str = "", pitch_mm: float = 2.54,
                               sch_json: str = "",
                               highlight: str = "") -> str:
        """ASCII grid of an electrical schematic sheet + cell index + checks.

        Symbols render as refdes boxes, wires as path characters, power ports as
        'P'+net, labels as 'L'+text, junctions as '*'. Returns a stable ``grid``
        contract (origin/pitch/y-flip) and per-cell ``index`` so models can target
        edits via apply_ascii_edits(kind='schematic', ...). Every response carries
        a LEGEND, a SCALE/ORIGIN block and a CALLOUT list.

        Args:
          doc_id: open schematic session id (default: current sch).
          pitch_mm: world-mm per ASCII cell (default 2.54 = one pin grid).
          sch_json: optional full schematic document JSON — offline / unit tests.
          highlight: element(s) to emphasise — symbol ref ('R1') or id ('s1'),
            wire id ('w2'), junction id, label text ('MID'), power net
            ('GND' / 'net:GND'), or a comma-separated / JSON list. Matched
            elements are drawn with a marker letter, anchor bracketed '>A<', and
            named in ``callouts``.

        Returns JSON: {ok, ascii, ascii_ruled, ascii_annotated, summary, findings,
        scale, legend, callouts, highlight, grid, index}.
        """
        try:
            if sch_json:
                sch = json.loads(sch_json) if isinstance(sch_json, str) else sch_json
            else:
                sch = _load_schematic(doc_id)
            return _dumps(schematic_ascii_report(
                sch, pitch_mm=float(pitch_mm) or 2.54, highlight=highlight))
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass sch_json=... or open a schematic with "
                             "ecad_new_schematic first.")

    @mcp.tool()
    def render_sim_ascii(doc_id: str = "", spice_text: str = "",
                         circuit_json: str = "", netlist_json: str = "",
                         results_json: str = "",
                         highlight: str = "") -> str:
        """Deterministic plaintext of a SPICE circuit / deck / sim results.

        Topological (not a 2D grid): device roster, optional SPICE deck text,
        netlist components/nets, and probe results. Use after ecad_simulate or
        pass offline JSON. For spatial schematic edits use render_schematic_ascii.
        Every response carries a LEGEND (what each section and row means), a
        SCALE block (states that this view has no spatial scale and explains the
        4-character marker gutter) and a CALLOUT list.

        Args:
          doc_id: open schematic — when set, builds netlist + SPICE circuit live.
          spice_text: optional raw SPICE deck.
          circuit_json / netlist_json / results_json: offline payloads.
          highlight: row(s) to emphasise — device/instance name ('R1'), node
            ('mid' / 'node:mid'), netlist component ref, net name, probe key
            ('V(mid)'), or a comma-separated / JSON list. Marked rows get a
            '>A<' gutter and are named in ``callouts``.
        """
        try:
            circuit = json.loads(circuit_json) if circuit_json else None
            netlist = json.loads(netlist_json) if netlist_json else None
            results = json.loads(results_json) if results_json else None
            if doc_id or (not circuit and not spice_text and not netlist):
                try:
                    sch = _load_schematic(doc_id)
                    from services.mcp.ecad_common import _netlist_to_circuit  # noqa: PLC0415
                    from services.mcp.ecad_common import netlist_mod as _nl_mod  # noqa: PLC0415
                    netlist = netlist or _nl_mod.build_netlist(sch)
                    try:
                        circuit, _warns = _netlist_to_circuit(netlist)
                    except Exception:
                        pass
                except Exception:
                    if not (circuit or spice_text or netlist):
                        raise
            return _dumps(_render_sim_ascii_impl(circuit=circuit, spice_text=spice_text,
                                           netlist=netlist, results=results,
                                           highlight=highlight))
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass circuit_json/spice_text or open a schematic.")

    @mcp.tool()
    def apply_ascii_edits(kind: str, edits_json: str,
                          doc_id: str = "",
                          layer: str = "F.Cu",
                          sketch_id: str = "",
                          grid_json: str = "",
                          index_json: str = "",
                          dry_run: bool = False) -> str:
        """Apply cell-addressed / ref-targeted edits from an ASCII grid review.

        ``kind``: schematic | pcb | sketch.

        ``edits_json``: JSON list of edit objects, e.g.::

            [
              {"op": "move", "ref": "R1", "to_cell": [12, 4]},
              {"op": "swap", "a": "R1", "b": "R2"},
              {"op": "delete", "cell": [3, 5], "kinds": ["track"]},
              {"op": "replace_section", "cell_min": [0,0], "cell_max": [8,6],
               "clear": ["track", "via"],
               "place": [{"op": "add_track", "net": "A",
                          "from_cell": [0,0], "to_cell": [8,0]}]},
              {"op": "place_symbol", "libid": "Device:R", "cell": [10, 2],
               "ref": "R9", "value": "10k"},
              {"op": "set_value", "ref": "R1", "value": "4.7k"}
            ]

        Prefer passing ``grid_json`` + ``index_json`` from the last
        render_*_ascii response so cell coords resolve against the same raster.
        When omitted, the live doc is re-rendered to rebuild the contract.

        ``dry_run=true`` returns the planned engine ops without applying them.
        On apply, a compact ASCII readback is attached (schematic/pcb/sketch).
        """
        try:
            edits = json.loads(edits_json) if isinstance(edits_json, str) else edits_json
            if not isinstance(edits, list):
                return _err("edits_json must be a JSON list of edit objects")
            grid = json.loads(grid_json) if grid_json else None
            index = json.loads(index_json) if index_json else None
            report = _apply_ascii_edits_impl(
                kind, edits, doc_id=doc_id, layer=layer or "F.Cu",
                sketch_id=sketch_id, grid=grid, index=index,
                dry_run=bool(dry_run), apply_live=not bool(dry_run),
            )
            return _dumps(report)
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass kind=schematic|pcb|sketch and edits_json=[...]. "
                             "Include grid_json/index_json from the last render.")

    @mcp.tool()
    def render_sketch_ascii(sketch: str = "", doc_id: Optional[str] = None,
                            pitch: float = 0.5,
                            sketch_json: str = "",
                            near_tol: float = 0.01,
                            highlight: str = "") -> str:
        """ASCII/plaintext rendering of sketch entities + dimensions/constraints + checks.

        Lines/arcs/circles/splines are drawn as control-point polylines. Dimensions and
        constraints are narrated in English. Checks flag open profiles, near-coincident
        endpoints, and under-dimensioned entities (wraps sketch_health when a live
        doc/sketch id is available — does not re-solve). Every response carries a
        LEGEND (what each glyph means), a SCALE/ORIGIN block (units per cell + the
        cell->world formula) and a CALLOUT list.

        Call this (full render) before declaring a sketch done. Sketch mutation batches
        via add_features already append a compact auto-readback — READ that first after
        every edit; use this tool + sketch_health for the final verify loop.

        Args:
          sketch: sketch feature id (e.g. 's1') for a live part doc.
          doc_id: part document id (default: current part).
          pitch: sketch-units per ASCII cell.
          sketch_json: optional offline payload
            ``{"entities":[...], "constraints":[...], "health"?:{...}}``.
          near_tol: endpoint snap tolerance for open/near-coincident checks (mm).
          highlight: entity/entities to emphasise — entity id ('e3'), an endpoint
            handle from sketch_health ('e3.end'), an entity type ('circle' marks
            every circle), or a comma-separated / JSON list ('e1,e3'). Matched
            entities are drawn with a marker letter instead of their normal path
            glyph, anchor bracketed '>A<', and named in ``callouts``.

        Returns JSON: {ok, ascii, ascii_ruled, ascii_annotated, summary, findings,
        scale, legend, callouts, highlight, grid, index}. ``ascii_annotated`` is
        the self-sufficient plate: scale + drawing + legend + callouts.
        """
        try:
            health = None
            if sketch_json:
                data = json.loads(sketch_json) if isinstance(sketch_json, str) else sketch_json
                health = data.get("health")
            elif sketch:
                data, health = _load_sketch(sketch, doc_id)
            else:
                return _err("sketch id or sketch_json required",
                            hint="render_sketch_ascii(sketch='s1') or pass sketch_json")
            report = sketch_ascii_report(data, pitch=float(pitch) or 0.5,
                                         health=health, near_tol=float(near_tol),
                                         highlight=highlight)
            return _dumps(report)
        except Exception as e:  # noqa: BLE001
            return _err(str(e),
                        hint="Pass sketch_json for offline use, or a live sketch id.")

    @mcp.tool()
    def render_ascii(doc_id: Optional[str] = None, view: str = "front",
                     pitch: float = 1.0,
                     bbox_json: str = "",
                     polylines_json: str = "",
                     highlight: str = "") -> str:
        """[EXPERIMENTAL] Single-view silhouette ASCII of a 3D part for non-vision models.

        Prefers HLR visible edges from the drawing projector when the core is available;
        falls back to an axis-aligned bbox wireframe. Lossy — good for coarse shape
        questions, not dimensions. Prefer describe_part / render_part when vision or
        GSL text is available. Every response carries a LEGEND, a SCALE/ORIGIN block
        (projection-plane units per cell + the cell->world formula) and a CALLOUT
        list enumerating the addressable silhouette polylines.

        Args:
          doc_id: part document id (default: current part).
          view: front|back|left|right|top|bottom|iso.
          pitch: model-units per ASCII cell.
          bbox_json: optional offline bbox ``{"min":[x,y,z],"max":[x,y,z]}``.
          polylines_json: optional offline 2D/3D polylines JSON list.
          highlight: polyline handle(s) to emphasise — 'p3' (or the bare index
            '3'), a projected-plane region 'box:x0,y0,x1,y1', or a
            comma-separated / JSON list. Feature ids/sids do NOT survive
            hidden-line projection, so p-handles (listed in ``callouts``) are the
            addressable elements here; use inspect_feature / render_part with a
            feature name when you need feature-level emphasis in 3D.

        Returns JSON: {ok, experimental:true, ascii, ascii_ruled, ascii_annotated,
        summary, scale, legend, callouts, highlight, grid, view, source}.
        """
        try:
            if polylines_json:
                pls = json.loads(polylines_json) if isinstance(polylines_json, str) \
                    else polylines_json
                return _dumps(render_ascii_3d(polylines=pls, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="polylines",
                                              highlight=highlight))
            if bbox_json:
                bb = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
                return _dumps(render_ascii_3d(bbox=bb, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="bbox_wireframe",
                                              highlight=highlight))

            # live path: try HLR drawing views, then measure bbox
            from services.mcp.cad_tools import _doc  # noqa: PLC0415
            import services.mcp.core_bridge as cb  # noqa: PLC0415
            d = _doc(doc_id)
            source = "experimental"
            pls2: list = []
            try:
                # Prefer associative drawing projection if available
                tree = cb.call("doc_tree", {"doc_id": d}) or {}
                doc_body = tree.get("tree") or tree.get("doc") or tree
                dv = cb.call("doc_drawing_views", {
                    "doc": doc_body,
                    "views": [{"orientation": view, "view_id": "v0",
                               "scale": 1.0, "position": [0, 0],
                               "show_hidden": False}],
                })
                views = dv.get("views") or []
                if views:
                    pls2 = silhouette_from_hlr(views[0])
                    source = "hlr"
            except Exception:
                pls2 = []

            if not pls2:
                # bbox fallback via measure / tree
                bb = None
                try:
                    m = cb.call("doc_measure", {"doc_id": d, "of": "bbox"})
                    bb = m.get("bbox") or m.get("result") or m
                except Exception:
                    try:
                        # describe-style digest sometimes carries bbox
                        from services.mcp.gsl_tools import _digest  # noqa: PLC0415
                        dig = _digest(d)
                        bb = dig.get("bbox")
                    except Exception:
                        bb = None
                if bb is None:
                    return _err(
                        "could not obtain HLR edges or bbox for render_ascii",
                        hint="Pass bbox_json= or polylines_json= for offline use.",
                        experimental=True)
                return _dumps(render_ascii_3d(bbox=bb, view=view,
                                              pitch=float(pitch) or 1.0,
                                              source="bbox_wireframe",
                                              highlight=highlight))
            return _dumps(render_ascii_3d(polylines=pls2, view=view,
                                          pitch=float(pitch) or 1.0,
                                          source=source, highlight=highlight))
        except Exception as e:  # noqa: BLE001
            return _err(str(e), experimental=True,
                        hint="[EXPERIMENTAL] Pass bbox_json / polylines_json offline.")
