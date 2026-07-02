# HANDOFF — top-of-viewport dead space + full-app glass-styling completeness (2026-07-02)

**STATUS: DONE. All 5 pieces below landed, verified, and committed.** This doc is now a
record of what was done and where, plus the still-open backlog in §4. Nothing in §1/§2
needs to be re-executed — read §0 for the commit hashes, then skip to §4 for actual
remaining work.

Companion to `docs/HANDOFF_GUI_OVERHAUL.md` (locked design decisions in its §1 still apply:
pinned glass columns, no visible splitter bars, dynamicColor default ON, docked layout
deleted — do not re-litigate). Repo root `/home/hunter/Repositories/CAD`.

## 0. Final status — all landed and committed

Every write-set below was independently verified (`git diff --stat` against the exact
expected file list, plus a green `flock /tmp/host-build-linux.lock -c "cmake --build
host/build-linux -j$(nproc)"` and an `AI_CAD_SHOT` screenshot where a scenario existed)
before being committed. Screenshots live in `scratch/gui-overhaul-shots/` (gitignored).

| Scope | Files | Commit | Notes |
|---|---|---|---|
| Dead-space + collapse chevron (§1) | `host/src/ui/layout.cpp`, `doc_tabs.cpp`, `legacy_chat.cpp` | `043e752` | `viewport_panel.cpp`/`layout.h` diffs are pre-existing/unrelated (predate this session), correctly excluded |
| Glass audit: PM forms core (§2) | `host/src/ui/pm_forms.cpp`, `property_manager.cpp` | `ddb1a71` | `pm_forms.cpp` needed real edits (Range/Signed/Seg helpers) despite one agent's self-report claiming otherwise — verify diffs yourself, don't trust self-reports blindly |
| Glass audit: dialogs (§2) | `evaluate_dialogs.cpp`, `equations_dialog.cpp`, `design_table_panel.cpp`, `cutlist_panel.cpp` | `3292023` | Also fixed a real bug: full-rect glass was painting over the title bar/close-X |
| Glass audit: sim + assembly (§2) | `sim_manager.cpp`, `asm_panels.cpp` | `0700584`* | `sim_manager.h` diff is pre-existing/unrelated, correctly excluded |
| Glass audit: drawing + section (§2) | `drawing_panel.cpp`, `section_tool.cpp` | `0700584`* | Section View panel: flat opaque `0.94f` fill → adaptive frosted glass |
| `docs/REFACTOR_LOG.md` (append-only shared log) | — | `f6e156c` | Committed once at the very end per protocol in §3 |

\* Two independent haiku commit-subagents raced on the sim/assembly and drawing/section
pieces; one landed all 4 files in a single commit (`0700584`) before the other could run.
Content was verified correct for all 4 files (`git diff HEAD -- <file>` empty, matches
pre-commit diffs exactly) — the only cosmetic wart is the commit message only narrates the
drawing/section half. Not worth unbundling via a destructive git op; left as-is.

`grep -rc panelsOverlay host/src/` still all-zero (docked layout stays deleted, unaffected
by this work).

## 1. Fix set A — top-of-viewport dead space + collapse chevron (DONE — `043e752`)

Kept for reference/reasoning trail. Erik annotated a screenshot of a blank part (`AI_CAD_SHOT=m1`). Ribbon (menu bar → Features/
Sketch/.../Manufacture tab strip → tool icon row) ends at the top of the window; below it
there's ~60-100px of dead blank space before the viewport's own toolbar row and the
"AI-CAD Copilot" panel actually start. The doc tab ("Part1 [x]" + lock + new-file button)
sits pinned above the FeatureManager column's top-left corner; to its left, down the
column's own left edge, is a vertical strip of small square icon buttons (Explorer/
FeatureManager/PropertyManager/Configurations/Simulation tabs).

Four fixes, in this order (each depends on the previous one's freed space):

1. **Left icon rail → horizontal, across the top of the FeatureManager column.** Closes
   `docs/HANDOFF_GUI_OVERHAUL.md` §5 backlog item 1 (already scoped there verbatim).
   `host/src/ui/layout.cpp`, `drawLeftPane()` (~line 220 on): the tab array around
   line 405-408 (`kLeftPaneExplorer/FeatureManager/PropertyManager/Configurations/
   Simulation` — id/icon/letter/tooltip tuples) currently draws a VERTICAL strip down the
   column's left edge. Make it a horizontal row across the column's TOP edge so tab bodies
   get the full panel width (kills the left gutter).
2. **Doc tab strip → floats at the top of the VIEWPORT, not above the FeatureManager
   column.** `host/src/ui/doc_tabs.cpp`, `drawDocTabs()` (~line 259 on). Move it to float
   over the viewport, starting just right of the FeatureManager column's live right edge
   (`layout.cpp`'s `drawLeftPane` geometry / `publishOverlayColumnEdge`), vertically flush
   with the top of the viewport (same Y as fix 3). `drawLeftPane`'s `colY` (currently offset
   by `ui::docTabStripHeight(st)`/`ui::docTabBandBottom()` to reserve space above itself)
   should collapse to `band.minY + margin` — the column starts right at the top of the
   viewport band, and fix 1's horizontal icon row fills the space the doc tabs used to
   occupy above it. Check other callers of `docTabStripHeight`/`docTabBandBottom` (HUD-dodge
   logic etc. in `layout.cpp`/`layout.h`) for stale assumptions.
3. **Viewport toolbar row + Copilot panel top → flush with the top of the viewport.**
   (a) the viewport's zoom/pan/orientation row — `host/src/ui/viewport_panel.cpp`
   ~line 2800-3030, `huBtn(...)` calls for `Zoom to Fit (F)`/`Zoom to Area`/pan/rotate/
   `View Orientation`/`Display Style`/`Hide / Show Items`/`Section View`; (b) the
   `AI-CAD Copilot` panel header (grep `"AI-CAD Copilot"` in
   `host/src/ui/legacy_chat.cpp`). Raise both flush to the top of the viewport work area
   (probably also keyed off `overlayBand()`/`band.minY`) — no dead strip above either.
4. **Fix the FeatureManager collapse chevron ("«").** `host/src/ui/layout.cpp`
   ~line 370-392, the `##lp_collapse` InvisibleButton block. Currently two bare chevron
   lines with no visible containing chip at rest (background only appears on hover via
   `ui::glassRowBackground`) — reads as a stray floating mark. Give it a persistent subtle
   glass-chip background, and make sure it doesn't collide with the relocated icon rail
   from fix 1.

**Verify**: rebuild, re-shoot `AI_CAD_SHOT=m1`/`sketch`/`fea`/`appearance`, compare before/
after, save to `scratch/gui-overhaul-shots/fix4_<scenario>.png`. Re-run
`grep -rc panelsOverlay host/src/` — must stay all-zero (docked layout stays deleted).

## 2. Fix set B — glass-styling completeness audit (DONE — see §0 commit table)

Kept for reference/reasoning trail — the API notes below are still the canonical
explanation of the embedded-vs-standalone pattern if you're touching this code later.

Erik's directive verbatim: *every single view in the feature manager (extrude, or any other
advanced modeling feature) needs the correct color scheme — the proper updated colored
liquid-glass semi-transparent/translucent background pill on every single UI element
rendered.* Confirmed by grep at the time: these files had **zero** occurrences of the
adaptive-glass API — the liquid-glass v2 wave hadn't touched them yet (all now fixed, see §0):

| File | Lines | Notes |
|---|---|---|
| `host/src/ui/pm_forms.cpp` | 833 | The ~44 PropertyManager feature-editing forms (extrude/revolve/fillet/chamfer/shell/hole-wizard/pattern/etc) — Erik's named example |
| `host/src/ui/property_manager.cpp` | 2920 | The PM shell hosting those forms |
| `host/src/ui/evaluate_dialogs.cpp` | 346 | |
| `host/src/ui/equations_dialog.cpp` | 254 | |
| `host/src/ui/design_table_panel.cpp` | 553 | |
| `host/src/ui/cutlist_panel.cpp` | 173 | |
| `host/src/ui/sim_manager.cpp` | 3624 | FEA/CFD setup, results, charts |
| `host/src/ui/asm_panels.cpp` | 1294 | Assembly mate/component panels |
| `host/src/ui/drawing_panel.cpp` | 2126 | 2D drawing-sheet mode |
| `host/src/ui/section_tool.cpp` | 420 | Only 3 hits — partially done, audit for completeness, don't assume finished |

`host/src/ui/settings_modal.cpp` (891 lines, 7 hits) is the one file that's already done —
use it as the reference for the "standalone window" pattern.

### API reference (`host/src/ui_theme.h`)

- `enum class GlassLevel { UltraThin, Thin, Regular, Thick };`
- `enum class GlassGroup : uint8_t { Global = 0, FeatureManager, Copilot, Hud, Count };`
- `ImGuiID panelId(const char* label);` — stable id for a named surface, shared across TUs.
- `const AdaptiveSurface& adaptivePanel(ImGuiID id, const ImRect& rect, GlassGroup group);`
  — samples backdrop luminance under `rect`, returns contrast-matched
  `surf.textPrimary`/`textSecondary`/tint.
- `void glassPanel(ImDrawList* dl, const ImRect& rect, GlassLevel level, float rounding, const AdaptiveSurface& surf, float alpha=1.f);`
  — actually paints the translucent glass background.
- `void glassRowBackground(ImDrawList* dl, ImVec2 mn, ImVec2 mx, float rounding=4.0f, ...);`
  — smaller translucent "pill" for one hoverable row/button/chip within a panel.

### The one nuance that matters: embedded vs. standalone

PropertyManager forms render EMBEDDED inside the pinned FeatureManager glass column, not as
their own window. `host/src/ui/layout.cpp`'s `drawLeftPane()` paints ONE glass background
for the whole column right after `ImGui::Begin`, via
`ui::adaptivePanel(ui::panelId("host_left_glass"), lpPaneRect, ui::GlassGroup::FeatureManager)`
+ `ui::glassPanel(...)`. Its own comment says `feature_tree.cpp`/`file_explorer.cpp` fetch
that SAME surface via the SAME `panelId("host_left_glass")` instead of painting their own
background — that's the pattern for anything embedded in the column (`pm_forms.cpp`,
`property_manager.cpp`, and the "Simulation" tab body inside `sim_manager.cpp`). **Do not
call `glassPanel` again for embedded content** — that double-paints and fights the column's
background. Instead: look up the same surface for adaptive text color, and use
`glassRowBackground` pills for individual rows/buttons/hover states (worked example: the
collapse chevron in `layout.cpp` ~line 370-392).

For anything that opens its OWN floating `ImGui::Begin(...)` window (most of `sim_manager.cpp`'s
setup/results dialogs, `asm_panels.cpp`'s mate editor, `evaluate_dialogs.cpp`,
`equations_dialog.cpp`, `design_table_panel.cpp`, `cutlist_panel.cpp`, most of
`drawing_panel.cpp`, `section_tool.cpp`'s `"Section View##panel"`), follow
`settings_modal.cpp`'s pattern instead (grep `adaptivePanel|glassPanel` there,
~lines 424-441): `adaptivePanel(panelId("<unique name>"), rect, GlassGroup::Global)` +
`glassPanel(...)` right after `Begin` succeeds, then `surf.textPrimary`/`textSecondary` for
text. Determine per-window which case applies by checking whether it's dispatched from
inside `drawLeftPane`'s tab-body switch or is an independent top-level `Begin` — don't
assume by filename alone (`sim_manager.cpp` and `asm_panels.cpp` almost certainly contain
both kinds).

### Suggested parallel split (disjoint write-sets, safe to run concurrently)

1. `pm_forms.cpp` + `property_manager.cpp` (+ `.h` if a signature changes) — highest
   priority, Erik's named example.
2. `evaluate_dialogs.cpp` + `equations_dialog.cpp` + `design_table_panel.cpp` +
   `cutlist_panel.cpp`.
3. `sim_manager.cpp` (+ `.h`) + `asm_panels.cpp`.
4. `drawing_panel.cpp` + `section_tool.cpp`.

Each worker should: enumerate every per-widget render function in its files (grep `void
draw`-style signatures), bring text color, panel backgrounds, and row/button pills up to the
standard above, verify with `flock /tmp/host-build-linux.lock -c "cmake --build
host/build-linux -j$(nproc)"` (the lock avoids racing concurrent builds from other workers)
plus whatever `AI_CAD_SHOT` scenario in `host/src/ui/scenarios.cpp` exercises that UI (if
none exists, at minimum confirm a clean build + no ImGui assertions and say so explicitly in
its report), and append ONE paragraph to the tail of `docs/REFACTOR_LOG.md` (append-only —
re-read the tail immediately before appending since other workers append concurrently).

## 3. Commit protocol — commit as you go, via lightweight subagents (DONE, all 5 pieces + log)

Kept as the reusable protocol for the next lane of work (§4). One lesson learned running
this live: launching two haiku commit-subagents for adjacent pieces can race (see §0's
`0700584` footnote) — harmless here since content was pre-verified, but prefer sequencing
commit subagents one at a time, or explicitly partitioning file lists, if you want to avoid
the bundling. Erik's directive: commit each piece as it lands rather than batching everything
into one final commit, using cheap/small-model subagents to do the mechanical write-up. Per
piece (one of the write-sets above, once its own build + verification is clean):

1. Confirm the write-set is exactly what's expected: `git diff --stat -- <exact files>` —
   no surprise files.
2. Draft a commit message in this repo's existing style (see `git log` for examples —
   short imperative summary line, occasional body). No emojis.
3. `git add <exact files>` — never `git add -A`/`git add .`, never scoop another lane's
   dirty files (this repo has multiple concurrent lanes; see
   `docs/HANDOFF_GUI_OVERHAUL.md` §6 for the write-set boundaries already established for
   the GUI vs. AI-experience lanes).
4. Commit via heredoc:
   ```
   git commit -m "$(cat <<'EOF'
   <summary line>

   <optional body>

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```
5. Never `--no-verify`, never force anything, never amend — always a new commit.
6. `docs/REFACTOR_LOG.md` is append-only shared state across every lane in this repo —
   leave it UNCOMMITTED after each piece lands (don't fold it into any single piece's
   commit) so the next lane's paragraph can still land cleanly on top; commit it once, at
   the very end, after all pieces are in.

## 4. Also still open (tracked elsewhere — not required by this doc, don't lose them)

Per `docs/HANDOFF_GUI_OVERHAUL.md` §5: E3 in-app AI hooks (import-summary toast,
"Reverse-engineer import" Copilot chip, `ai_reverse_engineer`/`ai_explain_part` palette
commands — host/ only), settings reorg + dead-code removal, a final human-eye screenshot
pass, a ~10-minute manual interactive-input pass (resize hit-zones, PM slider ctrl-click,
chip firing), and a Windows/MSVC build check (needs an actual Windows machine). None of
these are in scope for this doc — only fold them in if you're told to.
