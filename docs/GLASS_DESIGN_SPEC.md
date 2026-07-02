# Liquid Glass Design Spec — iOS-style GUI restyle

Design language for the unified `ui/imgui` shell. All panel code consumes **tokens only**
(`ui/imgui/src/theme/tokens.h`); `scripts/check.py gui_lint theme` enforces no literal
colors in panel files. Read this doc + your DAG node before porting any host panel.

---

## 1. Design principles

1. **True frosted glass over the 3D viewport** — the viewport renders to an FBO; panels
   floating above it sample a Kawase-blurred copy of that buffer (not a flat tint fake).
2. **Adaptive contrast** — panel tint and text color derive from sampled viewport luminance
   under the panel rect (ports host "viewport-adaptive glass" work).
3. **Row pills, not full washes** — interactive rows get `glassRowBackground()` pills;
   panel backgrounds stay transparent unless a material level requires a wash.
4. **One file per UI element** — changing a control updates both Linux and Windows builds.

---

## 2. Material elevation (4 levels)

Map to iOS `UIBlurEffect` material semantics. Use via `glassPanel(MaterialLevel, rect)`.

| Level | Token | Alpha (tint) | Blur passes | Use |
|-------|-------|--------------|-------------|-----|
| `ultraThin` | `mat.ultraThin` | 0.12 | 2 (¼ res) | HUD pills, tooltips over viewport |
| `thin` | `mat.thin` | 0.22 | 3 | FM tree rows, chat bubbles, composer toolbar |
| `regular` | `mat.regular` | 0.35 | 4 | Floating panels, popovers, PM forms |
| `thick` | `mat.thick` | 0.52 | 5 | Modals, settings sheet, blocking dialogs |

**Over non-viewport regions** (solid chrome, other docked panels): fall back to layered
translucent fills matching `thin`/`regular` appearance — no backdrop blur available.

---

## 3. Spacing grid (8pt base)

All layout constants are multiples of **8px** logical (@1.0× DPI; scale via `platform/dpi`).

| Token | Value | Use |
|-------|-------|-----|
| `space.xs` | 4px | Icon-text gap, inline padding |
| `space.sm` | 8px | Row internal padding, pill inset |
| `space.md` | 16px | Section gap, panel edge inset |
| `space.lg` | 24px | Panel header/footer bands |
| `space.xl` | 32px | Modal outer margin |

Extend `theme::Metrics` in `tokens.h`: set `grid = 8.f` (was 4.f) during F2.

---

## 4. Typography (SF-like scale)

| Role | Size | Weight | Token |
|------|------|--------|-------|
| Large title | 22px | Bold | `type.largeTitle` |
| Title | 17px | Semibold | `type.title` |
| Headline | 15px | Medium | `type.headline` |
| Body | 13px | Regular | `type.body` |
| Caption | 11px | Regular | `type.caption` |
| Mono | 12px | Regular | `type.mono` (JetBrains Mono) |

**Vibrancy text** over glass: `vibrancy.primary`, `vibrancy.secondary`, `vibrancy.tertiary`
— chosen by `adaptiveSurface()` from background luminance (dark text on bright viewport bleed,
light text on dark model).

---

## 5. Core theme APIs (F2 implements)

### `glassPanel(ImDrawList*, ImRect, MaterialLevel, float rounding)`

1. Sample blurred viewport texture UV-mapped to `rect` (from F1 `ViewportBackdrop`).
2. Tint with material alpha + `adaptiveSurface(luminance).tint`.
3. Draw 1px specular top edge (`glassHighlight` at 0.09–0.14 alpha).
4. Continuous-corner rounding (`roundingLg` = 10px for panels, `rounding` = 6px for rows).

### `adaptiveSurface(float avgLuminance)` / `adaptivePanel(ImGuiID, ImRect)`

`AdaptiveSurface { tint, textPrimary, textSecondary, textTertiary, darkText, scrim }` is
THEME-INDEPENDENT for `avgLuminance >= 0`: text tone derives only from the backdrop (vibrancy
tokens are byte-identical in both palettes), so over-viewport text reads the same in light and
dark mode. There is deliberately NO mid-range palette fallback — text tone is decisive (hysteretic
dark/light switch at 0.58/0.42 with temporal smoothing, `motion::adaptive` tau), while a continuous
contrast scrim (max at mid luminance, total tint alpha ≤ 0.62) compensates busy backdrops.
`avgLuminance < 0` = chrome fallback (palette txt roles, `darkText = !ctx().dark`).

Panels use `theme::adaptivePanel(theme::panelId("<pane>_glass"), rect)` — window-independent ids,
idempotent per frame, backed by `theme::sampleLuminance` (a sampler the viewport registers).
Luminance queries are pure CPU reads of an async grid: the renderer downsamples the blurred
backdrop and reads it back through a 3-slot PBO ring + fence polling (1–2 frame latency, ≤64×40
cells, zero GL calls at query time — the old per-rect `glReadPixels` stalls are gone).

### `glassPanel(dl, rect, level, rounding, surf)` — per-rect projection

The backdrop publishes its screen rect (`ViewportBackdrop::screenMin/screenMax`, flipV); glassPanel
projects each panel rect into backdrop UV space (clamped). Rects outside the viewport clamp to an
inset edge band — the frosted "bleed" under side panels. Backdrop invalid → layered fallback.

### `glassRowBackground(dl, mn, mx, rounding, stripe, selected)`

Row pill for FM, Explorer, Copilot composer rows:

- Idle wash: **0.26** alpha (host regression fix)
- Hover: **0.32**
- Selected: **0.42**
- Faint border: `palette.line` at 0.25 alpha

### Kawase blur (F1, consumed by F2)

Downsample viewport color to ¼ resolution; 4-tap Kawase passes per material level.
Expose texture ID + UV transform on `IViewport::backdrop()`.

---

## 6. Per-panel rules

### Feature Manager (C1)

- ONE UltraThin true-glass surface per over-viewport pane (`browser_glass`, painted once by
  left_dock; `agent_glass`, `doctabs_glass` likewise) — row pills on top. (The old "no full-panel
  wash" rule predates the working per-rect backdrop projection; a full opaque wash is still wrong.)
- Toolbar: explicit row reservation + `glassRowBackground` per icon button (undo/redo hit-test fix).
- Tree rows: `fmRowPill()` → `glassRowBackground` with `GetFrameHeightWithSpacing()`.
- Origin → nested planes hierarchy; datum icons use `ref_plane`.

### Copilot / agent panel (C3)

- Sessions toggle in header; composer **prelude** row (Design | Permissions + context meter).
- Composer **toolbar** row (model | thinking | gear | send) as pills.
- Assistant message body on frosted row pill (`thin` material), not full-panel wash.
- User bubbles: `regular` material with accent border.

### Viewport HUD (C4)

- **Per-button pills** top-right (not one full-width bar).
- Icon size 22dp; idle **auto-fade** (ramps on hover / zoom rect / open popup).
- `adaptiveSurface()` on each pill from luminance under button rect.

### Dock / layout (S1)

- `DockingEmptyBg` fully transparent.
- Left tab rail: persistent frosted pill per icon (0.20 idle / 0.32 hover / 0.40 selected).
- **Splitter lines**: 1px `palette.line` at 0.35 alpha between dock splits (fix invisible splitters).

### Title bar + doc tabs (S2)

- Doc-tab band: **semi-opaque** `mat.thin` wash (not fully opaque chrome) — tabs readable over viewport.
- Active tab: accent underline + `mat.regular` fill.
- Inactive tabs: `ultraThin` glass.

### Ribbon + context toolbar (S3)

- Ribbon background: `mat.thin` over viewport region only; solid `ribbonBg` when over chrome.
- Big tool buttons: 52dp squares with hover pill.

### Status / toast (S4)

- Status bar: solid `statusBg` (not glass — always legible).
- Toasts: `glassPanel(regular)` + alpha animation via `glassBackground(..., alpha)`.

### Property Manager (C2)

- Form body: `mat.regular` glass card inside PM tab.
- Field rows: standard ImGui frames; no nested full washes.

### Settings modal (S6)

- Full modal: `mat.thick` + blocking dim scrim (0.45 alpha black).

---

## 7. Known shortcomings (fix in P1)

Carried from `docs/REFACTOR_LOG.md` — must be cleared before G5:

| ID | Issue | Status (liquid-glass v2, 2026-07) |
|----|-------|-----------------------------------|
| G1 | Row pills vs full-panel washes inconsistent | RESOLVED — one UltraThin adaptive surface per pane + rowWash-constant pills |
| G2 | HUD idle fade too aggressive | RESOLVED — floor 0.25, ~120 ms hover ramp (viewport.cpp) |
| G3 | Chat contrast poor on bright bleed | RESOLVED — hysteretic adaptive vibrancy + contrast scrim |
| G4 | Dock splitter lines invisible | VERIFIED — 1px `line` hairlines in layout.cpp unchanged |
| G5 | Doc-tab band opacity | RESOLVED — adaptive thin glass band + tabs (doc_tabs.cpp) |
| G6 | Empty dock transparency | VERIFIED — DockingEmptyBg transparent (application.cpp) |

---

## 8. P1 acceptance criteria

P1 is done when integrator signs off on all of:

1. **Screenshot spot-check** — `tree`, `chat`, `pick`, `multidoc` on AI-CAD2: glass reads as
   frosted over viewport (blur visible on model edges through panels).
2. **Lint** — `python3 scripts/check.py gui_lint theme` PASS (zero hardcoded colors in `ui/imgui/src/ui/**`).
3. **Shortcomings table** — all six IDs in §7 resolved.
4. **Contrast** — body text ≥ 4.5:1 effective contrast on both dark-model and bright-floor viewport samples (visual review).
5. **HUD** — toolbar fades to ≤0.25 opacity idle; full opacity within 120ms of hover.
6. **No regressions** — FM undo/redo, dock resize, and doc-tab switching still work.

---

## 9. Before / after reference

| Surface | Before (host) | After (target) |
|---------|---------------|----------------|
| FM docked pane | Full `glassBackground` wash | Transparent; row pills only |
| Copilot | Mixed panel wash + rows | Row pills + bubble glass |
| Viewport HUD | Single glass bar | Per-button `ultraThin` pills + fade |
| Doc tabs | Opaque band | `thin` glass band over viewport |
| Tooltips | `pushGlassTooltipStyle` | `glassPanel(ultraThin)` + UV blur |

Host baselines: `scratch/gui_baseline/<scenario>.png` (G0). Styling on AI-CAD2 should be
**intentionally better** (more glass, better contrast) while preserving layout parity.

---

## 10. File ownership

| Concern | Owner node | Path |
|---------|------------|------|
| Blur FBO + luminance | F1 | `ui/viewport/renderer.*` |
| Tokens + glass APIs | F2 | `ui/imgui/src/theme/*` |
| Panel consumption | S*, C*, H*, P1 | `ui/imgui/src/ui/*` |
| Lint enforcement | G5 | `scripts/check.py` |

Do not add literal `ImVec4{...}` or `IM_COL32(...)` in panel files — only `theme::` / `palette.` accessors.
