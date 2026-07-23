"""Self-contained folded-stacks -> SVG flame graph (no Perl / Node / external deps).

The Chudware sampling profiler (core/prof/sampler.cpp) emits Brendan-Gregg "folded" stacks:
each line is ``root_frame;next;...;leaf_frame <sample_count>``. This renders them as an
interactive-ish flame graph SVG (hover tooltips via <title>), so you can SEE where modeling
time goes without uploading anything. (You can also drop the .folded or the .speedscope.json
file straight into https://speedscope.app for a richer viewer.)

Usage:
  python tools/flamegraph.py prof.folded -o prof.svg
  python tools/flamegraph.py prof.folded            # writes prof.svg next to the input
  cat prof.folded | python tools/flamegraph.py - -o prof.svg

Importable:  render_svg(folded_text, title="...") -> str
"""
from __future__ import annotations

import html
import sys
import zlib


class _Node:
    __slots__ = ("name", "total", "children")

    def __init__(self, name: str):
        self.name = name
        self.total = 0
        self.children: dict[str, "_Node"] = {}


def _build_tree(folded_text: str):
    root = _Node("all")
    for raw in folded_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # split off the trailing " <count>"
        sp = line.rfind(" ")
        if sp < 0:
            continue
        stack_str, cnt_str = line[:sp], line[sp + 1:]
        try:
            cnt = int(cnt_str)
        except ValueError:
            continue
        if cnt <= 0:
            continue
        frames = [f for f in stack_str.split(";") if f]
        root.total += cnt
        cur = root
        for fr in frames:
            child = cur.children.get(fr)
            if child is None:
                child = _Node(fr)
                cur.children[fr] = child
            child.total += cnt
            cur = child
    return root


def _module_of(frame: str) -> str:
    i = frame.find("!")
    return frame[:i] if i >= 0 else "?"


# A small fixed palette for the modules that matter, so the same subsystem keeps its colour
# across runs. Everything else gets a stable hash-derived warm colour.
_MODULE_COLORS = {
    "libchudware": "#4e79a7",   # our dispatch / sketch solver / doc compile (cool = "us")
    "TKBO": "#e15759",       # booleans  (fuse/cut/common)  -> red
    "TKBRep": "#f28e2b",     # B-rep core
    "TKFillet": "#edc948",   # fillet / chamfer (blends)
    "TKMesh": "#76b7b2",     # tessellation
    "TKTopAlgo": "#ff9da7",
    "TKGeomAlgo": "#59a14f",
    "TKG3d": "#b07aa1",
    "TKGeomBase": "#9c755f",
    "TKMath": "#bab0ac",
}


def _color(frame: str) -> str:
    mod = _module_of(frame)
    if mod in _MODULE_COLORS:
        return _MODULE_COLORS[mod]
    # stable warm colour from a hash of the module name
    h = zlib.crc32(mod.encode("utf-8"))
    r = 205 + (h & 0x3F)            # 205..268 -> clamp
    g = 80 + ((h >> 6) & 0x7F)      # 80..207
    b = 55 + ((h >> 13) & 0x3F)     # 55..118
    return f"#{min(r,255):02x}{min(g,255):02x}{min(b,255):02x}"


ROW_H = 16
PLOT_W = 1180.0
MARGIN_X = 10
PAD_TOP = 54


def render_svg(folded_text: str, title: str = "Chudware modeling profile") -> str:
    root = _build_tree(folded_text)
    total = root.total
    if total <= 0:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="60">'
                '<text x="10" y="30" font-family="monospace" font-size="13">'
                'no samples</text></svg>')

    rects = []          # (x, depth, w, node)
    max_depth = [0]

    def layout(node: _Node, depth: int, x: float):
        if node is not root:
            w = node.total / total * PLOT_W
            rects.append((x, depth, w, node))
            if depth > max_depth[0]:
                max_depth[0] = depth
        cx = x
        # widest children first -> hottest paths on the left, easier to read
        for child in sorted(node.children.values(), key=lambda n: -n.total):
            layout(child, depth + 1, cx)
            cx += child.total / total * PLOT_W

    layout(root, 0, MARGIN_X)

    height = PAD_TOP + (max_depth[0] + 1) * ROW_H + 14
    width = int(PLOT_W + 2 * MARGIN_X)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{int(height)}" '
        f'font-family="Consolas, monospace" font-size="12">',
        f'<rect width="{width}" height="{int(height)}" fill="#fbfbf7"/>',
        f'<text x="{MARGIN_X}" y="22" font-size="15" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="{MARGIN_X}" y="40" fill="#555">{total} samples · '
        f'{len(rects)} frames · widest = hottest (left). Hover a box for details.</text>',
    ]
    plot_bottom = PAD_TOP + (max_depth[0] + 1) * ROW_H
    for (x, depth, w, node) in rects:
        if w < 0.4:
            continue
        y = plot_bottom - (depth + 1) * ROW_H
        pct = 100.0 * node.total / total
        fill = _color(node.name)
        tip = f"{node.name}\n{node.total} samples ({pct:.1f}%)"
        out.append(
            f'<g><title>{html.escape(tip)}</title>'
            f'<rect x="{x:.2f}" y="{y}" width="{max(w-0.5,0.4):.2f}" height="{ROW_H-1}" '
            f'fill="{fill}" stroke="#fbfbf7" stroke-width="0.5"/>')
        if w > 42:
            label = node.name
            # ~7px per char in this font; trim to fit
            maxc = int((w - 6) / 7)
            if len(label) > maxc:
                label = label[:max(maxc - 1, 1)] + "…"
            out.append(
                f'<text x="{x+3:.2f}" y="{y+ROW_H-4}" fill="#1a1a1a">{html.escape(label)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def main(argv):
    args = [a for a in argv[1:]]
    out_path = None
    if "-o" in args:
        i = args.index("-o")
        out_path = args[i + 1]
        del args[i:i + 2]
    src = args[0] if args else "-"
    if src == "-":
        folded = sys.stdin.read()
        if not out_path:
            out_path = "prof.svg"
    else:
        with open(src, "r", encoding="utf-8") as f:
            folded = f.read()
        if not out_path:
            out_path = src.rsplit(".", 1)[0] + ".svg"
    svg = render_svg(folded)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
