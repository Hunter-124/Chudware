// FeatureManager tree panel (C16).
// Shows the SolidWorks-style left panel with fixed header nodes and the feature history.
// Reads AppState::docMirror for the document tree; calls AppState::loadDocMesh on rebuild.
//
// C37-E: FeatureManager overhaul — tree<->viewport selection sync (owner-map face highlights +
// scroll-to + bbox zoom), stability fixes (stable fids/folder ids, O(N) status lookup, canDoc
// gating), SolidWorks interactions (double-click edit, slow-double-click rename, Ctrl multi-select,
// per-row appearance/zoom/rollback), a real grabbable rollback bar, and SW-style sketch nesting.
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"  // ImRect (ui::adaptivePanel rect argument)

#include "feature_tree.h"
#include "file_dialog.h"   // C41: native Win32 Save/Open dialogs
#include "icons.h"
#include "sketch_mode.h"   // C18 (Edit Sketch context entry)
#include "sketch3d_mode.h" // Edit 3D Sketch context/double-click entry
#include "property_manager.h"  // sweep/loft PATH pick from the tree (routePathFeature)
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker
#include "cursors.h"        // C37-E: ResizeV cursor over the rollback bar

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"
#include "../render/id_pass.h"
#include "../renderer.h"   // C34: clearViewportForNewDoc resets meshes

// Safe JSON string accessor (local copy avoids including app_state.h accessors).
static inline std::string jfld(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline bool jftbool(const nlohmann::json& o, const char* k, bool def = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : def;
}
// C37-E: tolerant number accessor (feature_status "ms" timing).
static inline double jftnum(const nlohmann::json& o, const char* k, double def = -1.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : def;
}

namespace ui {

// ---- Liquid glass v2: adaptive-surface palette shim (host contract §H4) ----------------------
// Over-viewport panels get maximum-contrast, theme-independent text driven by the sampled
// viewport luminance (ui_theme.h AdaptiveSurface / adaptivePanel). fmSurf()/fmHalo() are cheap,
// idempotent-per-frame fetches (adaptivePanel caches by id — see design doc amendment A2) of the
// ONE glass surface layout.cpp's drawLeftPane paints for the whole pane ("host_left_glass");
// every tree/row/icon site below reads through them instead of painting its own background.
// FmPalette mirrors the ui/imgui/src/ui/feature_tree.cpp v2 port: the handful of `pal.<role>`
// call sites throughout this file keep compiling unchanged once their local `pal` alias is
// rebound to fmPalette() below — only text/dim/border roles switch to adaptive contrast when a
// backdrop is live; semantic roles (accent/status/hover/panelBg) always stay palette.
static const ui::AdaptiveSurface& fmSurf() {
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect r(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    // Group tag MUST match layout.cpp's paint site (adaptivePanel contract: same group every
    // frame for a given id) — v2.1 per-group knobs ride in through it.
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}
// Contrast halo for drawIconGlass: surf.tint is already the opposite-tone neutral (see
// ui_theme.cpp adaptiveSurface) at ~0x50 alpha; 0 alpha (no live backdrop) => drawIconGlass
// draws plain (icons.h contract).
static ImU32 fmHalo(const ui::AdaptiveSurface& surf) {
    if (!ui::viewportBackdrop().valid()) return 0;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0x50 / 255.f));
}

struct FmPalette {
    float text[4], textDim[4], accent[4], accentSoft[4], accentMid[4];
    float statusWarn[4];
    float treeHover[4], treeSection[4];
    float border[4], panelBg[4];
};
static FmPalette g_fmPal;

static void fmPaletteRefresh(const ui::AdaptiveSurface* live) {
    const auto& p = ui::palette;
    auto c4 = [](const float src[4], float* o) { o[0]=src[0]; o[1]=src[1]; o[2]=src[2]; o[3]=src[3]; };
    auto v4 = [](const ImVec4& v, float* o) { o[0]=v.x; o[1]=v.y; o[2]=v.z; o[3]=v.w; };
    c4(p.text, g_fmPal.text);
    c4(p.textDim, g_fmPal.textDim);
    c4(p.accent, g_fmPal.accent);
    c4(p.accentSoft, g_fmPal.accentSoft);
    c4(p.accentMid, g_fmPal.accentMid);
    c4(p.statusWarn, g_fmPal.statusWarn);
    c4(p.treeHover, g_fmPal.treeHover);
    c4(p.treeSection, g_fmPal.treeSection);
    c4(p.border, g_fmPal.border);
    c4(p.panelBg, g_fmPal.panelBg);
    // Over a LIVE viewport backdrop, adaptive contrast wins for the text/dim/border roles (max
    // legibility over frosted glass); semantic roles (accent/status/hover/panelBg) stay palette.
    if (live) {
        v4(live->textPrimary,   g_fmPal.text);
        v4(live->textSecondary, g_fmPal.textDim);
        v4(live->textSecondary, g_fmPal.treeSection);
        v4(live->textTertiary,  g_fmPal.border);
    }
}
static const FmPalette& fmPalette() { return g_fmPal; }

// ---- icon helper ----
// Attempt to draw a named atlas icon; fall back to a text glyph if the atlas is absent.
// C37-E: `alpha` dims the icon (suppressed / rolled-back rows get a faded icon, item 6).
static void treeIcon(const char* name, const char* fallback, float size = 16.0f, float alpha = 1.0f) {
    // Red-pen fix (Hunter round-2): the old ImGui::Image draw sat at the LINE TOP (too high in
    // the frame-height row pill) and at the row's raw left edge (the Origin icon clipped into
    // the pill's rounded corner). Reserve a frame-height slot inset by FramePadding.x, then
    // paint the glyph draw-list-centered in it — same vertical math as treeIconInArrowSlot.
    const float px     = size * ui::uiScale;                  // #4.1: DPI-scaled quad
    const float insetX = ImGui::GetStyle().FramePadding.x;    // 6dp inside fmRowPill (polish G2)
    const float rowH   = ImGui::GetFrameHeight();             // == the row pill's height
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(insetX + px, rowH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    IconUV iv = getIcon(name, 20);
    if (iv.valid) {
        ImVec2 ip(p0.x + insetX, p0.y + (rowH - px) * 0.5f);
        dl->AddImage(iv.texId, ip, ImVec2(ip.x + px, ip.y + px), iv.uv0, iv.uv1,
                     IM_COL32(255, 255, 255, (int)(alpha * 255.0f)));
    } else {
        const ui::AdaptiveSurface& surf = fmSurf();
        dl->AddText(ImVec2(p0.x + insetX, p0.y + (rowH - ImGui::GetFontSize()) * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(
                        ImVec4(surf.textSecondary.x, surf.textSecondary.y,
                               surf.textSecondary.z, alpha)),
                    fallback);
    }
}

// ---- shared row hover paint (the ONE approved treatment, used panel-wide) ----
// T-G18 (Erik round-1): the feature rows' hover — an accent-outline pill + a soft treeHover
// wash, inset 2/1 dp, 4 dp corners — is the ratified look. Every row in the panel (feature
// rows AND the filename/equations/material/bodies/origin/plane meta rows) now routes through
// THIS one function so the hover is pixel-identical everywhere; the meta rows used to fall
// through to ImGui's default HeaderHovered paint, which read as the "terrible" hover Erik
// flagged. `hovered` is passed in (not sampled here) because a feature row's last item is its
// status badge, not the node, so the caller captures IsItemHovered at the node.
static void fmRowHoverPaint(ImVec2 rowMin, ImVec2 rowMax, bool hovered, bool selected) {
    if (selected || !hovered || ImGui::IsMouseDragging(ImGuiMouseButton_Left)) return;
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    ImVec2 hovMin(rowMin.x + 2.0f * S, rowMin.y + 1.0f * S);
    ImVec2 hovMax(rowMax.x - 3.0f * S, rowMax.y - 1.0f * S);
    ImVec4 hoverCol(pal.accent[0], pal.accent[1], pal.accent[2], 0.45f);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRect(hovMin, hovMax, ImGui::ColorConvertFloat4ToU32(hoverCol),
                4.0f * S, 0, std::max(1.0f, 1.0f * S));
    dl->AddRectFilled(hovMin, hovMax, ui::Palette::toU32(pal.treeHover), 4.0f * S);
}

// Frosted row pill at the current cursor (full content width). Stripe index advances per call.
// Height is the FRAME height only (font + FramePadding), never WithSpacing — a pill that also
// covered the inter-row spacing bled into the next row / the section dividers and read as
// stacked, overlapping slabs (visual-review fix).
//
// T-G18: fmRowPill()/fmRowPillEnd() bracket every meta/header row. fmRowPill pushes the SAME
// FramePadding featureRow() uses (6dp x, 4dp y — so every row is the same height + text baseline) and the
// SAME transparent HeaderHovered/HeaderActive (so ImGui's default hover never paints); the
// returned rect feeds fmRowPillEnd(), which pops those styles and draws the shared hover on
// the row's just-submitted item. Pairs must balance (one End per Pill) or the style stack
// asserts; for folders, call End right after the header node and BEFORE its children.
// FmRowRect lives in feature_tree.h (shared with the Explorer tab); fmRowPill/fmRowPillEnd are
// exported (non-static) for the same reason — see the header's "Shared FeatureManager row
// styling" block.
static int s_fmStripeIdx = 0;
void fmRowStripeReset() {
    s_fmStripeIdx = 0;
    // Also refresh the FM palette: when a sibling tab (Explorer) is the active pane body,
    // drawFeatureTreeBody's own refresh never runs this frame, and the dividers/hover paint
    // below read g_fmPal — without this they'd render with a stale (or first-frame zero) palette.
    fmPaletteRefresh(ui::viewportBackdrop().valid() ? &fmSurf() : nullptr);
}
FmRowRect fmRowPill(bool selected, const float* tint) {
    // polish(G2): 6dp horizontal gives icons breathing room from the left edge; 4dp vertical gives
    // text more vertical breathing without making rows too tall.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(6.0f * ui::uiScale, 4.0f * ui::uiScale));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    const float S = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    float availW = ImGui::GetContentRegionAvail().x;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Frosted glass base + alternating stripe ONLY — selection is now the glow border below (never
    // the old blue accent card/vertical bar), so it composites correctly over the color wash too.
    const float wash = selected ? 0.42f : 0.26f;
    ui::glassRowBackground(dl, rowMin, rowMax, 3.5f, (s_fmStripeIdx++ % 2) == 0, false, wash);
    // Layers-style color wash, bounded to the inset pill: a section/element `tint` when supplied,
    // else the neutral app accent so every row shares the same tinted-background language. Glow
    // border marks selection, dyed the same color.
    const float* eff = tint ? tint : ui::palette.accent;   // accent is float[4]; wash reads [0..2]
    const float strength = tint ? (selected ? 0.34f : 0.20f) : (selected ? 0.22f : 0.11f);
    // Same outer rect as the frosted base — coloredRowWash self-insets by the identical 2dp, so the
    // two stay perfectly concentric (no early-ending corner seam).
    ui::coloredRowWash(dl, rowMin, rowMax, 3.5f, eff, strength, selected);
    return {rowMin, rowMax};
}
void fmRowPillEnd(const FmRowRect& r, bool selected) {
    bool hovered = ImGui::IsItemHovered();
    ImGui::PopStyleColor(2);
    ImGui::PopStyleVar(1);
    fmRowHoverPaint(r.mn, r.mx, hovered, selected);
}

// Draw a row's type icon INSIDE the (empty) leaf-arrow slot of the TreeNodeEx that was just
// submitted, given the cursor position captured BEFORE the node. Leaf tree nodes still reserve
// the arrow column (FontSize + padding) ahead of their label; drawing the icon as a separate
// item BEFORE the node doubled that gap and left the icon stranded far left of its label
// (visual-review fix). The icon is clamped to the arrow slot so it never overlaps the label.
static void treeIconInArrowSlot(ImVec2 rowPos, const char* name, const char* fallback,
                                float alpha = 1.0f) {
    const float rowH = ImGui::GetItemRectSize().y;
    const float px = std::min(16.0f * ui::uiScale, ImGui::GetFontSize());
    // polish(G2): use full FramePadding.x (not 0.5x) so icon sits 6dp from the left edge.
    ImVec2 p(rowPos.x + ImGui::GetStyle().FramePadding.x,
             rowPos.y + (rowH - px) * 0.5f);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    IconUV iv = getIcon(name, 20);
    if (iv.valid) {
        dl->AddImage(iv.texId, p, ImVec2(p.x + px, p.y + px), iv.uv0, iv.uv1,
                     IM_COL32(255, 255, 255, (int)(alpha * 255.0f)));
    } else {
        const ui::AdaptiveSurface& surf = fmSurf();
        dl->AddText(p, ImGui::ColorConvertFloat4ToU32(
                            ImVec4(surf.textSecondary.x, surf.textSecondary.y,
                                   surf.textSecondary.z, alpha)),
                    fallback);
    }
}

// ---- vector eye glyph (per-row visibility toggle) ----
// The atlas "hide_show_eye" icon rendered faint/ambiguous at native scale (the hidden state read
// as a bare diagonal slash — visual-review fix), so the eye is drawn as crisp vector geometry:
// two lens arcs + a pupil, with a slash overlaid when hidden. Ink comes from the adaptive surface
// so it holds contrast on any backdrop.
static void drawEyeGlyph(ImDrawList* dl, ImVec2 mn, ImVec2 mx, bool shown, bool hovered,
                         const ui::AdaptiveSurface& surf) {
    const float S = ui::uiScale;
    const ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
    const float w = (mx.x - mn.x) * 0.5f;          // half-width of the eye
    const float h = w * 0.62f;                     // lens half-height
    const float a = shown ? (hovered ? 1.0f : 0.85f) : (hovered ? 0.85f : 0.60f);
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, a));
    const float th = std::max(1.0f, 1.3f * S);
    // Lens: two mirrored quadratic arcs through the corners.
    dl->PathLineTo(ImVec2(c.x - w, c.y));
    dl->PathBezierQuadraticCurveTo(ImVec2(c.x, c.y - 2.0f * h), ImVec2(c.x + w, c.y), 8);
    dl->PathStroke(ink, 0, th);
    dl->PathLineTo(ImVec2(c.x - w, c.y));
    dl->PathBezierQuadraticCurveTo(ImVec2(c.x, c.y + 2.0f * h), ImVec2(c.x + w, c.y), 8);
    dl->PathStroke(ink, 0, th);
    if (shown) dl->AddCircleFilled(c, std::max(1.5f, w * 0.30f), ink);
    else       dl->AddLine(ImVec2(c.x - w, c.y + h + 1.0f * S),
                           ImVec2(c.x + w, c.y - h - 1.0f * S), ink, th);
}

// ---- Solid Bodies visibility helper ----
// Show/hide one renderer body slot. Host-side state only (AppState::bodyVisible) — no doc ops,
// so it is applied immediately (no s_pendingTreeOps detour needed). Hiding also drops any
// selection/hover that references the body: the id pass stops rendering it, so a stale highlight
// would point at geometry the user can no longer see or re-pick.
static void setBodyShown(AppState& st, int slot, bool shown) {
    if (slot < 0 || slot >= (int)st.bodyVisible.size()) return;
    st.bodyVisible[slot] = shown ? 1 : 0;
    if (!shown) {
        uint32_t bid = slot < (int)st.docBodyIds.size() ? st.docBodyIds[slot] : 0;
        bool selHasBody = false;
        for (const auto& it : st.selection.items())
            if (it.hit.body_id == bid) { selHasBody = true; break; }
        if (selHasBody) st.selection.clear();
        if (st.hover.body_id == bid) { st.hover = render::PickHit{}; st.pickStatus.clear(); }
    }
    st.applyBodyVisibility();
}

// ---- per-feature eye (FeatureManager hide/show on every tree row) ----
// Renderer slot index for an AICM body id (or -1 if that body isn't currently loaded).
static int slotForBodyId(AppState& st, uint32_t bid) {
    for (int i = 0; i < (int)st.docBodyIds.size(); ++i)
        if (st.docBodyIds[i] == bid) return i;
    return -1;
}
// Is a feature currently shown? Features that OWN bodies derive it from those bodies' visibility
// (any owned body hidden => the feature reads as hidden); everything else from the hiddenFeatures
// set. This keeps the eye and the "Solid Bodies" folder in sync for solids while still giving
// sketches/datums a hide toggle.
static bool featureEyeShown(AppState& st, const std::string& fid,
                            const std::vector<uint32_t>& ownedBodies) {
    if (!ownedBodies.empty()) {
        for (uint32_t bid : ownedBodies) {
            int slot = slotForBodyId(st, bid);
            if (slot >= 0 && slot < (int)st.bodyVisible.size() && !st.bodyVisible[slot])
                return false;
        }
        return true;
    }
    return st.hiddenFeatures.find(fid) == st.hiddenFeatures.end();
}
// Flip a feature's visibility. Body-owning => toggle every owned body (reuses setBodyShown so the
// Solid Bodies folder agrees); other features => flip hiddenFeatures membership (the sketch-ghost
// renderer honors it).
static void toggleFeatureEye(AppState& st, const std::string& fid,
                             const std::vector<uint32_t>& ownedBodies, bool currentlyShown) {
    if (!ownedBodies.empty()) {
        for (uint32_t bid : ownedBodies) {
            int slot = slotForBodyId(st, bid);
            if (slot >= 0) setBodyShown(st, slot, !currentlyShown);
        }
    } else {
        if (currentlyShown) st.hiddenFeatures.insert(fid);
        else st.hiddenFeatures.erase(fid);
        // Datum features (ref axis/plane/point/csys) are reference overlays, not bodies — push the
        // hidden set to the renderer so the toggle hides/shows them immediately (sketches read
        // st.hiddenFeatures live in drawGhosts, so they need no extra push).
        st.applyDatumVisibility();
    }
}

// ---- status badge (ok/warn/error/rolled-back) ----
// C37-E: a rolled-back feature (after the rollback bar) is NOT an error — it gets a distinct muted
// "paused" marker, never the red "!". Pass rolledBack=true for those rows.
static void featureBadge(bool ok, bool hasError, const std::string& errText,
                         const std::vector<std::string>& danglingRefs,
                         bool rolledBack = false, const std::string& warnText = "") {
    (void)hasError;
    // Pick this row's health dot: color, filled-vs-hollow, and tooltip. Rendering the badge as a
    // draw-list circle (not a •/!/?/◦ text glyph) makes it pixel-identical at any DPI and removes the
    // per-font glyph-advance/size variance the old SameLine+TextUnformatted markers had.
    const float S = ui::uiScale;
    ImU32 col = 0;
    bool  hollow = false;
    bool  hasTip = false;
    std::string tip;
    if (rolledBack) {
        // Muted hollow ring — "not currently built" (distinct from ok/error/dangling).
        col = ImGui::ColorConvertFloat4ToU32(
            ImVec4(ui::palette.textDim[0], ui::palette.textDim[1], ui::palette.textDim[2], 0.75f));
        hollow = true;
        tip = "Rolled back (after the rollback bar — not built)"; hasTip = true;
    } else if (ok && danglingRefs.empty() && !warnText.empty()) {
        // Built, but with a non-fatal note (e.g. fillet rounded a subset of edges) — amber dot.
        col = ui::Palette::toU32(ui::palette.statusWarn);
        tip = warnText; hasTip = true;
    } else if (ok && danglingRefs.empty()) {
        col = ui::Palette::toU32(ui::palette.statusOk);    // healthy — teal dot
    } else if (!ok) {
        col = ui::Palette::toU32(ui::palette.statusErr);   // error — red-orange dot
        if (!errText.empty()) { tip = errText; hasTip = true; }
    } else if (!danglingRefs.empty()) {
        col = ui::Palette::toU32(ui::palette.statusWarn);  // dangling refs — amber dot
        tip = "Dangling refs:\n";
        for (const auto& r : danglingRefs) { tip += "  "; tip += r; tip += '\n'; }
        hasTip = true;
    } else {
        return;   // nothing to flag
    }

    const float r = 3.5f * S;
    const float w = 2.0f * r + 4.0f * S;   // dot diameter + breathing room
    ImGui::SameLine();
    ImVec2 p = ImGui::GetCursorScreenPos();
    // Center on the just-submitted node's FULL rect (pill-height rows, red-pen round-2) — the
    // old GetTextLineHeight() center rode ~4dp high once rows became frame-height.
    float  rowH = ImGui::GetItemRectSize().y;
    ImVec2 center(p.x + r + 2.0f * S, p.y + rowH * 0.5f);
    // A non-interactive Dummy advances the layout cursor by [w]; the tooltip uses a geometric
    // IsMouseHoveringRect (same pattern as the per-row eye above) so the badge never captures hover
    // or competes with the row's click / context-menu binding — tree behavior is unchanged.
    ImGui::Dummy(ImVec2(w, rowH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hollow) dl->AddCircle(center, r, col, 0, std::max(1.0f, 1.5f * S));
    else        dl->AddCircleFilled(center, r, col);
    if (hasTip && ImGui::IsMouseHoveringRect(p, ImVec2(p.x + w, p.y + rowH)))
        ImGui::SetTooltip("%s", tip.c_str());
}

// ---- glass popover styling (shared by every FeatureManager popup) ----
// A slightly lighter, blue-tinted, mostly-opaque popup bg (glass depth illusion), a teal accent
// border, rounded corners and roomier padding. Push BEFORE the BeginPopup*/picker call and pop
// AFTER EndPopup/the call: the styles must stay live for the popup's whole deferred draw. The
// push/pop is balanced every frame regardless of whether the popup is actually open.
static void pushTreePopupGlass() {
    const auto& pal = fmPalette();
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.panelBg[0] * 1.08f, pal.panelBg[1] * 1.08f, pal.panelBg[2] * 1.20f, 0.92f));
    ImGui::PushStyleColor(ImGuiCol_Border,
        ImVec4(pal.accentMid[0], pal.accentMid[1], pal.accentMid[2], 0.60f));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 8.0f * ui::uiScale);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
        ImVec2(10.0f * ui::uiScale, 7.0f * ui::uiScale));
}
static void popTreePopupGlass() {
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
}

// ---- styled section rule with a centered label ----
// Groups the tree into its three logical sections (document header / reference geometry / feature
// history) far more clearly than a bare Separator(): a treeSection-colored centered label flanked by
// two short draw-list hairlines, then a Dummy to re-sync the layout cursor below the drawn row.
static void treeSectionDivider(const char* label) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  w = ImGui::GetContentRegionAvail().x;
    float  lineH = ImGui::GetTextLineHeight();
    const float rowH = lineH + 4.0f * S;           // the Dummy row reserved below
    ImVec2 tsz = ImGui::CalcTextSize(label);
    float  textX = p.x + (w - tsz.x) * 0.5f;
    // Red-pen round-2: center label + hairlines in the FULL reserved row (the old text-at-p.y /
    // cy-at-lineH/2 pair sat 2dp high against the row's extra bottom padding).
    float  textY = p.y + (rowH - lineH) * 0.5f;
    float  cy = p.y + rowH * 0.5f;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 lineCol = ui::Palette::toU32(pal.border);
    if (textX > p.x + 4.0f * S) {   // guard tiny panels (avoid zero/negative-length segments)
        dl->AddLine(ImVec2(p.x + 4.0f * S, cy), ImVec2(textX - 6.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
        dl->AddLine(ImVec2(textX + tsz.x + 6.0f * S, cy), ImVec2(p.x + w - 4.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
    }
    ImGui::SetCursorScreenPos(ImVec2(textX, textY));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.treeSection[0], pal.treeSection[1], pal.treeSection[2], pal.treeSection[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, rowH));
}

// ---- exported thin wrappers (feature_tree.h "Shared FeatureManager row styling") ----
// The Explorer tab reuses the FM look through these so both left-pane tabs stay pixel-identical;
// the local static helpers keep their short names for the many call sites in this file.
void fmTreeIcon(const char* name, const char* fallback, float size, float alpha) {
    treeIcon(name, fallback, size, alpha);
}
void fmTreeIconInArrowSlot(ImVec2 rowPos, const char* name, const char* fallback, float alpha) {
    treeIconInArrowSlot(rowPos, name, fallback, alpha);
}
void fmSectionDivider(const char* label) { treeSectionDivider(label); }

// Collapsible section header — the same centered-label + hairline look as treeSectionDivider, but the
// WHOLE row is a toggle and an end-of-line chevron shows the open/closed state (mirrors the PCB
// Manager "Add copper layer" affordance: a control pinned at the right edge with the right hairline
// cut short to clear it). Toggles *open; returns *open. `rightClicked`, if given, reports a
// right-click on the row so the caller can raise a context menu (e.g. the filename's Appearance...).
bool fmSectionDividerCollapse(const char* label, bool* open, bool* rightClicked) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    const float chev = 18.0f * S;                     // end-of-line chevron hit box
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  w = ImGui::GetContentRegionAvail().x;
    float  lineH = ImGui::GetTextLineHeight();
    const float rowH = std::max(lineH + 4.0f * S, chev + 2.0f * S);
    ImVec2 tsz = ImGui::CalcTextSize(label);
    float  textX = p.x + (w - tsz.x) * 0.5f;
    float  textY = p.y + (rowH - lineH) * 0.5f;
    float  cy = p.y + rowH * 0.5f;
    const float chevX = p.x + w - chev;
    const float rightLineEnd = chevX - 8.0f * S;      // cut the right hairline short of the chevron
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Full-row invisible toggle FIRST so the label/hairlines/chevron paint on top of its hit rect.
    ImGui::PushID(label);
    ImGui::SetCursorScreenPos(p);
    const bool clicked = ImGui::InvisibleButton("##sec_toggle", ImVec2(w, rowH));
    const bool hov = ImGui::IsItemHovered();
    if (clicked) *open = !*open;
    if (rightClicked) *rightClicked = hov && ImGui::IsMouseClicked(ImGuiMouseButton_Right);
    if (hov)   // faint wash to signal the whole row is interactive
        ui::glassRowBackground(dl, p, ImVec2(p.x + w, p.y + rowH), 3.0f, false, false, 0.16f);

    ImU32 lineCol = ui::Palette::toU32(pal.border);
    if (textX > p.x + 4.0f * S) {
        dl->AddLine(ImVec2(p.x + 4.0f * S, cy), ImVec2(textX - 6.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
        const float rStart = textX + tsz.x + 6.0f * S;
        if (rStart < rightLineEnd)
            dl->AddLine(ImVec2(rStart, cy), ImVec2(rightLineEnd, cy), lineCol,
                        std::max(1.0f, 1.0f * S));
    }
    dl->AddText(ImVec2(textX, textY),
                ImGui::ColorConvertFloat4ToU32(ImVec4(pal.treeSection[0], pal.treeSection[1],
                                                      pal.treeSection[2], pal.treeSection[3])),
                label);

    // Chevron: pointing down when open, right when closed. Hand-drawn filled triangle.
    const ImVec2 cc(chevX + chev * 0.5f, cy);
    const float a = chev * 0.24f;
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(fmSurf().textPrimary);
    if (*open)
        dl->AddTriangleFilled(ImVec2(cc.x - a, cc.y - a * 0.6f), ImVec2(cc.x + a, cc.y - a * 0.6f),
                              ImVec2(cc.x, cc.y + a * 0.7f), ink);
    else
        dl->AddTriangleFilled(ImVec2(cc.x - a * 0.6f, cc.y - a), ImVec2(cc.x - a * 0.6f, cc.y + a),
                              ImVec2(cc.x + a * 0.7f, cc.y), ink);
    ImGui::PopID();

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, rowH));
    return *open;
}

// Section divider carrying a small square "+" button pinned at the right edge. The right hairline is
// cut short so it stops before the button (leaving a clean gap) instead of running under it. Returns
// true on the frame the button is clicked. Used by the ECAD PCB Manager's "Layers" header to add a
// copper layer (Hunter round-5c).
bool fmSectionDividerAddButton(const char* label, const char* btnId, const char* tooltip) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    const float btn = 18.0f * S;                      // compact square, smaller than the toolbar btns
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  w = ImGui::GetContentRegionAvail().x;
    float  lineH = ImGui::GetTextLineHeight();
    const float rowH = std::max(lineH + 4.0f * S, btn + 2.0f * S);
    ImVec2 tsz = ImGui::CalcTextSize(label);
    float  textX = p.x + (w - tsz.x) * 0.5f;
    float  textY = p.y + (rowH - lineH) * 0.5f;
    float  cy = p.y + rowH * 0.5f;
    const float btnX = p.x + w - btn;
    const float rightLineEnd = btnX - 8.0f * S;       // short-cut so the line clears the button
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 lineCol = ui::Palette::toU32(pal.border);
    if (textX > p.x + 4.0f * S) {
        dl->AddLine(ImVec2(p.x + 4.0f * S, cy), ImVec2(textX - 6.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
        const float rStart = textX + tsz.x + 6.0f * S;
        if (rStart < rightLineEnd)
            dl->AddLine(ImVec2(rStart, cy), ImVec2(rightLineEnd, cy), lineCol,
                        std::max(1.0f, 1.0f * S));
    }
    ImGui::SetCursorScreenPos(ImVec2(textX, textY));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.treeSection[0], pal.treeSection[1], pal.treeSection[2], pal.treeSection[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();

    // Square "+" button (glass pill + a hand-drawn plus glyph), vertically centered on the row.
    const ImVec2 bMin(btnX, p.y + (rowH - btn) * 0.5f), bMax(bMin.x + btn, bMin.y + btn);
    ImGui::SetCursorScreenPos(bMin);
    ImGui::PushID(btnId);
    ui::glassRowBackground(dl, bMin, bMax, 3.0f, false, false, 0.22f);
    const bool clicked = ImGui::InvisibleButton("##add", ImVec2(btn, btn));
    const bool hov = ImGui::IsItemHovered();
    if (hov) ui::glassRowBackground(dl, bMin, bMax, 3.0f, false, false, 0.40f);
    const ImVec2 bc((bMin.x + bMax.x) * 0.5f, (bMin.y + bMax.y) * 0.5f);
    const float arm = btn * 0.28f, th = std::max(1.0f, 1.4f * S);
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(fmSurf().textPrimary);
    dl->AddLine(ImVec2(bc.x - arm, bc.y), ImVec2(bc.x + arm, bc.y), ink, th);
    dl->AddLine(ImVec2(bc.x, bc.y - arm), ImVec2(bc.x, bc.y + arm), ink, th);
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    ImGui::PopID();

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, rowH));
    return clicked;
}
void fmDrawEyeGlyph(ImVec2 mn, ImVec2 mx, bool shown, bool hovered) {
    drawEyeGlyph(ImGui::GetWindowDrawList(), mn, mx, shown, hovered, fmSurf());
}

// ---- shared op applier (DocMirror ops -> rebuild -> reload mesh) ----
// Centralizes the apply_ops -> loadDocMesh pattern used by suppress/delete/reorder/rename/folder.
// Dependency violations (reorder/delete) are reported as a warning, leaving the tree unchanged.
//
// C25 CRASH FIX: DocMirror::applyOps REPLACES the mirror's tree json, but every caller here
// sits inside the draw code that iterates references INTO that json (drag-drop reorder,
// row context menus, Ctrl+Up/Down) — applying immediately is a use-after-free. applyTreeOps
// therefore only ENQUEUES; flushPendingTreeOps() applies after all tree reads are done
// (end of drawFeatureTree).
struct PendingTreeOps { nlohmann::json ops; std::string what; };
static std::vector<PendingTreeOps> s_pendingTreeOps;

static void applyTreeOpsNow(AppState& st, const nlohmann::json& ops, const char* what) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    auto resp = st.docMirror.applyOps(st.worker, ops);
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        if (resp.error_code == "dependency_exists") {
            st.addLog(std::string("[warn] ") + what +
                      ": blocked — a feature depends on this order");
            st.toast(ui::ToastLevel::Warn,
                     std::string(what) + " blocked: a feature depends on this order");
        } else {
            st.addLog(std::string("[err] ") + what + ": " + resp.error_message);
            // W1-B (item 2): surface the failure instead of only logging it. (Transport errors are
            // already toasted via the worker error sink; this covers method-level op rejections.)
            if (resp.error_code != "transport")
                st.toast(ui::ToastLevel::Error,
                         std::string(what) + " failed: " +
                         (resp.error_message.empty() ? resp.error_code : resp.error_message));
        }
    }
}

static void applyTreeOps(AppState& st, const nlohmann::json& ops, const char* what) {
    (void)st;
    s_pendingTreeOps.push_back({ops, what});
}

static void flushPendingTreeOps(AppState& st) {
    if (s_pendingTreeOps.empty()) return;
    auto pending = std::move(s_pendingTreeOps);
    s_pendingTreeOps.clear();
    for (const auto& p : pending) applyTreeOpsNow(st, p.ops, p.what.c_str());
}

// Inline-rename state: which feature id is being renamed + its edit buffer.
static std::string s_renamingId;
static char        s_renameBuf[128] = {0};

// Empty folders created via "Add Folder" with no selection. Folders are display-only tags on
// features (set_folder), so a folder with no members can't be derived from the tree — we keep the
// blank ones here (session-only) and render them at the BOTTOM of the Features list as drop targets.
// Once a feature is dragged in (tagged with the folder), it renders inline via the normal walk and we
// drop it from this list. A pending drop requested by drag-and-drop this frame (fid → folder name).
static std::vector<std::string> s_pendingFolders;
static std::string s_dropFid, s_dropFolder;

// C37-E: multi-selection (Ctrl+click). File-local; st.selectedFeatureId tracks the PRIMARY (last
// clicked). Cleared on a plain click, on doc change (featureTreeResetTransient), and whenever the
// selection is validated away. A row is "selected" if it is the primary OR in this set.
static std::unordered_set<std::string> s_multiSel;

// C37-E: slow-double-click rename arming. A second single-click on the already-primary row, with a
// gap in (0.4s, 2.0s), arms the inline editor (SolidWorks "click-pause-click to rename"). We record
// the time of the last single-click commit per row.
static std::string s_lastClickId;
static double      s_lastClickTime = -1.0;

// C37-E: hover-to-highlight is CLICK-ONLY in this revision (see DEVIATIONS). A transient hover tint
// would have to repaint st.featureHighlightFaces on enter and restore the click selection on leave
// every frame; in immediate mode that fought the click highlight and flickered, and the card
// explicitly permits click-only. So we do not track hover dwell.

// C37-E: rollback-bar drag state. While the user is dragging the bar handle we track it here so the
// drop computes the feature the bar should sit AFTER (or null = end). s_rbDropAfter is the fid the
// bar would land after given the cursor's current row (sentinel "\0end" = drop at the end).
static bool        s_rollbackDragging = false;
static std::string s_rbDropAfter;   // updated each frame while dragging; "" until a row is hit

// C37-E: per-row appearance picker target. The row's context menu stashes which body to recolor + a
// one-shot open flag; drawFeatureTree renders the picker popup (sibling) and turns a commit into a
// set_appearance op. File-local so one picker serves every row.
static std::string s_apprBody;     // body key ("b1") the picker targets, or empty
static bool        s_apprOpen = false;
static float       s_apprColor[3] = {0.71f, 0.76f, 0.82f};
static float       s_apprMetallic = 0.0f, s_apprRoughness = 0.5f;

// C41: the document's CURRENT file path ("" = never saved). Owned by this panel: Save re-uses
// it silently (the dialog only shows on first save / Save As), the bottom strip displays its
// file name, and a successful Open sets it. Cleared with the other transients on every doc
// change (featureTreeResetTransient), so a New Part never inherits the previous doc's path.
static std::string s_docPath;

// ---- Wave 4C: FeatureManager copy / cut / paste ----------------------------------------------
// copy_feature is READ-ONLY (no doc mutation) but the engine's session-level apply_ops still bumps
// the revision + pushes an undo snapshot + marks the session dirty (there is no read-only fast path
// at the worker level). We therefore wrap the copy in suppressTabDirty so it does NOT light the tab's
// unsaved marker, and we skip the (unchanged) mesh reload — the mirror's revision advances in
// lockstep so the next real edit is never stale. The portable bundle comes back via the op-result
// id_map side-channel ("$bundle"). include_dependencies defaults true so the paste is self-contained
// (the engine resolves cross-doc external refs at paste time).
//
// Returns true if at least one feature's bundle was captured into st.clipboard.
static bool copyFeatureToClipboard(AppState& st, const std::vector<std::string>& fids) {
    if (fids.empty() || !st.docMirror.hasDoc() || !st.workerStarted) return false;
    if (st.job.running.load()) return false;   // same canDoc gate the tree mutations use
    std::vector<nlohmann::json> bundles;
    bool prevSuppress = st.suppressTabDirty;
    st.suppressTabDirty = true;   // copy is read-only — don't dirty the tab on the phantom rev bump
    for (const auto& fid : fids) {
        nlohmann::json ops = nlohmann::json::array({
            {{"op", "copy_feature"}, {"id", fid}, {"include_dependencies", true}}});
        auto resp = st.docMirror.applyOps(st.worker, ops);
        if (!resp.ok) {
            st.addLog("[copy] copy_feature failed: " + resp.error_message);
            continue;
        }
        // The bundle rides the id_map side-channel. "$bundle" is the last copy's bundle;
        // "$bundle:0" is this batch's (we send one copy_feature per batch, so index 0).
        if (resp.id_map.is_object()) {
            if (resp.id_map.contains("$bundle") && resp.id_map["$bundle"].is_object())
                bundles.push_back(resp.id_map["$bundle"]);
            else if (resp.id_map.contains("$bundle:0") && resp.id_map["$bundle:0"].is_object())
                bundles.push_back(resp.id_map["$bundle:0"]);
        }
    }
    st.suppressTabDirty = prevSuppress;
    if (bundles.empty()) {
        st.toast(ui::ToastLevel::Warn, "Copy failed");
        return false;
    }
    st.clipboard.clear();
    st.clipboard.kind = AppState::Clipboard::Kind::Feature;
    st.clipboard.srcDocId = st.docMirror.docId();
    st.clipboard.payloads = std::move(bundles);
    st.addLog("[copy] " + std::to_string(st.clipboard.payloads.size()) + " feature bundle(s)");
    st.toast(ui::ToastLevel::Info,
             "Copied " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
    return true;
}

// The set of feature ids a Copy/Cut should act on: the full multi-selection if the primary is part
// of it, else just the primary. Mirrors the context menu's `multi` rule.
static std::vector<std::string> featureCopySet(AppState& st) {
    std::vector<std::string> fids;
    if (s_multiSel.size() > 1 && !st.selectedFeatureId.empty() &&
        s_multiSel.count(st.selectedFeatureId) > 0) {
        for (const auto& id : s_multiSel) fids.push_back(id);
    } else if (!st.selectedFeatureId.empty()) {
        fids.push_back(st.selectedFeatureId);
    }
    return fids;
}

// Wave 4D: public entry points so app.cpp's global Ctrl+C/X/V routing + the "edit_*" dispatcher ids
// can drive FeatureManager copy/cut/paste without reaching into this TU's file-statics. These run
// OUTSIDE the tree walk (app.cpp's pre-draw key handler), so the immediate DocMirror calls are safe
// (nothing is iterating the tree json). Each is a no-op (returns false) without an eligible
// selection / clipboard / live doc.
bool featureCopySelection(AppState& st) {
    return copyFeatureToClipboard(st, featureCopySet(st));
}
bool featureCutSelection(AppState& st) {
    auto fids = featureCopySet(st);
    if (!copyFeatureToClipboard(st, fids)) return false;
    // Cut = copy then delete (the same delete op the context menu / multi-delete uses). The engine
    // blocks the delete if a downstream feature depends on it (reported as a warning by
    // applyTreeOpsNow); the copy still stuck on the clipboard, so the user can paste it elsewhere.
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& id : fids)
        ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
    applyTreeOpsNow(st, ops, "cut");
    return true;
}
bool featurePasteSelection(AppState& st) {
    if (st.clipboard.kind != AppState::Clipboard::Kind::Feature ||
        st.clipboard.payloads.empty())
        return false;
    if (!st.docMirror.hasDoc() || !st.workerStarted || st.job.running.load()) return false;
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& bundle : st.clipboard.payloads)
        ops.push_back({{"op", "paste_feature"}, {"bundle", bundle}});
    applyTreeOpsNow(st, ops, "paste");
    st.toast(ui::ToastLevel::Info,
             "Pasted " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
    return true;
}

// Wave-5-fill: Edit > Delete — delete the FeatureManager selection. Mirrors featureCutSelection
// minus the copy: same delete_feature op (cascade:false) the row context menu and multi-delete use.
// Runs OUTSIDE the tree walk (app.cpp's pump), so the immediate DocMirror call is safe. applyTreeOpsNow
// surfaces an engine block (a downstream feature depends on this one) as a warning toast and leaves
// the tree unchanged. No-op (false) without a live, idle doc or an eligible selection.
bool featureDeleteSelection(AppState& st) {
    if (!st.docMirror.hasDoc() || !st.workerStarted || st.job.running.load()) return false;
    auto fids = featureCopySet(st);   // full multi-select if the primary is in it, else the primary
    if (fids.empty()) return false;
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& id : fids)
        ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
    applyTreeOpsNow(st, ops, "delete");
    // Drop the now-deleted ids from the selection so a stale primary/multi-set can't linger (the tree
    // walk also validates against the live tree, but clearing here keeps the menu's state honest).
    for (const auto& id : fids) s_multiSel.erase(id);
    st.selectedFeatureId.clear();
    return true;
}

// Wave-5-fill: Edit > Select All — select every feature in the active part's FeatureManager. Reads
// the doc tree's "features" array (the same source the tree-walk's `order` list is built from) and
// puts every feature id into the multi-select, with the last as the primary so context-menu multi
// actions light up. No-op (false) for assemblies (no flat feature history), empty docs, or no doc.
bool featureSelectAll(AppState& st) {
    if (!st.docMirror.hasDoc()) return false;
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return false;
    if (jfld(tree, "schema") == "aicad.asm/1") return false;  // assemblies have no feature history
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array() || feat->empty()) return false;
    s_multiSel.clear();
    std::string last;
    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        std::string id = jfld(f, "id");
        if (id.empty()) continue;
        s_multiSel.insert(id);
        last = id;
    }
    if (s_multiSel.empty()) return false;
    st.selectedFeatureId = last;        // primary = last feature (drives the multi-action gating)
    refreshFeatureHighlight(st);        // light the primary's owned faces in the viewport
    return true;
}

// ---- Wave 4C: deferred copy/cut/paste from the row CONTEXT MENU ----
// copyFeatureToClipboard calls DocMirror::applyOps IMMEDIATELY (it needs the id_map bundle back
// synchronously). That REPLACES the mirror's tree json — a use-after-free if invoked while the row
// walk is still iterating that json (the same hazard applyTreeOps defers around). So the context
// menu only RECORDS a request here; flushPendingClipboard() runs it after the tree walk, next to
// flushPendingTreeOps. The captured fid set is snapshotted so a later selection change can't move it.
enum class ClipReq { None, Copy, Cut, Paste };
static ClipReq                  s_clipReq = ClipReq::None;
static std::vector<std::string> s_clipReqFids;

static void requestClipboard(ClipReq req, std::vector<std::string> fids) {
    s_clipReq = req;
    s_clipReqFids = std::move(fids);
}

static void flushPendingClipboard(AppState& st) {
    if (s_clipReq == ClipReq::None) return;
    ClipReq req = s_clipReq;
    std::vector<std::string> fids = std::move(s_clipReqFids);
    s_clipReq = ClipReq::None;
    s_clipReqFids.clear();
    switch (req) {
        case ClipReq::Copy:
            copyFeatureToClipboard(st, fids);
            break;
        case ClipReq::Cut:
            if (copyFeatureToClipboard(st, fids)) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : fids)
                    ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
                applyTreeOpsNow(st, ops, "cut");
            }
            break;
        case ClipReq::Paste:
            if (st.clipboard.kind == AppState::Clipboard::Kind::Feature &&
                !st.clipboard.payloads.empty()) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& bundle : st.clipboard.payloads)
                    ops.push_back({{"op", "paste_feature"}, {"bundle", bundle}});
                applyTreeOpsNow(st, ops, "paste");
                st.toast(ui::ToastLevel::Info,
                         "Pasted " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
            }
            break;
        default: break;
    }
}

// W1-B (item 5): drop the tree's in-progress transient state on a doc change. Declared in
// feature_tree.h; called from AppState::clearViewportForNewDoc and wholesale-replace paths.
void featureTreeResetTransient() {
    s_renamingId.clear();
    s_renameBuf[0] = '\0';
    s_pendingTreeOps.clear();
    s_multiSel.clear();                 // C37-E
    s_lastClickId.clear();              // C37-E
    s_lastClickTime = -1.0;
    s_rollbackDragging = false;
    s_rbDropAfter.clear();
    s_apprBody.clear();
    s_apprOpen = false;
    s_docPath.clear();                  // C41: a fresh/replaced doc has no file path yet
    s_clipReq = ClipReq::None;          // Wave 4C: drop any unflushed copy/cut/paste request
    s_clipReqFids.clear();              // (the clipboard payload itself survives a doc switch)
}

// Clear only the multi-select set — see feature_tree.h.
void featureClearTreeMultiSel() { s_multiSel.clear(); }

// ---- C26: appearance color-picker popup ----
// W1-B (item 9): the MaterialPreset table + the appearance picker popup, formerly duplicated here
// and in viewport_panel.cpp, now live in ui/appearance_common.{h,cpp}. Both call ui::appearance-
// ColorPicker(); the CALLER opens the popup (ImGui::OpenPopup(popupId)) after stashing which body
// the edit targets, then turns a true return into a set_appearance op.

// ---- feature_status (ok / error / dangling / rolled-back / compile ms) ----
struct FStatus {
    bool ok = true;
    std::string error;
    std::string warning;         // non-fatal note on an ok feature (e.g. fillet rounded a subset)
    std::vector<std::string> dangling;
    bool   rolledBack = false;   // C37-E: feature sits after the rollback bar (not built, not error)
    double ms = -1.0;            // C37-E: last compile time (ms); <0 = unknown
};

static FStatus parseStatusRow(const nlohmann::json& s) {
    FStatus fs;
    fs.ok = jftbool(s, "ok", true);
    fs.rolledBack = jftbool(s, "rolled_back", false);
    fs.ms = jftnum(s, "ms", -1.0);
    auto it = s.find("error");
    if (it != s.end() && it->is_string()) fs.error = it->get<std::string>();
    auto wt = s.find("warning");
    if (wt != s.end() && wt->is_string()) fs.warning = wt->get<std::string>();
    auto dr = s.find("dangling_refs");
    if (dr != s.end() && dr->is_array())
        for (const auto& d : *dr)
            if (d.is_string()) fs.dangling.push_back(d.get<std::string>());
    return fs;
}

// Linear scan — kept for the assembly tree (component count is small). The feature history uses the
// per-frame map below (buildStatusMap) to avoid an O(N^2) scan.
static FStatus getFeatureStatus(const nlohmann::json& statusArr, const std::string& fid) {
    if (!statusArr.is_array()) return {};
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        if (jfld(s, "id") != fid) continue;
        return parseStatusRow(s);
    }
    return {};
}

// C37-E (item 2): build one id->status map per frame so each feature row is an O(1) lookup instead
// of re-scanning the whole status array (the old O(N^2)).
static std::unordered_map<std::string, FStatus> buildStatusMap(const nlohmann::json& statusArr) {
    std::unordered_map<std::string, FStatus> m;
    if (!statusArr.is_array()) return m;
    m.reserve(statusArr.size());
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        std::string id = jfld(s, "id");
        if (!id.empty()) m.emplace(std::move(id), parseStatusRow(s));
    }
    return m;
}

// ---- C37-E: feature -> viewport highlight (owner map -> featureHighlightFaces) ----
// Fills st.featureHighlightFaces with the (body<<32)|face keys this feature owns. The viewport
// agent tints them. Tolerant of a missing owner map: the vector is simply left empty.
static void fillHighlightFaces(AppState& st, const std::string& fid,
                               std::vector<uint64_t>& out) {
    out.clear();
    const auto& om = st.docMirror.ownerMap();
    auto it = om.find(fid);
    if (it == om.end()) return;
    out.reserve(it->second.faces.size());
    for (const auto& bf : it->second.faces)
        out.push_back(((uint64_t)bf.first << 32) | (uint64_t)bf.second);
}

// Apply the persistent (click) highlight for a feature: clear topology selection (the tree owns the
// highlight now) and repopulate featureHighlightFaces. No toast when there's no owner map — an empty
// highlight is fine (the card says so).
static void selectFeatureInViewport(AppState& st, const std::string& fid) {
    st.selection.clear();
    fillHighlightFaces(st, fid, st.featureHighlightFaces);
}

static void clearFeatureHighlight(AppState& st) {
    st.featureHighlightFaces.clear();
}

static void clearTreeFeatureSelection(AppState& st) {
    st.selectedFeatureId.clear();
    s_multiSel.clear();
    clearFeatureHighlight(st);
}

// C38 (T1): public re-backfill of the feature highlight from the CURRENT owner map. The owner map's
// face tags are rebuilt on every applyOps/undo/redo/resync, so a previously-populated
// featureHighlightFaces (captured at click time) points at stale face ids and tints the wrong faces
// — or none. Re-derive it for the currently-selected feature; clear when nothing is selected or the
// feature is gone. Cheap (one map lookup + small vector fill); safe to call every rebuild.
void refreshFeatureHighlight(AppState& st) {
    if (st.selectedFeatureId.empty()) { st.featureHighlightFaces.clear(); return; }
    fillHighlightFaces(st, st.selectedFeatureId, st.featureHighlightFaces);
}

// C37-E (item 3): "Zoom to Feature" — union the owned faces'/bodies' bboxes (from the host-side
// bbox caches) and post a one-shot camera request. Toasts when nothing framable was found.
static void zoomToFeature(AppState& st, const std::string& fid) {
    const auto& om = st.docMirror.ownerMap();
    auto it = om.find(fid);
    bool any = false;
    float mn[3] = {0, 0, 0}, mx[3] = {0, 0, 0};
    auto acc = [&](const std::array<float, 6>& b) {
        if (!any) { for (int i = 0; i < 3; ++i) { mn[i] = b[i]; mx[i] = b[3 + i]; } any = true; }
        else {
            for (int i = 0; i < 3; ++i) {
                if (b[i]     < mn[i]) mn[i] = b[i];
                if (b[3 + i] > mx[i]) mx[i] = b[3 + i];
            }
        }
    };
    if (it != om.end()) {
        for (const auto& bf : it->second.faces) {
            uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
            auto fit = st.faceBBoxCache.find(key);
            if (fit != st.faceBBoxCache.end()) acc(fit->second);
        }
        // Fall back to the owned bodies' boxes (faces may not all be cached).
        if (!any)
            for (uint32_t b : it->second.bodies) {
                auto bit = st.bodyBBoxCache.find(b);
                if (bit != st.bodyBBoxCache.end()) acc(bit->second);
            }
    }
    if (!any) {
        st.toast(ui::ToastLevel::Info, "Zoom to Feature: no cached geometry for this feature");
        return;
    }
    for (int i = 0; i < 3; ++i) { st.zoomToBBoxMin[i] = mn[i]; st.zoomToBBoxMax[i] = mx[i]; }
    st.zoomToBBoxRequested = true;
}

// ---- draw one feature row ----
// All the data + behavior a single feature row needs. Returns the click outcome so the caller can
// update selection. The struct keeps the (formerly sprawling) parameter list readable.
struct RowCtx {
    std::string fid, name, type;
    const char* iconName = nullptr;
    const char* iconFallback = nullptr;
    bool suppressed = false;
    bool selected   = false;        // primary OR multi-selected
    bool rolledBack = false;        // after the rollback bar → greyed/italic, NOT error styling
    FStatus fs;                     // ok / error / dangling / ms
    int  ownerBodyCount = 0;        // for the tooltip
    std::vector<uint32_t> ownedBodies;  // AICM body ids this feature owns (per-row eye toggle)
    bool nested = false;            // a sketch child node (indented under its consumer)
    bool expandable = false;        // has nested children (sketch/refs) → real collapse arrow
    bool canDoc = true;            // gates every mutating menu item + drag-reorder
    bool barAtEnd = true;          // hides "Roll to End" when already at the end
    std::string firstOwnedBody;    // body key for the per-row Appearance item ("b<n>")
    const char* convertLabel = nullptr;
    std::function<void()> convertCb = nullptr;
    // reorderCb(dragged, before): drop dragged immediately before `before` ("" = end).
    std::function<void(const std::string&, const std::string&)> reorderCb;
    const std::function<void(const std::string&)>* editCallback = nullptr;
};

// Outcomes of interacting with a row this frame (consumed by the caller).
struct RowResult {
    bool clicked     = false;   // plain left-click (select)
    bool ctrlClicked = false;   // Ctrl+left-click (toggle multi-select)
    bool doubleClick = false;   // open the edit form
    bool hovered     = false;
    bool open        = true;    // expandable row: is it expanded? (leaf rows: always true)
};

static RowResult featureRow(RowCtx& c, AppState& st) {
    RowResult res;
    const std::string& fid = c.fid;
    // Nested-child indent is now applied by drawOneRow AROUND the whole row (background pill
    // included), not here — so the row background indents with the text+icon (Hunter task-4).
    // C37-E (item 6): suppressed/rolled-back rows get a faded icon (clearer than just the [S] tag).
    // The icon is drawn INTO the leaf node's arrow slot after TreeNodeEx (treeIconInArrowSlot) so
    // the label sits directly beside it instead of across a doubled icon+arrow gap.
    float iconAlpha = c.suppressed ? 0.45f : (c.rolledBack ? 0.6f : 1.0f);

    // ---- inline rename editor (replaces the label while active) ----
    if (s_renamingId == fid) {
        // Inset the editor by 2dp and give it a frame whose height matches the row pill, so the box
        // (and its selection highlight) fit WITHIN the background pill and the text stays vertically
        // centered — the rename branch returns before the node's own FramePadding push, so without
        // this it inherited ambient padding (text sank below the pill) and a -1 width spilled past
        // the pill's rounded bounds (Hunter red-pen). FramePadding.y = 2dp → frame is 2dp shorter
        // than the pill top+bottom, leaving a hair of margin so the highlight sits inside it.
        const float S = ui::uiScale;
        const float inset = 2.0f * S;
        const ImVec2 cur = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(cur.x + inset, cur.y + inset));
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - inset * 2.0f);
        if (!ImGui::IsAnyItemActive()) ImGui::SetKeyboardFocusHere();
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6.0f * S, 2.0f * S));
        ImGui::PushStyleColor(ImGuiCol_FrameBg,
            ImVec4(ui::palette.accentSoft[0], ui::palette.accentSoft[1],
                   ui::palette.accentSoft[2], 0.25f));
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                   ui::palette.accent[2], 0.70f));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 3.0f * S);
        bool commit = ImGui::InputText(("##rn_" + fid).c_str(), s_renameBuf, sizeof(s_renameBuf),
                                       ImGuiInputTextFlags_EnterReturnsTrue |
                                       ImGuiInputTextFlags_AutoSelectAll);
        ImGui::PopStyleVar(2);
        ImGui::PopStyleColor(2);
        bool cancel = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool deact  = ImGui::IsItemDeactivated();
        if (commit || cancel || deact) {
            std::string newName = s_renameBuf;
            std::string old = s_renamingId;
            s_renamingId.clear();
            // C37-E (item 2): verify the fid still exists in the live tree before sending; a rename
            // committed onto a feature that was deleted/reordered away would be a stale op.
            bool stillExists = false;
            {
                const nlohmann::json& tr = st.docMirror.tree();
                auto fa = tr.is_object() ? tr.find("features") : tr.end();
                if (tr.is_object() && fa != tr.end() && fa->is_array())
                    for (const auto& f : *fa)
                        if (f.is_object() && jfld(f, "id") == old) { stillExists = true; break; }
            }
            if (!cancel && !newName.empty() && newName != c.name) {
                if (stillExists) {
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op", "rename"}, {"id", old}, {"name", newName}});
                    applyTreeOps(st, ops, "rename");
                } else {
                    st.toast(ui::ToastLevel::Warn, "Rename dropped: feature no longer exists");
                }
            }
        }
        return res;   // (nested indent is owned by drawOneRow now)
    }

    // FramePadding flag (red-pen round-2): without it a TreeNodeEx is TEXT height drawn at the
    // row top — every label sat ~4dp too high inside the frame-height row pill, and the eye /
    // badge (centered on the node rect) rode up with it. With it the node IS pill-height and
    // ImGui centers the label via the pushed 4dp FramePadding.y.
    // Expandable rows (a 3D body with a consumed sketch / nested refs) draw a REAL collapse arrow
    // whose open/closed state ImGui persists per node id — the caller renders the children only when
    // open. NoTreePushOnOpen keeps us in charge of the child indent (drawOneRow owns it), so there is
    // no TreePop to balance. Leaf rows keep the arrow-slot icon trick (no arrow drawn).
    ImGuiTreeNodeFlags flags =
        ImGuiTreeNodeFlags_NoTreePushOnOpen |
        ImGuiTreeNodeFlags_SpanFullWidth |
        ImGuiTreeNodeFlags_FramePadding;
    if (!c.expandable) flags |= ImGuiTreeNodeFlags_Leaf;
    else flags |= ImGuiTreeNodeFlags_DefaultOpen;   // start expanded (was always-visible before)

    // Suppressed → dim. Rolled back → dim + italic-ish look (we cannot truly italicize a
    // default font, so we use the lighter dim + the "(Rolled back)" cue in the badge/tooltip).
    // Alphas stay high enough for a readable dimmed state (no-low-contrast-text goal).
    bool dim = c.suppressed || c.rolledBack;
    if (dim) {
        float a = c.suppressed ? 0.75f : 0.85f;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(fmSurf().textSecondary.x, fmSurf().textSecondary.y,
                   fmSurf().textSecondary.z, a));
    }

    // Label: dimmed-icon suppressed cue handled via the tree icon; keep a compact [S] too. For an
    // expandable row the arrow occupies the leading slot, so we pad the label with leading spaces
    // sized to one icon width and paint the type icon into that gap (arrow → icon → text).
    const float iconPx = std::min(16.0f * ui::uiScale, ImGui::GetFontSize());
    std::string label = c.name;
    if (c.suppressed) label += " [S]";
    if (c.expandable) {
        const float sw = ImGui::CalcTextSize(" ").x;
        const int nsp = sw > 0.0f ? (int)std::ceil((iconPx + 5.0f * ui::uiScale) / sw) : 2;
        label = std::string(std::max(1, nsp), ' ') + label;
    }
    label += "##ft_" + fid;

    // polish(G2): match fmRowPill padding (6dp x, 4dp y) so featureRow height == pill height.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(6.0f * ui::uiScale, 4.0f * ui::uiScale));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 2.0f * ui::uiScale));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    ImVec2 rowPos = ImGui::GetCursorScreenPos();
    const bool nodeOpen = ImGui::TreeNodeEx(label.c_str(), flags);
    // Drag source: drag a feature onto a folder header to file it into that folder. The click/drag
    // threshold means a plain click still selects; only an actual drag starts this.
    if (ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceNoDisableHover)) {
        ImGui::SetDragDropPayload("FM_FEATURE", fid.c_str(), fid.size() + 1);
        ImGui::Text("Move %s", c.name.c_str());
        ImGui::EndDragDropSource();
    }
    res.open = c.expandable ? nodeOpen : true;
    if (c.expandable) {
        // Paint the type icon just after the arrow (at the label-spacing start).
        const float rowH = ImGui::GetItemRectSize().y;
        ImVec2 ip(rowPos.x + ImGui::GetTreeNodeToLabelSpacing(), rowPos.y + (rowH - iconPx) * 0.5f);
        ImDrawList* idl = ImGui::GetWindowDrawList();
        IconUV iv = getIcon(c.iconName, 20);
        if (iv.valid)
            idl->AddImage(iv.texId, ip, ImVec2(ip.x + iconPx, ip.y + iconPx), iv.uv0, iv.uv1,
                          IM_COL32(255, 255, 255, (int)(iconAlpha * 255.0f)));
    } else {
        treeIconInArrowSlot(rowPos, c.iconName, c.iconFallback, iconAlpha);
    }
    ImGui::PopStyleColor(2);
    ImGui::PopStyleVar(2);

    if (dim) ImGui::PopStyleColor();

    // ---- per-feature eye hit-box (right edge of the row) ----
    // Computed from the SpanFullWidth node rect now; drawn + handled at the end of the row. A click
    // landing on the eye must NOT also select/double-click the row, so the classification below skips
    // when the cursor is over the eye.
    ImVec2 nodeMin = ImGui::GetItemRectMin();
    ImVec2 nodeMax = ImGui::GetItemRectMax();
    float  eyeSz   = 14.0f * ui::uiScale;
    float  eyePad  = 6.0f  * ui::uiScale;
    ImVec2 eyeMin(nodeMax.x - eyeSz - eyePad, nodeMin.y + (nodeMax.y - nodeMin.y - eyeSz) * 0.5f);
    ImVec2 eyeMax(eyeMin.x + eyeSz, eyeMin.y + eyeSz);
    bool   overEye = ImGui::IsMouseHoveringRect(eyeMin, eyeMax);

    // ---- click classification (defer the actual selection change to the caller) ----
    bool itemHovered = ImGui::IsItemHovered();
    res.hovered = itemHovered;
    if (ImGui::IsItemClicked(ImGuiMouseButton_Left) && !overEye) {
        if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) res.doubleClick = true;
        else if (ImGui::GetIO().KeyCtrl)                        res.ctrlClicked = true;
        else                                                    res.clicked = true;
    }

    // ---- drag-drop reorder (gated on canDoc) ----
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropSource(ImGuiDragDropFlags_None)) {
        ImGui::SetDragDropPayload("FEATURE_ROW", fid.c_str(), fid.size() + 1);
        ImGui::Text("Move %s", c.name.c_str());
        ImGui::EndDragDropSource();
    }
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropTarget()) {
        // C37-E (item 3): draw the insertion indicator in the GAP at the real insertion position —
        // top edge if the cursor is in the upper half (drop BEFORE this row), bottom edge if in the
        // lower half (drop AFTER → before the next row).
        ImVec2 rmin = ImGui::GetItemRectMin(), rmax = ImGui::GetItemRectMax();
        float  mid  = (rmin.y + rmax.y) * 0.5f;
        bool   after = ImGui::GetIO().MousePos.y > mid;
        float  ly = after ? rmax.y : rmin.y;
        ImGui::GetWindowDrawList()->AddLine(ImVec2(rmin.x, ly), ImVec2(rmax.x, ly),
                                            ui::Palette::toU32(ui::palette.accent),
                                            std::max(1.0f, 2.5f * ui::uiScale));  // #4.5
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            if (dragged != fid) {
                // reorderCb takes a "before" id. Dropping in this row's UPPER half = insert before
                // this row (pass fid). LOWER half = insert after this row, which we encode with the
                // "@after:<fid>" sentinel so the caller resolves the following feature's id.
                c.reorderCb(dragged, after ? ("@after:" + fid) : fid);
            }
        }
        ImGui::EndDragDropTarget();
    }

    // Context menu — MUST come while the TreeNode row is still the last item:
    // BeginPopupContextItem opens on right-click of the LAST item, and featureBadge() below
    // emits its own SameLine text item. With the old order the menu only opened when the user
    // right-clicked the tiny status dot, i.e. effectively never (C40 fix).
    pushTreePopupGlass();
    if (ImGui::BeginPopupContextItem(("##ctx_" + fid).c_str())) {
        bool isSketch = c.iconName && std::strcmp(c.iconName, "sketch_node") == 0;
        bool multi    = s_multiSel.size() > 1 && s_multiSel.count(fid) > 0;
        if (c.selected && ImGui::MenuItem("Deselect"))
            clearTreeFeatureSelection(st);
        if (c.selected) ImGui::Separator();
        if (!c.canDoc) ImGui::BeginDisabled();   // gate every mutating item while an agent run is busy

        if (isSketch) {
            // A sketch gets "Edit Sketch" (enter the sketcher) + "Edit Sketch Plane" (re-pick the
            // plane it sits on) — NOT the generic "Edit" form, which doesn't apply to a sketch.
            if (ImGui::MenuItem("Edit Sketch")) {
                auto* sk = st.sketchMode;
                if (sk) sk->enterForFeature(st, fid);
            }
            if (ImGui::MenuItem("Edit Sketch Plane/Face")) {
                auto* sk = st.sketchMode;
                if (sk) sk->beginPlaneEdit(st, fid);
            }
        } else if (c.type == "sketch3d") {
            if (ImGui::MenuItem("Edit 3D Sketch")) {
                if (st.sketch3dMode) st.sketch3dMode->enterForFeature(st, fid);
            }
        } else if (c.editCallback && *c.editCallback) {
            if (ImGui::MenuItem("Edit")) (*c.editCallback)(fid);
        } else {
            ImGui::BeginDisabled(); ImGui::MenuItem("Edit"); ImGui::EndDisabled();
        }
        if (ImGui::MenuItem("Rename")) {
            s_renamingId = fid;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
        }
        if (ImGui::MenuItem("Remove from folder")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_folder"}, {"id", fid}, {"folder", ""}}}), "set_folder");
        }

        // ---- Wave 4C: Copy / Cut / Paste ----
        // The copy set is the whole multi-selection when this row is part of it, else just this row.
        // Recorded as a deferred request (run after the tree walk) because the copy's DocMirror call
        // replaces the tree json this menu sits inside. Paste enables only when a feature bundle is
        // on the clipboard.
        ImGui::Separator();
        {
            std::vector<std::string> copySet;
            if (multi) for (const auto& id : s_multiSel) copySet.push_back(id);
            else copySet.push_back(fid);
            std::string copyLabel = multi
                ? ("Copy " + std::to_string((int)copySet.size()) + " features\tCtrl+C")
                : std::string("Copy\tCtrl+C");
            std::string cutLabel = multi
                ? ("Cut " + std::to_string((int)copySet.size()) + " features\tCtrl+X")
                : std::string("Cut\tCtrl+X");
            if (ImGui::MenuItem(copyLabel.c_str())) requestClipboard(ClipReq::Copy, copySet);
            if (ImGui::MenuItem(cutLabel.c_str()))  requestClipboard(ClipReq::Cut, copySet);
            bool canPaste = st.clipboard.kind == AppState::Clipboard::Kind::Feature &&
                            !st.clipboard.payloads.empty();
            if (!canPaste) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Paste\tCtrl+V"))
                requestClipboard(ClipReq::Paste, std::vector<std::string>{});
            if (!canPaste) ImGui::EndDisabled();
        }

        ImGui::Separator();
        // C37-E (item 3): multi-selection applies Suppress/Unsuppress/Delete to ALL selected rows.
        if (multi) {
            int n = (int)s_multiSel.size();
            if (ImGui::MenuItem(("Suppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", true}});
                applyTreeOps(st, ops, "suppress");
            }
            if (ImGui::MenuItem(("Unsuppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", false}});
                applyTreeOps(st, ops, "unsuppress");
            }
            if (ImGui::MenuItem(("Delete " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
                applyTreeOps(st, ops, "delete");
            }
        } else {
            if (ImGui::MenuItem(c.suppressed ? "Unsuppress" : "Suppress")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_suppressed"}, {"id", fid}, {"value", !c.suppressed}}}), "suppress");
            }
        }

        // "Convert to Solid Body": sew an imported mesh (STL/OBJ/PLY/3MF/glTF/3dm) into an OCCT solid
        // so it can be cut/filleted/combined. Shown only for an `import` feature still in mesh form
        // (as_solid not yet set). Flips the import's as_solid param; the rebuild re-sews it. (Engine:
        // edit_feature -> import recompiles via mesh_solidify.) Deferred via applyTreeOps like the rest.
        if (!multi) {
            const nlohmann::json& tree = st.docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array()) {
                for (const auto& f : tree["features"]) {
                    if (!f.is_object() || f.value("id", std::string()) != fid) continue;
                    if (f.value("type", std::string()) == "import") {
                        const bool already = f.contains("params") && f["params"].is_object() &&
                                             f["params"].value("as_solid", false);
                        if (!already) {
                            ImGui::Separator();
                            if (ImGui::MenuItem("Convert to Solid Body")) {
                                applyTreeOps(st, nlohmann::json::array({
                                    {{"op", "edit_feature"}, {"id", fid},
                                     {"params", {{"as_solid", true}}}}}), "convert to solid");
                            }
                        }
                    }
                    break;
                }
            }
        }

        if (c.convertCb && c.convertLabel) {
            ImGui::Separator();
            if (ImGui::MenuItem(c.convertLabel)) c.convertCb();
        }

        // C37-E (item 3): Zoom to Feature / Rollback to Here / Roll to End / Appearance.
        ImGui::Separator();
        if (ImGui::MenuItem("Zoom to Feature")) zoomToFeature(st, fid);
        if (ImGui::MenuItem("Rollback to Here")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_rollback"}, {"after", fid}}}), "rollback");
        }
        if (!c.barAtEnd) {
            if (ImGui::MenuItem("Roll to End")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_rollback"}, {"after", nullptr}}}), "rollback");
            }
        }
        if (!c.firstOwnedBody.empty()) {
            if (ImGui::MenuItem("Appearance...")) {
                s_apprBody = c.firstOwnedBody;
                s_apprColor[0] = 0.71f; s_apprColor[1] = 0.76f; s_apprColor[2] = 0.82f;
                s_apprMetallic = 0.0f; s_apprRoughness = 0.5f;
                s_apprOpen = true;   // a sibling popup opens next frame (see drawFeatureTree)
            }
        }

        ImGui::Separator();
        if (ImGui::MenuItem("Delete")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "delete_feature"}, {"id", fid}, {"cascade", false}}}), "delete");
        }
        if (!c.canDoc) ImGui::EndDisabled();
        ImGui::EndPopup();
    }
    popTreePopupGlass();

    // ---- per-feature eye toggle (every row, right edge) ----
    // Hidden body-owning features hide their bodies; hidden sketches/datums drop out of the ghost
    // overlay. The glyph dims when hidden, with a slash (the atlas carries one eye icon, not a pair).
    {
        bool shown = featureEyeShown(st, fid, c.ownedBodies);
        ImDrawList* eyeDl = ImGui::GetWindowDrawList();
        drawEyeGlyph(eyeDl, eyeMin, eyeMax, shown, overEye, fmSurf());
        if (overEye) {
            ImGui::SetTooltip(shown ? "Hide" : "Show");
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                toggleFeatureEye(st, fid, c.ownedBodies, shown);
                // T-BL42: un-hiding an absorbed sketch selects + highlights it so it's easy
                // to spot in the viewport (ghost goes visible; tree row gets selection card).
                if (c.nested && !shown && c.type == "sketch") {
                    st.selectedFeatureId = fid;
                    selectFeatureInViewport(st, fid);
                    st.treeScrollToSelection = true;
                }
            }
        }
    }

    featureBadge(c.fs.ok, !c.fs.error.empty(), c.fs.error, c.fs.dangling, c.rolledBack);

    // The status badge owns detailed status/error hover text. Keeping the row itself quiet avoids
    // tooltip cards covering selected rows while the user is trying to scan or drag the tree.

    // (nested indent is owned by drawOneRow now — no Unindent here)
    return res;
}

// ---- feature type -> icon name ----
// C24: every type maps to an atlas name that actually exists (see host/assets/icons/atlas.json),
// so the FeatureManager shows a per-type glyph and the old 'datum_plane'/'feature' unknown-icon
// warnings are gone.
static bool featureModeIsCut(const nlohmann::json* feature) {
    if (!feature || !feature->is_object()) return false;
    auto pIt = feature->find("params");
    if (pIt == feature->end() || !pIt->is_object()) return false;
    auto mIt = pIt->find("mode");
    if (mIt != pIt->end() && mIt->is_string())
        return mIt->get<std::string>() == "cut";
    auto opIt = pIt->find("operation");
    if (opIt != pIt->end() && opIt->is_string()) {
        std::string op = opIt->get<std::string>();
        return op == "cut" || op == "subtract";
    }
    return false;
}

static const char* featureIcon(const std::string& type, const nlohmann::json* feature = nullptr) {
    if (type == "sketch")           return "sketch_node";
    if (type == "sketch3d")         return "spline";
    if (type == "extrude")          return featureModeIsCut(feature) ? "extrude_cut" : "extrude_boss";
    if (type == "revolve")          return featureModeIsCut(feature) ? "revolve_cut" : "revolve_boss";
    if (type == "sweep")            return featureModeIsCut(feature) ? "sweep_cut" : "sweep";
    if (type == "loft")             return featureModeIsCut(feature) ? "loft_cut" : "loft";
    if (type == "fillet")           return "fillet";
    if (type == "chamfer")          return "chamfer";
    if (type == "shell")            return "shell";
    if (type == "draft")            return "draft";
    if (type == "rib")              return "rib";
    if (type == "hole")             return "hole_wizard";
    if (type == "linear_pattern")   return "linear_pattern";
    if (type == "circular_pattern") return "circular_pattern";
    if (type == "mirror")           return "mirror";
    if (type == "plane" || type == "datum_plane") return "ref_plane";
    if (type == "axis" || type == "datum_axis")   return "ref_axis";
    if (type == "point" || type == "datum_point") return "ref_point";
    if (type == "import")           return "open";
    if (type == "script")           return "history";
    if (type == "helix")            return "curve_helix";
    if (type == "csys")             return "ref_csys";
    if (type == "boolean")          return "combine";
    if (type == "split")            return "split";
    // Sheet metal
    if (type == "sm_base_flange")   return "sm_base_flange";
    if (type == "sm_edge_flange")   return "sm_edge_flange";
    if (type == "sm_hem")           return "sm_hem";
    if (type == "sm_jog")           return "sm_jog";
    // Weldments
    if (type == "weld_member")      return "weld_member";
    if (type == "weld_gusset")      return "weld_gusset";
    if (type == "weld_end_cap")     return "weld_end_cap";
    // Surfacing — reuse closest solid icon where no dedicated surface glyph exists
    if (type == "surf_extrude")     return "extrude_boss";
    if (type == "surf_revolve")     return "revolve_boss";
    if (type == "surf_sweep")       return "sweep";
    if (type == "surf_loft")        return "loft";
    if (type == "surf_planar")      return "ref_plane";
    if (type == "surf_offset")      return "offset_ent";
    if (type == "surf_boundary")    return "boundary";
    if (type == "surf_fill")        return "boundary";
    if (type == "surf_trim")        return "trim";
    if (type == "surf_knit")        return "combine";
    if (type == "surf_thicken")     return "extrude_boss";
    if (type == "surf_ruled")       return "shell";
    if (type == "surf_untrim")      return "convert_ent";
    if (type == "surf_delete_face") return "subtract";
    if (type == "surf_replace_face")return "wrap";
    // Primitives (box/cylinder/sphere/cone/torus) and anything else: the generic part glyph.
    if (type == "box")              return "box_feature";
    if (type == "cylinder")         return "cylinder_feature";
    if (type == "sphere")           return "sphere_feature";
    if (type == "cone")             return "cone_feature";
    if (type == "torus")            return "torus_feature";
    return "part";
}
static const char* featureFallback(const std::string& type) {
    // Sketch
    if (type == "sketch")            return "[S]";
    if (type == "sketch3d")          return "[3S]";
    // Solid operations (boss)
    if (type == "extrude")           return "[E]";
    if (type == "revolve")           return "[Rv]";
    if (type == "sweep")             return "[Sw]";
    if (type == "loft")              return "[Lt]";
    // Dress-up
    if (type == "fillet")            return "[F]";
    if (type == "chamfer")           return "[C]";
    if (type == "shell")             return "[Sh]";
    if (type == "draft")             return "[Dr]";
    if (type == "rib")               return "[Rb]";
    // Feature operations
    if (type == "hole")              return "[H]";
    if (type == "mirror")            return "[Mi]";
    if (type == "linear_pattern")    return "[LP]";
    if (type == "circular_pattern")  return "[CP]";
    // Datum
    if (type == "plane" || type == "datum_plane") return "[Pl]";
    if (type == "axis" || type == "datum_axis")   return "[Ax]";
    if (type == "point" || type == "datum_point") return "[Pt]";
    if (type == "csys")              return "[CS]";
    // Primitives
    if (type == "box")               return "[Bx]";
    if (type == "cylinder")          return "[Cy]";
    if (type == "sphere")            return "[Sp]";
    if (type == "cone")              return "[Co]";
    if (type == "torus")             return "[To]";
    // Curves
    if (type == "helix")             return "[Hx]";
    // Sheet metal
    if (type == "sm_base_flange")    return "[BF]";
    if (type == "sm_edge_flange")    return "[EF]";
    if (type == "sm_hem")            return "[Hm]";
    if (type == "sm_jog")            return "[Jg]";
    // Weldments
    if (type == "weld_member")       return "[WM]";
    if (type == "weld_gusset")       return "[WG]";
    if (type == "weld_end_cap")      return "[EC]";
    // Multi-body boolean
    if (type == "boolean")           return "[Bl]";
    // Misc
    if (type == "import")            return "[Im]";
    if (type == "script")            return "[Sc]";
    if (type == "split")             return "[Sl]";
    // Surfaces
    if (type == "surf_extrude")      return "[SE]";
    if (type == "surf_revolve")      return "[SR]";
    if (type == "surf_sweep")        return "[SS]";
    if (type == "surf_loft")         return "[SL]";
    if (type == "surf_planar")       return "[SP]";
    if (type == "surf_offset")       return "[SO]";
    if (type == "surf_ruled")        return "[SU]";
    if (type == "surf_boundary")     return "[SB]";
    if (type == "surf_fill")         return "[SF]";
    if (type == "surf_knit")         return "[SK]";
    if (type == "surf_thicken")      return "[ST]";
    if (type == "surf_trim")         return "[Sm]";
    if (type == "surf_untrim")       return "[Sn]";
    if (type == "surf_delete_face")  return "[Sd]";
    if (type == "surf_replace_face") return "[Sf]";
    return "[?]";
}

// Category color strip: thin left-edge accent by feature group.
// Groups: sketch, solid-add, solid-cut, dress-up, pattern, datum, sheet-metal, weldment, surface.
static ImU32 categoryColor(const std::string& type, bool isCut) {
    if (type == "sketch" || type == "sketch3d")
        return IM_COL32(86, 152, 245, 180);   // soft blue
    if (isCut && (type == "extrude" || type == "revolve" ||
                  type == "sweep"   || type == "loft"))
        return IM_COL32(245, 120, 72, 190);   // orange-red (solid-cut)
    if (type == "extrude" || type == "revolve" || type == "sweep" || type == "loft" ||
        type == "box" || type == "cylinder" || type == "sphere" ||
        type == "cone" || type == "torus"   || type == "hole")
        return IM_COL32(45, 212, 191, 190);   // teal accent (solid-add)
    if (type == "fillet" || type == "chamfer" || type == "shell" ||
        type == "draft"  || type == "rib")
        return IM_COL32(155, 120, 220, 190);  // purple (dress-up)
    if (type == "linear_pattern" || type == "circular_pattern" || type == "mirror")
        return IM_COL32(74, 173, 204, 190);   // cyan (pattern)
    if (type == "plane" || type == "datum_plane" ||
        type == "axis"  || type == "point" || type == "csys")
        return IM_COL32(100, 140, 210, 190);  // slate-blue (datum)
    if (type.size() >= 3 && type[0]=='s' && type[1]=='m' && type[2]=='_')
        return IM_COL32(242, 182, 64, 190);   // amber (sheet-metal)
    if (type.size() >= 5 && type[0]=='w' && type[1]=='e' && type[2]=='l' &&
        type[3]=='d' && type[4]=='_')
        return IM_COL32(220, 90, 80, 190);    // coral-red (weldment)
    if (type.size() >= 5 && type[0]=='s' && type[1]=='u' && type[2]=='r' &&
        type[3]=='f' && type[4]=='_')
        return IM_COL32(90, 200, 110, 190);   // green (surface)
    if (type == "boolean")
        return IM_COL32(175, 120, 205, 190);  // soft purple (multi-body boolean)
    return IM_COL32(120, 130, 148, 140);      // grey (unknown)
}
// RGB (0..1) view of categoryColor for the coloredRowWash tint. Alpha is ignored (the wash owns it).
static void categoryColorRGB(const std::string& type, bool isCut, float out[3]) {
    ImVec4 c = ImGui::ColorConvertU32ToFloat4(categoryColor(type, isCut));
    out[0] = c.x; out[1] = c.y; out[2] = c.z;
}

// ---- C20b: assembly tree (components + Mates folder) ----
// Renders an aicad.asm/1 doc: each component as a "(f)"-fixed-marked row (SolidWorks), then a
// Mates folder listing each mate as "Concentric1 (base<->peg)" with a suppress/delete context
// menu (asm_edit_mate / asm_delete_mate). Returns nothing; mutates the doc via DocMirror.
static void drawAssemblyTree(AppState& st, const nlohmann::json& tree,
                             const nlohmann::json& statusArr) {
    const auto& pal = fmPalette();

    // Map component id -> display name (for the mate captions).
    auto compName = [&](const std::string& cid) -> std::string {
        if (tree.contains("components") && tree["components"].is_array())
            for (const auto& c : tree["components"])
                if (c.is_object() && jfld(c, "id") == cid) {
                    std::string n = jfld(c, "name");
                    return n.empty() ? cid : n;
                }
        return cid;
    };

    auto reloadFrom = [&](const core::OpsResponse& r) {
        if (r.ok && !r.aicm_path.empty()) {
            std::string le; st.loadDocMesh(r.aicm_path, le);
            if (!le.empty()) st.addLog("[err] loadDocMesh: " + le);
        } else if (!r.ok) {
            st.addLog("[err] mate op: " + r.error_message);
        }
    };

    // ---- components ----
    int ci = 0;
    if (tree.contains("components") && tree["components"].is_array()) {
        for (const auto& c : tree["components"]) {
            if (!c.is_object()) { ++ci; continue; }
            std::string cid   = jfld(c, "id");
            std::string cname = jfld(c, "name");
            if (cname.empty()) cname = cid;
            bool fixed = jftbool(c, "fixed");
            bool suppressed = jftbool(c, "suppressed");
            // Component vs subassembly icon (we only have parts for now; subassemblies = C24).
            bool isAsm = false;
            if (c.contains("source") && c["source"].is_object()) {
                std::string p = jfld(c["source"], "path");
                std::string low = p; for (auto& ch : low) ch = (char)std::tolower((unsigned char)ch);
                isAsm = low.find(".asmstep") != std::string::npos;  // reserved; none today
            }
            FStatus fs = getFeatureStatus(statusArr, cid);

            FmRowRect compRect = fmRowPill();
            // SolidWorks marks fixed components with a "(f)" prefix.
            std::string label = (fixed ? "(f) " : "") + cname;
            if (suppressed) label += " [S]";
            ImGuiTreeNodeFlags lf = ImGuiTreeNodeFlags_Leaf |
                ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth |
                ImGuiTreeNodeFlags_FramePadding;   // pill-height node (red-pen round-2)
            ImVec2 compRowPos = ImGui::GetCursorScreenPos();
            ImGui::TreeNodeEx((label + "##comp_" + cid).c_str(), lf);
            treeIconInArrowSlot(compRowPos, isAsm ? "assembly" : "part", isAsm ? "[A]" : "[P]");
            fmRowPillEnd(compRect);
            featureBadge(fs.ok, !fs.error.empty(), fs.error, fs.dangling, false, fs.warning);
            ++ci;
        }
    }

    ImGui::Separator();

    // ---- Mates folder ----
    FmRowRect matesRect = fmRowPill();
    treeIcon("mates_folder", "[M]", 16.0f);
    ImGui::SameLine(0, 4 * ui::uiScale);   // #4.5
    ImGuiTreeNodeFlags ff = ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_SpanFullWidth |
                            ImGuiTreeNodeFlags_FramePadding;   // pill-height node
    bool matesOpen = ImGui::TreeNodeEx("Mates##mates_folder", ff);
    fmRowPillEnd(matesRect);
    if (matesOpen) {
        if (tree.contains("mates") && tree["mates"].is_array()) {
            // Per-type running index so each mate gets a SolidWorks-style name "Concentric1".
            std::map<std::string, int> typeCount;
            int mi = 0;
            for (const auto& m : tree["mates"]) {
                if (!m.is_object()) { ++mi; continue; }
                std::string mid   = jfld(m, "id");
                std::string mtype = jfld(m, "type");
                bool msup = jftbool(m, "suppressed");
                int n = ++typeCount[mtype];
                std::string disp = mtype.empty() ? "Mate" : mtype;
                if (!disp.empty()) disp[0] = (char)std::toupper((unsigned char)disp[0]);
                disp += std::to_string(n);
                // "(base<->peg)" from the two refs' components.
                std::string pair;
                if (m.contains("refs") && m["refs"].is_array() && m["refs"].size() >= 2) {
                    std::string a = compName(jfld(m["refs"][0], "component"));
                    std::string b = compName(jfld(m["refs"][1], "component"));
                    pair = " (" + a + "<->" + b + ")";
                }
                std::string row = disp + pair;
                if (msup) row += " [S]";

                FmRowRect mateRect = fmRowPill();
                if (msup) ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.75f));
                ImGuiTreeNodeFlags mf = ImGuiTreeNodeFlags_Leaf |
                    ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth |
                    ImGuiTreeNodeFlags_FramePadding;   // pill-height node
                ImVec2 mateRowPos = ImGui::GetCursorScreenPos();
                ImGui::TreeNodeEx((row + "##mate_" + mid).c_str(), mf);
                treeIconInArrowSlot(mateRowPos, "mate", "[m]", msup ? 0.6f : 1.0f);
                if (msup) ImGui::PopStyleColor();
                fmRowPillEnd(mateRect);

                pushTreePopupGlass();
                if (ImGui::BeginPopupContextItem(("##mctx_" + mid).c_str())) {
                    // C25: deferred via applyTreeOps — applying here would invalidate the
                    // tree json this loop iterates (same UAF as feature reorder).
                    if (ImGui::MenuItem(msup ? "Unsuppress" : "Suppress")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_edit_mate"},{"id",mid},{"suppressed",!msup}}}), "mate");
                    }
                    ImGui::Separator();
                    if (ImGui::MenuItem("Delete")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_delete_mate"},{"id",mid}}}), "mate");
                    }
                    ImGui::EndPopup();
                }
                popTreePopupGlass();
                ++mi;
            }
            if (tree["mates"].empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.7f));
                ImGui::TextUnformatted("  (no mates)");
                ImGui::PopStyleColor();
            }
        }
        ImGui::TreePop();
    }
}

// ---- C24: small icon-button used in the FeatureManager top toolbar (24px square) ----
// Returns true on click. Disabled buttons render dimmed and don't fire.
static bool toolbarIconBtn(const char* id, const char* iconName, const char* tooltip,
                           bool enabled) {
    float btn = 24.0f * ui::uiScale;
    ImGui::PushID(id);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Always-on subtle pill; stronger wash on hover (draw-only — must not affect hit-test).
    ui::glassRowBackground(dl, p0, ImVec2(p0.x + btn, p0.y + btn), 3.0f, false, false, 0.22f);
    bool clicked = ImGui::InvisibleButton("##tb", ImVec2(btn, btn)) && enabled;
    bool hov = ImGui::IsItemHovered();
    if (hov && enabled)
        ui::glassRowBackground(dl, p0, ImVec2(p0.x + btn, p0.y + btn), 3.0f, false, false, 0.38f);
    float iz = 18.0f * ui::uiScale;
    const ui::AdaptiveSurface& surf = fmSurf();
    ImVec4 tintV = enabled ? surf.textPrimary
                           : ImVec4(surf.textTertiary.x, surf.textTertiary.y,
                                    surf.textTertiary.z, surf.textTertiary.w * (150.0f / 255.0f));
    drawIconGlass(dl, iconName, ImVec2(p0.x + btn * 0.5f, p0.y + btn * 0.5f), iz,
                 ImGui::ColorConvertFloat4ToU32(tintV), fmHalo(surf));
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    ImGui::PopID();
    return clicked;
}
// Exported wrapper (feature_tree.h) — the Explorer tab's toolbar reuses this exact button.
bool fmToolbarIconBtn(const char* id, const char* iconName, const char* tooltip, bool enabled) {
    return toolbarIconBtn(id, iconName, tooltip, enabled);
}

// ---- pane-header chip (feature_tree.h) ----
// Purpose-built line-art "feature tree" glyph: a root node with two branch children, stroked at
// the same weight as the row icons (1.3dp, like drawEyeGlyph) so it reads as part of the set —
// replaces the generic "history" three-dot atlas glyph that said nothing about features.
void drawFeatureTreeGlyph(ImDrawList* dl, ImVec2 mn, float sz, ImU32 ink) {
    const float S = ui::uiScale;
    const float th = std::max(1.0f, 1.3f * S);
    const float u = sz / 16.0f;                       // glyph designed on a 16-unit grid
    // Root node (top-left).
    ImVec2 r0(mn.x + 2.0f * u, mn.y + 2.0f * u), r1(mn.x + 6.5f * u, mn.y + 6.5f * u);
    dl->AddRect(r0, r1, ink, 1.0f * u, 0, th);
    // Trunk down from the root to the lower branch.
    const float trunkX = (r0.x + r1.x) * 0.5f;
    dl->AddLine(ImVec2(trunkX, r1.y), ImVec2(trunkX, mn.y + 12.75f * u), ink, th);
    // Two elbow branches to two child nodes.
    for (int i = 0; i < 2; ++i) {
        const float by = mn.y + (i == 0 ? 8.75f : 12.75f) * u;
        dl->AddLine(ImVec2(trunkX, by), ImVec2(mn.x + 9.0f * u, by), ink, th);
        dl->AddRect(ImVec2(mn.x + 9.0f * u, by - 1.75f * u),
                    ImVec2(mn.x + 13.5f * u, by + 1.75f * u), ink, 0.75f * u, 0, th);
    }
}

void fmPaneHeaderChip(AppState& st, const char* title, const char* titleIcon, float paneCenterX) {
    const float S = ui::uiScale;
    const ui::AdaptiveSurface& surf = fmSurf();
    const float iconSz  = 16.0f * S;
    const float iconGap = 6.0f * S;
    const float padX    = 10.0f * S;
    const float padY    = 5.0f * S;

    ImGui::PushFont(st.fonts.medium);
    const ImVec2 tsz = ImGui::CalcTextSize(title);
    const float chipH = std::max(tsz.y, iconSz) + 2.0f * padY;
    const float chipW = iconSz + iconGap + tsz.x + 2.0f * padX;

    const ImVec2 rowPos = ImGui::GetCursorScreenPos();
    const float  availW = ImGui::GetContentRegionAvail().x;
    // Center the chip on the pane's true center, clamped inside the content region so the pill
    // (and everything in it) always fits.
    float chipX = paneCenterX - chipW * 0.5f;
    chipX = std::max(rowPos.x, std::min(chipX, rowPos.x + availW - chipW));
    const ImVec2 c0(chipX, rowPos.y), c1(chipX + chipW, rowPos.y + chipH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    // ONE fitted pill sized from its content (the old chip painted a text-height row the 20px
    // icon overflowed, with the group centered off the pill).
    ui::glassRowBackground(dl, c0, c1, 6.0f, false, false, 0.26f);

    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
    const ImVec2 iMin(c0.x + padX, c0.y + (chipH - iconSz) * 0.5f);
    if (titleIcon && std::string(titleIcon) == "history") {
        drawFeatureTreeGlyph(dl, iMin, iconSz, ink);   // FM/PM: the feature-tree glyph
    } else if (titleIcon && *titleIcon) {
        drawIconGlass(dl, titleIcon, ImVec2(iMin.x + iconSz * 0.5f, iMin.y + iconSz * 0.5f),
                     iconSz, ink, fmHalo(surf));
    }
    dl->AddText(ImVec2(iMin.x + iconSz + iconGap, c0.y + (chipH - tsz.y) * 0.5f), ink, title);
    ImGui::PopFont();

    // Advance the layout cursor past the chip row (draw-list painting doesn't move it).
    ImGui::Dummy(ImVec2(availW, chipH));
}

// ---- C41: real file Save / Open (native dialogs + viewport refresh) -------------------------
// fm_save = classic Save: silent re-save to s_docPath, dialog only on first save (or Shift+click
// = Save As). fm_open = native Open dialog -> DocMirror::open -> applyOpenedDoc, the one shared
// "apply a just-opened doc" seam that resets the viewport AND refreshes the geometry (the old
// path-strip open updated the tree but never loaded the mesh).

// Save filter: native AI-CAD part only (a silent Save / first-save always writes .step+).
// Save As writes the native .step+ ONLY — non-native formats live under File > Export (the Win32
// dialog appends the default extension regardless of the chosen filter, so a multi-format Save As
// silently wrote .step+ anyway).
static const char* kStepPlusFilter = "AI-CAD part (*.step+)\0*.step+\0All files (*.*)\0*.*\0";

// F4: Open/Import filter — every format the engine can import (see engine/doc/formats.py
// FORMAT_REGISTRY / doc_formats RPC, the future dynamic source). A leading aggregate entry lets
// the user see all CAD+mesh files at once, then grouped entries narrow by family, then All files.
// Win32 common-dialog layout: NUL-separated "label\0patterns\0"... pairs, double-NUL terminated
// (the C string literal's own trailing NUL supplies the final one).
static const char* kImportFilter =
    "All CAD & mesh files\0*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.stl;*.3mf;*.obj;*.ply;*.gltf;*.glb;*.3dm;*.sldprt;*.sldasm;*.x_t;*.x_b\0"
    "AI-CAD part (*.step+)\0*.step+\0"
    "STEP (*.step;*.stp)\0*.step;*.stp\0"
    "IGES (*.iges;*.igs)\0*.iges;*.igs\0"
    "BREP (*.brep)\0*.brep\0"
    "Mesh (*.stl;*.obj;*.ply;*.3mf;*.gltf;*.glb;*.3dm)\0*.stl;*.obj;*.ply;*.3mf;*.gltf;*.glb;*.3dm\0"
    // SolidWorks/Parasolid import is experimental (engine routes via SolidWorks COM -> STEP).
    "SolidWorks/Parasolid (*.sldprt;*.sldasm;*.x_t;*.x_b)\0*.sldprt;*.sldasm;*.x_t;*.x_b\0"
    "All files (*.*)\0*.*\0";

// Mesh import formats that can be sewn into a solid on import (engine doc_open `as_solid`). B-rep
// sources (.step/.iges/.brep/.step+) already import as solids, so they are NOT here and never prompt.
// When a mesh file is picked we ask the user "import as solid?" (ui::promptImportMeshAsSolid).
static bool isMeshImportFormat(const std::string& path) {
    size_t dot = path.find_last_of('.');
    if (dot == std::string::npos) return false;
    std::string ext = path.substr(dot);
    for (char& ch : ext) ch = (char)std::tolower((unsigned char)ch);
    return ext == ".stl" || ext == ".obj" || ext == ".ply" || ext == ".3mf" ||
           ext == ".gltf" || ext == ".glb" || ext == ".3dm";
}

static std::string pathBaseName(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

// Escape hatch: headless shot scenarios / CI can't click a native dialog. With
// AI_CAD_NO_FILE_DIALOGS set the bottom strip keeps the old raw-path InputText fallback and
// the dialog-needing paths refuse politely instead of blocking a headless run.
static bool fileDialogsDisabled() {
    static const bool disabled = std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr;
    return disabled;
}

// Suggested file name for the first save: the doc's name (else "Part1") + ".step+".
static std::string suggestedSaveName(AppState& st) {
    std::string name = jfld(st.docMirror.tree(), "name");
    if (name.empty()) name = "Part1";
    if (name.find('.') == std::string::npos) name += ".step+";
    return name;
}

// Save the document. forceDialog = "Save As..." (always ask); otherwise the dialog only
// appears when no path is known yet (classic Save semantics).
static void doSaveDoc(AppState& st, bool forceDialog) {
    if (!st.docMirror.hasDoc()) return;
    st.addLog("[doc] save requested");
    std::string path = s_docPath;
    if (forceDialog || path.empty()) {
        if (fileDialogsDisabled()) {
            st.toast(ui::ToastLevel::Info,
                     "File dialogs disabled — use the path field below the tree");
            return;
        }
        std::string suggest = s_docPath.empty() ? suggestedSaveName(st)
                                                : pathBaseName(s_docPath);
        // Save / Save As always write the native .step+ (non-native formats are File > Export).
        path = saveFileDialog(kStepPlusFilter, "step+", suggest.c_str());
        if (path.empty()) return;  // canceled
    }

    std::string err;
    if (st.docMirror.save(st.worker, path, err)) {
        s_docPath = path;
        // Multi-document: a successful save clears the active tab's unsaved marker and records the
        // path so the tab tooltip / future Save reuses it.
        st.clearActiveTabDirty();
        if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
            st.docTabs[st.activeTab].path = path;
        // doc_save embeds this part's open drawings into the .step+, so any Drawing tabs sourced from
        // it are now persisted too — clear their unsaved markers.
        st.clearDrawingDirtyForSource(st.docMirror.docId());
        st.addLog("[doc] saved: " + path);
        st.toast(ui::ToastLevel::Info, "Saved " + pathBaseName(path));
    } else {
        st.addLog("[err] save: " + err);
        st.toast(ui::ToastLevel::Error, "Save failed: " + err);
    }
}

// Apply a JUST-OPENED document (DocMirror::open already succeeded, so the mirror holds the new
// doc id / revision / tree). Factored out so BOTH open paths (native dialog and the
// AI_CAD_NO_FILE_DIALOGS path-strip fallback) take the identical route.
//
// Geometry refresh: the worker's doc_open response DOES carry a tess block, but
// DocMirror::open() discards it (doc_mirror.cpp parses only doc_id/revision/tree and is owned
// elsewhere), so we fetch the mesh with a forced doc_rebuild — async via st.rpc to keep the GL
// thread live. A forced rebuild does NOT bump the revision (engine/doc/session.py rebuild()),
// so the mirror stays in step. The callback runs on the GL thread (AsyncRpc::drain), so
// loadDocMesh / toast / resync are all safe there.
static void applyOpenedDoc(AppState& st, const std::string& path) {
    st.clearViewportForNewDoc();   // resets viewport + tree transients (clears s_docPath too)
    s_docPath = path;
    // Multi-document: an opened file lands in its own tab (DocMirror::open already pointed the
    // mirror at the new worker doc). The tab is born clean (just-loaded == saved) with its path.
    {
        bool isAsm = jfld(st.docMirror.tree(), "schema") == "aicad.asm/1";
        st.addDocTab(isAsm);
        if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
            st.docTabs[st.activeTab].path = path;
            st.docTabs[st.activeTab].dirty = false;
        }
        // A real file is now open — discard the blank launch "starter" example if it's still
        // pristine (the user opened instead of modeling in it). Keeps the just-opened tab.
        st.dropBlankStarterTabs(st.activeTab);
    }
    st.addLog("[doc] opened: " + path + "  rev=" + std::to_string(st.docMirror.revision()));
    if (!st.handoffQuietOpen)   // silent on a live MCP-handoff refresh (would toast every build step)
        st.toast(ui::ToastLevel::Info, "Opened " + pathBaseName(path));
    // W12+ — re-open any drawings the engine restored from this .step+ (their `source` already
    // re-pointed to the just-opened part). Each becomes a Drawing tab ALONGSIDE the part tab (the
    // part stays active). Tolerant: an empty/absent list is a no-op (a file with no drawings).
    {
        const nlohmann::json& drws = st.docMirror.lastOpenedDrawings();
        if (drws.is_array() && !drws.empty()) {
            for (const auto& d : drws) {
                if (!d.is_object()) continue;
                st.adoptDrawingTab(jfld(d, "doc_id"), jfld(d, "name"), jfld(d, "source_doc_id"));
            }
            if (!st.handoffQuietOpen)
                st.toast(ui::ToastLevel::Info,
                         "Restored " + std::to_string(drws.size()) + " drawing(s)");
        }
    }
    std::string base = pathBaseName(path);
    // Multi-document: bind this open-refresh to the doc it targets so a tab switch before the async
    // result drains can't load THIS doc's mesh / tree into whatever doc is foreground by then.
    const std::string openDocId = st.docMirror.docId();
    st.rpc.call("doc_rebuild", {{"doc_id", openDocId}},
        [&st, base, openDocId](const core::RpcResult& r) {
            if (st.docMirror.docId() != openDocId) {
                st.addLog("[open] mesh refresh for " + openDocId + " ignored — active doc changed");
                return;
            }
            if (!r.ok || r.result.contains("code")) {
                std::string msg = !r.ok ? r.err : jfld(r.result, "message");
                st.addLog("[err] open mesh refresh: " + msg);
                st.toast(ui::ToastLevel::Warn,
                         "Opened " + base + " — mesh refresh failed: " + msg);
                return;
            }
            std::string aicm;
            if (r.result.contains("tess") && r.result["tess"].is_object())
                aicm = jfld(r.result["tess"], "aicm_path");
            if (!aicm.empty()) {
                std::string lerr;
                if (!st.loadDocMesh(aicm, lerr) && !lerr.empty())
                    st.addLog("[err] loadDocMesh: " + lerr);
            }
            // Flat 2D imports (DXF/DWG) lie exactly on a datum plane AND share its light-blue
            // colour, so they wash out against it (the geometry is drawn but invisible until the
            // planes are hidden). When the imported body has ~no volume, hide the default datum
            // planes and face the drawing head-on (Top view) so it just shows up. Solids
            // (volume > 0) keep the planes and the default framing — no behaviour change for them.
            if (!aicm.empty() && r.result.contains("volume") &&
                std::abs(r.result.value("volume", 1.0)) < 1e-6) {
                st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = false;
                st.planesUserToggled = true;   // stick — don't let the auto-show re-reveal them
                st.yaw = 0.0f; st.pitch = 89.5f; st.zoom = 1.0f;   // Top: face the flat drawing
                st.addLog("[open] flat 2D import — hid datum planes + oriented Top");
            }
            // doc_open left the mirror without feature_status / owner_map (DocMirror::open
            // clears them); doc_tree is a cheap no-rebuild pull that restores both (status
            // badges + feature->face highlights) and fires the listener so the tree repaints.
            st.docMirror.resync(st.worker);
        }, 60000);
}

// Native-dialog Open: pick a .step+ file, open it in the worker, apply it.
static void doOpenDoc(AppState& st) {
    if (fileDialogsDisabled()) {
        st.toast(ui::ToastLevel::Info,
                 "File dialogs disabled — use the path field below the tree");
        return;
    }
    std::string path = openFileDialog(kImportFilter);
    if (path.empty()) return;  // canceled
    // Mesh files: ask whether to import as a solid (vs. graphics mesh). Non-mesh formats never prompt.
    bool asSolid = false;
    if (isMeshImportFormat(path)) {
        int choice = ui::promptImportMeshAsSolid();
        if (choice < 0) return;        // canceled
        asSolid = (choice == 1);
    }
    std::string err;
    if (st.docMirror.open(st.worker, path, err, asSolid)) {
        applyOpenedDoc(st, path);
    } else {
        st.addLog("[err] open: " + err);
        st.toast(ui::ToastLevel::Error, "Open failed: " + err);
    }
}

// Features section header — centered "Features" label + hairlines, the collapse chevron pinned at
// the far right, and TWO small right-aligned icon buttons (Add Folder, then Name & group) sitting
// just left of the chevron. They carry 2x a single button's right-indent (two buttons) mirroring the
// chevron's own inset, per Hunter's spec. Returns the (possibly toggled) open state; sets
// *addFolder / *nameGroup on the frame their button is clicked. addEn/ngEn gray the buttons out.
static bool fmFeaturesHeader(bool* open, bool addEn, bool ngEn, bool* addFolder, bool* nameGroup) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    const float chev = 18.0f * S;
    const float btn = 18.0f * S;
    const float bgap = 3.0f * S;
    ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    const float lineH = ImGui::GetTextLineHeight();
    const float rowH = std::max(lineH + 4.0f * S, chev + 2.0f * S);
    const ImVec2 tsz = ImGui::CalcTextSize("Features");
    const float cy = p.y + rowH * 0.5f;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    ImGui::PushID("fm_features_hdr");
    // Whole-row collapse toggle FIRST; the two buttons + chevron are submitted on top and consume
    // their own clicks (SetNextItemAllowOverlap lets the later items receive them).
    ImGui::SetCursorScreenPos(p);
    ImGui::SetNextItemAllowOverlap();
    const bool rowClicked = ImGui::InvisibleButton("##feat_toggle", ImVec2(w, rowH));
    const bool rowHov = ImGui::IsItemHovered();
    if (rowClicked) *open = !*open;
    if (rowHov) ui::glassRowBackground(dl, p, ImVec2(p.x + w, p.y + rowH), 3.0f, false, false, 0.16f);

    // Layout: the two folder buttons sit at the far LEFT, then the "Features" label just to their
    // right, a hairline filling the gap, and the collapse chevron pinned far right (Hunter red-pen).
    const float chevX = p.x + w - chev;
    const float addX = p.x + 4.0f * S;
    const float ngX = addX + btn + bgap;
    const float textX = ngX + btn + 8.0f * S;   // label starts right of the button cluster
    const float textY = p.y + (rowH - lineH) * 0.5f;
    const ImU32 lineCol = ui::Palette::toU32(pal.border);
    dl->AddText(ImVec2(textX, textY),
                ImGui::ColorConvertFloat4ToU32(ImVec4(pal.treeSection[0], pal.treeSection[1],
                                                      pal.treeSection[2], pal.treeSection[3])),
                "Features");
    // Hairline from just right of the label to just left of the chevron.
    const float rStart = textX + tsz.x + 6.0f * S;
    const float rEnd = chevX - 8.0f * S;
    if (rStart < rEnd) dl->AddLine(ImVec2(rStart, cy), ImVec2(rEnd, cy), lineCol, std::max(1.0f, 1.0f * S));

    // Small glass-pill icon button helper (drawn atop the row toggle).
    auto iconBtn = [&](float x, const char* iconName, bool enabled, const char* tip) -> bool {
        const ImVec2 bMin(x, p.y + (rowH - btn) * 0.5f), bMax(bMin.x + btn, bMin.y + btn);
        ImGui::SetCursorScreenPos(bMin);
        ImGui::SetNextItemAllowOverlap();
        const bool clicked = enabled && ImGui::InvisibleButton(iconName, ImVec2(btn, btn));
        const bool hov = enabled && ImGui::IsItemHovered();
        if (hov) ui::glassRowBackground(dl, bMin, bMax, 3.0f, false, false, 0.40f);
        const ImVec2 bc((bMin.x + bMax.x) * 0.5f, (bMin.y + bMax.y) * 0.5f);
        const ImU32 col = enabled ? ImGui::ColorConvertFloat4ToU32(fmSurf().textPrimary)
                                  : ImGui::ColorConvertFloat4ToU32(ImVec4(0.55f, 0.55f, 0.55f, 0.5f));
        ui::drawIconGlass(dl, iconName, bc, btn * 0.42f, col, fmHalo(fmSurf()));
        if (hov && tip) ImGui::SetTooltip("%s", tip);
        return clicked;
    };
    if (addFolder)
        *addFolder = iconBtn(addX, "folder_new", addEn,
                             "Add Folder\n(groups the selected features, or drops an empty folder to "
                             "drag features into)");
    if (nameGroup)
        *nameGroup = iconBtn(ngX, "name_group_features", ngEn,
                             "Name & group features\n(auto-name every feature and sort into folders)");

    // Collapse chevron (down = open, right = closed).
    const ImVec2 cc(chevX + chev * 0.5f, cy);
    const float a = chev * 0.24f;
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(fmSurf().textPrimary);
    if (*open)
        dl->AddTriangleFilled(ImVec2(cc.x - a, cc.y - a * 0.6f), ImVec2(cc.x + a, cc.y - a * 0.6f),
                              ImVec2(cc.x, cc.y + a * 0.7f), ink);
    else
        dl->AddTriangleFilled(ImVec2(cc.x - a * 0.6f, cc.y - a), ImVec2(cc.x - a * 0.6f, cc.y + a),
                              ImVec2(cc.x + a * 0.7f, cc.y), ink);
    ImGui::PopID();

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, rowH));
    return *open;
}

// Group the currently-selected feature(s) into a new folder. Folder is a display-only tag (set_folder
// op), so a CONTIGUOUS selection becomes one folder block at the first selected feature's position
// with zero reordering — geometry is never touched. (Non-contiguous grouping that would need a
// history reorder is gated behind a geometry-invariance check, added separately.)
static void createFolderFromSelection(AppState& st) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return;

    // Selected fids = primary + multi-select, kept in history (array) order.
    std::unordered_set<std::string> sel = s_multiSel;
    if (!st.selectedFeatureId.empty()) sel.insert(st.selectedFeatureId);

    std::vector<std::string> members;
    std::unordered_set<std::string> existingFolders;
    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        const std::string fid = jfld(f, "id");
        const std::string fl = jfld(f, "folder");
        if (!fl.empty()) existingFolders.insert(fl);
        if (sel.count(fid)) members.push_back(fid);
    }
    // Unique "Folder<N>" name — unique across BOTH tagged folders and the blank pending ones.
    std::string name;
    for (int n = 1;; ++n) {
        name = "Folder" + std::to_string(n);
        if (!existingFolders.count(name) &&
            std::find(s_pendingFolders.begin(), s_pendingFolders.end(), name) == s_pendingFolders.end())
            break;
    }
    if (members.empty()) {
        // Nothing selected → create an empty folder at the bottom (rendered from s_pendingFolders)
        // that features can be dragged into.
        s_pendingFolders.push_back(name);
        st.toast(ui::ToastLevel::Info, "Added " + name + " — drag features into it");
        return;
    }
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& fid : members)
        ops.push_back({{"op", "set_folder"}, {"id", fid}, {"folder", name}});
    applyTreeOps(st, ops, "add folder");
    st.addLog("[tree] grouped " + std::to_string(members.size()) + " feature(s) into " + name);
}


// ---- "Name & group" — SolidWorks-style auto-naming + folder grouping ----
// Renames every feature to a conventional per-type name ("Boss-Extrude1", "Fillet2", ...) and
// sorts dress-up / pattern / reference / sketch features into folders. One batched op set, one
// rebuild. (A future revision can route this through the AI agent for semantic names.)
static void nameAndGroupFeatures(AppState& st) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return;

    std::map<std::string, int> counts;
    nlohmann::json ops = nlohmann::json::array();

    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        std::string fid = jfld(f, "id");
        std::string ty  = jfld(f, "type");
        std::string mode;
        if (f.contains("params") && f["params"].is_object())
            mode = jfld(f["params"], "mode");
        bool cut = (mode == "cut");

        std::string base, folder;
        if      (ty == "sketch")           { base = "Sketch";              folder = "Sketches"; }
        else if (ty == "extrude")          { base = cut ? "Cut-Extrude" : "Boss-Extrude"; }
        else if (ty == "revolve")          { base = cut ? "Cut-Revolve" : "Boss-Revolve"; }
        else if (ty == "sweep")            { base = cut ? "Cut-Sweep"   : "Boss-Sweep"; }
        else if (ty == "loft")             { base = cut ? "Cut-Loft"    : "Boss-Loft"; }
        else if (ty == "fillet")           { base = "Fillet";   folder = "Dress-up"; }
        else if (ty == "chamfer")          { base = "Chamfer";  folder = "Dress-up"; }
        else if (ty == "shell")            { base = "Shell";    folder = "Dress-up"; }
        else if (ty == "draft")            { base = "Draft";    folder = "Dress-up"; }
        else if (ty == "rib")              { base = "Rib";      folder = "Dress-up"; }
        else if (ty == "hole")             { base = "Hole"; }
        else if (ty == "linear_pattern")   { base = "LPattern"; folder = "Patterns"; }
        else if (ty == "circular_pattern") { base = "CPattern"; folder = "Patterns"; }
        else if (ty == "mirror")           { base = "Mirror";   folder = "Patterns"; }
        else if (ty == "plane" || ty == "datum_plane") { base = "Plane"; folder = "Reference"; }
        else if (ty == "axis")             { base = "Axis";  folder = "Reference"; }
        else if (ty == "point")            { base = "Point"; folder = "Reference"; }
        else                                { base = ty.empty() ? "Feature" : ty; }

        int n = ++counts[base];
        std::string name = base + std::to_string(n);
        ops.push_back({{"op", "rename"}, {"id", fid}, {"name", name}});
        ops.push_back({{"op", "set_folder"}, {"id", fid}, {"folder", folder}});
    }
    if (!ops.empty()) {
        applyTreeOps(st, ops, "name & group");
        st.addLog("[tree] named & grouped " + std::to_string(ops.size() / 2) + " features");
    }
}

// ---- C37-E (item 4): the rollback bar widget ----
// Draws a thick grabbable horizontal line at the CURRENT cursor position (the caller places it right
// after the bar feature's row, or after the last row when the bar is at the end). The bar is a
// proper drag interaction: hovering shows a vertical-resize cursor + a brighter line; dragging it and
// releasing over a row gap enqueues set_rollback{after: <fid above the drop gap> | null}. It also
// remains a DROP TARGET for a dragged FEATURE_ROW (drop-to-end reorder, kept from C24).
//
// While dragging, the per-row hit-test in the feature walk records s_rbDropAfter (the feature the
// bar would land after); the walk's release handler turns it into set_rollback. `enabled` (canDoc)
// gates the mutating feature-row drop. `barAfterFid` (non-empty = the bar sits mid-history) also
// drives the "rolled back" zone label drawn below the bar.
static void drawRollbackBarLine(AppState& st, bool enabled, const std::string& barAfterFid) {
    const auto& pal = fmPalette();
    ImGui::Dummy(ImVec2(0, 2.0f * ui::uiScale));
    ImVec2 r0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    float  rsc = ui::uiScale;
    float  barH = 12.0f * rsc;          // #4.6: thicker grabbable band height
    // An invisible button across the whole band gives us hover + active (drag) state.
    ImGui::PushID("##rollback_bar");
    ImGui::InvisibleButton("##rb", ImVec2(w, barH));
    bool hovered = ImGui::IsItemHovered();
    bool active  = ImGui::IsItemActive();
    if (hovered || active) st.requestCursor(ui::CursorKind::ResizeV);
    if (active) s_rollbackDragging = true;

    ImDrawList* rdl = ImGui::GetWindowDrawList();
    float ly = r0.y + barH * 0.5f;
    ImU32 lineCol = ui::Palette::toU32((hovered || active) ? pal.accent : pal.accentMid);
    rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly), lineCol,
                 std::max(1.0f, (hovered || active) ? 3.0f * rsc : 2.0f * rsc));  // #4.6
    float hw = 28.0f * rsc, hh = 8.0f * rsc, hx = r0.x + (w - hw) * 0.5f;  // #4.6: bigger handle
    rdl->AddRectFilled(ImVec2(hx, ly - hh * 0.5f), ImVec2(hx + hw, ly + hh * 0.5f),
                       ui::Palette::toU32(pal.accent), std::max(1.0f, 3.0f * rsc));  // #4.5
    if (hovered && !active)
        ImGui::SetTooltip("Rollback bar — drag up/down to roll the model back/forward,\n"
                          "or drop a feature here to move it to the end");

    // Drop target: a dragged feature row landing on the bar goes to the end (kept from C24).
    if (enabled && ImGui::BeginDragDropTarget()) {
        rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly),
                     ui::Palette::toU32(pal.accent), std::max(1.0f, 2.5f * rsc));  // #4.5
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            applyTreeOps(st, nlohmann::json::array(
                {{{"op", "reorder_feature"}, {"id", dragged}}}), "reorder");
        }
        ImGui::EndDragDropTarget();
    }
    // Bar mid-history (features below are rolled back): a dim centered label makes the divider's
    // meaning obvious — clearly not a plain separator. Skipped when the bar is at the end.
    if (!barAfterFid.empty()) {
        ImGui::Dummy(ImVec2(0, 3.0f * rsc));
        const char* rbLbl = "↓ rolled back";
        float rbW = ImGui::CalcTextSize(rbLbl).x;
        float rbX0 = ImGui::GetCursorPosX();
        ImGui::SetCursorPosX(rbX0 + std::max(0.0f, (w - rbW) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.55f));
        ImGui::TextUnformatted(rbLbl);
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * rsc));
    }
    ImGui::PopID();
}

static char s_fmFilter[128] = {};

static bool fm_icontains(const char* hay, const char* needle) {
    if (!needle || !needle[0]) return true;
    if (!hay) return false;
    for (const char* h = hay; *h; ++h) {
        const char* ph = h, *pn = needle;
        while (*ph && *pn) {
            char a = (*ph >= 'A' && *ph <= 'Z') ? (char)(*ph + 32) : *ph;
            char b = (*pn >= 'A' && *pn <= 'Z') ? (char)(*pn + 32) : *pn;
            if (a != b) break;
            ++ph; ++pn;
        }
        if (!*pn) return true;
    }
    return false;
}

// ---- C24: FeatureManager top strip — a SolidWorks-style tab row (tree active; properties /
// configurations tabs are visual stubs) plus a compact document toolbar (New Part / New Assembly /
// Open / Save / Undo / Redo) that replaces the old wide text-button strip at the panel bottom.
static void drawFeatureManagerTopStrip(AppState& st) {
    const auto& pal = fmPalette();
    bool canDoc = st.workerStarted && !st.job.running.load();
    const float S = ui::uiScale;

    // W5C: the old tree/Properties/Configurations stub tabs were removed here — they were visual
    // decoration that duplicated the real vertical tab strip in the LeftPane container (layout.cpp
    // drawLeftPane). This strip now hosts only the functional document toolbar + "Name & group".
    // --- centered document toolbar ---
    // glassRowAtCursor is draw-only and does not advance the layout cursor. Without an explicit
    // row reservation the full-width filter InputText below was placed at the same Y and stole
    // clicks from undo/redo (and the rest of the toolbar).
    // "Name & group" sits inline in the toolbar (between Save and Undo). It only applies to a part
    // doc that actually has features; computed up here so the toolbar width/centering is stable.
    bool isPart = st.docMirror.hasDoc() &&
                  jfld(st.docMirror.tree(), "schema") != "aicad.asm/1";
    int featCount = 0;
    if (isPart) {
        const nlohmann::json& tr = st.docMirror.tree();
        auto fa = tr.find("features");
        if (fa != tr.end() && fa->is_array()) featCount = (int)fa->size();
    }
    const bool canNameGroup = canDoc && isPart && featCount > 0;

    // Name & group moved OUT of this toolbar down to the Features section header (fmFeaturesHeader),
    // so the toolbar is now 6 buttons (new / new-asm / open / save / undo / redo).
    (void)canNameGroup;
    const float bw = 24.0f * S, gap = 2.0f * S, nBtns = 6.0f;
    const float toolW = nBtns * bw + (nBtns - 1) * gap;
    ImVec2 toolRowMin = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    ui::glassRowBackground(ImGui::GetWindowDrawList(), toolRowMin,
                           ImVec2(toolRowMin.x + availW, toolRowMin.y + bw),
                           3.5f, false, false, 0.24f);
    float bx = toolRowMin.x + std::max(0.0f, (availW - toolW) * 0.5f);
    const float by = toolRowMin.y;
    auto toolBtn = [&](const char* id, const char* icon, const char* tip, bool en) -> bool {
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        bool hit = toolbarIconBtn(id, icon, tip, en);
        bx += bw + gap;
        return hit;
    };
    ImGui::PushID("fm_toolbar");
    if (toolBtn("fm_new", "new_doc", "New Part", canDoc) && canDoc) {
        std::string err;
        // Multi-document: New Part opens a NEW tab (it does not replace the current doc). The
        // worker registry keeps the previous doc alive; addDocTab records this one and activates it.
        st.suppressTabDirty = true;
        bool ok = st.docMirror.newPart(st.worker, err);
        st.suppressTabDirty = false;
        if (ok) {
            st.addLog("[doc] new part  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();   // C34: fresh part = clean viewport (planes state)
            st.addDocTab(/*isAssembly=*/false);
        }
        else st.addLog("[err] new part: " + err);
    }
    if (toolBtn("fm_newasm", "assembly", "New Assembly", canDoc) && canDoc) {
        std::string err;
        st.suppressTabDirty = true;
        bool ok = st.docMirror.newAssembly(st.worker, err);
        st.suppressTabDirty = false;
        if (ok) {
            st.addLog("[doc] new assembly  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();   // C34
            st.addDocTab(/*isAssembly=*/true);
            st.showRibbon = true; st.ribbonTab = AppState::RT_Assembly;
        } else st.addLog("[err] new assembly: " + err);
    }
    if (toolBtn("fm_open", "open", "Open...", canDoc) && canDoc)
        doOpenDoc(st);   // C41: native dialog -> DocMirror::open -> viewport refresh
    bool canSave = canDoc && st.docMirror.hasDoc();
    if (toolBtn("fm_save", "save", "Save  (Shift-click: Save As...)", canSave) && canSave)
        doSaveDoc(st, /*forceDialog=*/ImGui::GetIO().KeyShift);   // C41
    // W1-B (item 4): route through the shared AppState helper so the toolbar buttons and the global
    // Ctrl+Z / Ctrl+Y keys take the identical path (mesh refresh + owner map + confirmation toast).
    if (toolBtn("fm_undo", "undo", "Undo", canDoc) && canDoc) st.docUndo();
    if (toolBtn("fm_redo", "redo", "Redo", canDoc) && canDoc) st.docRedo();
    ImGui::PopID();
    ImGui::SetCursorScreenPos(ImVec2(toolRowMin.x, toolRowMin.y + bw));
    ImGui::Dummy(ImVec2(availW, 0));

    ImGui::Spacing();
    {
        const float filterH = ImGui::GetFrameHeight();
        ImVec2 fMin = ImGui::GetCursorScreenPos();
        const float fW = ImGui::GetContentRegionAvail().x;
        ui::glassRowBackground(ImGui::GetWindowDrawList(), fMin,
                               ImVec2(fMin.x + fW, fMin.y + filterH),
                               3.5f, false, false, 0.24f);
        ImGui::SetCursorScreenPos(fMin);
        ImGui::SetNextItemWidth(fW);
        ImGui::InputTextWithHint("##fm_filter", "Filter features...", s_fmFilter, sizeof(s_fmFilter));
        ImGui::SetCursorScreenPos(ImVec2(fMin.x, fMin.y + filterH));
        ImGui::Dummy(ImVec2(0, 0));
    }

    ImGui::Separator();
}

// ---- main draw function ----

// Wave 1D: the tree body WITHOUT Begin/End — the caller (drawFeatureTree wrapper, or the Wave 1D
// drawLeftPane container) owns the surrounding window/child. Everything below was the interior of
// the former drawFeatureTree(); the only change is the removed Begin/End at the top and bottom.
void drawFeatureTreeBody(AppState& st,
                         const std::function<void(const std::string&)>& editCallback) {
    const ui::AdaptiveSurface& lpSurf = fmSurf();
    fmPaletteRefresh(ui::viewportBackdrop().valid() ? &lpSurf : nullptr);
    const auto& pal = fmPalette();
    s_fmStripeIdx = 0;

    // Apply a drag-and-drop "file into folder" requested last frame BEFORE we capture the tree below,
    // so the rebuilt tree renders the feature under its new folder tag. Deferred here (not mid-walk)
    // because applyTreeOps rebuilds the doc and would invalidate the walk's tree/features refs.
    // NOTE: this only re-tags the feature (display grouping). Reordering model history so a folder's
    // members become contiguous — only when geometry is unaffected — is still TODO (needs an engine
    // shadow-eval; see work notes) and is intentionally NOT done here.
    if (!s_dropFid.empty() && !s_dropFolder.empty()) {
        nlohmann::json ops = nlohmann::json::array();
        ops.push_back({{"op", "set_folder"}, {"id", s_dropFid}, {"folder", s_dropFolder}});
        applyTreeOps(st, ops, "move to folder");
        s_dropFid.clear();
        s_dropFolder.clear();
    }

    // Compact vertical rhythm: the fixed header rows (Equations/Material/References/Origin/planes)
    // and the feature list inherit the theme's loose ItemSpacing.y (9dp), which read "double
    // spaced". Tighten Y only (keep X for SameLine footers/top strip) for the whole tree body.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * ui::uiScale));

    // C24: SolidWorks-style top strip (tab row + compact doc toolbar), replacing the old wide
    // New Part / Undo / Redo text buttons that used to live at the panel bottom.
    drawFeatureManagerTopStrip(st);

    // ---- document title ----
    const nlohmann::json& tree = st.docMirror.tree();
    std::string docName = "Part1";
    if (tree.is_object()) docName = jfld(tree, "name");
    if (docName.empty()) docName = "Part1";

    // C37-E (item 2): the SAME guard the top toolbar uses (st.workerStarted && !st.job.running).
    // Every mutating tree interaction — context menu, drag-reorder, Ctrl+Up/Down, rollback drag —
    // is gated on this so no op fires while an agent run is in flight.
    bool canDocTree = st.workerStarted && !st.job.running.load();

    {
        // Filename / References / Features are three INDEPENDENT collapsible sections (Hunter):
        // collapsing the filename hides only its equations/material/solid-bodies, leaving the
        // References and Features sections in place. All three use the same section-header look.
        const float kSecIndent = 8.0f * ui::uiScale;
        static bool s_fileSecOpen = true;
        static bool s_refSecOpen  = true;
        static bool s_featSecOpen = true;
        bool fileRightClicked = false;
        fmSectionDividerCollapse(docName.c_str(), &s_fileSecOpen, &fileRightClicked);

        // C26: filename "Appearance…" — sets the primary body's color (the common single-body
        // case). Body key "b1" matches the engine's per-body appearance key (session._write_aicm).
        // Routed through the DEFERRED applyTreeOps queue, like every other tree-originated op.
        {
            bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            static float s_rootColor[3] = {0.71f, 0.76f, 0.82f};
            static float s_rootMetallic = 0.0f, s_rootRoughness = 0.5f;  // C32-A material
            static bool  s_openRootPicker = false;
            if (!isAsmDoc && st.docMirror.hasDoc()) {
                if (fileRightClicked) ImGui::OpenPopup("##root_ctx");
                pushTreePopupGlass();
                if (ImGui::BeginPopup("##root_ctx")) {
                    if (ImGui::MenuItem("Appearance...")) {
                        s_rootColor[0] = 0.71f; s_rootColor[1] = 0.76f; s_rootColor[2] = 0.82f;
                        s_rootMetallic = 0.0f; s_rootRoughness = 0.5f;
                        s_openRootPicker = true;  // open the picker (a sibling popup) next
                    }
                    if (ImGui::MenuItem("Clear Appearance")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op", "set_appearance"}, {"id", "b1"}, {"color", nullptr}}}),
                            "set_appearance");
                    }
                    ImGui::EndPopup();
                }
                popTreePopupGlass();
                // Picker popup lives OUTSIDE the context menu (a sibling) to avoid nested-popup
                // ordering pitfalls; it is opened by the menu item via s_openRootPicker.
                if (s_openRootPicker) { ImGui::OpenPopup("##appearance_root"); s_openRootPicker = false; }
                pushTreePopupGlass();
                if (appearanceColorPicker("##appearance_root", s_rootColor,
                                          &s_rootMetallic, &s_rootRoughness)) {
                    applyTreeOps(st, nlohmann::json::array({
                        {{"op", "set_appearance"}, {"id", "b1"},
                         {"color", {s_rootColor[0], s_rootColor[1], s_rootColor[2]}},
                         {"metallic", s_rootMetallic}, {"roughness", s_rootRoughness}}}),
                        "set_appearance");
                }
                popTreePopupGlass();
            }
        }

        if (s_fileSecOpen) {
            ImGui::Indent(kSecIndent);   // nest equations/material/bodies under the filename header

            // ---- fixed header nodes ----
            // Equations
            {
                int eqCount = 0;
                if (tree.is_object()) {
                    auto eq = tree.find("equations");
                    if (eq != tree.end() && eq->is_object()) {
                        auto gl = eq->find("globals");
                        if (gl != eq->end() && gl->is_array())
                            eqCount = (int)gl->size();
                    }
                }
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth |
                                       ImGuiTreeNodeFlags_FramePadding;   // pill-height node
                char eqLabel[64];
                std::snprintf(eqLabel, sizeof(eqLabel), "Equations (%d)##eqhdr", eqCount);
                FmRowRect eqRect = fmRowPill();
                ImVec2 eqRowPos = ImGui::GetCursorScreenPos();
                ImGui::TreeNodeEx(eqLabel, f);
                treeIconInArrowSlot(eqRowPos, "equations", "[=]");
                fmRowPillEnd(eqRect);
                // Double-click the node to open the Equations / Global Variables editor (SolidWorks).
                if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left))
                    st.showEquations = true;
            }

            // Material
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth |
                                       ImGuiTreeNodeFlags_FramePadding;   // pill-height node
                std::string matKey;
                if (tree.is_object()) {
                    auto m = tree.find("material");
                    if (m != tree.end() && m->is_object()) {
                        std::string key = jfld(*m, "key");
                        if (!key.empty()) matKey = key;
                    }
                }
                FmRowRect matRect = fmRowPill();
                ImVec2 matRowPos = ImGui::GetCursorScreenPos();
                ImGui::TreeNodeEx("Material##mat_hdr", f);
                treeIconInArrowSlot(matRowPos, "material", "[M]");
                fmRowPillEnd(matRect);
                // W2 (material UX): right-click the Material node to assign a PHYSICAL material.
                // Each choice (a) sets the part's material key (set_material) — read by FEA studies
                // and shown on this row — and (b) recolours every body to the material's look via a
                // derived set_appearance (the material->appearance map in appearance_common). An
                // Appearance-tab edit applied later still wins (it is a later set_appearance). Ops are
                // DEFERRED through applyTreeOps like every other tree-originated op (UAF-safe).
                {
                    bool matAsm = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
                    if (!matAsm && st.docMirror.hasDoc()) {
                        pushTreePopupGlass();
                        if (ImGui::BeginPopupContextItem("##mat_ctx")) {
                            ImGui::TextDisabled("Assign Material");
                            ImGui::Separator();
                            // The set_appearance keys are AICM body ordinals ("b<n>"); recolour every
                            // body so a multi-body part takes the material wholesale. Empty body list
                            // (mesh not loaded yet) falls back to "b1", the common single-body case.
                            std::vector<uint32_t> bodies = st.docBodyIds;
                            if (bodies.empty()) bodies.push_back(1);
                            for (int i = 0; i < kPhysicalMaterialsCount; ++i) {
                                const auto& pm = kPhysicalMaterials[i];
                                bool cur = (matKey == pm.key);
                                if (ImGui::MenuItem(pm.name, nullptr, cur)) {
                                    float col[3]; float mtl = 0.0f, rgh = 0.5f;
                                    materialAppearance(pm.key, col, mtl, rgh);
                                    nlohmann::json ops = nlohmann::json::array();
                                    ops.push_back({{"op", "set_material"}, {"key", pm.key}});
                                    for (uint32_t bid : bodies)
                                        ops.push_back({{"op", "set_appearance"},
                                                       {"id", "b" + std::to_string(bid)},
                                                       {"color", {col[0], col[1], col[2]}},
                                                       {"metallic", mtl}, {"roughness", rgh}});
                                    applyTreeOps(st, ops, "set_material");
                                }
                            }
                            if (!matKey.empty()) {
                                ImGui::Separator();
                                if (ImGui::MenuItem("Remove Material")) {
                                    applyTreeOps(st, nlohmann::json::array({
                                        {{"op", "set_material"}, {"key", nullptr}}}),
                                        "set_material");
                                }
                            }
                            ImGui::EndPopup();
                        }
                        popTreePopupGlass();
                    }
                }
                // Show the friendly material name (kPhysicalMaterials) when the assigned key is one
                // we know; fall back to the raw engine key for any other (custom / MCP-set) material.
                const char* matLabel = matKey.c_str();
                for (int i = 0; i < kPhysicalMaterialsCount; ++i)
                    if (matKey == kPhysicalMaterials[i].key) { matLabel = kPhysicalMaterials[i].name; break; }
                if (!matKey.empty()) {
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1.0f));
                    ImGui::TextUnformatted(matLabel);
                    ImGui::PopStyleColor();
                }
                if (ImGui::IsItemHovered()) {
                    if (matKey.empty()) ImGui::SetTooltip("No material assigned — right-click to assign");
                    else ImGui::SetTooltip("Material: %s", matLabel);
                }
            }

            // ---- Solid Bodies folder (per-body hide/show, SolidWorks-style) ----
            // Part docs only, when the loaded doc mesh has at least one body. One row per
            // renderer body slot: click toggles visibility (same interaction as the plane rows
            // below); the context menu adds Hide/Show, Isolate (when every OTHER body is already
            // hidden it acts as SolidWorks Exit Isolate = Show All), and Show All Bodies. Pure
            // host-side state — see setBodyShown above.
            {
                bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
                int nBodies = (int)st.bodyVisible.size();
                if (!isAsmDoc && st.hasDocMesh && nBodies >= 1) {
                    char hdr[64];
                    std::snprintf(hdr, sizeof(hdr), "Solid Bodies (%d)##solid_bodies", nBodies);
                    FmRowRect bodiesRect = fmRowPill();
                    treeIcon("body_folder", "[F]", 16.0f);   // #4.4: was borrowed "mates_folder"
                    ImGui::SameLine(0, 4 * ui::uiScale);   // #4.5
                    bool open = ImGui::TreeNodeEx(hdr, ImGuiTreeNodeFlags_DefaultOpen |
                                                       ImGuiTreeNodeFlags_SpanFullWidth |
                                                       ImGuiTreeNodeFlags_FramePadding);
                    fmRowPillEnd(bodiesRect);
                    if (open) {
                        for (int bi = 0; bi < nBodies; ++bi) {
                            bool hidden = !st.bodyVisible[bi];
                            uint32_t bid = bi < (int)st.docBodyIds.size()
                                               ? st.docBodyIds[bi] : (uint32_t)bi + 1;
                            char bname[32];
                            std::snprintf(bname, sizeof(bname), "Body%u", bid);
                            ImGuiTreeNodeFlags bf = ImGuiTreeNodeFlags_Leaf |
                                                    ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                    ImGuiTreeNodeFlags_SpanFullWidth |
                                                    ImGuiTreeNodeFlags_FramePadding;
                            FmRowRect bodyRect = fmRowPill();
                            if (hidden)
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(pal.textDim[0], pal.textDim[1],
                                           pal.textDim[2], 0.85f));
                            char rowId[48];
                            std::snprintf(rowId, sizeof(rowId), "%s##body%d", bname, bi);
                            ImVec2 bodyRowPos = ImGui::GetCursorScreenPos();
                            ImGui::TreeNodeEx(rowId, bf);
                            treeIconInArrowSlot(bodyRowPos, "part", "[B]",
                                                hidden ? 0.45f : 1.0f);
                            if (hidden) ImGui::PopStyleColor();
                            fmRowPillEnd(bodyRect);
                            // Capture row interactions NOW; BeginPopupContextItem binds to the
                            // LAST item, so it must come right after TreeNodeEx, before any
                            // other widget.
                            bool rowClicked = ImGui::IsItemClicked();
                            bool rowHovered = ImGui::IsItemHovered();
                            char ctxId[32];
                            std::snprintf(ctxId, sizeof(ctxId), "##body_ctx%d", bi);
                            pushTreePopupGlass();
                            if (ImGui::BeginPopupContextItem(ctxId)) {
                                if (ImGui::MenuItem(hidden ? "Show" : "Hide"))
                                    setBodyShown(st, bi, hidden);
                                if (ImGui::MenuItem("Isolate")) {
                                    bool othersAllHidden = true;
                                    for (int j = 0; j < nBodies; ++j)
                                        if (j != bi && st.bodyVisible[j]) {
                                            othersAllHidden = false; break;
                                        }
                                    if (othersAllHidden) {
                                        // Already isolated -> Exit Isolate = plain Show All.
                                        std::fill(st.bodyVisible.begin(),
                                                  st.bodyVisible.end(), (uint8_t)1);
                                        st.applyBodyVisibility();
                                    } else {
                                        for (int j = 0; j < nBodies; ++j)
                                            if (j != bi && st.bodyVisible[j])
                                                setBodyShown(st, j, false);
                                        if (!st.bodyVisible[bi]) setBodyShown(st, bi, true);
                                    }
                                }
                                if (ImGui::MenuItem("Show All Bodies")) {
                                    std::fill(st.bodyVisible.begin(),
                                              st.bodyVisible.end(), (uint8_t)1);
                                    st.applyBodyVisibility();
                                }
                                ImGui::EndPopup();
                            }
                            popTreePopupGlass();
                            if (rowClicked) setBodyShown(st, bi, hidden);
                            if (rowHovered)
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", bname);
                        }
                        ImGui::TreePop();
                    }
                }
            }
            ImGui::Unindent(kSecIndent);
        }   // end filename section

        // ---- References section (Origin + default planes) — its own collapse, independent of the
        // filename above and the Features below. ----
        fmSectionDividerCollapse("Reference", &s_refSecOpen);
        if (s_refSecOpen) {
            ImGui::Indent(kSecIndent);

            // Origin — expandable; the three default planes nest underneath (no duplicate flat list).
            // T-BL37: the Origin row body is now selectable (sets selectedFeatureId = "point.origin")
            // while the chevron still expands/collapses the child planes.
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_DefaultOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth |
                                       ImGuiTreeNodeFlags_OpenOnArrow |
                                       ImGuiTreeNodeFlags_FramePadding;   // pill-height node
                bool originSelected = (st.selectedFeatureId == "point.origin");
                bool originHidden   = st.hiddenFeatures.count("point.origin") > 0;
                FmRowRect originRect = fmRowPill(originSelected);
                treeIcon("origin", "[O]", 16.0f);
                ImGui::SameLine(0, 4 * ui::uiScale);
                if (originHidden)
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                bool originOpen = ImGui::TreeNodeEx("Origin##origin", f);
                if (originHidden) ImGui::PopStyleColor();
                // Eye toggle for the origin point glyph.
                {
                    ImVec2 oNodeMin = ImGui::GetItemRectMin();
                    ImVec2 oNodeMax = ImGui::GetItemRectMax();
                    float  oEyeSz = 14.0f * ui::uiScale, oEyePad = 6.0f * ui::uiScale;
                    ImVec2 oEyeMin(oNodeMax.x - oEyeSz - oEyePad,
                                   oNodeMin.y + (oNodeMax.y - oNodeMin.y - oEyeSz) * 0.5f);
                    ImVec2 oEyeMax(oEyeMin.x + oEyeSz, oEyeMin.y + oEyeSz);
                    bool   oOverEye = ImGui::IsMouseHoveringRect(oEyeMin, oEyeMax);
                    {
                        ImDrawList* odl = ImGui::GetWindowDrawList();
                        drawEyeGlyph(odl, oEyeMin, oEyeMax, !originHidden, oOverEye, fmSurf());
                        if (oOverEye) {
                            ImGui::SetTooltip(originHidden ? "Show Origin" : "Hide Origin");
                            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                if (originHidden)
                                    st.hiddenFeatures.erase("point.origin");
                                else {
                                    st.hiddenFeatures.insert("point.origin");
                                    if (originSelected) st.selectedFeatureId.clear();
                                }
                                st.applyDatumVisibility();
                            }
                        }
                    }
                    // Body-click (not on eye, not on chevron) → select the origin point.
                    // If a PM point-well is active, also route "point.origin" into it.
                    if (!oOverEye && ImGui::IsItemClicked()) {
                        st.selection.clear();
                        st.selectedFeatureId = "point.origin";
                        if (st.propertyManager && st.propertyManager->active() &&
                            st.propertyManager->activeWellWantsPoint())
                            st.propertyManager->routeDatumPoint("point.origin", "Origin");
                    }
                }
                fmRowPillEnd(originRect, originSelected);
                if (originOpen) {
                    static const char* kPlanes[] = {"Front Plane", "Top Plane", "Right Plane"};
                    bool planesAutoShow = !st.hasDocMesh && st.docMirror.hasDoc() && !st.planesUserToggled;
                    auto takePlaneControl = [&st, planesAutoShow]() {
                        if (st.planesUserToggled) return;
                        if (planesAutoShow)
                            st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = true;
                        st.planesUserToggled = true;
                    };
                    for (int pi = 0; pi < 3; ++pi) {
                        ImGuiTreeNodeFlags pf = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth |
                                                ImGuiTreeNodeFlags_FramePadding;
                        // T-G18: selection is shown by fmRowPill's glass card (like feature rows);
                        // the ImGui Selected/Header paint on top read heavier than a feature row.
                        bool planeShownEff = st.showPlane[pi] || planesAutoShow;
                        bool hidden = !planeShownEff;
                        // Pill FIRST (at the row's true left edge — drawing it after the icon left
                        // a misaligned slab starting right of the indent), then the node, then the
                        // icon into the leaf arrow slot. Hidden planes keep a readable dim state.
                        FmRowRect planeRect = fmRowPill(st.planeEmphasis == pi);
                        if (hidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                        ImVec2 planeRowPos = ImGui::GetCursorScreenPos();
                        ImGui::TreeNodeEx((std::string(kPlanes[pi]) + "##plane" + std::to_string(pi)).c_str(), pf);
                        treeIconInArrowSlot(planeRowPos, "ref_plane", "[P]",
                                            hidden ? 0.45f : 1.0f);
                        if (hidden) ImGui::PopStyleColor();
                        fmRowPillEnd(planeRect, st.planeEmphasis == pi);
                        ImVec2 pNodeMin = ImGui::GetItemRectMin();
                        ImVec2 pNodeMax = ImGui::GetItemRectMax();
                        float  pEyeSz = 14.0f * ui::uiScale, pEyePad = 6.0f * ui::uiScale;
                        ImVec2 pEyeMin(pNodeMax.x - pEyeSz - pEyePad,
                                       pNodeMin.y + (pNodeMax.y - pNodeMin.y - pEyeSz) * 0.5f);
                        ImVec2 pEyeMax(pEyeMin.x + pEyeSz, pEyeMin.y + pEyeSz);
                        bool   pOverEye = ImGui::IsMouseHoveringRect(pEyeMin, pEyeMax);
                        {
                            ImDrawList* pdl = ImGui::GetWindowDrawList();
                            drawEyeGlyph(pdl, pEyeMin, pEyeMax, !hidden, pOverEye, fmSurf());
                            if (pOverEye) {
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", kPlanes[pi]);
                                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                    static const char* kPlaneEyeId[] = {"plane.front","plane.top","plane.right"};
                                    takePlaneControl();
                                    st.showPlane[pi] = hidden;
                                    if (!st.showPlane[pi]) {
                                        if (st.planeEmphasis == pi) st.planeEmphasis = -1;
                                        if (st.selectedFeatureId == kPlaneEyeId[pi]) st.selectedFeatureId.clear();
                                    }
                                }
                            }
                        }
                        if (!pOverEye && ImGui::IsItemClicked()) {
                            if (st.sketchPlanePickActive && st.sketchMode) {
                                static const char* kPlaneSel[] = {"plane.front", "plane.top", "plane.right"};
                                st.sketchPlanePickActive = false;
                                st.sketchMode->consumePlanePick(st, kPlaneSel[pi]);
                            } else if (st.sketchMode && st.sketchMode->smartDimPicking()) {
                                st.sketchMode->addPlaneDimRef(st, pi);
                            } else {
                                // Row click = SELECT only (toggle). Visibility is owned solely by the
                                // eye toggle (Hunter): clicking the row must not hide/show the plane.
                                static const char* kPlaneSelId[] = {"plane.front", "plane.top", "plane.right"};
                                bool wasSel = (st.selectedFeatureId == kPlaneSelId[pi]);
                                if (wasSel) {
                                    st.planeEmphasis = -1;
                                    st.selectedFeatureId.clear();
                                } else {
                                    st.planeEmphasis = pi;
                                    st.selection.clear();
                                    st.selectedFeatureId = kPlaneSelId[pi];
                                }
                            }
                        }
                        if (ImGui::IsItemHovered() && !pOverEye)
                            ImGui::SetTooltip(st.sketchPlanePickActive ? "Sketch on %s"
                                              : ((st.sketchMode && st.sketchMode->smartDimPicking())
                                                     ? "Dimension to %s"
                                                     : (hidden ? "Show %s" : "Hide %s")), kPlanes[pi]);
                    }
                    ImGui::TreePop();
                }
            }

            ImGui::Unindent(kSecIndent);
        }   // end References section

        // ---- Features section — its own collapse, now hosting Add Folder + Name & group. ----
        {
            const bool isPartDoc = tree.is_object() && jfld(tree, "schema") != "aicad.asm/1";
            int fCount = 0;
            if (isPartDoc) {
                auto fa = tree.find("features");
                if (fa != tree.end() && fa->is_array()) fCount = (int)fa->size();
            }
            // Add Folder no longer requires a selection: with features selected it groups them; with
            // nothing selected it drops a blank folder at the bottom to drag features into (Hunter).
            const bool addEn = canDocTree && isPartDoc;
            const bool ngEn  = canDocTree && isPartDoc && fCount > 0;
            bool addClicked = false, ngClicked = false;
            fmFeaturesHeader(&s_featSecOpen, addEn, ngEn, &addClicked, &ngClicked);
            if (addClicked) createFolderFromSelection(st);
            if (ngClicked)  nameAndGroupFeatures(st);
        }
        if (s_featSecOpen) {
            ImGui::Indent(kSecIndent);

            // ---- feature history ----
            const nlohmann::json& statusArr = st.docMirror.featureStatus();
            std::string& selectedFeatureId = st.selectedFeatureId;

            // C20b: assemblies render components + a Mates folder instead of a feature history.
            bool isAssembly = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            if (isAssembly) {
                drawAssemblyTree(st, tree, statusArr);
            } else
            if (tree.is_object()) {
                auto feat = tree.find("features");
                if (feat != tree.end() && feat->is_array()) {
                    // C37-E (item 2): one status map per frame (kills the per-row O(N^2) scan).
                    std::unordered_map<std::string, FStatus> statusMap = buildStatusMap(statusArr);

                    // Ordered id list (drives selection validation, Ctrl+Up/Down, rollback math).
                    std::vector<std::string> order;
                    order.reserve(feat->size());
                    for (const auto& f : *feat)
                        if (f.is_object()) order.push_back(jfld(f, "id"));

                    // C37-E (item 2): validate the selection/multi-set against the live tree; drop
                    // anything that vanished (a delete/reorder/undo can orphan them) and clear the
                    // viewport highlight if the primary is gone.
                    {
                        std::unordered_set<std::string> live(order.begin(), order.end());
                        // The three reserved ORIGIN-plane ids are valid selections (SolidWorks
                        // "select a plane, then Sketch") but are NOT features, so they must be exempt
                        // from this live-feature prune or a plane selection would be wiped next frame.
                        bool isOriginPlane = selectedFeatureId == "plane.front" ||
                                             selectedFeatureId == "plane.top"   ||
                                             selectedFeatureId == "plane.right";
                        // T-BL37: "point.origin" is a synthetic ref like the planes — not a feature.
                        bool isOriginPoint = selectedFeatureId == "point.origin";
                        if (!selectedFeatureId.empty() && !live.count(selectedFeatureId) &&
                            !isOriginPlane && !isOriginPoint) {
                            selectedFeatureId.clear();
                            clearFeatureHighlight(st);
                        }
                        for (auto it = s_multiSel.begin(); it != s_multiSel.end();)
                            it = live.count(*it) ? std::next(it) : s_multiSel.erase(it);
                    }

                    // C37-E (item 4): rollback bar position. `rollback_after` = the feature the bar
                    // sits AFTER (null/absent = bar at end). Everything after it is "rolled back".
                    std::string rollbackAfter;
                    {
                        auto rb = tree.find("rollback_after");
                        if (rb != tree.end() && rb->is_string()) rollbackAfter = rb->get<std::string>();
                    }
                    int barPos = (int)order.size() - 1;   // index of the feature the bar sits after
                    if (!rollbackAfter.empty()) {
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == rollbackAfter) { barPos = i; break; }
                    }
                    bool barAtEnd = rollbackAfter.empty() || barPos >= (int)order.size() - 1;

                    // C37-E (item 5): sketch nesting. A sketch consumed by EXACTLY ONE downstream
                    // feature (via a region ref {"kind":"region","sketch":"fX"} or positions_sketch)
                    // nests under that consumer. Unconsumed / multiply-consumed sketches stay flat.
                    std::unordered_map<std::string, int> consumeCount;   // sketch fid -> #consumers
                    std::unordered_map<std::string, std::string> consumer;  // sketch fid -> consumer
                    std::unordered_map<std::string, std::vector<std::string>> childrenOf;  // consumer -> sketches
                    {
                        std::unordered_set<std::string> sketchIds;
                        for (const auto& f : *feat)
                            if (f.is_object() && jfld(f, "type") == "sketch")
                                sketchIds.insert(jfld(f, "id"));
                        // Recursively scan a feature's refs for sketch references. A sketch is
                        // "consumed" via (a) a region selector {"kind":"region","sketch":"fX"} or
                        // (b) a bare sketch-id string under a *_sketch / sketch / profile key (e.g.
                        // positions_sketch). We deliberately do NOT treat a bare sketch id under a
                        // generic "features" list (pattern/mirror) as consumption — patterns don't
                        // absorb sketches, and that would wrongly hide them.
                        std::function<void(const nlohmann::json&, const std::string&,
                                           std::unordered_set<std::string>&)> scan =
                            [&](const nlohmann::json& node, const std::string& key,
                                std::unordered_set<std::string>& seen) {
                            if (node.is_object()) {
                                if (jfld(node, "kind") == "region") {
                                    std::string sk = jfld(node, "sketch");
                                    if (!sk.empty() && sketchIds.count(sk)) seen.insert(sk);
                                }
                                for (auto it = node.begin(); it != node.end(); ++it)
                                    scan(it.value(), it.key(), seen);
                            } else if (node.is_array()) {
                                for (const auto& e : node) scan(e, key, seen);
                            } else if (node.is_string()) {
                                bool keyImpliesSketch =
                                    key.find("sketch") != std::string::npos || key == "profile";
                                std::string v = node.get<std::string>();
                                if (keyImpliesSketch && sketchIds.count(v)) seen.insert(v);
                            }
                        };
                        for (const auto& f : *feat) {
                            if (!f.is_object()) continue;
                            std::string fid = jfld(f, "id");
                            if (jfld(f, "type") == "sketch") continue;  // a sketch isn't a consumer
                            auto refs = f.find("refs");
                            if (refs == f.end()) continue;
                            std::unordered_set<std::string> seen;
                            scan(*refs, std::string(), seen);   // top-level refs object: no key yet
                            for (const auto& sk : seen) {
                                ++consumeCount[sk];
                                consumer[sk] = fid;  // last writer; only meaningful when count==1
                            }
                        }
                        for (const auto& kv : consumer) {
                            if (consumeCount[kv.first] != 1) continue;
                            // C48: a consumer that is ROLLED BACK or ERRORED does not absorb its
                            // sketch — the sketch un-nests to a flat row (SolidWorks behavior).
                            // Critically, this keeps the rollback bar drawable/grabbable when it
                            // sits after a profile sketch: a nested sketch never renders a flat
                            // row, so a bar anchored to it simply vanished from the tree.
                            auto cs = statusMap.find(kv.second);
                            if (cs != statusMap.end() && (!cs->second.ok || cs->second.rolledBack))
                                continue;
                            childrenOf[kv.second].push_back(kv.first);
                        }
                    }
                    // A sketch is nested (hidden from the flat list, shown under its consumer) iff
                    // it landed in childrenOf (one consumer, and that consumer actually builds).
                    std::unordered_set<std::string> nestedSketchIds;
                    for (const auto& kv : childrenOf)
                        for (const auto& sk : kv.second) nestedSketchIds.insert(sk);
                    auto isNestedSketch = [&](const std::string& fid) {
                        return nestedSketchIds.count(fid) != 0;
                    };

                    // Reorder op: drop draggedId before beforeId. A "@after:<id>" sentinel means drop
                    // AFTER <id> (i.e. before the feature following <id>, or end). "" = end.
                    auto reorderCb = [&st, &order](const std::string& dragged, const std::string& before) {
                        std::string beforeId = before;
                        if (before.rfind("@after:", 0) == 0) {
                            std::string anchor = before.substr(7);
                            beforeId.clear();
                            for (int i = 0; i < (int)order.size(); ++i)
                                if (order[i] == anchor) {
                                    if (i + 1 < (int)order.size()) beforeId = order[i + 1];
                                    break;
                                }
                        }
                        nlohmann::json op = {{"op", "reorder_feature"}, {"id", dragged}};
                        if (!beforeId.empty()) op["before"] = beforeId;  // omit => move to end
                        applyTreeOps(st, nlohmann::json::array({op}), "reorder");
                    };

                    // Owner-map → first owned body key ("b<n>") for the per-row Appearance item, and
                    // the owned-body count for the tooltip.
                    const auto& ownerMap = st.docMirror.ownerMap();
                    auto firstBodyKey = [&](const std::string& fid) -> std::string {
                        auto it = ownerMap.find(fid);
                        if (it == ownerMap.end() || it->second.bodies.empty()) return {};
                        return "b" + std::to_string(it->second.bodies.front());
                    };
                    auto bodyCount = [&](const std::string& fid) -> int {
                        auto it = ownerMap.find(fid);
                        return it == ownerMap.end() ? 0 : (int)it->second.bodies.size();
                    };
                    auto ownedBodies = [&](const std::string& fid) -> std::vector<uint32_t> {
                        auto it = ownerMap.find(fid);
                        return it == ownerMap.end() ? std::vector<uint32_t>{} : it->second.bodies;
                    };

                    // ---- render one feature row (shared by flat + nested-sketch paths) ----
                    int rowIdx = 0;   // visual stripe index (stable across nesting)
                    // Returns whether the row is expanded (leaf/child rows always true) so the caller
                    // renders consumed-sketch children only when the parent's arrow is open.
                    auto drawOneRow = [&](const nlohmann::json& f, bool nested) -> bool {
                        std::string fid    = jfld(f, "id");
                        std::string fname  = jfld(f, "name");
                        std::string ftype  = jfld(f, "type");
                        if (fname.empty()) fname = ftype + "·" + fid;  // C37-E: stable fid, not index
                        if (s_fmFilter[0] && !fm_icontains(fname.c_str(), s_fmFilter) &&
                            !fm_icontains(ftype.c_str(), s_fmFilter))
                            return true;

                        // Nested-child indent wraps the WHOLE row (background pill + text + icon) so
                        // the tinted background steps in with the content, not just the glyphs
                        // (Hunter task-4). Indent shifts both GetCursorScreenPos and
                        // GetContentRegionAvail, so the pill rect computed below is already inset.
                        const float nestIndent = 14.0f * ui::uiScale;
                        if (nested) ImGui::Indent(nestIndent);

                        FStatus fs;
                        { auto it = statusMap.find(fid); if (it != statusMap.end()) fs = it->second; }
                        bool suppressed = jftbool(f, "suppressed");
                        bool rolledBack = fs.rolledBack;   // authoritative (engine sets it)
                        bool sel = (selectedFeatureId == fid) || s_multiSel.count(fid) > 0;

                        // Row backgrounds: subtle striping plus a restrained selected-row card.
                        // Avoid ImGui's TreeNode selected paint here; it was stacking with our
                        // viewport highlight and made selected features look heavy and smeared.
                        ImVec2 rowMin = ImGui::GetCursorScreenPos();
                        float availW = ImGui::GetContentRegionAvail().x;
                        const float S = ui::uiScale;
                        // Must equal featureRow()'s node frame height (FramePadding 4dp pushed
                        // there): a taller pill bled over the following row (visual-review fix).
                        const float rowH = ImGui::GetTextLineHeight() + 8.0f * S;
                        ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
                        ImDrawList* rowDl = ImGui::GetWindowDrawList();
                        // Readable row pill via shared glass helper (panel stays transparent).
                        // Frosted glass base + stripe only (no selection glow here — the colored
                        // wash below owns selection, matching the ecad Layers style).
                        ui::glassRowBackground(rowDl, rowMin, rowMax, 3.5f, rowIdx % 2 == 0, false,
                                               sel ? 0.42f : 0.26f);
                        // Color-coded row wash dyed the feature's CATEGORY color (sketch=blue,
                        // solid-add=teal, cut=orange, dress-up=purple, pattern=cyan, datum=slate,
                        // ...) — the same Layers-style tinted background used across the app.
                        // Replaces the old 3px left color bar; selection is a glow border in-color.
                        {
                            const bool catIsCut = featureModeIsCut(&f);
                            float catTint[3]; categoryColorRGB(ftype, catIsCut, catTint);
                            // Same outer rect as the base — coloredRowWash self-insets to stay concentric.
                            ui::coloredRowWash(rowDl, rowMin, rowMax, 3.5f, catTint,
                                               sel ? 0.34f : 0.20f, sel);
                        }
                        if (suppressed)
                            rowDl->AddRectFilled(rowMin, rowMax,
                                IM_COL32((int)(pal.statusWarn[0] * 255),
                                         (int)(pal.statusWarn[1] * 255),
                                         (int)(pal.statusWarn[2] * 255), 8));
                        if (sel) {
                            // selection card drawn by glassRowBackground(selected=true)
                            if (selectedFeatureId == fid && st.treeScrollToSelection) {
                                ImGui::SetScrollHereY(0.5f);
                                st.treeScrollToSelection = false;
                            }
                        }
                        ++rowIdx;

                        // C27-C: convert fillet↔chamfer (kept).
                        const char* convertLabel = nullptr;
                        std::function<void()> convertCb = nullptr;
                        bool isFillet  = (ftype == "fillet");
                        bool isChamfer = (ftype == "chamfer");
                        if (isFillet || isChamfer) {
                            convertLabel = isFillet ? "Convert to Chamfer" : "Convert to Fillet";
                            nlohmann::json capturedFeat = f;
                            std::string capturedType = ftype, capturedFid = fid, nextId;
                            { bool found = false;
                              for (const auto& ff : *feat) {
                                  if (!ff.is_object()) continue;
                                  if (found) { nextId = jfld(ff, "id"); break; }
                                  if (jfld(ff, "id") == fid) found = true;
                              } }
                            convertCb = [&st, capturedFeat, capturedType, capturedFid, nextId]() {
                                std::string toType = (capturedType == "fillet") ? "chamfer" : "fillet";
                                nlohmann::json newParams = nlohmann::json::object();
                                if (capturedType == "fillet") {
                                    auto rIt = capturedFeat["params"].find("radius");
                                    newParams["length"] = (rIt != capturedFeat["params"].end())
                                        ? *rIt : nlohmann::json({{"value", 1.0}});
                                } else {
                                    auto lIt = capturedFeat["params"].find("length");
                                    newParams["radius"] = (lIt != capturedFeat["params"].end())
                                        ? *lIt : nlohmann::json({{"value", 2.0}});
                                }
                                nlohmann::json newFeat = {
                                    {"type", toType}, {"params", newParams},
                                    {"refs", capturedFeat.value("refs", nlohmann::json::object())},
                                };
                                nlohmann::json addOp = {{"op", "add_feature"}, {"feature", newFeat}};
                                if (!nextId.empty()) addOp["before"] = nextId;
                                applyTreeOps(st, nlohmann::json::array({
                                    {{"op", "delete_feature"}, {"id", capturedFid}, {"cascade", false}},
                                    addOp}), "convert");
                            };
                        }

                        RowCtx ctx;
                        ctx.fid = fid; ctx.name = fname; ctx.type = ftype;
                        ctx.iconName = featureIcon(ftype, &f);
                        ctx.iconFallback = featureFallback(ftype);
                        ctx.suppressed = suppressed; ctx.selected = sel;
                        ctx.rolledBack = rolledBack; ctx.fs = fs;
                        ctx.ownerBodyCount = bodyCount(fid);
                        ctx.ownedBodies = ownedBodies(fid);
                        ctx.nested = nested;
                        // A top-level row with consumed-sketch children draws a real collapse arrow.
                        ctx.expandable = !nested && childrenOf.count(fid) > 0;
                        ctx.canDoc = canDocTree;
                        ctx.barAtEnd = barAtEnd;
                        ctx.firstOwnedBody = firstBodyKey(fid);
                        ctx.convertLabel = convertLabel; ctx.convertCb = convertCb;
                        ctx.reorderCb = reorderCb;
                        ctx.editCallback = &editCallback;

                        RowResult rr = featureRow(ctx, st);
                        // T-G18: shared hover paint (identical to every meta/header row).
                        fmRowHoverPaint(rowMin, rowMax, rr.hovered, sel);

                        // ---- rollback-bar drag hit-test (item 4) ----
                        // While the bar is being dragged, find which row's gap the cursor is over so
                        // the release can compute "after: <fid>". A line is drawn at that gap.
                        if (s_rollbackDragging && !nested) {
                            ImVec2 imn = ImGui::GetItemRectMin(), imx = ImGui::GetItemRectMax();
                            float my = ImGui::GetIO().MousePos.y;
                            if (my >= imn.y && my <= imx.y) {
                                s_rbDropAfter = fid;  // drop the bar right after this feature
                                ImGui::GetWindowDrawList()->AddLine(
                                    ImVec2(imn.x, imx.y), ImVec2(imx.x, imx.y),
                                    ui::Palette::toU32(pal.accent),
                                    std::max(1.0f, 2.5f * ui::uiScale));  // #4.5
                            } else if (my < imn.y && s_rbDropAfter.empty()) {
                                // cursor above the first row → roll back before everything (to start)
                                s_rbDropAfter = "\1start";
                            }
                        }

                        // ---- selection / interaction handling ----
                        // User-defined datum PLANE clicked in the tree: targets it like the origin
                        // planes do — start a new sketch on it during plane-pick, or fill an armed
                        // plane well (SolidWorks lets you pick a reference plane from the tree). The
                        // sketch plane is the bare feature-id string; a plane well takes id + name.
                        if (rr.clicked && ftype == "plane" && st.sketchPlanePickActive && st.sketchMode) {
                            st.sketchPlanePickActive = false;
                            st.sketchMode->createSketchOnPlane(st, nlohmann::json(fid));
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                        } else if (rr.clicked && ftype == "plane" && st.propertyManager &&
                                   st.propertyManager->active() &&
                                   st.propertyManager->activeWellWantsPlane()) {
                            st.propertyManager->routeDatumPlane(fid, fname);
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                        } else if (rr.clicked && st.propertyManager && st.propertyManager->active() &&
                            st.propertyManager->activeWellWantsPathFeature() &&
                            (ftype == "sketch" || ftype == "sketch3d" || ftype == "helix" ||
                             ftype == "curve")) {
                            // sketch3d wires are sweep path/guide material (bare fid = single
                            // chain; the engine rejects a multi-chain bare fid loudly).
                            const bool isCurve = (ftype == "helix" || ftype == "curve" ||
                                                  ftype == "sketch3d");
                            st.propertyManager->routePathFeature(fid, fname, isCurve);
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                        } else if (rr.doubleClick) {
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                            if (ftype == "sketch") {
                                if (st.sketchMode) st.sketchMode->enterForFeature(st, fid);
                            } else if (ftype == "sketch3d") {
                                if (st.sketch3dMode) st.sketch3dMode->enterForFeature(st, fid);
                            } else if (editCallback) {
                                editCallback(fid);
                            }
                        } else if (rr.ctrlClicked) {
                            // Ctrl+click toggles this row in/out of the multi-set. The current
                            // primary joins the set first (so the FIRST ctrl-click on a 2nd row keeps
                            // both). Primary tracks the last-clicked; toggling the primary off moves
                            // it to any remaining member (or clears).
                            if (!selectedFeatureId.empty()) s_multiSel.insert(selectedFeatureId);
                            if (s_multiSel.count(fid)) {
                                s_multiSel.erase(fid);
                                if (selectedFeatureId == fid) {
                                    selectedFeatureId = s_multiSel.empty() ? std::string()
                                                                           : *s_multiSel.begin();
                                }
                            } else {
                                s_multiSel.insert(fid);
                                selectedFeatureId = fid;
                            }
                            selectFeatureInViewport(st, selectedFeatureId);
                        } else if (rr.clicked) {
                            // Slow-double-click rename: a 2nd plain click on the already-primary row
                            // within (0.4s, 2.0s) arms the inline editor.
                            double now = ImGui::GetTime();
                            bool armRename = (selectedFeatureId == fid && s_lastClickId == fid &&
                                              s_lastClickTime > 0 &&
                                              (now - s_lastClickTime) > 0.4 &&
                                              (now - s_lastClickTime) < 2.0);
                            if (armRename) {
                                s_renamingId = fid;
                                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", fname.c_str());
                                s_lastClickTime = -1.0;
                            } else if (selectedFeatureId == fid && s_multiSel.empty()) {
                                clearTreeFeatureSelection(st);
                                s_lastClickId.clear();
                                s_lastClickTime = -1.0;
                            } else {
                                selectedFeatureId = fid;
                                s_multiSel.clear();
                                selectFeatureInViewport(st, fid);
                                s_lastClickId = fid;
                                s_lastClickTime = now;
                            }
                        }

                        if (nested) ImGui::Unindent(nestIndent);   // balance the whole-row indent
                        return rr.open;
                    };

                    // fid the rollback bar currently sits AFTER (empty = end).
                    std::string barFid = (!barAtEnd && barPos >= 0 && barPos < (int)order.size())
                                       ? order[barPos] : std::string();

                    // Reset the bar drop-target each frame the bar is being dragged; rows below set
                    // it as the cursor passes over them. If no row claims it (cursor below the last
                    // row), it stays empty → set_rollback{after:null} = bar at the end.
                    if (s_rollbackDragging) s_rbDropAfter.clear();

                    // ---- walk the feature list ----
                    std::string openFolder;
                    bool folderOpen = false;
                    for (int i = 0; i < (int)feat->size(); ++i) {
                        const auto& f = (*feat)[i];
                        if (!f.is_object()) continue;
                        std::string fid    = jfld(f, "id");
                        std::string ftype  = jfld(f, "type");
                        std::string folder = jfld(f, "folder");

                        // Skip nested sketches in the flat pass — they render under their consumer.
                        if (ftype == "sketch" && isNestedSketch(fid)) continue;

                        // Folder grouping (C37-E item 2: ImGui id from the folder NAME, stable across
                        // reorders, not a per-frame counter).
                        if (folder != openFolder) {
                            if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
                            openFolder = folder;
                            if (!folder.empty()) {
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                                ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                    ImVec2(0, 4.0f * ui::uiScale));
                                FmRowRect fldRect = fmRowPill();
                                treeIcon("body_folder", "[F]", 16.0f);   // #4.4: was "mates_folder"
                                ImGui::SameLine(0, 4 * ui::uiScale);   // #4.5
                                folderOpen = ImGui::TreeNodeEx(
                                    (folder + "##fld_" + folder).c_str(),
                                    ImGuiTreeNodeFlags_DefaultOpen |
                                    ImGuiTreeNodeFlags_SpanFullWidth |
                                    ImGuiTreeNodeFlags_FramePadding);
                                // Drop target: dragging a feature here files it into this folder.
                                if (ImGui::BeginDragDropTarget()) {
                                    if (const ImGuiPayload* p =
                                            ImGui::AcceptDragDropPayload("FM_FEATURE")) {
                                        s_dropFid = (const char*)p->Data;
                                        s_dropFolder = folder;
                                    }
                                    ImGui::EndDragDropTarget();
                                }
                                fmRowPillEnd(fldRect);
                                ImGui::PopStyleVar();
                                ImGui::PopStyleColor();
                            }
                        }
                        if (!openFolder.empty() && !folderOpen) continue;

                        const bool rowOpen = drawOneRow(f, /*nested=*/false);

                        // Nested-sketch children: render absorbed sketches indented under this row,
                        // but only when the parent's collapse arrow is open (Hunter show/hide refs).
                        auto kids = childrenOf.find(fid);
                        if (rowOpen && kids != childrenOf.end()) {
                            for (const auto& skId : kids->second) {
                                for (const auto& sf : *feat)
                                    if (sf.is_object() && jfld(sf, "id") == skId) {
                                        drawOneRow(sf, /*nested=*/true);
                                        break;
                                    }
                            }
                        }

                        // C37-E (item 4): draw the rollback bar inline right after the bar feature
                        // (and its absorbed sketches) when it sits mid-history.
                        if (!barFid.empty() && fid == barFid)
                            drawRollbackBarLine(st, canDocTree, barFid);
                    }
                    if (folderOpen) { ImGui::TreePop(); folderOpen = false; }

                    // ---- empty folders (Add Folder with no selection) render at the BOTTOM ----
                    // Folders are display-only tags, so an empty one can't come from the tree; we keep
                    // the blank ones in s_pendingFolders and show them here as drop targets. Once a
                    // feature is filed into one it appears inline via the walk above, so we prune any
                    // pending name that now has members.
                    if (!s_pendingFolders.empty()) {
                        std::set<std::string> tagged;
                        for (const auto& f : *feat)
                            if (f.is_object()) {
                                std::string fl = jfld(f, "folder");
                                if (!fl.empty()) tagged.insert(fl);
                            }
                        s_pendingFolders.erase(
                            std::remove_if(s_pendingFolders.begin(), s_pendingFolders.end(),
                                           [&](const std::string& pf) { return tagged.count(pf) > 0; }),
                            s_pendingFolders.end());
                        for (const auto& pf : s_pendingFolders) {
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                ImVec2(0, 4.0f * ui::uiScale));
                            FmRowRect pRect = fmRowPill();
                            treeIcon("body_folder", "[F]", 16.0f);
                            ImGui::SameLine(0, 4 * ui::uiScale);
                            ImGui::TreeNodeEx((pf + "  (empty)##pfld_" + pf).c_str(),
                                ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_SpanFullWidth |
                                ImGuiTreeNodeFlags_FramePadding | ImGuiTreeNodeFlags_NoTreePushOnOpen);
                            if (ImGui::BeginDragDropTarget()) {
                                if (const ImGuiPayload* p =
                                        ImGui::AcceptDragDropPayload("FM_FEATURE")) {
                                    s_dropFid = (const char*)p->Data;
                                    s_dropFolder = pf;
                                }
                                ImGui::EndDragDropTarget();
                            }
                            fmRowPillEnd(pRect);
                            ImGui::PopStyleVar();
                            ImGui::PopStyleColor();
                        }
                    }
                    // Bar at the end → draw it after the last row.
                    if (barAtEnd)
                        drawRollbackBarLine(st, canDocTree, std::string());

                    // ---- Ctrl+Up / Ctrl+Down: move the selected feature among its siblings ----
                    if (canDocTree && !selectedFeatureId.empty() &&
                        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
                        int p = -1;
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == selectedFeatureId) { p = i; break; }
                        if (p >= 0) {
                            if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_UpArrow) && p > 0)
                                reorderCb(selectedFeatureId, order[p - 1]);
                            else if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_DownArrow) &&
                                     p < (int)order.size() - 1) {
                                if (p + 2 < (int)order.size())
                                    reorderCb(selectedFeatureId, order[p + 2]);
                                else
                                    reorderCb(selectedFeatureId, "");  // already next-to-last -> end
                            }
                        }
                    }

                    // ---- Rollback bar drag release (C37-E item 4) ----
                    // The bar is rendered inline above. When a bar drag ends, enqueue set_rollback
                    // for the gap the cursor landed in: after the hovered feature, or null (= end)
                    // when below the last row, or null/first when above (roll fully back ≈ start →
                    // we encode "to start" as set_rollback{after: first feature} so at least the
                    // first feature stays, matching SolidWorks' minimum-one rolled state... actually
                    // SW allows rolling to the very top; we map "above first" to the first feature).
                    if (s_rollbackDragging && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        std::string target = s_rbDropAfter;
                        nlohmann::json after = nullptr;   // default = end
                        if (target == "\1start") {
                            after = order.empty() ? nlohmann::json(nullptr)
                                                  : nlohmann::json(order.front());
                        } else if (!target.empty()) {
                            // Dropping after the last feature = bar at end (null).
                            if (!order.empty() && target == order.back()) after = nullptr;
                            else after = target;
                        }
                        if (canDocTree)
                            applyTreeOps(st, nlohmann::json::array({
                                {{"op", "set_rollback"}, {"after", after}}}), "rollback");
                        s_rollbackDragging = false;
                        s_rbDropAfter.clear();
                    } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        // Mouse up without a drag in progress — clear stale hit state.
                        s_rbDropAfter.clear();
                    }
                } else if (s_rollbackDragging) {
                    // C38 (T2): the feature list vanished mid-drag (no walk loop, so the release
                    // handler above never runs). Clear the drag flag so it can't linger a frame and
                    // leave the bar visually "stuck" to the cursor.
                    s_rollbackDragging = false;
                    s_rbDropAfter.clear();
                }
            }

            ImGui::Unindent(kSecIndent);
        }   // end Features section
    }

    if (!st.selectedFeatureId.empty() || !s_multiSel.empty()) {
        const bool treeFocused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
        const bool treeHovered = ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows |
                                                        ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
        if (treeFocused && !ImGui::GetIO().WantTextInput &&
            ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
            clearTreeFeatureSelection(st);
        } else if (treeHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                   !ImGui::IsAnyItemHovered() && !ImGui::IsAnyItemActive()) {
            clearTreeFeatureSelection(st);
        }
    }

    // C37-E (item 3): per-row "Appearance..." picker. The row's context menu stashed the target body
    // key + a one-shot open flag; render the picker as a sibling popup (same pattern as the root
    // node) and turn a commit into a set_appearance op on that body.
    if (s_apprOpen && !s_apprBody.empty()) { ImGui::OpenPopup("##appearance_feat"); s_apprOpen = false; }
    if (!s_apprBody.empty()) {
        pushTreePopupGlass();
        if (appearanceColorPicker("##appearance_feat", s_apprColor,
                                  &s_apprMetallic, &s_apprRoughness)) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_appearance"}, {"id", s_apprBody},
                 {"color", {s_apprColor[0], s_apprColor[1], s_apprColor[2]}},
                 {"metallic", s_apprMetallic}, {"roughness", s_apprRoughness}}}),
                "set_appearance");
            s_apprBody.clear();
        }
        popTreePopupGlass();
    }

    // ---- C41: bottom file strip ----
    // Compact row: [Save] [Save As...] [Open...] + the current file name (dimmed; "(unsaved)"
    // until the doc is saved/opened). Buttons size themselves from their labels — no fixed-width
    // math, so nothing clips off the panel edge (the old strip's Open button did).
    // AI_CAD_NO_FILE_DIALOGS keeps the old raw-path InputText fallback for headless/CI use.
    bool canDoc = canDocTree;
    if (canDoc && (st.docMirror.hasDoc() || !fileDialogsDisabled())) {
        ImGui::Separator();
        if (fileDialogsDisabled()) {
            // Escape hatch: raw path + Save/Open, the pre-C41 behavior — except Open now routes
            // through applyOpenedDoc so the viewport mesh refreshes (the old code never did).
            if (st.docMirror.hasDoc()) {
                static char savePath[512] = "";
                const ImGuiStyle& style = ImGui::GetStyle();
                float btnW = ImGui::CalcTextSize("Save").x + ImGui::CalcTextSize("Open").x +
                             4.0f * style.FramePadding.x + 2.0f * style.ItemSpacing.x;
                ImGui::SetNextItemWidth(-btnW);
                ImGui::InputText("##savepath", savePath, sizeof(savePath));
                ImGui::SameLine();
                if (ImGui::Button("Save") && savePath[0]) {
                    std::string err;
                    if (st.docMirror.save(st.worker, savePath, err)) {
                        s_docPath = savePath;
                        st.addLog("[doc] saved: " + std::string(savePath));
                    } else st.addLog("[err] save: " + err);
                }
                ImGui::SameLine();
                if (ImGui::Button("Open") && savePath[0]) {
                    std::string err;
                    std::string p = savePath;
                    if (st.docMirror.open(st.worker, p, err)) applyOpenedDoc(st, p);
                    else st.addLog("[err] open: " + err);
                }
            }
        } else {
            // File name on its own dimmed line (a narrow panel must never push it — or the
            // buttons — past the edge); the full path shows on hover. Red-pen round-2: the old
            // glassRowAtCursor + raw Text drew the label flush into the pill's rounded left
            // corner and at the row top — use the standard fmRowPill with the 6dp left inset and
            // frame-padded vertical centering (same treatment as the Explorer breadcrumb).
            {
                std::string fileLabel = s_docPath.empty() ? "(unsaved)" : pathBaseName(s_docPath);
                ui::FmRowRect fr = ui::fmRowPill(false);
                ImGui::SetCursorScreenPos(ImVec2(fr.mn.x + 6.0f * ui::uiScale, fr.mn.y));
                ImGui::AlignTextToFramePadding();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1.0f));
                ImGui::TextUnformatted(fileLabel.c_str());
                ImGui::PopStyleColor();
                ui::fmRowPillEnd(fr, false);
                if (!s_docPath.empty() && ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", s_docPath.c_str());
            }
            bool canSave = st.docMirror.hasDoc();
            // Adaptive glass chips: theme Button colors don't track the live backdrop, which left
            // the labels near-invisible over the frosted column (visual-review fix). Fill + ink
            // both come from the pane's adaptive surface so they always read; disabled rows keep
            // ImGui's DisabledAlpha over the same high-contrast ink.
            const ui::AdaptiveSurface& btnSurf = fmSurf();
            const ImVec4& ink = btnSurf.textPrimary;
            ImGui::PushStyleColor(ImGuiCol_Text, ink);
            ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(ink.x, ink.y, ink.z, 0.10f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(ink.x, ink.y, ink.z, 0.20f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(ink.x, ink.y, ink.z, 0.28f));
            ImGui::PushStyleColor(ImGuiCol_Border,
                ImVec4(btnSurf.textTertiary.x, btnSurf.textTertiary.y,
                       btnSurf.textTertiary.z, 0.60f));
            ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, std::max(1.0f, 1.0f * ui::uiScale));
            if (!canSave) ImGui::BeginDisabled();
            {
                const ImGuiStyle& style = ImGui::GetStyle();
                const float gap = style.ItemSpacing.x;
                const float halfW = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
                if (ImGui::Button("Save", ImVec2(halfW, 0))) doSaveDoc(st, /*forceDialog=*/false);
                ImGui::SameLine(0, gap);
                if (ImGui::Button("Save As...", ImVec2(halfW, 0))) doSaveDoc(st, /*forceDialog=*/true);
            }
            if (!canSave) ImGui::EndDisabled();
            if (ImGui::Button("Open...", ImVec2(-1, 0))) doOpenDoc(st);
            ImGui::PopStyleVar();
            ImGui::PopStyleColor(5);
            // Bottom breathing room so the last row never kisses the bubble's rounded edge.
            ImGui::Dummy(ImVec2(0, 6.0f * ui::uiScale));
        }
    }

    // C25: apply ops queued during drawing — safe now, nothing reads the tree json below.
    flushPendingTreeOps(st);
    // Wave 4C: run any copy/cut/paste requested from a row context menu (deferred to here because
    // the copy's DocMirror call replaces the tree json the menu was drawn over).
    flushPendingClipboard(st);

    ImGui::PopStyleVar();  // compact ItemSpacing (pushed at top of body)
}

// ---- Wave 3: public Save/Open wrappers (the File menu + File Explorer reuse these) ----
// The actual RPC/dialog/mesh-refresh logic lives in the file-local doSaveDoc / doOpenDoc /
// applyOpenedDoc above; these thin wrappers expose it without duplicating any of it, and keep
// s_docPath (the active doc's on-disk path) as the single source of truth.

// Open a file by absolute path (no dialog). Used by the File Explorer double-click and the
// Recent-files menu. Returns true on success; toasts + logs on failure (never throws).
bool featureTreeOpenPath(AppState& st, const std::string& path, bool asSolid) {
    if (path.empty()) return false;
    std::string err;
    if (st.docMirror.open(st.worker, path, err, asSolid)) {
        applyOpenedDoc(st, path);   // s_docPath = path, new tab, mesh refresh, recent handled by caller
        return true;
    }
    st.addLog("[err] open: " + err);
    // A live MCP-handoff refresh polls in the background — a failed open there must not
    // toast (it would spam once per poll for an unsupported doc, e.g. an assembly the C++
    // core's doc_open declines). The caller logs it; pollAiCadHandoff won't retry the rev.
    if (!st.handoffQuietOpen)
        st.toast(ui::ToastLevel::Error, "Open failed: " + pathBaseName(path) + " — " + err);
    return false;
}

// Live MCP handoff poll — see the header. The external MCP server (the engine Claude drives)
// publishes the part it's working on to %APPDATA%\AI-CAD\handoff\ as a NATIVE .step+ doc plus a
// tiny live.json {rev, step}. We open/refresh a single tab from it so the user watches the AI
// build in the viewport. Loading the native .step+ (not bare STEP) brings the whole feature tree.
// One published part the MCP currently has open, parsed from the handoff control file.
struct HandoffDoc { std::string docId, step; long long rev = -1; };

void pollAiCadHandoff(AppState& st) {
    if (!st.workerStarted) return;   // worker not up — nothing can open yet
    if (st.agentV2Running) return;   // an in-app agent run owns the doc; don't fight it
    namespace fs = std::filesystem;
    const std::string dir = appdataDir() + "/AI-CAD/handoff/";
    static const std::string ctrlNew = dir + "docs.json";   // multi-doc (one tab per open part)
    static const std::string ctrlOld = dir + "live.json";   // legacy single-doc fallback

    // Per-doc_id rev already drawn on screen. "" keys the legacy single-mirror (no doc_id).
    static std::map<std::string, long long> shownRev;
    static std::map<std::string, HandoffDoc> pending;   // doc_id -> latest published, awaiting a safe moment
    static fs::file_time_type lastMtime{};
    static std::string lastCtrl;   // which control file lastMtime belongs to (reset on a switch)
    std::error_code ec;

    // 1) Cheaply pick up a newer control file (mtime-gated). Prefer docs.json; fall back to the
    //    legacy live.json so an OLD MCP that only writes the single-doc file still mirrors.
    const std::string ctrl = fs::exists(ctrlNew, ec) ? ctrlNew : ctrlOld;
    if (ctrl != lastCtrl) { lastCtrl = ctrl; lastMtime = fs::file_time_type{}; }  // force a reparse
    std::vector<HandoffDoc> published;   // the doc set the control file currently advertises
    bool haveControl = false;
    if (fs::exists(ctrl, ec)) {
        fs::file_time_type mt = fs::last_write_time(ctrl, ec);
        if (!ec && mt != lastMtime) {
            lastMtime = mt;
            nlohmann::json j;
            std::ifstream f(ctrl, std::ios::binary);
            if (f) { try { f >> j; } catch (...) { j = nlohmann::json{}; } }  // half-written: retry next change
            if (j.contains("docs") && j["docs"].is_array()) {
                haveControl = true;
                for (const auto& e : j["docs"]) {
                    HandoffDoc hd;
                    hd.docId = e.value("doc_id", std::string());
                    hd.step  = e.value("step", std::string());
                    hd.rev   = e.value("rev", (long long)-1);
                    if (!hd.step.empty()) published.push_back(hd);
                }
            } else {
                // Legacy shape {rev, step, doc_id} — a single mirror keyed by "".
                HandoffDoc hd;
                hd.docId = std::string();  // stable legacy key
                hd.step  = j.value("step", std::string());
                hd.rev   = j.value("rev", (long long)-1);
                if (!hd.step.empty()) { haveControl = true; published.push_back(hd); }
            }
        }
    }

    // Fold freshly-published revs into `pending` (defer showing until safe, step 3).
    for (const auto& hd : published) {
        auto it = pending.find(hd.docId);
        const long long already = (it != pending.end()) ? it->second.rev : shownRev.count(hd.docId) ? shownRev[hd.docId] : -1;
        if (hd.rev > already && fs::exists(hd.step, ec)) pending[hd.docId] = hd;
    }

    // Removal: any mirror tab whose doc_id is no longer advertised gets closed (the AI closed that
    // part). Only act when we actually parsed a fresh control file this frame, so a transient read
    // miss never nukes tabs.
    if (haveControl) {
        std::set<std::string> live;
        for (const auto& hd : published) live.insert(hd.docId);
        for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
            if (st.docTabs[i].aiHandoff && !live.count(st.docTabs[i].handoffDocId)) {
                shownRev.erase(st.docTabs[i].handoffDocId);
                pending.erase(st.docTabs[i].handoffDocId);
                st.closeDocTab(i);
            }
        }
    }

    // 2) Nothing pending to reveal.
    if (pending.empty()) return;

    // 3) SAFETY GATES — defer rather than disrupt the user. The AI keeps building in its OWN engine
    //    process; the on-screen mirror just catches up the moment it is safe.
    const bool userMidEdit =
        (st.sketchMode && st.sketchMode->active()) ||           // an open sketch
        (st.propertyManager && st.propertyManager->active());   // an open feature-edit (PM) form
    const bool anyHandoffTab = [&]{
        for (const auto& t : st.docTabs) if (t.aiHandoff) return true;
        return false;
    }();

    // Apply each pending doc independently. Refresh an existing mirror in place; open a NEW part as
    // its own tab. Anything we can't safely do now stays pending for a later frame.
    std::vector<std::string> done;   // doc_ids fully applied this frame (remove from pending)
    for (auto& [docId, hd] : pending) {
        int idx = -1;
        for (int i = 0; i < (int)st.docTabs.size(); ++i)
            if (st.docTabs[i].aiHandoff && st.docTabs[i].handoffDocId == docId) { idx = i; break; }
        const bool firstReveal = (idx < 0);

        if (firstReveal) {
            // A brand-new part. Opening steals focus; don't pop it over an in-progress edit — wait.
            if (userMidEdit) continue;
            // The FIRST mirror ever opened may take focus (matches old behavior); later parts open in
            // the background so a swarm doesn't yank the user around while they inspect one.
            const bool takeFocus = !anyHandoffTab;
            const int prevActive = st.activeTab;
            const std::string prevActiveId =
                (prevActive >= 0 && prevActive < (int)st.docTabs.size())
                    ? st.docTabs[prevActive].docId : std::string();

            st.handoffQuietOpen = true;
            const bool opened = featureTreeOpenPath(st, hd.step);
            st.handoffQuietOpen = false;

            if (opened && st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
                const int newIdx = st.activeTab;              // applyOpenedDoc made the new tab active
                st.docTabs[newIdx].aiHandoff = true;
                st.docTabs[newIdx].handoffDocId = docId;
                // DEDUPE (fixes the doubled-view bug): if any OTHER tab already mirrors this doc_id
                // (a racing/stale open), close it so the part shows exactly once.
                for (int i = (int)st.docTabs.size() - 1; i >= 0; --i)
                    if (i != newIdx && st.docTabs[i].aiHandoff && st.docTabs[i].handoffDocId == docId)
                        st.closeDocTab(i);
                if (!takeFocus) {
                    // Background open: return focus to the tab the user was on (re-find it by doc id,
                    // since closeDocTab above may have shifted indices) and pulse the new tab.
                    int back = -1;
                    if (!prevActiveId.empty())
                        for (int i = 0; i < (int)st.docTabs.size(); ++i)
                            if (st.docTabs[i].docId == prevActiveId) { back = i; break; }
                    if (back >= 0) st.activeTab = back;
                    for (int i = 0; i < (int)st.docTabs.size(); ++i)
                        if (st.docTabs[i].handoffDocId == docId) { st.docTabs[i].aiPulse = true; break; }
                }
                shownRev[docId] = hd.rev;
                if (takeFocus)
                    st.toast(ui::ToastLevel::Info, "ShopCraft: live view of the AI's part — it updates as it builds");
                done.push_back(docId);
            } else if (!opened) {
                // Open failed (e.g. an assembly the core's doc_open declines). Consume the rev so we
                // don't retry every frame; a newer rev will still be attempted.
                shownRev[docId] = hd.rev;
                done.push_back(docId);
            }
            continue;
        }

        // Existing mirror — only refresh while the user is actually looking at it and not mid-edit.
        // Otherwise leave it pending (pulse the tab so the user sees the AI is working there); it
        // applies the next frame they return, never yanking focus.
        if (idx != st.activeTab || userMidEdit) {
            if (idx != st.activeTab) st.docTabs[idx].aiPulse = true;
            continue;
        }

        // Preserve the user's camera across the in-place refresh so watching the build doesn't reset
        // their orbit/zoom each step.
        const float cy = st.yaw, cp = st.pitch, cz = st.zoom,
                    px = st.pan[0], py = st.pan[1], pz = st.pan[2];
        st.closeDocTab(idx);

        st.handoffQuietOpen = true;
        const bool opened = featureTreeOpenPath(st, hd.step);
        st.handoffQuietOpen = false;

        if (opened && st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
            st.docTabs[st.activeTab].aiHandoff = true;
            st.docTabs[st.activeTab].handoffDocId = docId;
            st.docTabs[st.activeTab].aiPulse = false;   // user is viewing this refresh — no glow
            st.yaw = cy; st.pitch = cp; st.zoom = cz;
            st.pan[0] = px; st.pan[1] = py; st.pan[2] = pz;
            st.zoomToBBoxRequested = false;                  // keep our restored view; cancel auto-fit
            shownRev[docId] = hd.rev;
        } else if (!opened) {
            shownRev[docId] = hd.rev;
        }
        done.push_back(docId);
    }
    for (const auto& id : done) pending.erase(id);
}

// Native "Open..." dialog -> open. Returns the opened path (for Recent), or "" on cancel/failure.
std::string featureTreeOpenDialog(AppState& st) {
    if (fileDialogsDisabled()) {
        st.toast(ui::ToastLevel::Info,
                 "File dialogs disabled — use the path field below the tree");
        return std::string();
    }
    std::string path = openFileDialog(kImportFilter);
    if (path.empty()) return std::string();   // canceled
    // Mesh files: ask whether to import as a solid (vs. graphics mesh). Non-mesh formats never prompt.
    bool asSolid = false;
    if (isMeshImportFormat(path)) {
        int choice = ui::promptImportMeshAsSolid();
        if (choice < 0) return std::string();   // canceled
        asSolid = (choice == 1);
    }
    return featureTreeOpenPath(st, path, asSolid) ? path : std::string();
}

// Save the active doc. saveAs=true forces the "Save As..." dialog; otherwise a silent re-save to
// the known path (dialog only on first save). Returns the path saved to (for Recent), or "" on
// cancel/failure/no-doc.
std::string featureTreeSaveDoc(AppState& st, bool saveAs) {
    if (!st.docMirror.hasDoc()) return std::string();
    doSaveDoc(st, /*forceDialog=*/saveAs);
    return s_docPath;   // doSaveDoc updates s_docPath on success; unchanged on cancel/failure
}

// The active document's on-disk path ("" if the doc has never been saved). Lets the File menu grey
// "Save As" vs. "Save" correctly and lets the Explorer highlight the open file.
std::string featureTreeActivePath() { return s_docPath; }

}  // namespace ui
