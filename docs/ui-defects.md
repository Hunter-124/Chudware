# UI defect register

Provable, reproducible UI defects with `file:line`. Recorded deliberately rather than
fixed immediately, so the backlog is honest and a later wave can pick items up without
re-deriving the evidence.

Rules for this file:

- Every entry names a file and line range someone actually opened.
- No "looks off" entries. If it cannot be pointed at in source, it does not belong here.
- When an entry is fixed, delete it and note the fix in `docs/REFACTOR_LOG.md`.
- Line numbers drift. Treat them as a starting point and re-confirm before editing.

Established 2026-08-27 from a read-only audit of `host/src/ui/**`, focused on the AI
workspace but recording anything provable found en route.

---

## 1. Dead / contradictory UI code

**`host/src/ui/account_panel.cpp:965-970` — unreachable entitlement branch.**
`else if (state.entitlementSignedIn) evaluateOffline(state)` cannot be reached, and the
same function unconditionally re-evaluates offline entitlement for signed-in,
non-refused accounts a few lines later (`:973-980`). This is dead state handling around
the subscription/grace surface. Remove it or make it reachable on purpose; leaving
contradictory flow around billing state is how a wrong entitlement ships.

**`host/src/ui/ai/active_work.cpp:150-155` — `activeWorkReset()` has no caller.**
Declared at `active_work.h:29-36`. A scoped search of `host/src` and `host/tests` found
no call site. Note the neighbouring DAG serialization pair IS live
(`copilot/conversations.cpp:1045-1048,1234-1235`) — do not delete that by association.

**`host/src/app.cpp:7447-7451` — stale layout comment.**
Describes an obsolete `[sessions + sub-agents rail] | … | [AI settings]` layout. The
implementation states the opposite: no permanent sessions rail and no bottom options
strip (`ai_workspace.cpp:10-13`, `:1849-1851`). Documentation drift on a visible
workspace boundary routes future work at a rail that does not exist.

---

## 2. Layout / padding defects

**`host/src/ui/parts/parts_filters.cpp:159-162` — pane gutter is never applied.**
Pushes `partsPadX` padding, then opens `##parts_filters` with `ImGuiChildFlags_None`.
Per the layout contract, a plain child ignores `WindowPadding` unless it passes
`AlwaysUseWindowPadding` or has a border
(`.claude/skills/gui-theme-layout/SKILL.md:146-149`), so the intended gutter is silently
dropped and content sits flush to the edge. The same body then nests a zero-padding
child (`:173-176`) while its own comments reason about an outer/inner left-edge
alignment problem (`:169-172`) — fixing the outer flag is a prerequisite to trusting
that reasoning.

**`host/src/ui/ai/rules_authoring.cpp:567-576` — modal fixed at 460x470 scaled px.**
No viewport-bound size and no compact branch, with a further fixed reserved
transcript/composer band (`:605-611`). On a small display the modal is cramped before
its own scrolling log child can compensate.

**Fixed-width, non-responsive AI splits.** Rail `168*S`, gap `10*S`, chat min `360*S` /
max `780*S` (`ai_workspace.cpp:1852-1858`); Documents forces its list to 190–340*S
(`docs_panel.cpp:918-920`). At narrow full-body widths the minimums consume 538 logical
px before the view pane gets anything, and Documents' preview then has another 190px
sibling minimum. Neither layout has a breakpoint or compact branch.

**`host/src/ui/ai/ai_workspace.cpp:1383-1399` — no mutual width reservation.**
An Agent Hub row's role text starts after the status glyph while the model/age meta is
independently right-anchored, so a long role paints into the meta region. Only the
second line reserves its 90*S queued badge (`:1401-1417`).

**`host/src/ui/ai/active_work.cpp:347-357` — card titles clip without ellipsis.**
Emitted with `TextUnformatted` and left to ImGui clipping, though the kit offers
elision for exactly this (`widgets/kit.h:211-221`). Long document titles get no
ellipsis and no full-title affordance.

---

## 3. Empty states that waste a whole pane

**`host/src/ui/ai/ai_workspace.cpp:383-389` + `:1493-1505`.** `emptyState` inserts 35% of
available height before the shared empty state, and Agent Hub returns early when both
the DAG and the sub-agent registry are empty. That is the large "no sub-agents yet"
void — a deliberate layout choice, not a loading state.

**`host/src/ui/ai/metrics_view.cpp:164-190`.** Hand-builds its own no-data state with the
same 35% vertical spacer instead of the shared one, repeating the defect.

---

## 4. Nested scroll ownership

Three independent vertical scroll layers stack in Activity: the outer pill-scroll child
`##ai_ws_view` (`ai_workspace.cpp:1769-1827`), then `##activity_work` and
`##activity_docs` (`activity_view.cpp:39-59`), then Documents' independently
pill-scrolled left and right children (`docs_panel.cpp:927-953`). Separately, Session
Plan adds `##pb_plan` and `##pb_tl` with `AutoResizeY` and no `NoScrollbar`/pill calls
(`plan_board.cpp:319-323,476-480`), leaving those children outside the documented
pill-scroll contract.

---

## 5. Animation that stutters by construction

Idle repaint is 20 Hz, so any continuous animation must either use `ui::anim` helpers or
call `requestFrames` (`anim.h:9-22,66-94`). Two AI surfaces do neither and drive
`std::sin` directly: the Agent Hub running-status pulse
(`ai_workspace.cpp:1374-1378`) and the Active Work card-frame pulse
(`active_work.cpp:296-300`). A scoped search found no `anim::` or `requestFrames` usage
anywhere in `host/src/ui/ai`. Both visibly stutter once mouse motion stops.

---

## 6. Duplicated kit primitives

`host/src/ui/widgets/kit.h` exists precisely to stop this (see its own header comment,
`kit.h:6-18`). Current re-implementations:

| Surface | Hand-rolled | Kit primitive it should use |
|---|---|---|
| Agent Hub rows | wash/bar/card painting `ai_workspace.cpp:1361-1366`; ASCII `[>] [x] [v] [o] [ ]` `:1374-1387`; manual queued chip `:1412-1424` | `kit::row`/`rowBegin` (`kit.h:109-134`), `kit::chip` (`:136-146`), `kit::statusGlyph` (`:175-187`) |
| Documents rows | selectable glass row with truncation/tag/remove `docs_panel.cpp:739-814` | `kit::RowStyle` already supports thumbnail, meta, selected, tooltip, action-width reservation (`kit.h:109-128`) |
| Session Plan rows | glass rows, status chips, ASCII `^ v x` `plan_board.cpp:588-653` | kit row/status/chip/iconButton |
| Metrics header | bespoke underline painter `metrics_view.cpp:38-50` | `kit::sectionHeader` (`kit.h:102-107`) |

Note these may not trip `gui_lint.py --rule dupe` today because the helpers are named
differently — absence of a lint hit is not evidence of absence here.

---

## 7. Execution-trace specific

**Zoom is geometrically inconsistent** (`ai_workspace.cpp:1156-1165` vs `:1240-1259`).
Card geometry scales with the zoom factor, but both node `AddText` calls use the default
font with no scaled size. Labels are suppressed entirely at minimum zoom (`:1229`) while
cards grow without their type at high zoom. The character cap is a heuristic rather than
a measured width (`:1253-1256`).

**The canvas is fully hand-rolled** (`ai_workspace.cpp:1128-1203`) — manual clipping,
Bézier edges, rectangles. `kit.h` exports no canvas/DAG primitive, so this is not a
duplication finding, but any overhaul should keep the data/control topology and replace
only the chrome. Layout is a depth indent plus a preorder vertical stack (`slotPos`,
`:1159-1165`), not a routed graph layout.

**Two node kinds have no producer.** `spawn` and `advisor` nodes are supported
(`ai_workspace.cpp:82-90`) but production emits only tool/thought/context. Either wire
the producers or drop the node kinds; a renderer branch that cannot execute is the same
class of defect as section 1.

---

## 8. Iconography gaps

The AI nav rail is entirely text-only: `navRow` accepts only label/active/surface/badge
(`ai_workspace.cpp:1531-1572`) and all six call sites pass labels only (`:1606-1622`).
The Agent Hub, trace, Activity and token-spend headers all pass `nullptr` for their
`kit::sectionHeader` icon (`ai_workspace.cpp:1518-1520`; `activity_view.cpp:39`;
`insights_view.cpp:31-36`).

`ai_panel`, `activity`, `history` and `settings` already exist in the atlas
(`contracts/icons.md:22-24,43-45`), so those are free wins; no dedicated Session
Plan or Insights icon is inventoried yet. Documents' attach/add/remove controls are
text-only (`docs_panel.cpp:806-841`) despite an inventoried `paperclip` for attachments
(`contracts/icons.md:102-110`) and a general `cancel_x` (`:43-45`).

Glyph safety when adding copy or marks: `gui_lint.py --rule glyph` is an allowlist
derived from the theme's glyph/CAD ranges, not a generic non-ASCII ban
(`scripts/gui_lint.py:223-227,236-254`). Anything outside it renders as `?` and must go
through `kit::statusGlyph` or the atlas (`:393-398`).
