// Document tab strip — see doc_tabs.h. Renders the open documents as browser-style tabs and routes
// click→switch, X/middle-click→close (with an unsaved-changes prompt), and "+"→New chooser. All
// mutations to AppState::docTabs are DEFERRED until after the strip is fully drawn (we iterate the
// vector while drawing), mirroring the feature tree's s_pendingTreeOps discipline so a switch/close
// never invalidates the loop it fires from.

// Liquid Glass v2 port (host, H6b write-set): tab pills + the whole band read the live
// viewport-backdrop glass surface projected via ui::glassPanel/ui::adaptivePanel (contract:
// local://glass_host.md). Ported from ui/imgui/src/ui/doc_tabs.cpp (drawOneTab / drawDocTabs) —
// see that file for the landed v2 reference. The opaque panelBg fill the band used to paint
// unconditionally is now drawn ONLY as the backdrop-invalid fallback.
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImGuiDockNode + GetWindowDockNode — the doc-tab band spans the node

#include "doc_tabs.h"
#include "layout.h"         // publishDocTabBandBottom — pinned columns anchor below the real band
#include "feature_tree.h"
#include "feature_tree_nested.h"
#include "file_dialog.h"    // saveFileDialog — ECAD Save-As from the unsaved-changes prompt
#include "icons.h"
#include "sim_manager.h"   // flush accepted simulation setup edits before document saves
#include "plm/plm_quickbar.h"  // ui::plmAutoRecordSave — auto-record on save
#include "metrics.h"
#include "anim.h"

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../core/engine_events.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette
#include "ecad/ecad_panel.h"
#include "workspace_home.h"   // the launch starter's tab is hidden on the empty-workspace home

#ifndef __EMSCRIPTEN__
void ecadRecoverySaved(const std::string& docId, int savedRevision);
#endif

namespace ui {

// Same platform close-red the window caption uses (#E81123). An OS-standard destructive affordance
// rather than a theme role, so it must not track dark/light like the rest of the strip.
static const ImVec4 kTabCloseRed(232.0f / 255.0f, 17.0f / 255.0f, 35.0f / 255.0f, 200.0f / 255.0f);

// Deferred actions, applied after the strip finishes drawing (see header note).
static int  s_switchTo   = -1;   // tab index to activate, or -1
static int  s_closeIdx   = -1;   // tab index to close, or -1
// Edge-tab reveal: hovering a lane-clipped tab past the short dwell asks the NEXT frame's layout to
// scroll it fully into view, so the hover highlight never stays dissolved at a lane edge.
static int  s_hoverReveal = -1;
// Wave 2: the "+" New-Document chooser. The clicked menu item records which kind of doc to create;
// the actual create+addTab runs after the strip is drawn (deferred, like switch/close), so it never
// mutates docTabs mid-iteration. -1 = nothing requested this frame.
enum class NewDocKind { None = -1, Part = 0, Assembly, Drawing, KicadProject };
static NewDocKind s_newDoc = NewDocKind::None;
// Right-click tab menu: close every OTHER tab (keep this one), or close all. Deferred + applied
// after the strip is drawn so they never mutate docTabs mid-iteration. -1 / false = nothing asked.
static int  s_closeOthersIdx = -1;
static bool s_closeAll = false;
// A modal can outlive tab-vector reordering; keep the requested document's identity.
struct CloseTabIdentity {
    AppState::DocTab::TabKind kind = AppState::DocTab::TabKind::Part;
    std::string id;
};
static CloseTabIdentity s_confirmClose;
static CloseTabIdentity s_saveClose;
static CloseTabIdentity s_closeConfirmed;
struct DeferredTabClose {
    CloseTabIdentity identity;
    bool askIfDirty = false;
};
static std::vector<DeferredTabClose> s_deferredCloses;

static const std::string& closeTabId(const AppState::DocTab& tab) {
    using K = AppState::DocTab::TabKind;
    if (tab.kind == K::Drawing) return tab.drawingDocId;
    if (tab.kind == K::Schematic || tab.kind == K::PCB) return tab.ecadDocId;
    return tab.docId;
}

static CloseTabIdentity closeTabIdentity(const AppState::DocTab& tab) {
    return {tab.kind, closeTabId(tab)};
}

static int resolveCloseTab(const AppState& st, const CloseTabIdentity& identity) {
    if (identity.id.empty()) return -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        const auto& tab = st.docTabs[i];
        if (tab.kind == identity.kind && closeTabId(tab) == identity.id) return i;
    }
    return -1;
}

static bool hasUnsavedEmbeddedDrawing(const AppState& st, const AppState::DocTab& source) {
    if (source.docId.empty()) return false;
    for (const auto& tab : st.docTabs)
        if (tab.kind == AppState::DocTab::TabKind::Drawing &&
            tab.sourceDocId == source.docId && tab.path.empty() &&
            (tab.dirty || st.hasPendingHumanMutations(tab.drawingDocId)))
            return true;
    return false;
}

static void requestTabClose(AppState& st, const CloseTabIdentity& identity, bool askIfDirty) {
    const int idx = resolveCloseTab(st, identity);
    if (idx < 0) return;
    const auto& tab = st.docTabs[idx];
    if (st.hasPendingHumanMutations(identity.id)) {
        for (const auto& pending : s_deferredCloses)
            if (pending.identity.id == identity.id && pending.identity.kind == identity.kind) return;
        s_deferredCloses.push_back({identity, askIfDirty});
        return;
    }
    if (hasUnsavedEmbeddedDrawing(st, tab)) {
        if (askIfDirty)
            st.toast(ui::ToastLevel::Warn, "Save or close this part's unsaved drawings first");
        return;
    }
    if (tab.dirty) {
        if (askIfDirty) s_confirmClose = identity;
        return;
    }
    st.closeDocTab(idx);
}
// Wave 2: set true to request the New-Document chooser popup open this frame (the "+" button click,
// or a Ctrl+N hook from app.cpp via openNewDocChooser()). Consumed inside drawDocTabs.
static bool s_openChooser = false;

// ---- horizontal scroll + overflow edge treatment (file-display strip) ----
// The strip never wraps. When the open tabs overflow the visible width we scroll them horizontally
// and reserve a dedicated "+N" glass chip after the last visible tab (and before the first when
// scrolled) so the count never paints over a filename. The lane always shows whole tabs (it scrolls
// in tab steps); only a tab wider than the lane is cut, with a clean alpha fade at the cut edge.
static float s_scrollX = 0.0f;   // px of content hidden past the LEFT edge; a tab boundary
// Reveal only on selection/content/strip-size changes, not on every frame: browsing other tabs
// with the wheel or overflow chips must be allowed to move away from the selected document.
static CloseTabIdentity s_visibleActive;
static float s_visibleActiveOffset = -1.0f;
static float s_visibleActiveWidth = -1.0f;
static float s_visibleStripW = -1.0f;
// Overflow never changes the strip's height: there is no scrollbar lane. Scrolling is the wheel,
// the "+N" chips, and click/hover-reveal of a clipped edge tab; position feedback is a hairline
// thumb laid INSIDE the band's bottom edge while the strip is hovered (see drawDocTabs).
// Trailing "+" button slot: lead gap (x2) + button + trailing inset (x1), in units of the
// inter-tab gap. Reserved out of the tab lane up front in BOTH the window sizing and the layout,
// so the button is never pushed past the window / Copilot column edge.
static constexpr float kBtnLeadGaps = 2.0f;
static constexpr float kBtnTrailGaps = 1.0f;

// Forward decls (definitions below drawDocTabs, which calls them).
void createNewDoc(AppState& st, int kindOrd);
void openNewDocChooser();

static const char* docIconName(const AppState::DocTab& t) {
    using K = AppState::DocTab::TabKind;
    switch (t.kind) {
        case K::Assembly: return "assembly";
        case K::Drawing: return "section_props";
        case K::Schematic: return "ai_panel";
        case K::PCB: return "ai_panel";
        case K::Part:
        default: return "part";
    }
}


// A single tab, placed explicitly at screen position (x, y0). Returns the tab WIDTH so the caller
// can advance to the next tab. Records a deferred switch/close into the file statics. Layout:
// [ title (dirty *) × ]. The whole pill switches; the × hot-zone (submitted on top) closes.
// How visible a clipped tab's high-contrast edge treatments (accent outline, AI-pulse glow) may
// be: 1.0 when the tab is fully inside the lane, ramping to 0.0 as the clipped edge reaches
// fadeW past the lane edge. Without this, a sliced accent outline / glow ring paints a bright
// vertical fleck right at the clip boundary that the (max-0.62-alpha) dissolve fade can't cover.
static float tabEdgeFade(float p0x, float p1x, float clipL, float clipR, float fadeW) {
    if (fadeW < 0.5f) return 1.0f;
    float a = 1.0f;
    if (p0x < clipL - 0.5f)
        a = std::min(a, 1.0f - ImClamp((clipL - p0x) / fadeW, 0.0f, 1.0f));
    if (p1x > clipR + 0.5f)
        a = std::min(a, 1.0f - ImClamp((p1x - clipR) / fadeW, 0.0f, 1.0f));
    return a;
}

// clipL/clipR bound the visible tab lane (the outer PushClipRect); fadeW is the edge-dissolve
// width. A tab clipped by a lane edge fades its right-edge separator out over that zone so the
// separator never reads as a bright vertical fleck where the title is truncated.
static float drawOneTab(AppState& st, int idx, float x, float y0, float barH,
                        const ui::AdaptiveSurface& surf,
                        float clipL, float clipR, float fadeW) {
    const auto& pal = ui::palette;
    AppState::DocTab& t = st.docTabs[idx];
    const bool active = (idx == st.activeTab);
    float s = ui::uiScale;

    ImGui::PushID(idx);

    std::string label = t.displayTitle();   // filename-first (real file name, not internal "Part1")
    if (t.dirty) label += " *";

    ImDrawList* dl = ImGui::GetWindowDrawList();
    float padX = 12.0f * s;
    float closeW = 18.0f * s;
    float iconSz = 16.0f * s;
    float iconGap = 6.0f * s;
    float textW = ImGui::CalcTextSize(label.c_str()).x;
    float tabW = padX + iconSz + iconGap + textW + 6.0f * s + closeW + padX * 0.5f;
    float tabH = barH;

    ImVec2 p0(x, y0);
    ImVec2 p1(p0.x + tabW, p0.y + tabH);
    const ImRect tabRect(p0, p1);
    // Dissolve factor for this tab's high-contrast edge strokes when a lane edge clips it.
    const float edgeA = tabEdgeFade(p0.x, p1.x, clipL, clipR, fadeW);

    // Whole-pill click target (switch). The × button sits on top and consumes its own clicks — but
    // ImGui occludes a later item by an earlier overlapping one unless the underlying item opts in,
    // so allow the pill to be overlapped or the × (submitted below) never receives its clicks.
    ImGui::SetCursorScreenPos(p0);
    ImGui::SetNextItemAllowOverlap();
    bool switchClicked = ImGui::InvisibleButton("##tab", ImVec2(tabW, tabH));
    bool hovered = ImGui::IsItemHovered();
    bool middleClose = hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Middle);

    // Hovering a lane-clipped edge tab: past the short dwell, scroll it fully into view next frame
    // so its hover highlight isn't left half-dissolved at the clip edge.
    if (hovered && (p0.x < clipL - 0.5f || p1.x > clipR + 0.5f) &&
        ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort))
        s_hoverReveal = idx;

    // Right-click context menu (attaches to the ##tab item above). "Close Others" is the headline
    // request; Close / Close All round it out. Actions are deferred via the same statics the ×/middle
    // paths use so nothing mutates docTabs mid-draw.
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem("##tabctx")) {
        const int n = (int)st.docTabs.size();
        if (ImGui::MenuItem("Close")) {
            const bool aiSilent = t.aiHandoff && !t.userAdopted;
            if (t.dirty && !aiSilent) s_confirmClose = closeTabIdentity(t); else s_closeIdx = idx;
        }
        if (ImGui::MenuItem("Close Others", nullptr, false, n > 1)) s_closeOthersIdx = idx;
        ImGui::Separator();
        if (ImGui::MenuItem("Close All")) s_closeAll = true;
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();

    // Shared pill geometry for fill + outline (same radius, same flags, same rect). Mismatched
    // inset/radius between glassPanel and a later AddRect was the source of the corner seam/step
    // artifacts where the rounded corner begins.
    const float kTabRdp = 4.0f;
    const float kTabR   = kTabRdp * s;

    // Glass pill: active = Regular material, inactive = UltraThin, hover lifts an inactive tab to
    // Thin (per-rect UV-projected live backdrop; glassBackground fallback when no backdrop is live).
    ui::GlassLevel level = active ? ui::GlassLevel::Regular : ui::GlassLevel::UltraThin;
    if (hovered && !active) level = ui::GlassLevel::Thin;

    // AI-activity glow: a warm fill tint + breathing accent outline ON the pill rect of a tab a
    // subagent is editing in the background (never the active tab — viewing it clears the pulse).
    // Everything stays strictly ON/INSIDE the pill geometry — the old expanding halo rings painted
    // up to ~7px OUTSIDE the pill, so the overflow lane clip sliced them into bright vertical line
    // fragments in the gaps between tabs and at the strip edges (the recurring "glowing line"
    // artifact). Nothing here may paint outside (p0, p1).
    const float pulsePhase = 0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 3.2f);  // ~2 Hz breath
    if (t.aiPulse && !active) {
        if (ImGui::IsRectVisible(p0, p1)) anim::requestFramesAt(30.0f);
        const float ar = pal.accent[0], ag = pal.accent[1], ab = pal.accent[2];
        // Warm fill tint — same rounded rect the glass pill will occupy (no square fill under curves).
        dl->AddRectFilled(p0, p1,
                          ImGui::ColorConvertFloat4ToU32(
                              ImVec4(ar, ag, ab, (0.06f + 0.10f * pulsePhase) * edgeA)),
                          kTabR);
    }

    // Only the ACTIVE tab (or a hovered / AI-pulsing one) paints its own glass-pill background.
    // Inactive middle tabs stay transparent against the shared band so no stray rounded background
    // bar shows behind them — the effect belongs to the active tab + the "+N" overflow chips, not to
    // every file title (a background was displaying where it isn't supposed to be).
    if (active || hovered || t.aiPulse)
        ui::glassPanel(dl, tabRect, level, kTabRdp /*dp — glassPanel scales by uiScale*/, surf,
                       edgeA /*clipped pill dissolves its fill/border with the title*/);
    if (t.aiPulse && !active) {
        // Breathing outline ON TOP of the pill, exact pill geometry. edgeA dissolves it with the
        // clipped pill at a lane edge.
        dl->AddRect(p0, p1,
                    ImGui::ColorConvertFloat4ToU32(
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2],
                               (0.25f + 0.45f * pulsePhase) * edgeA)),
                    kTabR, 0, std::max(1.0f, 1.5f * s));
    }
    if (active) {
        // Accent outline on the EXACT same rounded rect as the glass fill (no inset / no radius
        // mismatch). Stroke sits on the pill edge so corner AA of fill + outline share one geometry.
        // edgeA dissolves the outline when a lane edge clips the active tab — a full-strength
        // accent stroke sliced at the clip boundary reads as a bright vertical line.
        float oc[4] = {pal.accent[0], pal.accent[1], pal.accent[2], pal.accent[3] * edgeA};
        dl->AddRect(p0, p1,
                    ui::Palette::toU32(oc),
                    kTabR,
                    0,
                    std::max(1.0f, 1.5f * s));
    } else if (!t.aiPulse) {  // thin separator on the right edge of inactive tabs (inset from pill
        // corners) — pulsing tabs already carry their own pill outline, a separator would double it.
        // Fade the separator out inside the lane-edge dissolve zone: a tab clipped by the strip
        // edge (or one whose right edge sits within the fade) otherwise paints a bright vertical
        // line right where the title is truncated.
        float sepA = 1.0f;
        if (fadeW > 0.5f) {
            if (p0.x < clipL - 0.5f)
                sepA = std::min(sepA, ImClamp((p1.x - clipL) / fadeW, 0.0f, 1.0f));
            if (p1.x > clipR + 0.5f)
                sepA = std::min(sepA, ImClamp((clipR - p1.x + fadeW) / fadeW, 0.0f, 1.0f));
        }
        if (sepA > 0.01f) {
            float sc[4] = {pal.border[0], pal.border[1], pal.border[2], pal.border[3] * sepA};
            dl->AddLine(ImVec2(p1.x - 0.5f, p0.y + kTabR), ImVec2(p1.x - 0.5f, p1.y - kTabR),
                        ui::Palette::toU32(sc), 1.0f);
        }
    }

    // Title text + kind icon — max-contrast glass surface tones (theme-independent vibrancy ramp).
    // Clip to the pill AABB so close-button / label never paint past the tab edge under overflow.
    dl->PushClipRect(p0, p1, true);
    const ImU32 textCol = ImGui::ColorConvertFloat4ToU32(active ? surf.textPrimary : surf.textSecondary);
    const float iy = p0.y + (tabH - iconSz) * 0.5f;
    // drawIconGlass sizePx is the FULL square size (not radius). Multi-color raster tiles
    // (part/assembly/…) keep white vertex color so atlas shading survives; SDF line-art
    // glyphs take the surface text role so they match the label.
    {
        const char* iname = docIconName(t);
        const ImU32 icol = ui::hasIconSdf(iname)
            ? textCol
            : IM_COL32(255, 255, 255, (active ? 255 : 210));
        ui::drawIconGlass(dl, iname,
                          ImVec2(p0.x + padX + iconSz * 0.5f, iy + iconSz * 0.5f),
                          iconSz, icol, ui::glassIconHalo(surf));
    }
    dl->AddText(ImVec2(p0.x + padX + iconSz + iconGap,
                       p0.y + (tabH - ImGui::GetFontSize()) * 0.5f),
                textCol, label.c_str());

    // × close button — submitted AFTER (on top of) the pill so ImGui routes clicks in this zone to
    // it, not to the switch button underneath.
    float cx = p1.x - closeW - padX * 0.4f;
    ImGui::SetCursorScreenPos(ImVec2(cx, p0.y + (tabH - closeW) * 0.5f));
    bool xClicked = ImGui::InvisibleButton("##x", ImVec2(closeW, closeW));
    bool xHover = ImGui::IsItemHovered();
    ImVec2 xc(cx + closeW * 0.5f, p0.y + tabH * 0.5f);
    if (xHover)
        dl->AddCircleFilled(xc, closeW * 0.42f, ImGui::ColorConvertFloat4ToU32(kTabCloseRed));
    // Crisp × : slightly longer arms + thicker stroke so it doesn't read as a broken fleck
    // next to the doc-type icon at HiDPI.
    float g = closeW * 0.26f;
    const ImU32 xCol = ImGui::ColorConvertFloat4ToU32(xHover ? surf.textPrimary : surf.textSecondary);
    const float xThick = std::max(1.5f, 1.8f * s);
    dl->AddLine(ImVec2(xc.x - g, xc.y - g), ImVec2(xc.x + g, xc.y + g), xCol, xThick);
    dl->AddLine(ImVec2(xc.x - g, xc.y + g), ImVec2(xc.x + g, xc.y - g), xCol, xThick);
    dl->PopClipRect();

    if (hovered && !t.path.empty())
        ImGui::SetTooltip("%s%s", t.path.c_str(), t.dirty ? "  (unsaved changes)" : "");
    else if (hovered && t.dirty)
        ImGui::SetTooltip("Unsaved changes");

    // Route interactions (deferred).
    if (xClicked || middleClose) {
        s_closeIdx = idx;
    } else if (switchClicked) {
        s_hoverReveal = idx;  // also reveal a clipped tab that was already selected
        if (!active) s_switchTo = idx;
    }

    ImGui::PopID();
    return tabW;
}

// Width a tab occupies — same formula drawOneTab uses, factored so the scroll layout can measure the
// whole strip up front without drawing it.
static float measureTabW(const AppState::DocTab& t, float s) {
    std::string label = t.displayTitle();
    if (t.dirty) label += " *";
    const float padX = 12.0f * s, closeW = 18.0f * s, iconSz = 16.0f * s, iconGap = 6.0f * s;
    return padX + iconSz + iconGap + ImGui::CalcTextSize(label.c_str()).x + 6.0f * s + closeW +
           padX * 0.5f;
}

// Width of the "+N" overflow chip for a given hidden count (text + horizontal padding).
static float measureOverflowChipW(int count, float s) {
    char lbl[16];
    std::snprintf(lbl, sizeof(lbl), "+%d", count < 0 ? 0 : count);
    return ImGui::CalcTextSize(lbl).x + 16.0f * s;
}

// Clean alpha fade at a truncated tab edge — neutral surface tone ramping to transparent, NOT a
// saturated accent gradient block. `fromRight` true: opaque at the right edge, transparent leftward.
//
// AddRectFilledMultiColor is SQUARE-cornered. Drawn full-height ON the band's outer rounded
// outline it produced the bright vertical strip / square-corner fleck (same class of bug as
// coloredRowWash / ribbon_vGrad — MultiColor poking past the rounded glass outline, plus a
// near-opaque light-tint wall at the solid edge). So: pass cornerR = the band's corner radius
// when the fade edge coincides with that rounded outline (inset kept), and cornerR = 0 when the
// fade edge is INTERIOR to the band (a "+N" chip owns that side) — there the fade deliberately
// runs the full pill height so the truncated tab's border/separator/shadow dissolve along with
// the title instead of leaving a sliced pill edge with a text-only fade. Either way the ramp is
// a soft two-stop dissolve, not a bright bar.
static void drawTabEdgeFade(ImDrawList* dl, float edgeX, float y0, float y1, float fadeW,
                            bool fromRight, const ui::AdaptiveSurface& surf, float cornerR) {
    if (fadeW < 1.0f) return;
    const float lo = fromRight ? (edgeX - fadeW) : edgeX;
    const float hi = fromRight ? edgeX : (edgeX + fadeW);
    if (hi - lo < 1.0f) return;

    const float H = y1 - y0;
    // MultiColor is square-cornered — pull it off the band's outer rounded outline so it never
    // leaves bright corner flecks (same class of fix as coloredRowWash). A light vertical inset
    // is enough: the text baseline sits in the middle of the bar, and over-insetting by the full
    // corner radius left hard-clipped glyphs at the top/bottom of the fade.
    const float rnd = ImClamp(std::min(cornerR, 3.0f * (H / 26.0f)), 0.0f, H * 0.35f);
    const float yA = y0 + rnd;
    const float yB = y1 - rnd;
    if (yB <= yA + 0.5f) return;

    // Match the band's glass scrim so the fade reads as the title dissolving, not a blue smear.
    // Cap well below opaque — near-1 alpha on a light-theme tint was the bright vertical strip.
    const ImVec4 base = surf.tint;
    const float aEdge = std::min(0.62f, 0.28f + surf.scrim * 0.55f);
    const float aMid  = aEdge * 0.38f;
    const ImU32 cSolid = ImGui::ColorConvertFloat4ToU32(ImVec4(base.x, base.y, base.z, aEdge));
    const ImU32 cMid   = ImGui::ColorConvertFloat4ToU32(ImVec4(base.x, base.y, base.z, aMid));
    const ImU32 cClear = ImGui::ColorConvertFloat4ToU32(ImVec4(base.x, base.y, base.z, 0.0f));

    // Two-stop L→R (or R→L) ramp: solid at the scroll edge, soft mid, clear inward. Interior only
    // — never a full-height square rect that meets the band's rounded corners.
    const float mid = fromRight ? (edgeX - fadeW * 0.42f) : (edgeX + fadeW * 0.42f);
    if (fromRight) {
        if (mid > lo + 0.5f)
            dl->AddRectFilledMultiColor(ImVec2(lo, yA), ImVec2(mid, yB), cClear, cMid, cMid, cClear);
        if (hi > mid + 0.5f)
            dl->AddRectFilledMultiColor(ImVec2(mid, yA), ImVec2(hi, yB), cMid, cSolid, cSolid, cMid);
    } else {
        if (mid > lo + 0.5f)
            dl->AddRectFilledMultiColor(ImVec2(lo, yA), ImVec2(mid, yB), cSolid, cMid, cMid, cSolid);
        if (hi > mid + 0.5f)
            dl->AddRectFilledMultiColor(ImVec2(mid, yA), ImVec2(hi, yB), cMid, cClear, cClear, cMid);
    }
}

// Dedicated overflow "+N" glass chip in its own reserved slot (never painted over a tab title).
// Click pages the lane toward the hidden side. Tooltip lists the hidden document titles when cheap.
// Returns true when the chip is clicked.
static bool drawOverflowChip(ImDrawList* dl, float x, float y0, float barH, float chipW,
                             int count, const ui::AdaptiveSurface& surf, float s,
                             const char* id, const std::vector<std::string>& hiddenTitles) {
    // Full tab-pill height (no vertical inset) so the chip's glass outline lines up with the tab
    // pills beside it — a shorter chip made the whole control read as vertically off.
    const ImVec2 p0(x, y0);
    const ImVec2 p1(x + chipW, y0 + barH);
    const ImRect chipRect(p0, p1);
    const float kChipRdp = 4.0f;

    ImGui::PushID(id);
    ImGui::SetCursorScreenPos(p0);
    const bool clicked = ImGui::InvisibleButton("##ovf", ImVec2(chipW, p1.y - p0.y));
    const bool hovered = ImGui::IsItemHovered();

    // Glass-style pill via the theme helpers (fill + border share one rounded geometry).
    ui::glassPanel(dl, chipRect,
                   hovered ? ui::GlassLevel::Thin : ui::GlassLevel::UltraThin,
                   kChipRdp, surf);
    // Soft accent wash so the chip reads as an overflow control without the old flame smear.
    {
        const float ar = ui::palette.accent[0], ag = ui::palette.accent[1], ab = ui::palette.accent[2];
        const float a = hovered ? 0.28f : 0.16f;
        dl->AddRectFilled(p0, p1,
                          ImGui::ColorConvertFloat4ToU32(ImVec4(ar, ag, ab, a)),
                          kChipRdp * s);
        dl->AddRect(p0, p1,
                    ImGui::ColorConvertFloat4ToU32(ImVec4(ar, ag, ab, hovered ? 0.70f : 0.45f)),
                    kChipRdp * s, 0, std::max(1.0f, 1.0f * s));
    }

    char lbl[16];
    std::snprintf(lbl, sizeof(lbl), "+%d", count < 0 ? 0 : count);
    const ImVec2 ts = ImGui::CalcTextSize(lbl);
    // Horizontally centered (measureOverflowChipW pads both sides equally); vertically use the
    // SAME baseline formula drawOneTab uses for tab titles so "+N" sits on the exact visual line
    // of the filenames beside it rather than being centered against an inset chip rect.
    const float tx = p0.x + (chipW - ts.x) * 0.5f;
    const float ty = y0 + (barH - ImGui::GetFontSize()) * 0.5f;
    dl->AddText(ImVec2(tx, ty), ImGui::ColorConvertFloat4ToU32(surf.textPrimary), lbl);

    if (hovered) {
        // Cheap title list for the hidden docs (cap so a huge strip stays readable).
        std::string tip;
        tip.reserve(64 + hiddenTitles.size() * 24);
        tip += std::to_string(count);
        tip += count == 1 ? " more open document — click to reveal\n" : " more open documents — click to reveal\n";
        const int show = std::min((int)hiddenTitles.size(), 12);
        for (int i = 0; i < show; ++i) {
            tip += "• ";
            tip += hiddenTitles[(size_t)i];
            if (i + 1 < show) tip += '\n';
        }
        if ((int)hiddenTitles.size() > show) {
            tip += "\n… +";
            tip += std::to_string((int)hiddenTitles.size() - show);
            tip += " more";
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
    ImGui::PopID();
    return clicked;
}

float docTabStripHeight(const AppState& st) {
    // Constant: drawDocTabs decides per frame whether anything shows (the workspace home hides a
    // strip that would hold only the "+", since the home card carries the same actions).
    (void)st;
    return 26.0f * ui::uiScale;
}

// Index of the Part/Assembly tab that owns worker doc `docId`, or -1. Used to resolve a Drawing's
// source part (a drawing is persisted by saving that part's .step+).
static int partTabIndexForDoc(AppState& st, const std::string& docId) {
    using K = AppState::DocTab::TabKind;
    if (docId.empty()) return -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if ((st.docTabs[i].kind == K::Part || st.docTabs[i].kind == K::Assembly) &&
            st.docTabs[i].docId == docId)
            return i;
    return -1;
}

struct PendingCadSave {
    // Callbacks include later requests waiting for this save to finish.
    std::vector<TabSaveDone> callbacks;
};
static std::map<std::string, PendingCadSave> s_pendingCadSaves;

static TabSaveResult finishSave(TabSaveDone& done, TabSaveResult result) {
    if (done) done(result);
    return result;
}

static bool sameSaveDestination(const std::string& a, const std::string& b) {
    if (a.empty() || b.empty()) return false;
    if (a == b || ui::featureTreeCanonicalPath(a) == ui::featureTreeCanonicalPath(b)) return true;
    std::error_code ec;
    return std::filesystem::equivalent(std::filesystem::u8path(a), std::filesystem::u8path(b), ec) && !ec;
}

TabSaveResult saveCadDocument(AppState& st, const std::string& docId,
                              const std::string& path, bool automatic, TabSaveDone done) {
    if (docId.empty() || path.empty() || !st.workerStarted) {
        st.addLog("[tabs] save failed: document, destination, or engine unavailable");
        st.toast(ui::ToastLevel::Error, "Save failed: document or destination unavailable");
        return finishSave(done, TabSaveResult::Failed);
    }
    bool overwriteChanged = false;
    nlohmann::json expectedDiskStamp;
    for (const auto& tab : st.docTabs) {
        if (tab.docId != docId) continue;
        const bool samePath = sameSaveDestination(path, tab.path) ||
                              sameSaveDestination(path, tab.conflictPath);
        if (tab.diskConflict && (automatic || samePath)) {
            st.toast(ui::ToastLevel::Warn, "File changed on disk. Choose Reload or Keep mine first.");
            return finishSave(done, TabSaveResult::Failed);
        }
        overwriteChanged = samePath && tab.overwriteDiskConflict;
        if (overwriteChanged) expectedDiskStamp = tab.diskConflictStamp;
        break;
    }
    auto existing = s_pendingCadSaves.find(docId);
    if (existing != s_pendingCadSaves.end()) {
        existing->second.callbacks.push_back(
            [&st, docId, path, automatic, done = std::move(done)](TabSaveResult) mutable {
                saveCadDocument(st, docId, path, automatic, std::move(done));
            });
        return TabSaveResult::Pending;
    }
    std::map<std::string, uint64_t> humanGenerations;
    for (const auto& tab : st.docTabs) {
        if (tab.docId == docId)
            humanGenerations[docId] = tab.humanEditGeneration;
        else if (tab.kind == AppState::DocTab::TabKind::Drawing &&
                 tab.sourceDocId == docId && tab.path.empty())
            humanGenerations[tab.drawingDocId] = tab.humanEditGeneration;
    }
    PendingCadSave pending;
    if (done) pending.callbacks.push_back(std::move(done));
    s_pendingCadSaves.emplace(docId, std::move(pending));
    nlohmann::json params = {{"doc_id", docId}, {"path", path}};
    if (automatic) params["if_no_human_dirty"] = true;
    if (overwriteChanged) {
        params["overwrite_changed"] = true;
        params["expected_disk_stamp"] = expectedDiskStamp;
    }
    ++st.rebuildsInFlight;
    st.rpc.call("doc_save", std::move(params),
        [&st, docId, path, automatic, humanGenerations = std::move(humanGenerations)](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            TabSaveResult outcome = TabSaveResult::Failed;
            const bool valid = r.ok && r.result.is_object() &&
                !r.result.contains("code") && r.result.value("ok", true) &&
                r.result.contains("saved_revision") && r.result["saved_revision"].is_number_integer();
            if (!valid && automatic && r.result.is_object() &&
                r.result.value("code", std::string()) == "human_dirty") {
                st.addLog("[tabs] automatic save skipped for " + docId + ": unsaved human edits");
                outcome = TabSaveResult::Nothing;
            } else if (!valid && r.result.is_object() &&
                       r.result.value("code", std::string()) == "disk_changed") {
                bool humanConflict = !automatic || st.hasPendingHumanMutations(docId);
                for (auto& tab : st.docTabs) {
                    if (tab.docId != docId) continue;
                    humanConflict = humanConflict || tab.dirty;
                    if (humanConflict) {
                        tab.diskConflict = true;
                        tab.conflictPath.clear();
                        tab.overwriteDiskConflict = false;
                        tab.diskConflictStamp = r.result.value("observed_stamp", nlohmann::json());
                    }
                    break;
                }
                st.addLog("[tabs] save blocked because the file changed on disk: " + path);
                st.toast(ui::ToastLevel::Warn, humanConflict
                    ? "File changed on disk. Choose Reload or Keep mine first."
                    : "Automatic save could not continue because the file changed on disk.");
            } else if (!valid) {
                const std::string message = !r.ok ? r.err : r.result.is_object()
                    ? r.result.value("message", r.result.value("code", std::string("Save returned no saved revision")))
                    : "Invalid save response";
                st.addLog("[tabs] save failed for " + docId + " (" + path + "): " + message);
                st.toast(ui::ToastLevel::Error, "Save failed: " + message);
            } else {
                const int revision = r.result["saved_revision"].get<int>();
                const std::string saved = r.result.value("path", path);
                for (auto& tab : st.docTabs) {
                    if (tab.docId != docId) continue;
                    if (tab.diskConflict && !sameSaveDestination(saved, tab.path) &&
                        !sameSaveDestination(saved, tab.conflictPath)) {
                        if (!tab.conflictPath.empty()) tab.aiHandoff = false;
                        tab.diskConflict = false;
                        tab.diskConflictStamp = nullptr;
                        tab.conflictPath.clear();
                    }
                    tab.overwriteDiskConflict = false;
                    tab.path = saved;
                    const auto generation = humanGenerations.find(docId);
                    const bool unchanged = generation == humanGenerations.end()
                        ? !tab.dirty : generation->second == tab.humanEditGeneration;
                    if (r.result.value("human_dirty", false))
                        st.applyHumanDirty(docId, true);
                    else if (unchanged && (!automatic || !tab.dirty))
                        st.markSaved(docId, revision, saved);
                    break;
                }
                // Only drawings whose actual destination is this embedded archive
                // are certified, and only at the exact revisions it captured.
                const auto drawings = r.result.find("drawing_saved_revisions");
                if (drawings != r.result.end() && drawings->is_object()) {
                    for (const auto& tab : st.docTabs) {
                        if (tab.kind != AppState::DocTab::TabKind::Drawing ||
                            tab.sourceDocId != docId || !tab.path.empty()) continue;
                        const auto item = drawings->find(tab.drawingDocId);
                        const auto generation = humanGenerations.find(tab.drawingDocId);
                        const bool unchanged = generation == humanGenerations.end()
                            ? !tab.dirty : generation->second == tab.humanEditGeneration;
                        if (item != drawings->end() && item->is_number_integer() && unchanged &&
                            (!automatic || !tab.dirty))
                            st.markSaved(tab.drawingDocId, item->get<int>(), {});
                    }
                }
                ui::featureTreeSetSavedPath(st, docId, saved);
                st.pushRecentFile(saved);
                ui::plmAutoRecordSave(st, saved);
                ui::boxMirrorNotifySave(st, saved);
                st.addLog(std::string("[tabs] ") + (automatic ? "auto-saved " : "saved ") + saved);
                const std::string warning = r.result.value("warning", std::string());
                if (!warning.empty()) st.addLog("[tabs] " + warning);
                if (!automatic) {
                    if (!warning.empty()) st.toast(ui::ToastLevel::Warn, warning);
                    else st.toast(ui::ToastLevel::Info, "Saved " + saved);
                }
#ifdef __EMSCRIPTEN__
                if (!automatic) ui::afterFileWritten(saved);
#endif
                outcome = TabSaveResult::Saved;
            }
            auto it = s_pendingCadSaves.find(docId);
            std::vector<TabSaveDone> callbacks;
            if (it != s_pendingCadSaves.end()) {
                callbacks = std::move(it->second.callbacks);
                s_pendingCadSaves.erase(it);
            }
            for (auto& callback : callbacks) callback(outcome);
        }, core::kLongEngineCallTimeoutMs);
    return TabSaveResult::Pending;
}

TabSaveResult saveDocTab(AppState& st, int idx, bool forceDialog, TabSaveDone done) {
    if (idx < 0 || idx >= (int)st.docTabs.size())
        return finishSave(done, TabSaveResult::Nothing);
    using K = AppState::DocTab::TabKind;
    const K kind = st.docTabs[idx].kind;
    if (kind == K::Part || kind == K::Assembly) {
        const std::string docId = st.docTabs[idx].docId;
        const auto session = st.docTabs[idx].session;
        if (session && session->sim && !session->sim->flushPendingEdits(st, docId)) {
            st.toast(ui::ToastLevel::Error, "Save failed: retry the failed simulation setup edit");
            return finishSave(done, TabSaveResult::Failed);
        }
        idx = partTabIndexForDoc(st, docId);
        if (idx < 0) return finishSave(done, TabSaveResult::Nothing);
        const auto& tab = st.docTabs[idx];
        if (tab.diskConflict && !forceDialog) {
            st.toast(ui::ToastLevel::Warn, "File changed on disk. Choose Reload or Keep mine first.");
            return finishSave(done, TabSaveResult::Failed);
        }
        std::string path = tab.path;
        if (forceDialog || path.empty()) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
                st.toast(ui::ToastLevel::Info, "File dialogs disabled");
                return finishSave(done, TabSaveResult::Cancelled);
            }
            const std::string name = tab.displayTitle().empty() ? "Part" : tab.displayTitle();
            path = ui::saveFileDialog(
                "Chudware part (*.step+)\0*.step+\0All files (*.*)\0*.*\0",
                "step+", name.c_str());
            if (path.empty()) return finishSave(done, TabSaveResult::Cancelled);
        }
        return saveCadDocument(st, docId, path, false, std::move(done));
    }

    AppState::DocTab& t = st.docTabs[idx];
    if (kind == K::Schematic || kind == K::PCB) {
        if (t.ecadDocId.empty()) return finishSave(done, TabSaveResult::Nothing);
        std::string path = t.path;
        const bool choosePath = forceDialog || path.empty();
        if (choosePath) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS"))
                return finishSave(done, TabSaveResult::Cancelled);
            const std::string name = std::filesystem::u8path(t.displayTitle()).stem().u8string();
            const bool sch = kind == K::Schematic;
            path = ui::saveFileDialog(
                sch ? "KiCad schematic (*.kicad_sch)\0*.kicad_sch\0"
                    : "KiCad board (*.kicad_pcb)\0*.kicad_pcb\0",
                sch ? "kicad_sch" : "kicad_pcb", name.empty() ? "design" : name.c_str());
            if (path.empty()) return finishSave(done, TabSaveResult::Cancelled);
        }
        const std::string id = t.ecadDocId;
        nlohmann::json params = {{"doc_id", id}}, res;
        if (choosePath) params["path"] = path;
        std::string err;
        if (!st.worker.call("ecad_save", params, res, err) || !res.is_object() ||
            res.contains("code") || !res.contains("saved_revision") ||
            !res["saved_revision"].is_number_integer()) {
            const std::string code = res.is_object() ? res.value("code", std::string()) : std::string();
            const std::string msg = !err.empty() ? err : res.is_object()
                ? res.value("message", std::string("Save returned no saved revision")) : "Invalid save response";
            st.addLog("[tabs] ecad_save failed for " + id + ": " + msg);
            if (code == "kicad_locked")
                st.toast(ui::ToastLevel::Warn, "KiCad has this file open. Close it there, or use Save As.");
            else if (code == "external_change")
                st.toast(ui::ToastLevel::Warn, "The KiCad file changed on disk. Reload it or use Save As.");
            else if (code == "needs_path")
                st.toast(ui::ToastLevel::Warn, "Choose a KiCad file with Save As before saving.");
            else
                st.toast(ui::ToastLevel::Error, "Save failed: " + msg);
            return finishSave(done, TabSaveResult::Failed);
        }
        const std::string saved = res.value("path", path);
        st.markSaved(id, res["saved_revision"].get<int>(), saved);
        t.title = std::filesystem::u8path(saved).filename().u8string();
        std::filesystem::path project = std::filesystem::u8path(saved);
        project.replace_extension(".kicad_pro");
        if (t.ecadProjectPath != project.u8string()) {
            t.ecadProjectSchPath.clear();
            t.ecadProjectPcbPath.clear();
        }
        t.ecadProjectPath = project.u8string(); // ecad_save creates the companion project
        st.syncEcadProjectFiles(t.ecadProjectPath);
        t.ecadFileChanged = false;
        t.ecadSessionDirty = false;
        t.ecadKicadLocked = false;
        ui::ecadSetFileState(id, saved, t.dirty, false, false);
#ifndef __EMSCRIPTEN__
        ecadRecoverySaved(id, res["saved_revision"].get<int>());
#endif
        st.pushRecentFile(saved);
        st.addLog("[tabs] ecad saved -> " + saved);
        ui::plmAutoRecordSave(st, saved);
        ui::boxMirrorNotifySave(st, saved);
        return finishSave(done, TabSaveResult::Saved);
    }

    if (kind == K::Drawing) {
        const std::string drawingId = t.drawingDocId;
        if (st.hasPendingHumanMutations(drawingId)) {
            st.rpc.call("doc_list", nlohmann::json::object(),
                [&st, drawingId, forceDialog, done = std::move(done)](const core::RpcResult& r) mutable {
                    if (!r.ok || (r.result.is_object() && r.result.contains("code"))) {
                        st.toast(ui::ToastLevel::Error, "Could not wait for the drawing edit before saving");
                        finishSave(done, TabSaveResult::Failed);
                        return;
                    }
                    const int current = resolveCloseTab(st, {K::Drawing, drawingId});
                    if (current < 0) {
                        finishSave(done, TabSaveResult::Nothing);
                        return;
                    }
                    saveDocTab(st, current, forceDialog, std::move(done));
                });
            return TabSaveResult::Pending;
        }
        const int source = partTabIndexForDoc(st, t.sourceDocId);
        if (t.path.empty() && !forceDialog && source >= 0)
            return saveDocTab(st, source, false, std::move(done));
        std::string path = t.path;
        if (forceDialog || path.empty()) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS"))
                return finishSave(done, TabSaveResult::Cancelled);
            const std::string name = t.displayTitle();
            path = ui::saveFileDialog(
                "Chudware drawing (*.draw+)\0*.draw+\0All files (*.*)\0*.*\0",
                "draw+", name.empty() ? "drawing" : name.c_str());
            if (path.empty()) return finishSave(done, TabSaveResult::Cancelled);
        }
        const std::string id = t.drawingDocId;
        nlohmann::json res;
        std::string err;
        if (!st.worker.call("drawing_save_standalone", {{"drawing_id", id}, {"path", path}},
                            res, err) || !res.is_object() || res.contains("code") ||
            !res.contains("saved_revision") || !res["saved_revision"].is_number_integer()) {
            const std::string msg = !err.empty() ? err : res.is_object()
                ? res.value("message", std::string("Save returned no saved revision")) : "Invalid save response";
            st.addLog("[tabs] drawing save failed for " + id + ": " + msg);
            st.toast(ui::ToastLevel::Error, "Save failed: " + msg);
            return finishSave(done, TabSaveResult::Failed);
        }
        const std::string saved = res.value("path", path);
        st.markSaved(id, res["saved_revision"].get<int>(), saved);
        st.pushRecentFile(saved);
        st.addLog("[tabs] drawing saved -> " + saved);
        ui::plmAutoRecordSave(st, saved);
        ui::boxMirrorNotifySave(st, saved);
        return finishSave(done, TabSaveResult::Saved);
    }
    return finishSave(done, TabSaveResult::Nothing);
}

// Draws the document tab strip at the top of the central dock area. In the app shell the strip
// floats over the viewport in its own pinned borderless window (see the overlay block below); the
// inline fallback draws it at the current cursor of the central document window. `availW` is the
// content width to span. Returns the strip height (the pill row; overflow never adds height).
float drawDocTabs(AppState& st, float availW) {
    // The strip still draws with zero tabs outside the workspace home — just the band and the "+"
    // New-Document button, so the user can always start a new file. On the home (Model workspace,
    // nothing open) the card carries those actions and the bare strip is skipped.
    const auto& pal = ui::palette;
    float s = ui::uiScale;
    float barH = 26.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();

    s_switchTo = -1;
    s_closeIdx = -1;
    s_saveClose = {};
    s_closeConfirmed = {};
    s_newDoc = NewDocKind::None;
    // A close requested while an edit was in flight is re-evaluated after it
    // settles, so a newly accepted edit still receives the save prompt.
    for (size_t i = 0; i < s_deferredCloses.size();) {
        const auto request = s_deferredCloses[i];
        if (st.hasPendingHumanMutations(request.identity.id)) { ++i; continue; }
        s_deferredCloses.erase(s_deferredCloses.begin() + i);
        requestTabClose(st, request.identity, request.askIfDirty);
    }

    // ---- pinned floating overlay window (visual-overhaul fix 2) ------------------------------------
    // The strip used to draw INLINE at the top of the central document window and reserve its own
    // height, which pinned it above the FeatureManager column's top-left corner AND pushed the whole
    // viewport (toolbar + 3D) down by barH — the dead gap under the ribbon. It now floats over the
    // viewport in its OWN borderless window, starting just RIGHT of the FeatureManager column's right
    // edge (overlayColumnEdge(0)) and flush under the ribbon (band.minY), so the FM column can start
    // at the very top of the viewport band and the viewport chrome sits flush with the ribbon bottom.
    // Same flag set as the FM / Copilot pinned columns (floating over the docked central node, input
    // scoped to this window, glass samples the backdrop window-independently). Falls back to the
    // band's left edge when the FM column hasn't published an edge yet (first frame / collapsed pill).
    // Content width (tabs + the trailing "+" button) measured up front — no window context needed,
    // just st.docTabs + CalcTextSize — so the floating overlay window can HUG it. Sizing the window
    // full-width would leave a huge invisible (NoBackground but input-blocking) right half that eats
    // clicks meant for the viewport toolbar + Copilot header, which sit on the SAME top row to the
    // right (they render clean in a screenshot but become unclickable). Widths come from
    // measureTabW — the same formula drawOneTab uses — so band, pills, and window all agree.
    const float gap = 2.0f * s;               // inter-tab gap
    const float btn = barH;                   // trailing "+" button, always visible
    // Full trailing slot (lead gap + button + trailing inset) — see kBtnLeadGaps.
    const float btnSlot = btn + (kBtnLeadGaps + kBtnTrailGaps) * gap;
    const int nTabs = (int)st.docTabs.size();
    std::vector<float> tabWs((size_t)nTabs, 0.0f);
    std::vector<float> tabOff((size_t)nTabs, 0.0f);   // start x within the (unscrolled) content
    // Workspace home (workspace_home.h): the untouched launch starter is not a document of the
    // user's, so its tab takes no room (width 0, skipped below) until it is edited or left.
    const bool home = workspaceHomeActive(st);
    const bool homeHidesActive = home && st.activeTab >= 0 && st.activeTab < nTabs;
    float fullContentW = 0.0f;
    for (int i = 0; i < nTabs; ++i) {
        tabOff[i] = fullContentW;
        if (homeHidesActive && i == st.activeTab) continue;
        tabWs[i] = measureTabW(st.docTabs[i], s);
        fullContentW += tabWs[i] + (i + 1 < nTabs ? gap : 0.0f);
    }
    // Home with no other tab: the home card already offers New Part / New Assembly / Open, so a
    // lone "+" floating over the empty studio is noise; Ctrl+N still reaches the chooser. No close
    // prompt or deferred tab action can be pending: a dirty or on-disk tab is never the hidden
    // starter, so it would have kept the strip.
    if (home && fullContentW <= 0.0f) {
        if (s_openChooser) { st.wantNewDocChooser = true; s_openChooser = false; }
        return barH;
    }
    const float contentW = fullContentW + btnSlot;

    ui::OverlayBand band = ui::overlayBand();
    bool floatingWindow = false;
    if (band.valid) {
        const float margin = 12.0f * s;
        const float fmEdge = ui::overlayColumnEdge(0);
        const float docY = band.minY + 4.0f * s;   // hug the ribbon; matches the pinned columns' top
        // Hug content, clamped to the Copilot column's LEFT edge (never run tabs under it). The
        // viewport toolbar lives to the LEFT of that edge, so hugging content is what actually frees
        // it — clamping alone would not.
        float rightBound = ui::overlayColumnEdge(1);   // Copilot left edge
        if (rightBound < 0.0f) rightBound = band.maxX - margin;
        const float minimumStripW = nTabs > 0
            ? std::max(80.0f * s, btnSlot + 24.0f * s +
                       2.0f * (measureOverflowChipW(nTabs, s) + gap))
            : 80.0f * s;
        // Keep the minimum usable strip on the visible side of Copilot even if resized columns
        // leave less than that width. Shift the strip left rather than extending its hit window
        // under the right column or squeezing its controls.
        const float docX = std::min((fmEdge >= 0.0f ? fmEdge : band.minX) + margin,
                                    rightBound - minimumStripW);
        const float winW = std::max(minimumStripW,
                                    std::min(contentW + 6.0f * s, rightBound - docX));
        // Height hugs the pill row + a hair of slack so the rounded pills are not clipped. Overflow
        // never grows the strip (no stacked scrollbar lane), so tabs sit at the same y either way.
        const float winH = barH + 2.0f * s;
        availW = winW;   // the strip lays out relative to this window's content width
        ImGui::SetNextWindowPos(ImVec2(docX, docY), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(winW, winH), ImGuiCond_Always);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        const ImGuiWindowFlags dtFlags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
            ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoDocking |
            ImGuiWindowFlags_NoBackground | ImGuiWindowFlags_NoScrollbar |
            ImGuiWindowFlags_NoScrollWithMouse | ImGuiWindowFlags_NoSavedSettings;
        const bool dtOpen = ImGui::Begin("##DocTabsOverlay", nullptr, dtFlags);
        ImGui::PopStyleVar(3);
        if (!dtOpen) { ImGui::End(); return barH; }
        floatingWindow = true;
    }

    // Liquid Glass v2 port (ported from ui/imgui/src/ui/doc_tabs.cpp's drawDocTabs): the tab band
    // is now ONE Thin glass surface whose backdrop is the live blurred viewport, per-rect UV
    // projected across the FULL dockspace width (ui::glassPanel handles the projection + clamped
    // edge bleed) — still backfilling the FeatureManager | doc-tab | Copilot dock-node splitter
    // gaps like the old opaque bar did. The OPAQUE panelBg fill from the pre-glass design is kept,
    // but now drawn ONLY when no viewport backdrop is currently live (ECAD/CAM tab active, or
    // before the first frame publishes one): that opaque fill was the original two-tone-banding
    // workaround, and with a live backdrop the new per-rect UV projection samples one continuous
    // texture so that banding cannot recur (host facts, see local://glass_host.md).
    ImVec2 origin = ImGui::GetCursorScreenPos();
    // In the floating overlay window origin is already the strip's top-left.
    const float bandT = origin.y, bandL = origin.x;
    // Tab lane + overflow state: the tabs scroll in the window content minus the pinned trailing
    // "+" button slot. Identical formula to the pre-Begin window sizing above.
    const float outerL = origin.x;
    const float outerW = std::max(40.0f * s, availW - btnSlot);
    const float outerR = outerL + outerW;
    const bool overflow = fullContentW > outerW + 0.5f;
    const float bandB = origin.y + barH;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // The band hugs its CONTENT (contentW measured above the Begin) instead of spanning the full
    // window — a full-width glass slab with zero/few tabs read as a large dead strip (visual-review
    // fix); it now also matches the hugged window footprint.
    const float bandR = std::min(origin.x + availW, bandL + contentW);
    const ImRect bandRect(ImVec2(bandL, bandT), ImVec2(bandR, bandB));
    // Tell the pinned glass columns where the band REALLY ends (bandB sits below band.minY +
    // docTabStripHeight because origin.y is the central window's content cursor, not the node top)
    // so the band never overlaps their top corners (visual-review fix).
    ui::publishDocTabBandBottom(bandB);
    if (!ui::viewportBackdrop().valid())
        dl->AddRectFilled(bandRect.Min, bandRect.Max, ui::Palette::toU32(pal.panelBg),
                          8.0f * s);
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("host_doctabs_glass"), bandRect);
    ui::glassPanel(dl, bandRect, ui::GlassLevel::Thin, 8.0f /*dp — glassPanel scales by uiScale*/, surf);

    // ---- scrollable tab lane + reserved overflow chips ---------------------------------------------
    // The trailing "+" button is always pinned at the far right; the tabs scroll within the space to
    // its left. When they overflow, each side that hides files gets a dedicated "+N" glass chip in
    // its OWN slot (the tab clip region shrinks so chips never paint over a filename). Boundary tab
    // titles fade out with a clean alpha ramp. When everything fits this collapses to the plain
    // flush-left, no-chip layout. The tab row sits at the band top whether or not it overflows.
    const float y0 = origin.y;

    // Deferred selection is applied AFTER drawing, so reveal it on the next frame, including
    // selections made outside this strip. Keep the document identity rather than a stale index.
    // A changed title/order or strip width can also move its close button out of the lane.
    int reveal = s_hoverReveal;
    if (st.activeTab >= 0 && st.activeTab < nTabs) {
        const auto& active = st.docTabs[st.activeTab];
        const bool changed = s_visibleActive.kind != active.kind ||
            s_visibleActive.id != closeTabId(active);
        if (changed || s_visibleActiveOffset != tabOff[st.activeTab] ||
            s_visibleActiveWidth != tabWs[st.activeTab] || s_visibleStripW != availW)
            reveal = st.activeTab;
        if (changed) s_visibleActive = closeTabIdentity(active);
        s_visibleActiveOffset = tabOff[st.activeTab];
        s_visibleActiveWidth = tabWs[st.activeTab];
    } else {
        s_visibleActive = {};
        s_visibleActiveOffset = s_visibleActiveWidth = -1.0f;
    }
    s_visibleStripW = availW;
    s_hoverReveal = -1;

    // Refine the chip counts against the lane AND the requested reveal position. The extra
    // pass lets a revealed selection reserve the newly-hidden edge's chip before it is drawn.
    float leftChipW = 0.0f, rightChipW = 0.0f;
    float tabsL = outerL, tabsR = outerR, tabsW = outerW;
    float maxScroll = 0.0f;
    int nLeft = 0, nRight = 0;
    std::vector<std::string> leftTitles, rightTitles;
    leftTitles.reserve((size_t)nTabs);
    rightTitles.reserve((size_t)nTabs);

    // Whole-tab lane: while the strip overflows, the lane's left edge always sits on a tab
    // boundary and a tab that would straddle the right edge is folded into the right "+N" chip.
    // A sliver of a tab (its clipped title or a lone "×" slot) beside a "+N" chip read as a
    // rendering fault, so the only tab ever drawn cut is one wider than the whole lane (the
    // revealed oversized title keeps its close control in view).
    // Smallest tab boundary that still shows the content's right end — the scroll ceiling.
    auto boundaryCap = [&](float maxS) {
        for (int i = 0; i < nTabs; ++i)
            if (tabOff[(size_t)i] >= maxS - 0.5f) return tabOff[(size_t)i];
        return maxS;
    };
    // Moves `v` forward onto the next boundary when it lands inside a tab.
    auto snapForward = [&](float v, float laneW) {
        for (int i = 0; i < nTabs; ++i) {
            const float l = tabOff[(size_t)i];
            const float r = l + tabWs[(size_t)i];
            if (r <= v + 0.5f) continue;                       // fully left of the lane
            if (l >= v - 0.5f) return v;                       // already on a boundary
            if (i == reveal && tabWs[(size_t)i] > laneW) return v;  // oversized reveal
            return i + 1 < nTabs ? tabOff[(size_t)i + 1] : l;
        }
        return v;
    };
    // Tab-boundary stepping for the wheel and the "+N" chips: free pixel scrolling would land
    // mid-tab and the lane would snap forward again, so a short step could never move left.
    auto prevBoundary = [&](float v) {
        float best = 0.0f;
        for (int i = 0; i < nTabs; ++i)
            if (tabOff[(size_t)i] < v - 0.5f) best = tabOff[(size_t)i];
        return best;
    };
    auto nextBoundary = [&](float v) {
        for (int i = 0; i < nTabs; ++i)
            if (tabOff[(size_t)i] > v + 0.5f) return tabOff[(size_t)i];
        return v;
    };
    // Wheel over the strip scrolls it horizontally (horizontal wheel, or a plain wheel as a
    // fallback so a trackpad / mouse without an H-wheel still works), one tab per notch. Applied
    // before the layout so this frame's chip counts match what is drawn; the layout clamps it.
    if (overflow && ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows)) {
        float wheel = ImGui::GetIO().MouseWheelH;
        if (wheel == 0.0f) wheel = ImGui::GetIO().MouseWheel;
        if (wheel > 0.0f) s_scrollX = prevBoundary(s_scrollX);
        else if (wheel < 0.0f) s_scrollX = nextBoundary(s_scrollX);
    }
    float viewScroll = 0.0f;
    float scrollCap = 0.0f;

    for (int pass = 0; pass < 3; ++pass) {
        tabsL = outerL + (leftChipW > 0.5f ? leftChipW + gap : 0.0f);
        tabsR = outerR - (rightChipW > 0.5f ? rightChipW + gap : 0.0f);
        tabsW = std::max(24.0f * s, tabsR - tabsL);
        tabsR = tabsL + tabsW;   // keep consistent if clamped

        maxScroll = std::max(0.0f, fullContentW - tabsW);
        scrollCap = overflow ? boundaryCap(maxScroll) : 0.0f;
        // Count hidden tabs against a CLAMPED VIEW of the scroll — never write it back inside
        // this loop: pass 0 runs with no chips reserved, so clamping the persistent s_scrollX
        // here permanently ate the chip width off the reachable scroll range. The settled view
        // is stored once, after the final geometry (below the loop).
        viewScroll = overflow ? std::min(std::max(s_scrollX, 0.0f), scrollCap) : 0.0f;
        if (overflow && reveal >= 0 && reveal < nTabs) {
            const float tL = tabOff[(size_t)reveal];
            const float tR = tL + tabWs[(size_t)reveal];
            // An oversized title cannot fit at once; reveal its trailing close control first.
            if (tabWs[(size_t)reveal] > tabsW)
                viewScroll = tR - tabsW;
            else if (tL < viewScroll + 0.5f)
                viewScroll = tL;
            else if (tR > viewScroll + tabsW - 0.5f)
                viewScroll = tR - tabsW;
            viewScroll = std::max(viewScroll, 0.0f);
        }
        if (overflow) viewScroll = std::min(snapForward(viewScroll, tabsW), scrollCap);

        nLeft = 0;
        nRight = 0;
        leftTitles.clear();
        rightTitles.clear();
        bool anyShown = false;
        for (int i = 0; i < nTabs; ++i) {
            if (tabWs[i] <= 0.0f) continue;   // hidden starter (workspace home)
            const float x = tabsL + tabOff[i] - viewScroll;
            const float xr = x + tabWs[i];
            if (xr <= tabsL + 0.5f) {
                ++nLeft;
                leftTitles.push_back(st.docTabs[i].displayTitle());
                continue;
            }
            // Past the right edge, or straddling it behind a fully shown tab: folded into "+N".
            if (x >= tabsR - 0.5f || (overflow && anyShown && xr > tabsR + 0.5f)) {
                ++nRight;
                rightTitles.push_back(st.docTabs[i].displayTitle());
                continue;
            }
            anyShown = true;
        }

        // Show a chip only when at least one tab is hidden on that side.
        const float nextLeft  = (overflow && nLeft  > 0) ? measureOverflowChipW(nLeft,  s) : 0.0f;
        const float nextRight = (overflow && nRight > 0) ? measureOverflowChipW(nRight, s) : 0.0f;
        if (std::fabs(nextLeft - leftChipW) < 0.5f && std::fabs(nextRight - rightChipW) < 0.5f)
            break;
        leftChipW = nextLeft;
        rightChipW = nextRight;
    }

    // NOW store the settled view against the FINAL geometry (chips settled) — see the in-loop
    // comment above. Overflow-off collapses the scroll to flush-left.
    s_scrollX = overflow ? viewScroll : 0.0f;

    // Page toward `target` on a boundary that keeps the previous lane edge in view (no tab is
    // skipped), moving at least one tab even when the lane holds a single wide tab.
    auto pageTo = [&](float v, float target) {
        float best = v;
        if (target < v) {
            for (int i = nTabs - 1; i >= 0; --i)
                if (tabOff[(size_t)i] >= target - 0.5f) best = tabOff[(size_t)i];
            if (best >= v - 0.5f) best = prevBoundary(v);
        } else {
            for (int i = 0; i < nTabs; ++i)
                if (tabOff[(size_t)i] <= target + 0.5f) best = tabOff[(size_t)i];
            if (best <= v + 0.5f) best = nextBoundary(v);
        }
        return std::min(std::max(best, 0.0f), scrollCap);
    };

    // Draw the tabs clipped to the reserved tab region (chips live outside this clip). Tabs
    // folded into a chip are skipped; only an oversized tab can still cross a lane edge, and the
    // edge fade below dissolves it (title AND pill edge treatment) instead of a hard slice.
    const float fadeW = 28.0f * s;
    // ImGui's clip updates BOTH renderer and window logical bounds. A draw-list-only clip
    // hides pixels but leaves the invisible tab/close targets live over the chips and "+".
    // The item audit captures this same logical lane at submission time.
    ImGui::PushClipRect(ImVec2(tabsL, y0), ImVec2(tabsR, y0 + barH), true);
    bool cutLeft = false, cutRight = false, drewAny = false;
    for (int i = 0; i < nTabs; ++i) {
        if (tabWs[i] <= 0.0f) continue;   // hidden starter (workspace home)
        const float x = tabsL + tabOff[i] - s_scrollX;
        const float xr = x + tabWs[i];
        if (xr <= tabsL + 0.5f) continue;
        if (x  >= tabsR - 0.5f) continue;
        if (overflow && drewAny && xr > tabsR + 0.5f) continue;   // counted in the right chip
        cutLeft = cutLeft || x < tabsL - 0.5f;
        cutRight = cutRight || xr > tabsR + 0.5f;
        drewAny = true;
        drawOneTab(st, i, x, y0, barH, surf, tabsL, tabsR, fadeW);
    }
    // Clean alpha fade on a cut edge (drawn inside the tab clip so it only covers tab content).
    // When a "+N" chip owns that side the fade edge is INTERIOR to the band, so it runs the full
    // pill height and dissolves the sliced border/separator/shadow with the title. When the fade
    // edge coincides with the band's own rounded corner (no chip), keep the MultiColor inset past
    // the 8dp corner radius — the square-corner guard shared with coloredRowWash/ribbon_vGrad.
    const float bandRdp = 8.0f * s;
    if (cutLeft)
        drawTabEdgeFade(dl, tabsL, y0, y0 + barH, fadeW, /*fromRight=*/false, surf,
                        leftChipW > 0.5f ? 0.0f : bandRdp);
    if (cutRight)
        drawTabEdgeFade(dl, tabsR, y0, y0 + barH, fadeW, /*fromRight=*/true, surf,
                        rightChipW > 0.5f ? 0.0f : bandRdp);
    ImGui::PopClipRect();

    // Reserved-slot overflow chips — never over a tab title. Each click pages most of a lane.
    if (leftChipW > 0.5f && nLeft > 0) {
        if (drawOverflowChip(dl, outerL, y0, barH, leftChipW, nLeft, surf, s, "ovfL", leftTitles))
            s_scrollX = pageTo(s_scrollX, s_scrollX - tabsW * 0.8f);
    }
    if (rightChipW > 0.5f && nRight > 0) {
        if (drawOverflowChip(dl, outerR - rightChipW, y0, barH, rightChipW, nRight, surf, s,
                             "ovfR", rightTitles))
            s_scrollX = pageTo(s_scrollX, s_scrollX + tabsW * 0.8f);
    }

    // Scroll-position hint: an app-default 2dp hairline (ui::drawScrollPill idiom) laid INSIDE the
    // band along its bottom edge under the tab lane, shown only while the strip is hovered. It adds
    // no height and takes no input (the old 10dp lane above the pills stacked a chunky orange bar on
    // the tabs and dropped the row out of line with a non-overflowing strip). Scrolling itself is
    // the wheel, the "+N" chips, and click/hover-reveal of a clipped edge tab.
    if (overflow && maxScroll > 0.5f && ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) &&
        ImGui::IsMouseHoveringRect(bandRect.Min, bandRect.Max)) {
        const float trackL = tabsL + bandRdp;   // clear of the band's rounded bottom corners
        const float trackR = tabsR - bandRdp;
        const float trackW = trackR - trackL;
        if (trackW > 16.0f * s) {
            const float thumbW = std::min(trackW, std::max(16.0f * s, trackW * (tabsW / fullContentW)));
            // The whole-tab lane may rest one boundary past maxScroll (trailing slack).
            const float thumbX =
                trackL + std::min(1.0f, s_scrollX / maxScroll) * (trackW - thumbW);
            const float lineY = bandB - 2.5f * s;   // hairline centre, just above the pill bottoms
            ui::drawScrollPill(dl, ImVec2(trackL, lineY - 1.0f * s), ImVec2(trackR, lineY + 1.0f * s),
                               thumbX, thumbX + thumbW, /*vertical=*/false, /*hovered=*/false, surf);
        }
    }

    // "+" New-Document button, pinned at the far right of the strip — opens the chooser popup (Wave 2).
    // Clicking it (or Ctrl+N via openNewDocChooser()) sets wantNewDocChooser.
    {
        // Pinned at the far right, OUTSIDE the scroll lane — always visible, never scrolled and
        // never painted over a tab. Its whole slot (btnSlot: lead gap + button + trailing inset)
        // was reserved out of outerW / contentW above, so in both states it ends one gap inside
        // the band and the overlay window — never clipped by the Copilot column edge. Otherwise it
        // hugs the last tab so the band (which hugs its content) doesn't strand it outside the glass.
        const float btnX = (overflow ? outerR : outerL + fullContentW) + kBtnLeadGaps * gap;
        const float btnSide = barH;               // matches the pill row's height
        const ImVec2 p0(btnX, y0);
        ImGui::SetCursorScreenPos(p0);
        const bool clicked = ImGui::InvisibleButton("##newtab", ImVec2(btnSide, btnSide));
        const bool hov = ImGui::IsItemHovered();
        if (hov) {
            const ImRect pill(p0, ImVec2(p0.x + btnSide, p0.y + btnSide));
            ui::glassPanel(dl, pill, ui::GlassLevel::UltraThin, 4.0f /*dp*/, surf);
        }
        // Same 16dp icon size as the tab kind icons (drawOneTab), tinted with the primary text tone
        // (the tab labels' max-contrast role) so the always-available action never reads as a faint
        // fleck next to the tabs.
        const float iconSz = 16.0f * s;
        ui::drawIconGlass(dl, "new_doc", ImVec2(p0.x + btnSide * 0.5f, p0.y + btnSide * 0.5f),
                          iconSz, ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                          ui::glassIconHalo(surf));
        if (hov) ImGui::SetTooltip("New document");
        if (clicked || s_openChooser) { st.wantNewDocChooser = true; s_openChooser = false; }
    }

    // Close the pinned floating overlay window (the band + tabs + "+" are all drawn now). The modal
    // prompt + deferred mutations below run at the caller's (central window) scope.
    if (floatingWindow) ImGui::End();

    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        st.docTabs[st.activeTab].diskConflict) {
        const std::string docId = st.docTabs[st.activeTab].docId;
        ImGui::SetNextWindowPos(ImVec2(outerL, bandB + 6.0f * s), ImGuiCond_Always);
        ImGui::SetNextWindowBgAlpha(0.95f);
        if (ImGui::Begin("##doc_disk_conflict", nullptr,
                         ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoNav)) {
            ImGui::TextUnformatted("File changed on disk");
            ImGui::SameLine();
            const bool pending = st.hasPendingHumanMutations(docId);
            if (pending) ImGui::BeginDisabled();
            if (ImGui::Button("Reload (discard my edits)"))
                core::reloadDiskConflict(st, docId);
            if (pending) ImGui::EndDisabled();
            ImGui::SameLine();
            if (ImGui::Button("Keep mine")) {
                const int idx = resolveCloseTab(st, {AppState::DocTab::TabKind::Part, docId});
                const int asmIdx = resolveCloseTab(st, {AppState::DocTab::TabKind::Assembly, docId});
                const int current = idx >= 0 ? idx : asmIdx;
                if (current >= 0) {
                    st.docTabs[current].diskConflict = false;
                    st.docTabs[current].overwriteDiskConflict = true;
                    st.docTabs[current].conflictPath.clear();
                }
            }
        }
        ImGui::End();
    }

    // ---- unsaved-changes confirmation prompt ----
    const int confirmIdx = resolveCloseTab(st, s_confirmClose);
    if (!s_confirmClose.id.empty() && confirmIdx < 0) s_confirmClose = {};
    if (!s_confirmClose.id.empty()) ImGui::OpenPopup("Close document?##doctab");
    bool open = true;
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    if (ImGui::BeginPopupModal("Close document?##doctab", &open,
                               ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        if (confirmIdx < 0) {
            ImGui::CloseCurrentPopup();
        } else {
            const std::string name = st.docTabs[confirmIdx].displayTitle();
            ImGui::Text("\"%s\" has unsaved changes.", name.c_str());
            ImGui::TextUnformatted("Save your edits before closing?");
            ImGui::Spacing();
            if (ImGui::Button("Save", ImVec2(ui::metrics::px(120), 0))) {
                s_saveClose = s_confirmClose;
                s_confirmClose = {};
                ImGui::CloseCurrentPopup();
            }
            ImGui::SameLine();
            if (ImGui::Button("Don't Save", ImVec2(ui::metrics::px(120), 0))) {
                s_closeConfirmed = s_confirmClose;
                s_confirmClose = {};
                ImGui::CloseCurrentPopup();
            }
            ImGui::SameLine();
            if (ImGui::Button("Cancel", ImVec2(ui::metrics::px(90), 0))) {
                s_confirmClose = {};
                ImGui::CloseCurrentPopup();
            }
        }
        ImGui::EndPopup();
    }
    if (!open) s_confirmClose = {};

    // No height reservation any more — the strip is a pinned floating overlay (fix 2), so the central
    // window's content cursor stays at the top and the viewport chrome (toolbar + 3D) sits flush under
    // the ribbon. The old inline strip advanced the cursor by barH with a trailing Dummy here.

    // ---- apply deferred actions (after all tabs were drawn) ----
    // A close that targets the active tab leaves the mirror on a neighbor, so handle close before
    // switch; only one of these fires per frame in practice.
    if (!s_saveClose.id.empty()) {
        // Save the chosen document, not whichever tab now occupies its old index.
        const CloseTabIdentity chosen = s_saveClose;
        const int idx = resolveCloseTab(st, chosen);
        s_saveClose = {};
        if (idx >= 0) {
            saveDocTab(st, idx, /*forceDialog=*/false, [&st, chosen](TabSaveResult result) {
                if (result != TabSaveResult::Saved) return;
                requestTabClose(st, chosen, true);
            });
        }
    } else if (!s_closeConfirmed.id.empty()) {
        const int idx = resolveCloseTab(st, s_closeConfirmed);
        s_closeConfirmed = {};
        if (idx >= 0 && !st.hasPendingHumanMutations(closeTabId(st.docTabs[idx])) &&
            !hasUnsavedEmbeddedDrawing(st, st.docTabs[idx]))
            st.closeDocTab(idx);
    } else if (s_closeOthersIdx >= 0) {
        // Keep the clicked tab, close the rest. Make it active first so the close-index bookkeeping
        // keeps it selected; skip dirty tabs so unsaved work is never silently discarded. Iterate
        // high→low so removals only shift indices we've already passed.
        const int keep = s_closeOthersIdx;
        s_closeOthersIdx = -1;
        if (keep < (int)st.docTabs.size()) st.switchToTab(keep);
        for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
            if (i == keep) continue;
            requestTabClose(st, closeTabIdentity(st.docTabs[i]), false);
        }
    } else if (s_closeAll) {
        s_closeAll = false;
        for (int i = (int)st.docTabs.size() - 1; i >= 0; --i)
            requestTabClose(st, closeTabIdentity(st.docTabs[i]), false);
    } else if (s_closeIdx >= 0) {
        if (s_closeIdx < (int)st.docTabs.size())
            requestTabClose(st, closeTabIdentity(st.docTabs[s_closeIdx]), true);
        s_closeIdx = -1;
    } else if (s_switchTo >= 0) {
        st.switchToTab(s_switchTo);
        s_switchTo = -1;
    }
    if (s_newDoc != NewDocKind::None) {
        const NewDocKind kind = s_newDoc;
        s_newDoc = NewDocKind::None;
        createNewDoc(st, (int)kind);
    }

    return barH;
}

// Create a document directly, or request the KiCad project name prompt.
void createNewDoc(AppState& st, int kindOrd) {
#ifndef __EMSCRIPTEN__
    // A deliberate New action supersedes the launch starter, even if this
    // document is closed before the background engine-readiness probe settles.
    if (!st.shot) st.starterTried = true;
#endif
    const NewDocKind kind = (NewDocKind)kindOrd;
    if (kind == NewDocKind::Part || kind == NewDocKind::Assembly) {
        const bool asm_ = (kind == NewDocKind::Assembly);
        std::string err;
        bool ok = asm_ ? st.docMirror.newAssembly(st.worker, err)
                       : st.docMirror.newPart(st.worker, err);
        if (ok) {
            st.addLog(std::string("[tabs] new ") + (asm_ ? "assembly" : "part") +
                      "  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();
            st.addDocTab(/*isAssembly=*/asm_);
            if (asm_) { st.showRibbon = true; st.ribbonTab = AppState::RT_Features; }
        } else {
            st.addLog(std::string("[err] new ") + (asm_ ? "assembly" : "part") + ": " + err);
            st.toast(ui::ToastLevel::Error,
                     std::string("New ") + (asm_ ? "assembly" : "part") + " failed: " + err);
        }
        return;
    }
    if (kind == NewDocKind::Drawing) {
        st.makeDrawing();   // guards on an active part/asm tab itself + toasts if none
        return;
    }
    if (kind == NewDocKind::KicadProject) {
        st.activeWorkspace = AppState::Workspace::Electronics;
        st.ecadPanelDocId.clear();
        st.ecadPanelCmd = "ecad_new_project";
    }
}

// Wave 2: request the New-Document chooser popup open on the next strip draw (e.g. a Ctrl+N hook in
// app.cpp). Safe to call anytime; the flag is consumed inside drawDocTabs.
void openNewDocChooser() { s_openChooser = true; }

}  // namespace ui
