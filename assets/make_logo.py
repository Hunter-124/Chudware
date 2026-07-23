"""Generate the Chudware brand assets (logo tile, wordmark, window icon, doc icons) with Pillow.

Run with the runtime venv python:
    python assets/make_logo.py
Outputs PNGs into the same assets folder. Everything is drawn supersampled and
downsampled for clean anti-aliased edges.

The brand mark is a machined teal cube with a cubic corner notch boolean-cut out
of the top and a four-point spark seated in the notch — "the AI cut".
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
EDGE = (200, 255, 244)
EDGE_IN = (160, 240, 224)
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


def _iso_pts(cx, cy, r):
    """The 7 canonical isometric-cube silhouette points (2:1 projection)."""
    w, h = r, r * 0.5
    return {
        "top": (cx, cy - 2 * h), "rt": (cx + w, cy - h), "lt": (cx - w, cy - h),
        "c": (cx, cy), "rb": (cx + w, cy + h), "lb": (cx - w, cy + h),
        "bot": (cx, cy + 2 * h),
    }


def draw_mark(img, cx, cy, r, spark=True):
    """Draw the brand mark onto an RGBA canvas at supersampled coordinates.

    NOTE: the notch is carved by erasing to transparent, so anything already
    painted behind the notch silhouette is removed before the interior faces
    are drawn back in — draw the mark onto the final background layer.
    """
    s = max(img.size)
    p = _iso_pts(cx, cy, r)
    n = 0.5  # notch fraction of the cube edge
    q = _iso_pts(cx, cy - r + r * n, r * n)  # notch sub-cube seated at the top corner
    lw = max(2, int(r * 0.055))

    # outer faces
    _face_grad(img, [p["top"], p["rt"], p["c"], p["lt"]], TEAL_HI, TEAL)
    _face_grad(img, [p["c"], p["lt"], p["lb"], p["bot"]], TEAL_MID, TEAL_DK)
    _face_grad(img, [p["c"], p["rt"], p["rb"], p["bot"]], TEAL_DK, (10, 80, 72))

    # carve the notch, then draw its three interior faces (they tile the hexagon)
    ImageDraw.Draw(img).polygon(
        [q["top"], q["rt"], q["rb"], q["bot"], q["lb"], q["lt"]], fill=(0, 0, 0, 0))
    _face_grad(img, [q["lt"], q["top"], q["rt"], q["c"]], (20, 110, 100), (14, 84, 76))
    _face_grad(img, [q["lt"], q["c"], q["bot"], q["lb"]], (16, 90, 82), (10, 60, 55))
    _face_grad(img, [q["rt"], q["c"], q["bot"], q["rb"]], (12, 70, 64), (8, 50, 46))

    d = ImageDraw.Draw(img)
    for a, b in [(p["top"], p["lt"]), (p["lt"], p["c"]), (p["c"], p["rt"]), (p["rt"], p["top"]),
                 (p["lt"], p["lb"]), (p["rt"], p["rb"]), (p["lb"], p["bot"]),
                 (p["rb"], p["bot"]), (p["c"], p["bot"])]:
        d.line([a, b], fill=EDGE, width=lw)
    for a, b in [(q["lt"], q["c"]), (q["c"], q["rt"]), (q["c"], q["bot"]),
                 (q["lt"], q["lb"]), (q["rt"], q["rb"]), (q["lb"], q["bot"]), (q["rb"], q["bot"]),
                 (q["lt"], q["top"]), (q["rt"], q["top"])]:
        d.line([a, b], fill=EDGE_IN, width=max(1, lw * 2 // 3))

    if spark:
        sx, sy = q["c"][0], q["c"][1] - r * 0.02
        spark_r = r * 0.30
        pts = []
        for i in range(8):
            ang = i * math.pi / 4 - math.pi / 2
            rad = spark_r if i % 2 == 0 else spark_r * 0.32
            pts.append((sx + rad * math.cos(ang), sy + rad * math.sin(ang)))
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).polygon(pts, fill=(*SPARK_GLOW, 255))
        glow = glow.filter(ImageFilter.GaussianBlur(s * 0.02))
        img.alpha_composite(glow)
        ImageDraw.Draw(img).polygon(pts, fill=SPARK)


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


def make_tile(px=256, radius_frac=0.235, with_spark=True):
    s = px * SS
    tile = _tile_bg(s, radius_frac)
    draw_mark(tile, s * 0.5, s * 0.52, s * 0.315, spark=with_spark)
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
    draw_mark(img, pad + cube_r, s * 0.54, cube_r)
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
    s = px * SS
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
    draw_mark(img, s * 0.5, s * 0.655, s * 0.195)
    _ = rad  # rounded page corners skipped: fold + shadow already soften the shape
    return img.resize((px, px), Image.LANCZOS)


def make_ico(path, maker=make_tile):
    """Windows .ico (File Explorer / taskbar). Each size is rendered natively
    rather than downsampled from one bitmap so the small entries (16/24/32)
    keep crisp edges."""
    sizes = [256, 128, 64, 48, 32, 24, 16]
    imgs = [maker(s) for s in sizes]
    imgs[0].save(path, format="ICO", sizes=[(s, s) for s in sizes],
                 append_images=imgs[1:])


def main():
    make_tile(256).save(HERE / "logo_icon.png")
    make_tile(48).save(HERE / "logo_window.png")
    make_wordmark(200).save(HERE / "logo_wordmark.png")              # dark theme (white "-CAD")
    make_wordmark(200, cad_color=SLATE).save(HERE / "logo_wordmark_dark.png")  # light theme (slate "-CAD")
    make_ico(HERE / "app.ico")
    make_doc_tile(256).save(HERE / "doc_icon.png")
    make_ico(HERE / "doc.ico", maker=make_doc_tile)
    print("wrote logo_icon.png, logo_window.png, logo_wordmark.png, logo_wordmark_dark.png,"
          " app.ico, doc_icon.png, doc.ico to", HERE)


if __name__ == "__main__":
    main()
