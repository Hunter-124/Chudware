"""Generate the AI-CAD brand assets (logo tile, wordmark, window icon) with Pillow.

Run with the runtime venv python:
    python host/assets/make_logo.py
Outputs PNGs into the same assets folder. Everything is drawn supersampled and
downsampled for clean anti-aliased edges.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT_BOLD = HERE / "fonts" / "Rubik-Bold.ttf"
SS = 4  # supersample factor

# palette
TILE_TOP = (32, 36, 44)
TILE_BOT = (18, 20, 26)
CUBE_TOP = (52, 225, 196)
CUBE_LEFT = (31, 184, 166)
CUBE_RIGHT = (17, 124, 112)
EDGE = (180, 255, 240)
SPARK = (182, 255, 239)
ACCENT = (45, 212, 191)
WHITE = (236, 241, 245)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vgrad(size, top, bot):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        c = _lerp(top, bot, y / max(1, h - 1))
        for x in range(w):
            px[x, y] = c
    return img


def _iso_cube(draw: ImageDraw.ImageDraw, cx, cy, r, edge=True):
    w = r
    h = r * 0.5
    top = (cx, cy - 2 * h)
    r_top = (cx + w, cy - h)
    l_top = (cx - w, cy - h)
    center = (cx, cy)
    r_bot = (cx + w, cy + h)
    l_bot = (cx - w, cy + h)
    bottom = (cx, cy + 2 * h)
    draw.polygon([top, r_top, center, l_top], fill=CUBE_TOP)        # top face
    draw.polygon([center, r_top, r_bot, bottom], fill=CUBE_RIGHT)   # right face
    draw.polygon([center, l_top, l_bot, bottom], fill=CUBE_LEFT)    # left face
    if edge:
        lw = max(1, int(r * 0.045))
        for a, b in [(top, r_top), (top, l_top), (center, top),
                     (center, r_top), (center, l_top), (center, bottom),
                     (r_top, r_bot), (l_top, l_bot), (r_bot, bottom), (l_bot, bottom)]:
            draw.line([a, b], fill=EDGE, width=lw)


def make_tile(px=256, radius_frac=0.235, with_spark=True):
    s = px * SS
    grad = _vgrad((s, s), TILE_TOP, TILE_BOT)
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * radius_frac), fill=255)
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(tile)
    _iso_cube(d, s * 0.5, s * 0.53, s * 0.27)
    if with_spark:
        sx, sy = s * 0.5, s * 0.53 - s * 0.27 * 2
        for rr, col in [(s * 0.055, (*SPARK, 60)), (s * 0.03, (*SPARK, 130)), (s * 0.016, (*SPARK, 255))]:
            d.ellipse([sx - rr, sy - rr, sx + rr, sy + rr], fill=col)
    return tile.resize((px, px), Image.LANCZOS)


# "-CAD" text color for the light-theme wordmark (near-black slate, matches palette.text).
SLATE = (30, 35, 50)


def make_wordmark(height=200, cad_color=WHITE):
    s = height * SS
    cube_r = s * 0.30
    pad = s * 0.16
    font = ImageFont.truetype(str(FONT_BOLD), int(s * 0.50))
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    seg_ai = "AI"
    seg_cad = "-CAD"
    w_ai = tmp.textlength(seg_ai, font=font)
    w_cad = tmp.textlength(seg_cad, font=font)
    cube_w = cube_r * 2
    total_w = int(pad + cube_w + pad * 0.9 + w_ai + w_cad + pad)
    img = Image.new("RGBA", (total_w, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _iso_cube(d, pad + cube_r, s * 0.52, cube_r)
    tx = pad + cube_w + pad * 0.9
    ty = s * 0.5 - s * 0.30
    d.text((tx, ty), seg_ai, font=font, fill=ACCENT)
    d.text((tx + w_ai, ty), seg_cad, font=font, fill=cad_color)
    target_h = height
    target_w = int(total_w * target_h / s)
    return img.resize((target_w, target_h), Image.LANCZOS)


def main():
    make_tile(256).save(HERE / "logo_icon.png")
    make_tile(48).save(HERE / "logo_window.png")
    make_wordmark(200).save(HERE / "logo_wordmark.png")              # dark theme (white "-CAD")
    make_wordmark(200, cad_color=SLATE).save(HERE / "logo_wordmark_dark.png")  # light theme (slate "-CAD")
    print("wrote logo_icon.png, logo_window.png, logo_wordmark.png, logo_wordmark_dark.png to", HERE)


if __name__ == "__main__":
    main()
