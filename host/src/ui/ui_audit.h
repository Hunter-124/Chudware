// Automated ImGui UI-correctness audit built on Dear ImGui's test-engine item hooks.
//
// The four imgui .cpp files of the app target are compiled with IMGUI_ENABLE_TEST_ENGINE
// (host/CMakeLists.txt); ui_audit.cpp provides the hooks. While an audit needs them,
// ImGuiContext::TestEngineHookItems is set and ImGui reports EVERY item it submits in EVERY window:
// ItemAdd() (id, rect, item flags), ButtonBehavior() on an id that skipped ItemAdd(), each window's
// own registration in Begin(), and ItemInfo() (the label) from the stock widgets. With the flag
// cleared (the default) every hook site is a single branch, so the hooks cost nothing.
//
// Detection, per recorded frame:
//  * ID conflict — one window submitted the same non-zero ID twice in one frame (what ImGui's
//    "N visible items with conflicting ID" tooltip reports, but without needing the item to be
//    hovered). Not conflicts: items flagged ImGuiItemFlags_AllowDuplicateId; a window's own
//    registration in Begin(); the item EndChild() adds under a child's ChildId (BeginChild()
//    appending to a child twice in a frame re-adds it, multiline InputText adds it besides its own
//    item); one ItemAdd() plus one ButtonBehavior() registration of the same ID (a single widget
//    that ItemAdd()s, submits another item, then runs ButtonBehavior() — ImGui counts it once
//    too); and everything in the implicit "Debug##Default" window (its own decorations).
//  * Horizontal overflow — content whose rect ends right of its window's InnerClipRect (+1 px),
//    i.e. content the window cuts off. The window's real scrollbar IDs are decoration, checked
//    instead against OuterRectClipped intersected with their submission-time renderer/ancestor
//    clip; a gutter is not content overflow, but chrome exceeding its actual window/host still is.
//    Content filters: windows showing a horizontal scrollbar or scrolled horizontally,
//    hidden/appearing windows and dock-node hosts, the window's own resize grips and borders,
//    items fully outside the clip vertically (scrolled out, title and menu bars), items clipped
//    by a narrower clip rect the code pushed itself (tab bars, canvases), ImGui's own span-to-edge
//    widgets (Selectable/CollapsingHeader end at WorkRect + half the item spacing).
//    Findings need two recorded frames (auto-resize lag).
// Each finding gets its ID-stack path from ImGui's ID Stack Tool machinery (DebugHookIdInfo), driven
// here one stack level per frame for the finding's ID, so no pointer movement is involved.
//
// Two users:
//  * begin()/active()/lastResult(): a bounded audit of the UI as it stands (CHUDWARE_SHOT=simaudit
//    runs one per state).
//  * CHUDWARE_IDAUDIT=1 under any CHUDWARE_SHOT scenario: conflict detection for the whole run.
//    Each conflict prints `idaudit: conflict window=… label=… id=… count=… path=…` once; the final
//    `idaudit: done conflicts=N` prints at ImGui shutdown (or idAuditFinish()), and the process then
//    exits with EXIT_FAILURE when N > 0, so any scenario can be gated on it.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace ui::audit {

// Window classes an audit reports on (bit flags; root windows are classified by name/flags).
enum Region : unsigned {
    RegionLeftPane = 1u << 0,  // FeatureManager##LeftPane: Simulation Manager + every study form
    RegionRibbon   = 1u << 1,  // ##RibbonBar
    RegionViewport = 1u << 2,  // the central Viewport window (3D HUD/legend, Hydraulics canvas)
    RegionFloating = 1u << 3,  // popups, modals and every other floating window (dialogs, HUDs)
    RegionChrome   = 1u << 4,  // full-width strips (title bar, status bar, doc tabs)
    RegionAll      = 0xFFu,
};

struct AuditOptions {
    unsigned regions = RegionAll;
    int frames = 4;           // frames recorded before the findings are final
    int maxFrames = 240;      // cap including ID-stack path resolution; 0 = unlimited
    int maxPathQueries = 16;  // findings that get an ID-stack path (conflicts first)
};

// Start an audit of the next frames. Call between frames (before ImGui::NewFrame), e.g. from a
// scenario tick. False when no ImGui context exists or an audit is running.
bool begin(const std::string& name, const AuditOptions& opt = AuditOptions());

// True until the audit has recorded its frames and resolved its ID-stack paths.
bool active();

// Result of the last finished audit:
// {name, conflicts:[{window, root_window, id, count, label, item_label, id_path, frames,
//                    items:[{window, rect, label}]}],
//  overflows:[{window, root_window, id, rect, clip_x, overflow_px, label, item_label, id_path, frames}],
//  frames, seconds, items_seen, windows_seen, windows:[root window names], truncated}
const nlohmann::json& lastResult();

// Last rendered frame's enabled labeled items, including submitted offscreen items:
// [{id, label, window, visible, rect:[x1,y1,x2,y2], full_rect, clip, openable, opened, text_only}].
// A visible rect is intersected with the real item clip; invisible rects are navigation evidence.
// Rendered status text has id=0/text_only=true and is never an interactive pointer target.
// Disclosure flags come from the final ImGui/kit annotation.
// Collection is shot/audit-only, bounded to 4096 items; ordinary frames allocate nothing.
const nlohmann::json& observedItems();

// Active, visible window clips and their real scrolling state, bounded to 256 windows:
// [{id, window, parent, rect, wheel_point, scroll_y, scroll_max_y, wheel_step_y, wheel_enabled}].
// Shares observedItems()'s opt-in. Used to hover and wheel the actual scrolling ancestor.
// wheel_step_y is the renderer's screen-pixel displacement for one vertical wheel unit.
const nlohmann::json& observedWindows();

// CHUDWARE_IDAUDIT=1 + CHUDWARE_SHOT set: start whole-run conflict detection (idempotent; call once
// per frame from the scenario tick, before ImGui::NewFrame).
void idAuditAutoStart();

// Print the `idaudit: done conflicts=N` summary once (no-op unless idaudit runs) and return N.
int idAuditFinish();

}  // namespace ui::audit
