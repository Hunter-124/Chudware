"""services.plm.visual_diff — the 2D half of the PLM graphical diff (PLM ext).

`services.plm.compare` answers *what* changed between two stored revisions in words;
this module answers it in *pixels*, for the document kinds whose geometry is already
2D and therefore needs no solid modeller: engineering drawings, ECAD boards, and any
pair of already-rendered PNGs. The 3D half lives in the engine (`doc_render_diff`).

Design, in the same spirit as the rest of `services/plm`:

* **Recipe in, image out.** The inputs are the *stored recipe* — the canonical
  ``document.json`` of an ECAD board, or a ``drawing_get`` payload reconstructed from
  one — never a solved/tessellated cache. Nothing here reads or writes the vault, and
  nothing here is a build artifact that belongs in it: the caller picks ``out_path``,
  which must live outside the shadow tree.
* **No engine dependency.** Pure Python. Pillow is *optional*: when it is missing
  every function returns ``None`` rather than raising, so a box without Pillow still
  gets the semantic half of a comparison.
* **Degrade, never raise.** Malformed, truncated or half-renamed payloads contribute
  no strokes instead of blowing up a comparison; an unexpected error returns ``None``.

How a vector diff is computed
-----------------------------

Both revisions are flattened into *strokes*. A stroke is one hashable key
``(kind, quantized_points, extra)`` — coordinates rounded to 3 decimals (drawings are
in sheet mm, so this discards a micron of sheet and nothing meaningful), ``extra``
carrying the non-geometric part of identity (a text string, a track's width+layer, a
footprint's ref). Each side becomes a :class:`collections.Counter` of keys, so a
stroke that existed twice and now exists once is still one removal. The diff is plain
multiset algebra: ``unchanged = a & b``, ``removed = a - b``, ``added = b - a``.

Because a stroke's *position* is part of its key, a moved entity reads as removed
(red, old place) + added (green, new place) rather than "changed" — which is exactly
the desired diff semantics for a drawing or a board: you see where it went.

Both revisions are drawn into one **shared frame**: the union bbox of *both* sides,
fitted to 0.85 of the canvas with a single equal-aspect scale (never stretched) and Y
flipped, so geometry that did not move lands on identical pixels regardless of which
revision is A. Unchanged is painted first, thin and grey; removed red and added green
go on top at width 3 so a change is never hidden under context.

Colours are the PLM diff colours, fixed across every slice (C++ and Python):
added ``#29B852``, removed ``#DB3D3D``, unchanged ``#9EA3AB``.
"""
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Any

# PLM diff colours — fixed contract, shared with the engine's doc_render_diff.
COLOR_ADDED = (41, 184, 82)      # #29B852 — present in B (newer) only
COLOR_REMOVED = (219, 61, 61)    # #DB3D3D — present in A (older) only
COLOR_UNCHANGED = (158, 163, 171)  # #9EA3AB — present in both
_COLOR_BG = (255, 255, 255)
_COLOR_LEGEND = (40, 40, 40)     # deliberately NOT the unchanged grey, so legend
                                 # pixels never masquerade as unchanged geometry.

_W_CHANGED = 3
_W_UNCHANGED = 1
_FIT = 0.85
_QUANT = 3                       # decimals kept in a stroke key (µm of sheet)

# Non-geometric stroke kinds: they carry no usable coordinates and are reported in
# the legend rather than the drawing area (so they never distort the shared frame).
_LEGEND_KINDS = ("title",)

# A pixel is "ink" when its luminance falls this far below white. 24/255 keeps
# antialiased strokes and light grey hatch while ignoring JPEG-ish paper noise.
_INK_THRESHOLD = 24


# ---------------------------------------------------------------------------
# Pillow (optional)
# ---------------------------------------------------------------------------

def _pil():
    """``(Image, ImageDraw, ImageChops)`` or ``None`` when Pillow is unavailable."""
    try:
        from PIL import Image, ImageChops, ImageDraw  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — absence of an optional dep is not an error
        return None
    return Image, ImageDraw, ImageChops


# ---------------------------------------------------------------------------
# stroke keys
# ---------------------------------------------------------------------------

def _q(v: Any) -> float:
    """Quantize one coordinate to the key grid (normalizing -0.0 to 0.0)."""
    return round(float(v), _QUANT) + 0.0


def _pts(raw: Any) -> tuple:
    """Quantized point tuple from an array of ``[x, y]`` pairs (tolerant of junk)."""
    out = []
    if not isinstance(raw, (list, tuple)):
        return ()
    for p in raw:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                out.append((_q(p[0]), _q(p[1])))
            except (TypeError, ValueError):
                continue
    return tuple(out)


def _stroke(kind: str, points, extra: str = "") -> tuple | None:
    """One stroke key, or ``None`` if it has too little geometry to draw."""
    pts = tuple(points)
    if kind in _LEGEND_KINDS:
        return (kind, (), extra)
    if kind == "text":
        return (kind, pts[:1], extra) if pts and extra else None
    return (kind, pts, extra) if len(pts) >= 2 else None


def _add(strokes: list, kind: str, points, extra: str = "") -> None:
    s = _stroke(kind, points, extra)
    if s is not None:
        strokes.append(s)


def _arc_pts(cx: float, cy: float, r: float, a0: float, a1: float) -> list:
    """Tessellate an arc exactly as ``drawing_render_png`` does (core/doc/session.cpp
    :6889) — ``n = max(10, |a1-a0|/0.3)`` — so an arc and a hand-built polyline of the
    same curve produce the same key instead of a spurious add+remove pair."""
    n = max(10, int(abs(a1 - a0) / 0.3))
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n),
             cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


# ---------------------------------------------------------------------------
# drawing flattening (mirrors core/doc/session.cpp drawing_render_png, :6841-6975)
# ---------------------------------------------------------------------------

def _pool(strokes: list, kind: str, raw: Any) -> None:
    """Flatten an array-of-polylines field (``visible`` / ``hidden`` / ``hatch`` /
    ``segments`` / ``dashed``) into strokes."""
    if isinstance(raw, (list, tuple)):
        for pl in raw:
            _add(strokes, kind, _pts(pl))


def _pool_draw(strokes: list, draw: Any) -> None:
    """The shared annotation ``draw`` primitives emitted per annotation by
    ``drawing_get``: segments, dashed, tris, arcs, texts."""
    if not isinstance(draw, dict):
        return
    _pool(strokes, "annot", draw.get("segments"))
    _pool(strokes, "annot_dashed", draw.get("dashed"))
    for tri in draw.get("tris") or []:
        pts = _pts(tri)
        if len(pts) == 3:
            _add(strokes, "annot_tri", pts + (pts[0],))
    for arc in draw.get("arcs") or []:
        if not isinstance(arc, dict):
            continue
        c = arc.get("center")
        if not (isinstance(c, (list, tuple)) and len(c) >= 2):
            continue
        try:
            r = float(arc.get("radius") or 0.0)
            if r <= 0:
                continue
            pts = _arc_pts(float(c[0]), float(c[1]), r,
                           float(arc.get("a0", 0.0)),
                           float(arc.get("a1", 2.0 * math.pi)))
        except (TypeError, ValueError):
            continue
        _add(strokes, "annot", _pts([list(p) for p in pts]))
    for t in draw.get("texts") or []:
        if not isinstance(t, dict):
            continue
        pos, text = t.get("pos"), str(t.get("text") or "")
        if text and isinstance(pos, (list, tuple)) and len(pos) >= 2:
            _add(strokes, "text", _pts([pos]), text)


def _drawing_strokes(get: Any) -> list:
    """Every stroke of a ``drawing_get`` payload, across all sheets and views."""
    strokes: list = []
    if not isinstance(get, dict):
        return strokes
    for sheet in get.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        for view in sheet.get("views") or []:
            if not isinstance(view, dict):
                continue
            _pool(strokes, "visible", view.get("visible"))
            _pool(strokes, "hidden", view.get("hidden"))
            _pool(strokes, "hatch", view.get("hatch"))
            for an in view.get("annotations") or []:
                if isinstance(an, dict):
                    _pool_draw(strokes, an.get("draw"))
            for sl in view.get("section_lines") or []:
                if not isinstance(sl, dict):
                    continue
                line = _pts(sl.get("line"))
                if len(line) < 2:
                    continue
                label = str(sl.get("label") or "A")
                _add(strokes, "section", line)
                _add(strokes, "text", line[:1], label)
                _add(strokes, "text", line[-1:], label)
        tb = sheet.get("title_block")
        if isinstance(tb, dict):
            for key in sorted(tb):
                val = tb[key]
                if isinstance(val, str) and val:
                    _add(strokes, "title", (), f"{key}={val}")
    return strokes


# ---------------------------------------------------------------------------
# ECAD flattening
#
# Field names are grounded in the code that already builds geometry from these
# documents, not guessed:
#   * board outline  — ``outline.shapes[]`` of ``{type:"line", p1, p2}`` /
#     ``{type:"arc", center, radius, a0, a1}`` (degrees) / ``{type:"circle",
#     center, radius}``  (core/geom/ecad_3d.cpp:100-127 outline_segments)
#   * tracks         — ``p1``, ``p2``, ``width``, ``layer``, ``kind``
#                      (services/ecad/drc.py:285-294 _track_shape)
#   * footprints     — ``pos``, ``angle``, ``side``, ``ref``, ``pads[]``
#                      (core/geom/ecad_3d.cpp:479-517)
#   * pads           — ``pos`` (footprint-local), ``size``, ``shape``, ``angle``
#                      placed by services/ecad/schema.py:186 pad_world
#   * vias           — ``at``, ``diameter``  (services/ecad/drc.py:297-302)
# Anything absent or renamed simply contributes no strokes.
# ---------------------------------------------------------------------------

def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _xy(v: Any) -> tuple[float, float] | None:
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        try:
            return float(v[0]), float(v[1])
        except (TypeError, ValueError):
            return None
    return None


def _circle_pts(cx: float, cy: float, r: float, n: int = 24) -> list:
    return [[cx + r * math.cos(2 * math.pi * i / n),
             cy + r * math.sin(2 * math.pi * i / n)] for i in range(n + 1)]


def _rect_pts(cx: float, cy: float, hx: float, hy: float, angle_deg: float) -> list:
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    corners = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy), (-hx, -hy))
    return [[cx + x * ca - y * sa, cy + x * sa + y * ca] for x, y in corners]


def _pad_world(fp: dict, pad: dict) -> tuple[float, float] | None:
    """Place a footprint-local pad position into board coordinates, reusing
    ``services.ecad.schema.pad_world`` when both placements are present."""
    if not (_xy(fp.get("pos")) and _xy(pad.get("pos"))):
        return None
    try:
        from services.ecad.schema import pad_world  # noqa: PLC0415
        return tuple(float(c) for c in pad_world(fp, pad))  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — fall back to a local placement
        px, py = _xy(pad["pos"])  # type: ignore[misc]
        ox, oy = _xy(fp["pos"])  # type: ignore[misc]
        if fp.get("side") == "bottom":
            px = -px
        a = math.radians(_num(fp.get("angle")))
        return ox + px * math.cos(a) - py * math.sin(a), oy + px * math.sin(a) + py * math.cos(a)


def _ecad_strokes(doc: Any) -> list:
    strokes: list = []
    if not isinstance(doc, dict):
        return strokes

    outline = doc.get("outline")
    for sh in (outline.get("shapes") if isinstance(outline, dict) else None) or []:
        if not isinstance(sh, dict):
            continue
        stype = str(sh.get("type") or "line")
        if stype == "line":
            p1, p2 = _xy(sh.get("p1")), _xy(sh.get("p2"))
            if p1 and p2:
                _add(strokes, "outline", _pts([list(p1), list(p2)]))
        elif stype in ("arc", "circle"):
            c, r = _xy(sh.get("center")), _num(sh.get("radius"))
            if not c or r <= 0:
                continue
            if stype == "circle":
                _add(strokes, "outline", _pts(_circle_pts(c[0], c[1], r, 48)))
            else:
                a0 = math.radians(_num(sh.get("a0"), 0.0))
                a1 = math.radians(_num(sh.get("a1"), 360.0))
                _add(strokes, "outline",
                     _pts([list(p) for p in _arc_pts(c[0], c[1], r, a0, a1)]))

    for t in doc.get("tracks") or []:
        if not isinstance(t, dict):
            continue
        extra = f"w{_num(t.get('width')):.3f}|{t.get('layer')}"
        pts = _pts(t.get("points")) if isinstance(t.get("points"), (list, tuple)) else ()
        if len(pts) < 2:
            p1, p2 = _xy(t.get("p1")), _xy(t.get("p2"))
            if not (p1 and p2):
                continue
            pts = _pts([list(p1), list(p2)])
        _add(strokes, "track", pts, extra)

    for v in doc.get("vias") or []:
        if not isinstance(v, dict):
            continue
        at, dia = _xy(v.get("at")), _num(v.get("diameter"))
        if at and dia > 0:
            _add(strokes, "via", _pts(_circle_pts(at[0], at[1], dia / 2.0, 12)))

    for fp in doc.get("footprints") or []:
        if not isinstance(fp, dict):
            continue
        origin = _xy(fp.get("pos"))
        ref = str(fp.get("ref") or fp.get("id") or "")
        if origin:
            # A labelled placement marker: a small diamond plus the ref, so a
            # footprint that moved or was renamed reads as red + green.
            m = 0.5
            _add(strokes, "fp", _pts([[origin[0] - m, origin[1]], [origin[0], origin[1] + m],
                                      [origin[0] + m, origin[1]], [origin[0], origin[1] - m],
                                      [origin[0] - m, origin[1]]]), ref)
            if ref:
                _add(strokes, "text", _pts([list(origin)]), ref)
        for pad in fp.get("pads") or []:
            if not isinstance(pad, dict):
                continue
            world = _pad_world(fp, pad)
            if not world:
                continue
            size = pad.get("size") or []
            w = _num(size[0]) if len(size) > 0 else 0.0
            h = _num(size[1]) if len(size) > 1 else w
            shape = str(pad.get("shape") or "rect")
            if shape in ("circle", "oval"):
                r = max(w, h) / 2.0
                if r <= 0:
                    continue
                _add(strokes, "pad", _pts(_circle_pts(world[0], world[1], r, 16)), shape)
            else:
                if w <= 0 and h <= 0:
                    continue
                angle = _num(pad.get("angle")) + _num(fp.get("angle"))
                _add(strokes, "pad",
                     _pts(_rect_pts(world[0], world[1], w / 2.0, h / 2.0, angle)), shape)
    return strokes


# ---------------------------------------------------------------------------
# shared-frame rendering
# ---------------------------------------------------------------------------

def _bbox(keys) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for kind, pts, _extra in keys:
        if kind in _LEGEND_KINDS:
            continue
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _frame(bbox, size: tuple[int, int]):
    """A ``(x, y) -> (px, py)`` mapper: equal-aspect, 0.85 fit, Y flipped, centred."""
    w, h = int(size[0]), int(size[1])
    x0, y0, x1, y1 = bbox
    dx, dy = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    scale = _FIT * min(w / dx, h / dy)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    def to_px(x: float, y: float) -> tuple[float, float]:
        return (w / 2.0 + (x - cx) * scale, h / 2.0 - (y - cy) * scale)

    return to_px


def _draw_keys(drawer, keys, to_px, color, width: int) -> None:
    for kind, pts, extra in keys:
        if kind in _LEGEND_KINDS or not pts:
            continue
        if kind == "text":
            px, py = to_px(*pts[0])
            drawer.text((px, py), extra, fill=color)
            continue
        drawer.line([to_px(x, y) for x, y in pts], fill=color, width=width)


def _render_diff(a_keys: list, b_keys: list, out_path: str,
                 size: tuple[int, int]) -> dict | None:
    """The shared body of the two vector diffs: multiset diff + shared-frame render."""
    pil = _pil()
    if pil is None:
        return None
    Image, ImageDraw, _ = pil

    a, b = Counter(a_keys), Counter(b_keys)
    if not a and not b:
        return None
    unchanged, removed, added = a & b, a - b, b - a

    bbox = _bbox(list(a) + list(b))
    img = Image.new("RGB", (int(size[0]), int(size[1])), _COLOR_BG)
    drawer = ImageDraw.Draw(img)
    if bbox is not None:
        to_px = _frame(bbox, size)
        # Context first, then the changes on top so nothing is hidden under grey.
        _draw_keys(drawer, unchanged.elements(), to_px, COLOR_UNCHANGED, _W_UNCHANGED)
        _draw_keys(drawer, removed.elements(), to_px, COLOR_REMOVED, _W_CHANGED)
        _draw_keys(drawer, added.elements(), to_px, COLOR_ADDED, _W_CHANGED)

    n_added, n_removed = sum(added.values()), sum(removed.values())
    drawer.text((6, 4), "DIFF  green=added  red=removed  grey=unchanged", fill=_COLOR_LEGEND)
    drawer.text((6, 16), f"+{n_added}  -{n_removed}", fill=_COLOR_LEGEND)
    y = 28
    for keys, color in ((removed, COLOR_REMOVED), (added, COLOR_ADDED)):
        for kind, _pts_, extra in keys.elements():
            if kind in _LEGEND_KINDS:
                drawer.text((6, y), extra[:80], fill=color)
                y += 12

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    img.save(out_path)
    return {"path": out_path, "added": n_added, "removed": n_removed,
            "unchanged": sum(unchanged.values())}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def drawing_diff_image(get_a: dict, get_b: dict, out_path: str,
                       size: tuple[int, int] = (900, 640)) -> dict | None:
    """Vector-level 2D diff of two ``drawing_get`` payloads rendered into ONE image.

    Every polyline of every view (visible, hidden, hatch, annotation segments/dashes/
    triangles/arcs, section-plane lines) plus every annotation text and title-block
    field becomes a stroke; the two stroke multisets are differenced and drawn in the
    shared frame. A changed dimension *value* therefore shows as one removed text and
    one added text, and a moved view as red old geometry beside green new geometry.

    Returns ``{"path", "added", "removed", "unchanged"}`` (stroke counts), or ``None``
    when Pillow is missing, both revisions are empty, or anything goes wrong.
    """
    try:
        return _render_diff(_drawing_strokes(get_a), _drawing_strokes(get_b),
                            out_path, size)
    except Exception:  # noqa: BLE001 — a diff picture is never worth failing a compare
        return None


def ecad_diff_image(doc_a: dict, doc_b: dict, out_path: str,
                    size: tuple[int, int] = (900, 640)) -> dict | None:
    """Board-layout diff (outline + tracks + vias + footprint placements + pads) of
    two canonical ECAD documents, rendered into ONE image.

    Position is part of a stroke's identity, so a *moved* track, pad or footprint
    renders as red (where it was) + green (where it is) rather than as an opaque
    "changed" entry — the point of a graphical diff. Width, layer, pad shape and
    footprint ref are in the key too, so a re-layered or re-named entity also shows.

    Returns ``{"path", "added", "removed", "unchanged"}`` or ``None`` (Pillow missing,
    nothing to draw, or an unexpected error).
    """
    try:
        return _render_diff(_ecad_strokes(doc_a), _ecad_strokes(doc_b), out_path, size)
    except Exception:  # noqa: BLE001
        return None


def pixel_diff_image(png_a: str, png_b: str, out_path: str) -> dict | None:
    """Registered pixel diff of two renders: ink only in A -> removed red, only in
    B -> added green, ink in both -> unchanged grey, paper -> white. This REPLACES
    the old 50%-blend overlay, which showed *where there was drawing* rather than
    *what changed*.

    Registration is preserved: mismatched sizes are **padded** onto a common
    max-width/max-height white canvas anchored top-left, never resized — resizing
    shifts every pixel and would report the whole image as changed. "Ink" is any
    pixel whose luminance is more than ``_INK_THRESHOLD`` (24/255) below white, which
    keeps antialiased edges and light hatch while ignoring paper noise.

    Returns ``{"path", "changed_px", "total_px", "changed_pct"}`` where changed =
    A-only + B-only pixels, or ``None`` if Pillow is missing or either file is
    unreadable. Pure Pillow (no numpy): the operations below are single C-level
    passes, ample for render-sized images.
    """
    pil = _pil()
    if pil is None:
        return None
    Image, _, ImageChops = pil
    try:
        with Image.open(png_a) as fa, Image.open(png_b) as fb:
            ia_src, ib_src = fa.convert("L"), fb.convert("L")
        w = max(ia_src.width, ib_src.width)
        h = max(ia_src.height, ib_src.height)

        def ink(src):
            canvas = Image.new("L", (w, h), 255)
            canvas.paste(src, (0, 0))
            # 255 = ink, 0 = paper.
            return canvas.point(lambda v: 255 if v < 255 - _INK_THRESHOLD else 0)

        ia, ib = ink(ia_src), ink(ib_src)
        both = ImageChops.darker(ia, ib)
        only_a = ImageChops.subtract(ia, ib)
        only_b = ImageChops.subtract(ib, ia)

        out = Image.new("RGB", (w, h), _COLOR_BG)
        for mask, color in ((both, COLOR_UNCHANGED), (only_a, COLOR_REMOVED),
                            (only_b, COLOR_ADDED)):
            out.paste(Image.new("RGB", (w, h), color), (0, 0), mask.convert("1"))

        changed = only_a.histogram()[255] + only_b.histogram()[255]
        total = w * h
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        out.save(out_path)
        return {"path": out_path, "changed_px": changed, "total_px": total,
                "changed_pct": round(100.0 * changed / total, 4) if total else 0.0}
    except Exception:  # noqa: BLE001
        return None
