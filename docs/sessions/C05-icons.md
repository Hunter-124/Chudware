# C05 — Icon pipeline (PIL atlases, light+dark)

Model: **sonnet** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
`host/assets/make_icons.py`: generate the full icon inventory as two theme atlases + JSON map,
per the icons contract. Pure generation, no C++ in this card.

## Read first
1. `contracts/icons.md` — inventory (~95 names), sizes, style, atlas JSON shape
2. `host/assets/make_logo.py` — the house pattern (PIL, supersampling)

## Owned paths
`host/assets/make_icons.py` · `host/assets/icons/**` (generated: `atlas_dark.png`,
`atlas_light.png`, `atlas.json`) · `host/assets/test_icons.py`

## Deliverables
1. `make_icons.py`:
   - One small draw function per icon (8× supersample → LANCZOS downsample), drawing simple
     geometric glyphs that READ at 20 px: e.g. `extrude_boss` = profile square with up-arrow;
     `fillet` = corner with rounded inner arc; `mate` = paperclip-like link; `measure` =
     ruler diagonal. Favor clarity over artistry; reuse helper primitives (stroke polyline,
     filled poly, arc, circle, arrow) so icons stay consistent.
   - Both 32 px and 20 px cells for every name; both themes from one draw routine
     parameterized by a palette (stroke/accent/fill colors per icons.md).
   - Deterministic output (fixed seeds, no timestamps); grid-packed atlas; `atlas.json` =
     `{"atlas_px": [w,h], "cell": {"name@32": {x,y,w,h}, "name@20": {...}}}` — identical layout
     in both theme PNGs.
   - Every name from icons.md MUST exist; add a `MISSING` placeholder glyph (diagonal box) and
     fail loudly listing any inventory name without a draw function.
2. `test_icons.py` (runner): regenerates into a temp dir, asserts: every icons.md inventory
   name present at both sizes, atlases same dimensions, layouts identical, PNGs decode, and
   regeneration is byte-identical (determinism).

## Constraints
- venv python has Pillow. Parse the inventory FROM `contracts/icons.md` (the backticked
  name lists) so contract and atlas can't drift — if parsing is brittle, mirror the list in
  one Python constant and assert it matches the contract's names.

## Verification
`& $py host/assets/make_icons.py` writes the three files; `& $py -m host.assets.test_icons` —
if package imports are awkward run it as a script: `& $py host/assets/test_icons.py`. Green.
