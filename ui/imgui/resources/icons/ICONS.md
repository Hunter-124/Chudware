# AI-CAD (ui/imgui) Icon Pipeline

Factual description of what actually ships and runs today. This directory holds the **runtime
assets** consumed by `ui/imgui/src/theme/icons.{h,cpp}` + `icons_sdf.cpp` — it is not a source-SVG
staging area (see "Stale files" below).

## 1. Atlas generation (offline, unchanged by Liquid Glass v2)

`host/assets/make_icons.py` procedurally renders every icon with Pillow (`ImageDraw` primitives —
lines, arcs, filled shapes — not SVG) at 32px and 20px, into a single packed atlas per theme, and
writes:

- `atlas_dark.png`, `atlas_light.png` — RGBA cell grid, one column per size, one row per icon name.
- `atlas.json` — `{"atlas_px": [w, h], "cell": {"<name>@<32|20>": {"x","y","w","h"}, ...}}`.

The dark/light atlases share an identical cell layout (`make_icons.py` asserts this at build time).
This directory's copies are refreshed by re-running that script; **Liquid Glass v2 made no change
to atlas generation.**

## 2. Runtime load (`ui::loadIcons`, `icons.cpp`)

`ui::loadIcons(assetsDir)` parses `atlas.json` into a name→UV cell map, uploads both PNGs as
`GL_RGBA`/`GL_LINEAR`/`CLAMP_TO_EDGE` textures, and uploads a 1×1 magenta placeholder texture for
unknown names. `ui::getIcon(name, size)` looks up a cell (falling back 32↔20) and returns UVs into
whichever atlas matches the active theme (`theme::ctx().dark`) — this is the **raster path**, still
used for every multi-color icon (material swatches, axis triads, status badges, ...).

## 3. SDF derivation (`icons_sdf.cpp`, built once at load)

Immediately after the dark atlas PNG is decoded (while its pixel buffer is still live, before
`stbi_image_free`), `icons_sdf::build()` derives a signed-distance-field glyph for every
**monochrome-safe** cell:

- For each icon name's `@32` cell (falling back to `@20`), compute the max color-channel spread
  (`max(r,g,b) - min(r,g,b)`) over pixels with alpha > 32. A cell **qualifies** when that spread is
  `<= 40/255` — this is a pure pixel-content test, not a name list, so it automatically excludes
  multi-hue glyphs (badges, XYZ triads, appearance swatches) without any hardcoded exceptions.
- Qualifying cells are thresholded (alpha ≥ 96 = inside) and run through an 8SSEDT (two-pass,
  8-neighbor signed sequential Euclidean distance transform), clamped to a 6px spread, and encoded
  to a single R8 byte per source pixel.
- Every glyph's R8 bytes are shelf-packed into one static atlas buffer, uploaded once as a
  `GL_R8`/`GL_LINEAR`/`CLAMP_TO_EDGE` texture (`GL_UNPACK_ALIGNMENT` set to 1 for the upload, then
  restored). The CPU-side buffer that produced it is kept resident — it is the sampling source for
  re-rasterization (§4), so no GPU readback is ever needed.

## 4. CPU re-raster cache (`icons_sdf.cpp`, on demand)

Icons are drawn crisp at *any* requested pixel size by re-rasterizing from the SDF, not by
stretching a fixed-resolution bitmap:

- Cache key: `(icon id, rounded render px, glyph|halo variant)`.
- On a cache miss, the SDF is bilinearly sampled per output texel and thresholded with a
  `smoothstep` band (antialiasing width shrinks as the render size grows) to produce a white
  RGBA texel with `alpha = coverage`. The `halo` variant shifts the threshold outward (dilates the
  silhouette) to produce a soft contrast backing.
- The result is written into a dynamic, shelf-packed RGBA atlas (grows — recreating its GL texture
  — only when a shelf runs out of room; entries are never evicted since the working set is bounded:
  names × ~6 rounded sizes × 2 variants).
- Drawn via the same `ImDrawList::AddImage(...)` + vertex-color tint every other icon uses — a
  white/coverage texel tinted by the caller's color is how both the plain glyph and the halo
  backing get their final color.

## 5. Draw API (`icons.h` / `icons.cpp`)

Priority order in `icons::draw()`: **(1)** hand-drawn chrome glyph (`drawGlyph()` — a handful of
simple shapes drawn directly with draw-list primitives, e.g. window min/max/close, checkmarks,
chevrons) **(2)** SDF re-raster cache (§3–4), for any alias-resolved name that qualified at build
**(3)** raster atlas cell (§2), for everything else **(4)** a dashed monogram placeholder for a
name with no cell at all.

```cpp
void icons::draw(dl, name, c, size, col);                          // priority order above
void icons::drawGlass(dl, name, c, size, col, haloCol);             // SDF + optional contrast halo
bool icons::hasSdf(name);                                           // true -> served by §3/§4
```

`drawGlass()` draws the halo (if `haloCol` has nonzero alpha) beneath the glyph for names with an
SDF glyph; for anything else it falls back to plain `draw()` (no halo — halos need the SDF path).

A small alias map in `icons.cpp` (`aliasMap()`) lets a handful of "friendly" call-site names
resolve to the actual atlas cell name (e.g. `extrude` → `extrude_boss`, `rail_configs` →
`settings`); both the raster and SDF paths resolve aliases identically.

## 6. Lint coverage

`scripts/gui_lint.py --rule icons` validates every `icons::draw(`/`icons::inlineIcon(`/
`icons::drawGlass(` literal name under `ui/imgui/src` against this atlas ∪ the hand-drawn glyph
names ∪ the alias names (both hardcoded in the lint script, commented as kept in sync with
`icons.cpp`). It runs the legacy `getIcon`/`treeIcon`/`asmIcon`/`hasIcon`/`huBtn` check against the
separate `host/assets/icons/atlas.json` only for `host/src` — the two icon systems are unrelated.

## Stale files in this directory

The loose `*.svg` files here (`arc.svg`, `chamfer.svg`, `extrude.svg`, ...) predate the
`make_icons.py` procedural pipeline and are **not read by any runtime code** (nothing under
`ui/imgui/src` opens a `.svg`). They are left as-is (out of scope for this change) rather than
deleted speculatively.
