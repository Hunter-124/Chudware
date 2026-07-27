"""Generate the Chudware brand assets (logo tile, wordmark, window icon, doc icons) with Pillow.

Run with the runtime venv python:
    python assets/make_logo.py
Outputs PNGs into the same assets folder. Everything is drawn supersampled and
downsampled for clean anti-aliased edges.

The brand mark is a 3D projection of a tesseract (4-cube) — a still of the SAME figure that
spins live in the app's copilot header, with a four-point spark seated in its inner cell.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT_BOLD = HERE / "fonts" / "Rubik-Bold.ttf"
SS = 4  # supersample factor

# palette
TILE_TOP = (30, 34, 44)
TILE_BOT = (14, 16, 22)
GLOW = (10, 34, 32)
TEAL_HI = (66, 236, 206)
TEAL = (48, 216, 190)
TEAL_MID = (34, 190, 170)
TEAL_DK = (16, 128, 115)
SPARK = (235, 255, 250)
SPARK_GLOW = (190, 255, 242)
ACCENT = (45, 212, 191)
WHITE = (236, 241, 245)
# "-CAD" text color for the light-theme wordmark (near-black slate, matches palette.text).
SLATE = (30, 35, 50)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lerp2(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _vgrad(size, top, bot):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        c = _lerp(top, bot, y / max(1, h - 1))
        for x in range(w):
            px[x, y] = c
    return img


def _face_grad(img, poly, c1, c2):
    """Composite a polygon filled with a vertical c1->c2 gradient onto img."""
    grad = _vgrad(img.size, c1, c2).convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(grad, (0, 0), mask)
    img.alpha_composite(layer)


# ---- brand mark: a tesseract (4-cube) projected 4D -> 3D -> 2D -------------------------------
# The pose mirrors host/src/ui/tesseract.cpp, TesseractAnim::EightCell frozen at theta = 0: the
# icon in a user's taskbar is a still of the mark that turns inside the product. The camera and
# the 4D eye multiplier below MUST match kCamYaw / kCamPitch / the Pass A factor in tesseract.cpp
# or the icon stops being the same object as the logo. The fit constants are DERIVED here the
# same way the C++ derives them rather than pasted in as magic numbers.

_CAM_YAW, _CAM_PITCH = 0.35, 0.22
_D4_FACTOR = 1.63

# 16 vertices (+-1, +-1, +-1, +-1); 32 edges = the pairs differing in exactly one coordinate.
_VERTS = [(1.0 if i & 1 else -1.0, 1.0 if i & 2 else -1.0,
           1.0 if i & 4 else -1.0, 1.0 if i & 8 else -1.0) for i in range(16)]
_EDGES = [(i, i ^ b) for i in range(16) for b in (1, 2, 4, 8) if i ^ b > i]


# Simple rotation in the zw plane: the fourth axis folds through the viewing direction, so the
# inner cell swells toward the camera, swallows the outer one and the figure turns itself inside
# out. Mirrors the EightCell row of animDef() in tesseract.cpp (rotatePlane(p, 2, 3, theta)).
def _rotate4(p, theta):
    c, s = math.cos(theta), math.sin(theta)
    return (p[0], p[1], p[2] * c - p[3] * s, p[2] * s + p[3] * c)


def _camera(p):
    """Fixed 3/4 view: yaw about +y, then pitch about +x. +z points INTO the screen."""
    cy, sy = math.cos(_CAM_YAW), math.sin(_CAM_YAW)
    cp, sp = math.cos(_CAM_PITCH), math.sin(_CAM_PITCH)
    x1 = p[0] * cy + p[2] * sy
    z1 = -p[0] * sy + p[2] * cy
    return (x1, p[1] * cp - z1 * sp, p[1] * sp + z1 * cp)


def _fit(samples=96):
    """The two eye distances, the depth normalizer and the scale that makes the widest frame of
    a full turn exactly fill the unit disc. Mirrors fitStyle() in ui/tesseract.cpp."""
    turn = [_rotate4(v, s / samples * 2 * math.pi)
            for s in range(samples) for v in _VERTS]
    d4 = max(abs(p[3]) for p in turn) * _D4_FACTOR
    cams = []
    for p in turn:
        k = d4 / (d4 - p[3])
        cams.append(_camera((p[0] * k, p[1] * k, p[2] * k)))
    d3 = max(math.hypot(math.hypot(c[0], c[1]), c[2]) for c in cams) * 3.0
    depth = max(abs(c[2]) for c in cams)
    flat = max(math.hypot(c[0] * d3 / (d3 + c[2]), c[1] * d3 / (d3 + c[2])) for c in cams)
    return d4, d3, depth, 1.0 / flat


_FIT = _fit()


def draw_mark(img, cx, cy, r, spark=True, on_light=False, ss=SS):
    """Draw the brand mark onto an RGBA canvas at supersampled coordinates: a tesseract centred
    on (cx, cy) and inscribed in radius r, painted back-to-front so the near cell reads in front.

    `ss` is the caller's supersample factor, so the mark's FINAL radius is r / ss. Below ~11
    final px the 32 edges fuse into a blob, so the small tiers drop the 8 edges along the 4th
    axis (leaving the cube-in-cube silhouette) and paint what remains heavier. make_ico renders
    every .ico entry natively, so 16/24/32 each get the treatment that reads at its size.
    """
    d4, d3, depth, scale = _FIT
    final_r = r / ss
    small = final_r <= 11.0
    near, far = (TEAL_MID, (10, 78, 70)) if on_light else (TEAL_HI, TEAL_DK)
    # The small tiers used to halve d4 to force the two cells apart, back when the shared eye
    # distance was a slack 2.4x and they sat ~1 px apart at 16 px. _D4_FACTOR is 1.63 now, which
    # already separates them; halving on top of that drives the near cell's divide to 0.41 and
    # blows it out of frame as a lopsided blob. The reduced edge set alone carries these sizes.
    fill_r = r * (1.10 if small else 1.0)

    pts, dep = [], []
    for v in _VERTS:
        k = d4 / (d4 - v[3])
        c = _camera((v[0] * k, v[1] * k, v[2] * k))
        k2 = d3 / (d3 + c[2]) * scale
        pts.append((c[0] * k2, c[1] * k2))
        dep.append(c[2])
    # Fit this single frame to the box. The C++ renderer instead reserves room for the widest
    # frame of a full turn (so a spinning mark never breathes); a still has no such constraint
    # and should use all of its box.
    span = max(math.hypot(x, y) for x, y in pts) or 1.0
    pts = [(cx + x / span * fill_r, cy - y / span * fill_r)   # canvas y is down, model is y-up
           for x, y in pts]

    idx = [k for k, (a, b) in enumerate(_EDGES) if not (small and a ^ b == 8)]
    idx.sort(key=lambda k: -(dep[_EDGES[k][0]] + dep[_EDGES[k][1]]))
    base_lw = fill_r * (0.110 if small else 0.115)
    ramp0, ramp1 = (0.80, 0.35) if small else (0.72, 0.50)
    d = ImageDraw.Draw(img)
    for k in idx:
        a, b = _EDGES[k]
        n01 = 0.5 - (dep[a] + dep[b]) * 0.5 / (2.0 * depth)   # 1 = nearest, 0 = farthest
        d.line([pts[a], pts[b]], fill=_lerp(far, near, n01),
               width=max(1, round(base_lw * (ramp0 + ramp1 * n01))))

    # Vertex dots, far to near over the edges — mirrors the dot pass in tesseract.cpp's paint():
    # they cap the stroke ends at every joint and anchor the eye. The C++ radius is
    # baseW*(0.95 + 0.55*n01) with baseW = 0.06 of the box radius; expressed off fill_r so the
    # still keeps the live mark's proportions. The colour ramp is biased a touch nearer than the
    # edges', echoing the dots' higher alpha floor in the C++.
    for i in sorted(range(16), key=lambda i: -dep[i]):
        n01 = 0.5 - dep[i] / (2.0 * depth)
        dot_r = max(1.0, fill_r * 0.06 * (0.95 + 0.55 * n01))
        x, y = pts[i]
        d.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r],
                  fill=_lerp(far, near, 0.2 + 0.8 * n01))

    # The spark seats in the inner cell. It is the one element that cannot survive the small
    # tiers: at 16 px it fills the cell it is meant to sit inside, so it drops out entirely.
    if spark and final_r >= 7.0:
        spark_r = fill_r * (0.23 if small else 0.30)
        pts_s = []
        for i in range(8):
            ang = i * math.pi / 4 - math.pi / 2
            rad = spark_r if i % 2 == 0 else spark_r * 0.32
            pts_s.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        if not small:
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).polygon(pts_s, fill=(*SPARK_GLOW, 255))
            img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(img.size) * 0.02)))
        ImageDraw.Draw(img).polygon(pts_s, fill=SPARK)


def _tile_bg(s, radius_frac):
    grad = _vgrad((s, s), TILE_TOP, TILE_BOT)
    g = Image.new("RGB", (s, s), (0, 0, 0))
    r = s * 0.42
    ImageDraw.Draw(g).ellipse([s * 0.5 - r, s * 0.5 - r, s * 0.5 + r, s * 0.5 + r], fill=GLOW)
    grad = ImageChops.add(grad, g.filter(ImageFilter.GaussianBlur(s * 0.12)))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * radius_frac), fill=255)
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    return tile


def _ss_for(px):
    """Supersample factor for a native render at `px`. make_ico renders 16/24/32 natively, and
    at the flat SS=4 those tiles were drawn on a 64-128 px canvas — coarse enough that the
    wireframe's sub-pixel strokes quantised into mush. Aim every tier at a >=512 px canvas."""
    return max(SS, -(-512 // px))


def make_tile(px=256, radius_frac=0.235, with_spark=True):
    ss = _ss_for(px)
    s = px * ss
    tile = _tile_bg(s, radius_frac)
    # Centred on the canvas: the old isometric cube hung low because its silhouette was taller
    # than wide; the tesseract projection is symmetric about its own centroid.
    draw_mark(tile, s * 0.5, s * 0.5, s * 0.335, spark=with_spark, ss=ss)
    return tile.resize((px, px), Image.LANCZOS)


def make_wordmark(height=200, cad_color=WHITE):
    """Mark + "Chudware" wordmark. The cube carries the accent; the type stays
    quiet (white on dark theme, slate on light)."""
    s = height * SS
    cube_r = s * 0.32
    pad = s * 0.16
    font = ImageFont.truetype(str(FONT_BOLD), int(s * 0.46))
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    word = "Chudware"
    w_word = tmp.textlength(word, font=font)
    cube_w = cube_r * 2
    total_w = int(pad + cube_w + pad * 0.9 + w_word + pad)
    img = Image.new("RGBA", (total_w, s), (0, 0, 0, 0))
    draw_mark(img, pad + cube_r, s * 0.5, cube_r)
    d = ImageDraw.Draw(img)
    tx = pad + cube_w + pad * 0.9
    ty = s * 0.5 - s * 0.30
    d.text((tx, ty), word, font=font, fill=cad_color)
    target_h = height
    target_w = int(total_w * target_h / s)
    return img.resize((target_w, target_h), Image.LANCZOS)


def make_doc_tile(px=256):
    """Document/file-type icon: a light page with a folded corner carrying the
    brand mark. Works on light and dark shell backgrounds (Explorer, Dolphin…)."""
    ss = _ss_for(px)
    s = px * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # page silhouette with folded top-right corner
    m = s * 0.14                     # side margin
    top, bot = s * 0.04, s * 0.96
    fold = s * 0.22                  # fold size
    x0, x1 = m, s - m
    page = [(x0, top), (x1 - fold, top), (x1, top + fold), (x1, bot), (x0, bot)]
    rad = int(s * 0.035)
    # soft drop shadow so the page reads on white backgrounds too
    sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x + s * 0.012, y + s * 0.018) for x, y in page], fill=(10, 12, 16, 110))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(s * 0.015)))
    _face_grad(img, page, (245, 248, 251), (222, 228, 235))
    d = ImageDraw.Draw(img)
    d.line(page + [page[0]], fill=(150, 160, 172), width=max(2, int(s * 0.008)), joint="curve")
    # fold flap
    flap = [(x1 - fold, top), (x1 - fold, top + fold), (x1, top + fold)]
    d.polygon(flap, fill=(196, 205, 214))
    d.line(flap + [flap[0]], fill=(150, 160, 172), width=max(2, int(s * 0.008)))
    # faint "sketch lines" above the mark so it reads as a CAD document
    lw = max(2, int(s * 0.010))
    for i, frac in enumerate((0.30, 0.40)):
        d.line([(x0 + s * 0.10, s * frac), (x1 - s * 0.10 - (i * s * 0.12), s * frac)],
               fill=(178, 188, 199), width=lw)
    # brand mark, lower center
    # on_light: a pale teal wireframe would vanish on the white page — darken the ramp.
    draw_mark(img, s * 0.5, s * 0.645, s * 0.205, on_light=True, ss=ss)
    _ = rad  # rounded page corners skipped: fold + shadow already soften the shape
    return img.resize((px, px), Image.LANCZOS)


# The sizes every OS surface asks for. Windows .ico carries them internally; Linux's hicolor
# theme and GLFW want them as separate files.
ICON_SIZES = [256, 128, 64, 48, 32, 24, 16]


def make_ico(path, maker=make_tile):
    """Windows .ico (File Explorer / taskbar / Task Manager). Each size is rendered natively
    rather than downsampled from one bitmap so the small entries (16/24/32) keep crisp edges."""
    imgs = [maker(s) for s in ICON_SIZES]
    imgs[0].save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES],
                 append_images=imgs[1:])


def make_icon_set(out_dir):
    """Natively-rendered app icon at every size, one PNG per size.

    The wireframe mark is far more sensitive to downsampling than the solid cube it replaced:
    squeezing the 256 master down to 16 px smears the 32 edges into a patch, while rendering
    16 px directly picks the reduced small-size treatment in draw_mark and stays legible.
    Windows already got this for free (make_ico renders per size); this gives the same to the
    consumers that were resampling one PNG — the GLFW window icon and the Linux hicolor theme.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for px in ICON_SIZES:
        make_tile(px).save(out_dir / f"{px}.png")
    return out_dir


def main():
    make_tile(256).save(HERE / "logo_icon.png")
    make_icon_set(HERE / "appicon")
    make_wordmark(200).save(HERE / "logo_wordmark.png")              # dark theme (white text)
    make_wordmark(200, cad_color=SLATE).save(HERE / "logo_wordmark_dark.png")  # light theme
    make_ico(HERE / "app.ico")
    make_doc_tile(256).save(HERE / "doc_icon.png")
    make_ico(HERE / "doc.ico", maker=make_doc_tile)
    print("wrote logo_icon.png, appicon/{%s}.png, logo_wordmark.png, logo_wordmark_dark.png,"
          " app.ico, doc_icon.png, doc.ico to %s"
          % (",".join(str(s) for s in ICON_SIZES), HERE))


if __name__ == "__main__":
    main()
