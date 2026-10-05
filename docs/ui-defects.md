# UI defect register

Provable, reproducible UI defects, each anchored to a file plus a stable symbol (function,
child-window id, or quoted comment) that someone actually opened. Recorded deliberately rather
than fixed immediately, so the backlog is honest and a later wave can pick items up without
re-deriving the evidence.

Rules for this file:

- Every entry names a file and a stable symbol someone actually opened. Do not add line
  numbers; they drift with every edit. Search for the symbol instead.
- No "looks off" entries. If it cannot be pointed at in source, it does not belong here.
- When an entry is fixed, delete it and note the fix in `docs/REFACTOR_LOG.md`.

Established 2026-08-27 from a read-only audit of `host/src/ui/**`, focused on the AI
workspace but recording anything provable found en route. Re-verified against the working
tree on 2026-10-04. Since the first audit, the AI workspace has been restructured into
Current Run / Plan / Activity / Sessions / Insights / Options compartments
(`ai_workspace.cpp`, `current_run_view.cpp`, `agent_dag.cpp`). Entries that the
restructure removed or fixed have been deleted. Every entry below was still present on
2026-10-04.

---

## 1. Dead / contradictory UI code

**`host/src/ui/ai/active_work.cpp` — `ui::activeWorkReset()` has no caller.**
It is declared in `active_work.h` ("Drop all per-doc op state (project switch / full
reset)"). A search of `host/` found only the declaration and the definition. Note that the
neighbouring per-conversation DAG serialization pair IS live: `ui::agentDagToJson` /
`ui::agentDagFromJson` in `copilot/conversations.cpp` (the `agent_dag` key in chat
history). Do not delete that pair by association.

**`host/src/ui/ai/ai_workspace.cpp` — file-static `emptyState()` wrapper has no caller.**
The wrapper adds a 35%-height spacer before `ui::kit::emptyState` ("The vertical push is
this workspace's own decision"). Nothing in the file calls it now. The views that draw
empty states call `kit::emptyState` directly. Either delete the wrapper or route the views
through it on purpose.

**`host/src/app.cpp` — stale AI-workspace layout comment.**
The comment on the AI full-body hub still describes a
`[sessions + sub-agents rail] | [expanded Copilot chat / project DAG] | [AI settings]`
layout. The implementation's own header comment in `ai_workspace.cpp` says otherwise: the
layout is main conversation plus a project-scoped utility column, with Sessions and
Options as nav compartments. Documentation drift on a visible workspace boundary sends
future work toward a rail that does not exist.

---

## 2. Layout / padding defects

**`host/src/ui/parts/parts_filters.cpp` — `##parts_filters` pane gutter is never applied.**
The code pushes `WindowPadding` = `ui::metrics::partsPadX()` (commented "pane gutter"),
then opens `##parts_filters` with `ImGuiChildFlags_None`. Per the layout contract in
`.claude/skills/gui-theme-layout/SKILL.md`, a plain child ignores `WindowPadding` unless it
passes `AlwaysUseWindowPadding` or has a border. The intended gutter is silently dropped.
The nested `##filters_scroll` child, which uses zero padding, has comments that reason
about inheriting that outer gutter. Fixing the outer flag is a prerequisite to trusting
that reasoning.

**`host/src/ui/ai/docs_panel.cpp` — `drawDocsPanel` has a fixed, non-responsive split.**
The list column is `std::clamp(availW * 0.32f, 190*S, 340*S)` with no compact branch. The
AI workspace only guarantees its compartment `viewMin` = 300*S (`drawAiWorkspace`). At that
width, Documents' preview (`##docs_right`) is left about 100 logical px beside the
190-px list minimum.

---

## 3. Empty states that waste a whole pane

Several AI views push the shared `ui::kit::emptyState` down with their own fractional
spacer, so a "nothing yet" message sits in a large void, and each view picks its own push:

- `drawMetricsView` zero-turn state in `metrics_view.cpp`: 35%, built inline.
- The `emptyState` wrapper in `active_work.cpp`: 30%.
- The `emptyState` wrapper in `docs_panel.cpp`: 28%.

`kit::emptyState` has no placement parameter, so there is no single rule for this push.

---

## 4. Nested scroll ownership

Three independent vertical scroll layers stack in Activity:

1. The compartment child `##ai_ws_view` (`ai_workspace.cpp` `drawViewPane`, pill
   scrollbar).
2. `##activity_work` and `##activity_docs` (`activity_view.cpp`).
3. Documents' independently pill-scrolled `##docs_left` and `##docs_right` children
   (`docs_panel.cpp` `drawDocsPanel`).

Separately, Session Plan's `##pb_tl` and `##pb_plan` children (`plan_board.cpp`) use
`AutoResizeY`, with no `NoScrollbar` flag and no `drawWindowPillScrollbarV` call. That
leaves them outside the documented pill-scroll contract.

---

## 5. Duplicated kit primitives

`host/src/ui/widgets/kit.h` exists precisely to stop this (see its own header comment).
Current re-implementations:

| Surface | Hand-rolled | Kit primitive it should use |
|---|---|---|
| Documents rows | `docRow` in `docs_panel.cpp`: `InvisibleButton` + `glassRowBackground` + manual tag/remove layout | `kit::row` with `kit::RowStyle` (icon/thumb, meta, selected, tooltip, trailing action-width reservation) |
| Session Plan rows | `plan_board.cpp` checkpoint and plan-item rows: `glassRowBackground`, ASCII `SmallButton("^##up")` / `("v##down")` / `("x##del")` | `kit::row` + `kit::chip` + `kit::statusGlyph` + an icon button |
| Metrics header | `secHeader` in `metrics_view.cpp`: bold label plus a hand-painted accent underline | `kit::sectionHeader` |

These may not trip `gui_lint.py --rule dupe` today, because the helpers are named
differently. The absence of a lint hit is not evidence that the duplication is absent.

---

## 6. Iconography gaps

- **Nav rail.** The AI nav rail is text-only: `navRow` in `ai_workspace.cpp` builds a
  `kit::RowStyle` but never sets `RowStyle::icon`, although the field exists ("contracts/icons.md
  name drawn in the leading slot").
- **Section headers.** The AI section headers pass `nullptr` for the `kit::sectionHeader`
  icon:
  - the `sectionHeader` wrapper and "Active project" in `ai_workspace.cpp`;
  - "Active Work" in `activity_view.cpp`;
  - "Model leaderboard" in `insights_view.cpp`;
  - the roster and inspector headers in `current_run_view.cpp`.
- **Free wins.** `ai_panel`, `activity`, `history` and `settings` already exist in the atlas
  (`contracts/icons.md`, General and feature-tree families). No dedicated Session Plan or
  Insights icon is inventoried yet.
- **Documents controls.** Documents' attach and remove controls are text-only
  (`"Attach to next message"` / `"Attach"` buttons in `docs_panel.cpp`). An inventoried
  `paperclip` exists for attachments, and `cancel_x` exists for removal.

Glyph safety when adding copy or marks: `scripts/gui_lint.py --rule glyph` uses
`load_glyph_ranges` to parse the allowlist in `host/src/ui/glyph_ranges.h` and
`glyph_offenders` to report unsupported string-literal codepoints; it is not a generic
non-ASCII ban. Unsupported marks must use a supported glyph or the atlas.
