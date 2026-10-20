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
#include <chrono>
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
#include "feature_tree_nested.h"  // recursive cached component document data
#include "file_dialog.h"   // C41: native Win32 Save/Open dialogs
#include "icons.h"
#include "sketch_mode.h"   // C18 (Edit Sketch context entry)
#include "sketch3d_mode.h" // Edit 3D Sketch context/double-click entry
#include "property_manager.h"  // sweep/loft PATH pick from the tree (routePathFeature)
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker
#include "cursors.h"        // C37-E: ResizeV cursor over the rollback bar
#include "plm/plm_hub.h"       // g_plmHub, PlmPage, plmLifecycleBadge (FM PLM badge)
#include "plm/plm_quickbar.h"  // ui::plmActiveDocPlmState
#include "plm/graph_tab.h"     // ui::plmGraphFocus (jump to this part's links)
#include "widgets/kit.h"       // ui::kit::elide — row labels elide against the right-edge eye
#include "copilot/context_chips.h"  // ui::addContextRef — context-cube picks from tree rows

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../core/sketch_consumers.h"
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

// Wave4: chat context-ref capture (composer "context cube"). ui::addContextRef stamps the doc
// and toggles the chip (re-adding the same row detaches it).
static void pushFeatureContextRef(AppState& st, const std::string& fid,
                                  const std::string& name, const std::string& type) {
    if (fid.empty()) return;
    core::ContextRef ref;
    if (type == "sketch" || type == "sketch3d")
        ref.kind = "sketch";
    else if (type == "import")
        ref.kind = "component";  // imported part treated as component-like for the agent
    else
        ref.kind = "feature";
    ref.id = fid;
    ref.type = type;
    ref.label = name.empty() ? fid : name;
    ui::addContextRef(st, std::move(ref));
}

static void pushComponentContextRef(AppState& st, const std::string& cid,
                                    const std::string& cname) {
    core::ContextRef ref;
    ref.kind = "component";
    ref.id = cid;
    ref.label = cname.empty() ? cid : cname;
    ref.componentName = cname;
    ui::addContextRef(st, std::move(ref));
}

// A solid body row: its pick ordinal rides along so the engine names the feature that made it.
static void pushBodyContextRef(AppState& st, uint32_t bid, const char* bname) {
    core::ContextRef ref;
    ref.kind = "body";
    ref.id = "b" + std::to_string(bid);
    ref.bodyId = bid;
    ref.label = bname;
    ui::addContextRef(st, std::move(ref));
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

// ---- shared selected-row paint (every FeatureManager row kind) ----
// A viewport pick lands the user's eye on the tree, so the selected row must read at a glance: a
// soft accent fill plus the crisp accent glow border, painted OVER the row's category wash so part
// features, assembly components and datum rows all select alike. accentSoft is translucent, so the
// row text keeps its contrast. Same outer rect as the pill (both self-inset 2dp).
static void fmRowSelectedPaint(ImDrawList* dl, ImVec2 rowMin, ImVec2 rowMax) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    const float inset = 2.0f * S;
    dl->AddRectFilled(ImVec2(rowMin.x + inset, rowMin.y + inset),
                      ImVec2(rowMax.x - inset, rowMax.y - inset),
                      ui::Palette::toU32(pal.accentSoft), 3.5f * S);
    ui::glassSelectionGlow(dl, rowMin, rowMax, 3.5f, pal.accent);
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
    // Frosted glass base + alternating stripe, then the Layers-style color wash bounded to the inset
    // pill: a section/element `tint` when supplied, else the neutral app accent so every row shares
    // the same tinted-background language. Selection paints on top (fmRowSelectedPaint).
    const float wash = selected ? 0.42f : 0.26f;
    ui::glassRowBackground(dl, rowMin, rowMax, 3.5f, (s_fmStripeIdx++ % 2) == 0, false, wash);
    const float* eff = tint ? tint : ui::palette.accent;   // accent is float[4]; wash reads [0..2]
    const float strength = tint ? 0.28f : 0.11f;
    // Same outer rect as the frosted base — coloredRowWash self-insets by the identical 2dp
    // (and keeps MultiColor inside the rounded outline — see ui_theme.cpp).
    ui::coloredRowWash(dl, rowMin, rowMax, 3.5f, eff, strength, false);
    if (selected) fmRowSelectedPaint(dl, rowMin, rowMax);
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

// ---- right-edge row adornments: label budget ----
// Every visibility row draws its eye at nodeMax.x - 14dp - 6dp on the SpanFullWidth node rect, and
// a status row appends an inline dot (featureBadge) right after its label. ImGui never clips a tree
// node's label, so a long name ran straight under the eye. Rows elide their visible label against
// these reserves instead, so the text always ends a clear gap left of the adornments at any scale.
static float fmEyeColumnW() { return (14.0f + 6.0f + 6.0f) * ui::uiScale; }  // glyph + inset + gap
// Horizontal footprint of featureBadge's dot as laid out after the label: SameLine() spacing plus
// the dot's Dummy width (see featureBadge). Only reserve it when the row actually shows a badge.
static float fmBadgeW() { return ImGui::GetStyle().ItemSpacing.x + (2.0f * 3.5f + 4.0f) * ui::uiScale; }
static bool fmBadgeShown(bool ok, const std::vector<std::string>& danglingRefs, bool rolledBack,
                         const std::string& warnText = "") {
    return rolledBack || !ok || !danglingRefs.empty() || !warnText.empty();
}
// Elide `text` for a TreeNodeEx about to be submitted at the current cursor (FramePadding already
// pushed — it sizes the arrow slot), keeping `rightReserve` px clear at the row's right edge.
// `leadW` covers any extra label prefix (the expandable row's icon spacer).
static std::string fmFitRowLabel(const std::string& text, float rightReserve, float leadW = 0.0f) {
    const float maxW = ImGui::GetContentRegionAvail().x - ImGui::GetTreeNodeToLabelSpacing() -
                       leadW - rightReserve;
    return ui::kit::elide(text.c_str(), maxW);
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
                             const std::vector<uint32_t>& ownedBodies, bool currentlyShown,
                             bool isDatumPlane, bool isRefGeom = false) {
    if (isDatumPlane) {
        st.datumPlanesUserToggled.insert(fid);
        st.autoHiddenPlanes.erase(fid);
    }
    // User points/csys: sticky so solid-content auto-hide never rewrites the eye choice.
    if (isRefGeom) st.refGeomUserToggled.insert(fid);
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
        return;   // healthy rows: no green status dot (eye remains; errors/warns still badge)
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
// cut short to clear it). This is the ONE body behind every FeatureManager section header (document
// name / Reference / Features), so they share structure, rhythm and chevron placement exactly.
// A header that carries a leading control reserves `leadW` px at the row's left edge (the left
// hairline starts after it) and paints it in `lead(rowMin, rowH)`, submitted on top of the row
// toggle (which allows overlap so the control receives its own clicks). Trailing controls work
// the same way: `trailW` px just left of the chevron (the right hairline stops short of it),
// painted by `trail(slotMin, rowH)` where slotMin is the slot's top-left. The label stays centred
// on the full row — every header's label shares one axis — and is clamped/elided so it never runs
// into the lead/trail controls or the chevron at narrow widths (full text in a hover tooltip).
template <class LeadFn, class TrailFn>
static bool fmSectionHeaderRow(const char* label, bool* open, bool* rightClicked, float leadW,
                               LeadFn&& lead, float trailW, TrailFn&& trail) {
    const auto& pal = fmPalette();
    const float S = ui::uiScale;
    const float chev = 18.0f * S;                     // end-of-line chevron hit box
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float w = ImGui::GetContentRegionAvail().x;
    const float lineH = ImGui::GetTextLineHeight();
    const float rowH = std::max(lineH + 4.0f * S, chev + 2.0f * S);
    const float cy = p.y + rowH * 0.5f;
    const float chevX = p.x + w - chev;
    const float trailX = chevX - 6.0f * S - trailW;   // trailing-control slot (empty when trailW==0)
    const float leftLineStart = p.x + 4.0f * S + leadW;
    // Cut the right hairline short of the chevron — or of the trailing controls when present.
    const float rightLineEnd = (trailW > 0.0f ? trailX : chevX) - 8.0f * S;
    const std::string shown = ui::kit::elide(label, rightLineEnd - leftLineStart);
    const ImVec2 tsz = ImGui::CalcTextSize(shown.c_str());
    const float textX = std::max(leftLineStart,
                                 std::min(p.x + (w - tsz.x) * 0.5f, rightLineEnd - tsz.x));
    const float textY = p.y + (rowH - lineH) * 0.5f;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Full-row invisible toggle FIRST so the label/hairlines/chevron (and any lead control) paint
    // on top of its hit rect.
    ImGui::PushID(label);
    ImGui::SetCursorScreenPos(p);
    ImGui::SetNextItemAllowOverlap();
    const bool clicked = ImGui::InvisibleButton("##sec_toggle", ImVec2(w, rowH));
    const bool hov = ImGui::IsItemHovered();
    if (clicked) *open = !*open;
    if (rightClicked) *rightClicked = hov && ImGui::IsMouseClicked(ImGuiMouseButton_Right);
    if (hov) {   // faint wash to signal the whole row is interactive
        ui::glassRowBackground(dl, p, ImVec2(p.x + w, p.y + rowH), 3.0f, false, false, 0.16f);
        if (shown != label) ImGui::SetTooltip("%s", label);
    }

    const ImU32 lineCol = ui::Palette::toU32(pal.border);
    const float lineTh = std::max(1.0f, 1.0f * S);
    const float lEnd = textX - 6.0f * S;
    if (lEnd > leftLineStart) dl->AddLine(ImVec2(leftLineStart, cy), ImVec2(lEnd, cy), lineCol, lineTh);
    const float rStart = textX + tsz.x + 6.0f * S;
    if (rStart < rightLineEnd) dl->AddLine(ImVec2(rStart, cy), ImVec2(rightLineEnd, cy), lineCol, lineTh);
    dl->AddText(ImVec2(textX, textY),
                ImGui::ColorConvertFloat4ToU32(ImVec4(pal.treeSection[0], pal.treeSection[1],
                                                      pal.treeSection[2], pal.treeSection[3])),
                shown.c_str());

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

    lead(p, rowH);
    if (trailW > 0.0f) trail(ImVec2(trailX, p.y), rowH);
    ImGui::PopID();

    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, rowH));
    return *open;
}

// Toggles *open; returns *open. `rightClicked`, if given, reports a right-click on the row so the
// caller can raise a context menu (e.g. the filename's Appearance...).
bool fmSectionDividerCollapse(const char* label, bool* open, bool* rightClicked) {
    return fmSectionHeaderRow(label, open, rightClicked, 0.0f, [](ImVec2, float) {},
                              0.0f, [](ImVec2, float) {});
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


static bool refuseAgentLockedTreeDoc(AppState& st, const std::string& docId) {
    if (!st.docLockedByAgent(docId)) return false;
    st.toast(ui::ToastLevel::Warn, st.agentLockReason(docId));
    return true;
}

// Nested edits can target a different, already-open part as well as the active assembly.
static std::string nestedAgentLockReason(AppState& st, const std::string& sourcePath) {
    if (st.activeDocLockedByAgent()) return st.agentLockReason(st.docMirror.docId());
    if (sourcePath.empty()) return {};
    for (const auto& tab : st.docTabs) {
        if (tab.path.empty() || !st.docLockedByAgent(tab.docId)) continue;
        if (tab.path == sourcePath ||
            std::filesystem::path(tab.path).lexically_normal() ==
                std::filesystem::path(sourcePath).lexically_normal())
            return st.agentLockReason(tab.docId);
    }
    return {};
}
static void applyTreeOpsNow(AppState& st, const nlohmann::json& ops, const char* what) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return;
    // T-PERF1: async apply so suppress/reorder/delete never freezes the GL thread. Batch
    // multiple pending ops from one flush into a single doc_apply_ops (see flushPendingTreeOps).
    const std::string whatStr = what ? what : "tree op";
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, ops,
        [&st, whatStr](const core::OpsResponse& resp, bool applied) {
            --st.rebuildsInFlight;
            if (!applied) return;
            if (resp.ok && !resp.aicm_path.empty()) {
                st.loadDocMeshAsync(resp.aicm_path);  // T-PERF3
            } else if (!resp.ok) {
                if (resp.error_code == "dependency_exists") {
                    st.addLog(std::string("[warn] ") + whatStr +
                              ": blocked — a feature depends on this order");
                    st.toast(ui::ToastLevel::Warn,
                             whatStr + " blocked: a feature depends on this order");
                } else {
                    st.addLog(std::string("[err] ") + whatStr + ": " + resp.error_message);
                    if (resp.error_code != "transport")
                        st.toast(ui::ToastLevel::Error,
                                 whatStr + " failed: " +
                                 (resp.error_message.empty() ? resp.error_code
                                                             : resp.error_message));
                }
            }
        });
}

static void applyTreeOps(AppState& st, const nlohmann::json& ops, const char* what) {
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return;
    s_pendingTreeOps.push_back({ops, what});
}

static void flushPendingTreeOps(AppState& st) {
    if (s_pendingTreeOps.empty()) return;
    auto pending = std::move(s_pendingTreeOps);
    s_pendingTreeOps.clear();
    // Batch every pending tree mutation from this frame into ONE applyOps (T-PERF1 DoD:
    // feature tree suppress/reorder/copy batched). Labels join for the toast/log.
    if (pending.size() == 1) {
        applyTreeOpsNow(st, pending[0].ops, pending[0].what.c_str());
        return;
    }
    nlohmann::json batch = nlohmann::json::array();
    std::string labels;
    for (const auto& p : pending) {
        if (p.ops.is_array())
            for (const auto& op : p.ops) batch.push_back(op);
        else
            batch.push_back(p.ops);
        if (!labels.empty()) labels += ", ";
        labels += p.what;
    }
    applyTreeOpsNow(st, batch, labels.c_str());
}

// ---- Nested component "Assign Material" (assembly FeatureManager) ----
// The part Material node routes through applyTreeOps on the ACTIVE doc. Nested component rows
// display a READ of a foreign part's document.json (mtime-cached NestedDocument), so assigning
// must open THAT part by path, apply set_material (+ derived set_appearance), save, and close —
// never touch the assembly. When the component is already the active edit target (enterComponentEdit
// / openComponentPart), apply on that live mirror instead of double-opening.
// Menu selection only ENQUEUES (same UAF hazard as applyTreeOps while the tree walks NestedDocument
// / assembly tree json); flushPendingNestedMaterial applies after the walk.
struct PendingNestedMaterial {
    std::string absPath;
    std::string key;   // empty => Remove Material (set_material key:null)
    bool        remove = false;
};
static std::vector<PendingNestedMaterial> s_pendingNestedMaterial;

static std::string canonicalDocPath(const std::string& path) {
    if (path.empty()) return {};
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path want = fs::weakly_canonical(fs::path(path), ec);
    if (ec) want = fs::path(path).lexically_normal();
    return want.string();
}

static bool pathsMatchCanonical(const std::string& a, const std::string& b) {
    if (a.empty() || b.empty()) return false;
    return canonicalDocPath(a) == canonicalDocPath(b);
}

// Build set_material + per-body set_appearance ops matching the part Material menu.
static nlohmann::json nestedMaterialOps(const std::string& key, bool remove,
                                        const std::vector<uint32_t>& bodiesIn) {
    nlohmann::json ops = nlohmann::json::array();
    if (remove || key.empty()) {
        ops.push_back({{"op", "set_material"}, {"key", nullptr}});
        return ops;
    }
    float col[3]; float mtl = 0.0f, rgh = 0.5f;
    materialAppearance(key, col, mtl, rgh);
    ops.push_back({{"op", "set_material"}, {"key", key}});
    std::vector<uint32_t> bodies = bodiesIn;
    if (bodies.empty()) bodies.push_back(1);
    for (uint32_t bid : bodies)
        ops.push_back({{"op", "set_appearance"},
                       {"id", "b" + std::to_string(bid)},
                       {"color", {col[0], col[1], col[2]}},
                       {"metallic", mtl}, {"roughness", rgh}});
    return ops;
}

// Collect AICM body ordinals from an ops-response / open envelope (owner_map or tess.bodies).
static std::vector<uint32_t> bodyIdsFromWorkerEnvelope(const nlohmann::json& res) {
    std::vector<uint32_t> bodies;
    auto pushId = [&](uint32_t id) {
        if (id == 0) return;
        if (std::find(bodies.begin(), bodies.end(), id) == bodies.end())
            bodies.push_back(id);
    };
    if (res.contains("owner_map") && res["owner_map"].is_object()) {
        for (auto it = res["owner_map"].begin(); it != res["owner_map"].end(); ++it) {
            if (!it.value().is_object()) continue;
            auto b = it.value().find("bodies");
            if (b == it.value().end() || !b->is_array()) continue;
            for (const auto& e : *b)
                if (e.is_number_integer()) pushId((uint32_t)e.get<long long>());
        }
    }
    if (bodies.empty() && res.contains("tess") && res["tess"].is_object()) {
        const auto& tess = res["tess"];
        if (tess.contains("bodies") && tess["bodies"].is_array()) {
            for (const auto& b : tess["bodies"]) {
                if (b.is_object() && b.contains("id") && b["id"].is_number_integer())
                    pushId((uint32_t)b["id"].get<long long>());
                else if (b.is_number_integer())
                    pushId((uint32_t)b.get<long long>());
            }
        }
        if (bodies.empty() && tess.contains("body_count") && tess["body_count"].is_number_integer()) {
            int n = tess["body_count"].get<int>();
            for (int i = 1; i <= n; ++i) pushId((uint32_t)i);
        }
    }
    if (bodies.empty()) bodies.push_back(1);
    return bodies;
}

// Apply material to a component part that is ALREADY the active DocMirror (in-context edit or
// openComponentPart tab). Does NOT save — the user owns that file; the live tree Material node
// updates immediately via applyTreeOpsNow (safe: we only call this from post-walk flush).
// NestedDocument re-reads on next save/exit (mtime-keyed cache).
static void applyNestedMaterialOnActiveMirror(AppState& st, const std::string& key, bool remove) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return;
    std::vector<uint32_t> bodies = st.docBodyIds;
    if (bodies.empty()) bodies.push_back(1);
    applyTreeOpsNow(st, nestedMaterialOps(key, remove, bodies), "set_material");
}

// Open component .step+ off-mirror, apply set_material (+ appearances), save, close. Leaves the
// assembly mirror untouched. Invalidates the NestedDocument cache so the row re-reads material.
static void applyNestedMaterialOnDisk(AppState& st, const std::string& absPath,
                                      const std::string& key, bool remove) {
    if (!st.workerStarted || absPath.empty()) return;
    const std::string lockReason = nestedAgentLockReason(st, absPath);
    if (!lockReason.empty()) { st.toast(ui::ToastLevel::Warn, lockReason); return; }
    nlohmann::json openRes;
    std::string err;
    if (!st.worker.call("doc_open", nlohmann::json{{"path", absPath}}, openRes, err)) {
        st.addLog("[material] open component failed: " + err);
        st.toast(ui::ToastLevel::Error, "Assign Material: open failed");
        return;
    }
    if (openRes.contains("code")) {
        const std::string msg = openRes.value("message", std::string("open failed"));
        st.addLog("[material] open component failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Assign Material: " + msg);
        return;
    }
    const std::string did = openRes.value("doc_id", std::string());
    if (did.empty()) {
        st.toast(ui::ToastLevel::Error, "Assign Material: open returned no doc_id");
        return;
    }
    // The open may bind an agent's doc not represented by a GUI tab. Do not close it on refusal.
    if (refuseAgentLockedTreeDoc(st, did)) return;
    // Never steal the assembly's active doc_id — if open re-bound an already-open tab doc, bail
    // without applying (the live-mirror path should have caught this). Close only when the id is
    // distinct from every open tab AND the active mirror.
    auto isLiveDocId = [&](const std::string& id) -> bool {
        if (id.empty()) return false;
        if (st.docMirror.docId() == id) return true;
        for (const auto& t : st.docTabs)
            if (t.docId == id) return true;
        return false;
    };
    const bool alreadyLive = isLiveDocId(did);
    if (alreadyLive) {
        // Component is open as a tab/mirror but path matching missed (or handoff). Apply via
        // mirror only when it IS the active doc; otherwise refuse rather than mutating a parked
        // tab's worker doc without activating it.
        if (st.docMirror.docId() == did) {
            applyNestedMaterialOnActiveMirror(st, key, remove);
            return;
        }
        st.toast(ui::ToastLevel::Warn,
                 "Assign Material: open the component part first (or Edit Model)");
        return;
    }

    const int rev = openRes.value("revision", 0);
    const std::vector<uint32_t> bodies = bodyIdsFromWorkerEnvelope(openRes);
    const nlohmann::json ops = nestedMaterialOps(key, remove, bodies);
    nlohmann::json applyRes;
    if (!st.worker.call("doc_apply_ops",
            nlohmann::json{{"doc_id", did}, {"base_revision", rev}, {"ops", ops}},
            applyRes, err)) {
        st.addLog("[material] apply on component failed: " + err);
        st.toast(ui::ToastLevel::Error, "Assign Material failed: " + err);
        nlohmann::json cl; std::string cerr;
        st.worker.call("doc_close", nlohmann::json{{"doc_id", did}}, cl, cerr);
        return;
    }
    if (applyRes.contains("code")) {
        const std::string msg = applyRes.value("message", std::string("apply failed"));
        st.addLog("[material] apply on component failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Assign Material failed: " + msg);
        nlohmann::json cl; std::string cerr;
        st.worker.call("doc_close", nlohmann::json{{"doc_id", did}}, cl, cerr);
        return;
    }
    nlohmann::json saveRes;
    if (!st.worker.call("doc_save",
            nlohmann::json{{"doc_id", did}, {"path", absPath}}, saveRes, err) ||
        saveRes.contains("code")) {
        const std::string msg = saveRes.contains("code")
            ? saveRes.value("message", std::string("save failed")) : err;
        st.addLog("[material] save component failed: " + msg);
        st.toast(ui::ToastLevel::Error, "Assign Material: save failed");
        nlohmann::json cl; std::string cerr;
        st.worker.call("doc_close", nlohmann::json{{"doc_id", did}}, cl, cerr);
        return;
    }
    nlohmann::json cl; std::string cerr;
    st.worker.call("doc_close", nlohmann::json{{"doc_id", did}}, cl, cerr);

    // Force NestedDocument re-parse: save bumps mtime, but also drop the slot so a same-frame
    // re-draw (or coarse FS mtime) cannot keep "not specified".
    ui::invalidateFeatureTreeNestedDocument(absPath);
    (void)ui::featureTreeNestedDocument(absPath);

    const char* label = "removed";
    if (!remove && !key.empty()) {
        label = key.c_str();
        for (int i = 0; i < kPhysicalMaterialsCount; ++i)
            if (key == kPhysicalMaterials[i].key) { label = kPhysicalMaterials[i].name; break; }
    }
    st.addLog(std::string("[material] component ") + absPath + " -> " + label);
    st.toast(ui::ToastLevel::Info,
             remove ? "Material removed from component"
                    : (std::string("Material: ") + label));
}

static void queueNestedMaterial(const std::string& absPath, const std::string& key, bool remove) {
    if (absPath.empty()) return;
    s_pendingNestedMaterial.push_back({absPath, key, remove});
}

static void flushPendingNestedMaterial(AppState& st) {
    if (s_pendingNestedMaterial.empty()) return;
    auto pending = std::move(s_pendingNestedMaterial);
    s_pendingNestedMaterial.clear();
    for (const auto& p : pending) {
        if (p.absPath.empty()) continue;
        // Prefer the live mirror when this component is the active edit target.
        bool useMirror = false;
        if (st.docMirror.hasDoc()) {
            if (st.inComponentEdit() &&
                pathsMatchCanonical(st.componentEdit.sourcePath, p.absPath))
                useMirror = true;
            else {
                // Active tab path matches the component source (user Open Part'd it).
                if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
                    const auto& tab = st.docTabs[st.activeTab];
                    if (!tab.path.empty() && pathsMatchCanonical(tab.path, p.absPath) &&
                        tab.docId == st.docMirror.docId())
                        useMirror = true;
                }
            }
        }
        if (useMirror)
            applyNestedMaterialOnActiveMirror(st, p.key, p.remove);
        else
            applyNestedMaterialOnDisk(st, p.absPath, p.key, p.remove);
    }
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
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return false;
    if (st.anyRunActive()) return false;   // same canDoc gate the tree mutations use
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
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return false;
    if (!st.docMirror.hasDoc() || !st.workerStarted || st.anyRunActive()) return false;
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
    if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return false;
    if (!st.docMirror.hasDoc() || !st.workerStarted || st.anyRunActive()) return false;
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

// ---- viewport -> FeatureManager sync (see feature_tree.h featureTreeRevealPick) ----
// Assembly AICM bodies: body_id is the 1-based ordinal among NON-suppressed components (engine
// assembly build: "counts ALL non-suppressed components"). Component-pattern instances carry
// synthetic ids >= kAsmPatternBodyBase whose component ("<seed>#<k>") only the engine's
// doc_resolve_pick reply knows; featureTreeNoteAssemblyPick caches those per doc revision.
static constexpr uint32_t kAsmPatternBodyBase = 1000000;
struct AsmPickComponents {
    std::string docId;
    int revision = -1;
    std::unordered_map<uint32_t, std::string> byBody;   // pattern body id -> seed component id
};
static AsmPickComponents s_asmPickComponents;

const nlohmann::json* featureTreeAssemblyComponent(const nlohmann::json& tree, uint32_t bodyId) {
    if (bodyId < 1 || bodyId >= kAsmPatternBodyBase || !tree.is_object()) return nullptr;
    auto comps = tree.find("components");
    if (comps == tree.end() || !comps->is_array()) return nullptr;
    uint32_t ordinal = 0;
    for (const auto& c : *comps) {
        if (!c.is_object() || jftbool(c, "suppressed")) continue;
        if (++ordinal == bodyId) return &c;
    }
    return nullptr;
}

// The engine's owner_feature for the latest assembly face/edge pick: the feature inside the
// picked component's own part. Its nested feature row (drawComponentFeatureRow) lights while that
// exact pick stays selected. One-shot requests: `openRow` expands the component row once (the
// user can collapse it again), `reveal` opens the feature's folder and scrolls to its row.
struct AsmPickOwner {
    std::string docId;
    int revision = -1;
    render::PickHit hit;
    std::string component;   // component tree row id (a pattern instance's seed)
    std::string feature;     // owning feature id within that component's part
    bool openRow = false;
    bool reveal = false;
};
static AsmPickOwner s_asmPickOwner;

void featureTreeNoteAssemblyPick(AppState& st, const render::PickHit& hit,
                                 const std::string& componentId,
                                 const std::string& ownerFeature) {
    if (componentId.empty()) return;
    const std::string seed = componentId.substr(0, componentId.find('#'));   // instance -> seed row
    if (!ownerFeature.empty()) {
        s_asmPickOwner = {st.docMirror.docId(), st.docMirror.revision(), hit, seed, ownerFeature,
                          true, true};
        st.treeScrollToSelection = true;
    }
    if (hit.body_id < kAsmPatternBodyBase) return;   // ordinals resolve locally
    AsmPickComponents& cache = s_asmPickComponents;
    if (cache.docId != st.docMirror.docId() || cache.revision != st.docMirror.revision()) {
        cache.docId = st.docMirror.docId();
        cache.revision = st.docMirror.revision();
        cache.byBody.clear();
    }
    cache.byBody[hit.body_id] = seed;
    st.treeScrollToSelection = true;   // the row can light only now — bring it into view
}

// The recorded owner for component row `componentId`, while its pick is still selected in the
// same doc revision; nullptr otherwise (a stale answer never lights a row).
static AsmPickOwner* asmPickOwnerFor(AppState& st, const std::string& componentId) {
    AsmPickOwner& o = s_asmPickOwner;
    if (o.feature.empty() || o.component != componentId || o.docId != st.docMirror.docId() ||
        o.revision != st.docMirror.revision())
        return nullptr;
    for (const auto& item : st.selection.items())
        if (item.hit == o.hit) return &o;
    o.openRow = o.reveal = false;   // deselected: never open/scroll to it later
    return nullptr;
}

// Component id (tree row) owning an assembly pick's body, or "" when unknown.
static std::string asmPickComponentId(AppState& st, const nlohmann::json& tree, uint32_t bodyId) {
    if (const nlohmann::json* c = featureTreeAssemblyComponent(tree, bodyId)) return jfld(*c, "id");
    const AsmPickComponents& cache = s_asmPickComponents;
    if (cache.docId != st.docMirror.docId() || cache.revision != st.docMirror.revision()) return {};
    auto it = cache.byBody.find(bodyId);
    return it == cache.byBody.end() ? std::string() : it->second;
}

// The LATEST feature (tree order) among a body's owners — the owner map's per-body list follows
// json key order ("f10" < "f2"), not history order.
static std::string latestBodyOwner(AppState& st, const nlohmann::json& tree, uint32_t bodyId) {
    const std::vector<std::string> owners = st.docMirror.ownerOfBody(bodyId);
    if (owners.empty()) return {};
    auto feats = tree.find("features");
    if (feats != tree.end() && feats->is_array()) {
        for (auto it = feats->rbegin(); it != feats->rend(); ++it) {
            if (!it->is_object()) continue;
            const std::string id = jfld(*it, "id");
            if (std::find(owners.begin(), owners.end(), id) != owners.end()) return id;
        }
    }
    return owners.back();
}

void featureTreeRevealPick(AppState& st, const render::PickHit& hit) {
    if (!hit.valid() || !st.docMirror.hasDoc()) return;
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    if (jfld(tree, "schema") == "aicad.asm/1") {
        // Component rows light from the live selection (drawAssemblyTree); only request the scroll.
        st.treeScrollToSelection = true;
        return;
    }
    std::string owner = hit.kind == render::PickKind::Face
        ? st.docMirror.ownerOfFace(hit.body_id, hit.topo_id)
        : hit.kind == render::PickKind::Edge
            ? st.docMirror.ownerOfEdge(hit.body_id, hit.topo_id) : std::string();
    if (owner.empty()) owner = latestBodyOwner(st, tree, hit.body_id);
    if (owner.empty()) return;   // no owner map (e.g. dumb import) — leave the tree alone
    s_multiSel.clear();          // the pick replaces any tree multi-selection
    st.selectedFeatureId = owner;
    st.featureHighlightFaces.clear();   // the pick owns the viewport highlight, not an old tree click
    st.treeScrollToSelection = true;
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
    bool forceOpen = false;         // expand now: a viewport pick revealed a row nested under it
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

// Forward decls — featureRow runs before the featureKindTint block further down.
static void fmSeedCollapsed();
static const float* featureKindTint(const std::string& type, bool isCut, bool isError = false);

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
    else flags |= ImGuiTreeNodeFlags_OpenOnArrow;  // collapsed by default; user expansions stick

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
    // The visible name is elided against the eye column (+ the status dot when this row shows one,
    // measured at the ambient ItemSpacing featureBadge lays out with) so it never runs under them.
    const float iconPx = std::min(16.0f * ui::uiScale, ImGui::GetFontSize());
    const float rightReserve =
        fmEyeColumnW() + (fmBadgeShown(c.fs.ok, c.fs.dangling, c.rolledBack) ? fmBadgeW() : 0.0f);
    std::string fullName = c.name;
    if (c.suppressed) fullName += " [S]";
    std::string lead;
    if (c.expandable) {
        const float sw = ImGui::CalcTextSize(" ").x;
        const int nsp = sw > 0.0f ? (int)std::ceil((iconPx + 5.0f * ui::uiScale) / sw) : 2;
        lead.assign(std::max(1, nsp), ' ');
    }

    // polish(G2): match fmRowPill padding (6dp x, 4dp y) so featureRow height == pill height.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(6.0f * ui::uiScale, 4.0f * ui::uiScale));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 2.0f * ui::uiScale));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    const std::string shownName =
        fmFitRowLabel(fullName, rightReserve, lead.empty() ? 0.0f : ImGui::CalcTextSize(lead.c_str()).x);
    const bool labelClipped = shownName != fullName;
    const std::string label = lead + shownName + "##ft_" + fid;
    ImVec2 rowPos = ImGui::GetCursorScreenPos();
    if (c.expandable) {
        fmSeedCollapsed();
        if (c.forceOpen) ImGui::SetNextItemOpen(true);
    }
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
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem(("##ctx_" + fid).c_str())) {
        const bool isSketch = (c.type == "sketch") ||
            (c.iconName && std::strcmp(c.iconName, "sketch_node") == 0);
        const bool isSketch3d = (c.type == "sketch3d");
        const bool multi = s_multiSel.size() > 1 && s_multiSel.count(fid) > 0;
        // Resolve import source once for "Open Part" (item 11 FM). Prefer params.source_path;
        // fall back to original_name only as a label — path is required to open.
        std::string importSrcPath;
        bool importIsMeshOnly = false;
        if (!multi && c.type == "import") {
            const nlohmann::json& tree = st.docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array()) {
                for (const auto& f : tree["features"]) {
                    if (!f.is_object() || jfld(f, "id") != fid) continue;
                    if (f.contains("params") && f["params"].is_object()) {
                        importSrcPath = jfld(f["params"], "source_path");
                        importIsMeshOnly = !f["params"].value("as_solid", false);
                    }
                    break;
                }
            }
        }
        const bool isImportComponent = !multi && c.type == "import";

        if (c.selected && ImGui::MenuItem("Deselect"))
            clearTreeFeatureSelection(st);
        // Wave4: attach this row (or multi-set) to the chat without arming the cube.
        if (ImGui::MenuItem("Add to AI context")) {
            if (multi) {
                for (const auto& id : s_multiSel) {
                    // Prefer display name from the live tree when available.
                    std::string nm = id, tp;
                    const auto& tree = st.docMirror.tree();
                    if (tree.is_object() && tree.contains("features") &&
                        tree["features"].is_array()) {
                        for (const auto& f : tree["features"]) {
                            if (!f.is_object() || jfld(f, "id") != id) continue;
                            nm = jfld(f, "name");
                            if (nm.empty()) nm = id;
                            tp = jfld(f, "type");
                            break;
                        }
                    }
                    pushFeatureContextRef(st, id, nm, tp);
                }
            } else {
                pushFeatureContextRef(st, fid, c.name, c.type);
            }
        }
        ImGui::Separator();
        // Read-only referenced-file navigation stays available during an agent edit.
        if (!multi && isImportComponent) {
            // Imported component: Open Part is the primary action (item 11).
            const bool canOpen = !importSrcPath.empty();
            if (!canOpen) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Open Part"))
                openComponentPart(st, importSrcPath);
            if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                revealInFileManager(importSrcPath);
            if (!canOpen) ImGui::EndDisabled();
        }
        if (!multi && ImGui::MenuItem("Zoom to Feature")) zoomToFeature(st, fid);
        if (st.activeDocLockedByAgent())
            ImGui::TextWrapped("%s", st.agentLockReason(st.docMirror.docId()).c_str());
        if (!c.canDoc) ImGui::BeginDisabled();   // gate mutating items while an agent run is busy

        // ---- type-specific edit / open (selection-dependent; item 8 FM RMB) ----
        if (multi) {
            // Multi-select: no Edit / Open / Rename — only bulk ops below.
        } else if (isImportComponent) {
            if (ImGui::MenuItem("Rename")) {
                s_renamingId = fid;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
            }
        } else if (isSketch) {
            if (ImGui::MenuItem("Edit Sketch")) {
                auto* sk = st.sketchMode;
                if (sk) sk->enterForFeature(st, fid);
            }
            if (ImGui::MenuItem("Edit Sketch Plane/Face")) {
                auto* sk = st.sketchMode;
                if (sk) sk->beginPlaneEdit(st, fid);
            }
            if (ImGui::MenuItem("Rename")) {
                s_renamingId = fid;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
            }
        } else if (isSketch3d) {
            if (ImGui::MenuItem("Edit 3D Sketch")) {
                if (st.sketch3dMode) st.sketch3dMode->enterForFeature(st, fid);
            }
            if (ImGui::MenuItem("Rename")) {
                s_renamingId = fid;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
            }
        } else {
            if (c.editCallback && *c.editCallback) {
                if (ImGui::MenuItem("Edit")) (*c.editCallback)(fid);
            }
            if (ImGui::MenuItem("Rename")) {
                s_renamingId = fid;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
            }
            if (ImGui::MenuItem("Remove from folder")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_folder"}, {"id", fid}, {"folder", ""}}}), "set_folder");
            }
        }

        // ---- Copy / Cut / Paste (all feature rows; multi uses the whole set) ----
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

        // ---- Suppress / Delete ----
        ImGui::Separator();
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

        // Import-only: Convert to Solid Body (mesh imports not yet sewn).
        if (isImportComponent && importIsMeshOnly) {
            ImGui::Separator();
            if (ImGui::MenuItem("Convert to Solid Body")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "edit_feature"}, {"id", fid},
                     {"params", {{"as_solid", true}}}}}), "convert to solid");
            }
        }

        if (!multi && c.convertCb && c.convertLabel) {
            ImGui::Separator();
            if (ImGui::MenuItem(c.convertLabel)) c.convertCb();
        }

        // View / history tools (single-row only).
        if (!multi) {
            ImGui::Separator();
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
                    s_apprOpen = true;
                }
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Delete")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "delete_feature"}, {"id", fid}, {"cascade", false}}}), "delete");
            }
        }
        if (!c.canDoc) ImGui::EndDisabled();
        ImGui::EndPopup();
    }

    ui::popContextMenuStyle();

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
                const bool isPlane = c.type == "plane" || c.type == "datum_plane" ||
                                     c.type == "ref_plane";
                const bool isRefGeom = c.type == "point" || c.type == "datum_point" ||
                                      c.type == "ref_point" || c.type == "csys" ||
                                      c.type == "coordinate_system" || c.type == "ref_csys";
                toggleFeatureEye(st, fid, c.ownedBodies, shown, isPlane, isRefGeom);
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

    // The status badge owns detailed status/error hover text. Keeping the row itself quiet avoids
    // tooltip cards covering selected rows while the user is trying to scan or drag the tree —
    // except when the name was elided: then hovering the label reveals the full name. Emitted
    // BEFORE the badge so the badge's own tooltip still wins while the dot is hovered.
    if (labelClipped && res.hovered && !overEye && !ImGui::IsMouseDragging(ImGuiMouseButton_Left))
        ImGui::SetTooltip("%s", fullName.c_str());

    featureBadge(c.fs.ok, !c.fs.error.empty(), c.fs.error, c.fs.dangling, c.rolledBack);

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

const char* fmFeatureIcon(const std::string& type, const nlohmann::json* feature) {
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
    if (type == "transform")        return "part";
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
    if (type == "transform")         return "[Tr]";
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

// Feature-kind → palette accent for the row wash + leading glyph tint.
// Palette roles only (no raw hex). Strengths stay subtle (PCB Layers-style).
// Mapping:
//   sketch / sketch3d          → sketchUnder (blue)
//   extrude/revolve/sweep/loft → accent (teal); cuts → statusErr (red-orange)
//   box/cyl/sphere/cone/torus  → accent (teal solids)
//   hole                       → statusWarn (amber)
//   fillet/chamfer/shell/...   → sketchExternal (violet dress-up)
//   patterns / mirror          → camPlunge (cyan)
//   helix / curves             → camArc (violet-blue)
//   split                      → camCut (blue)
//   boolean                    → sketchExternal (violet)
//   datums (plane/axis/point)  → axisZ (blue); axes/planes keep RGB at call site
//   import / components        → textDim (neutral slate)
//   sheet-metal (sm_*)         → statusWarn (amber)
//   weldment (weld_*)          → simLoad (coral)
//   surface (surf_*)           → axisY (green) / camLead family
//   script                     → statusWarn (amber)
//   sketch-in-error            → statusErr (caller passes isError)
static const float* featureKindTint(const std::string& type, bool isCut,
                                    bool isError) {
    const auto& p = ui::palette;
    if (isError) return p.statusErr;
    if (type == "sketch" || type == "sketch3d") return p.sketchUnder;
    if (type == "hole") return p.statusWarn;
    if (type == "fillet" || type == "chamfer" || type == "shell" ||
        type == "draft"  || type == "rib")
        return p.sketchExternal;
    if (type == "linear_pattern" || type == "circular_pattern" ||
        type == "mirror" || type == "curve_pattern" || type == "fill_pattern" ||
        type == "table_pattern" || type == "sketch_pattern")
        return p.camPlunge;
    if (type == "helix") return p.camArc;
    if (type == "split") return p.camCut;
    if (type == "boolean") return p.sketchExternal;
    if (type == "plane" || type == "datum_plane" || type == "ref_plane" ||
        type == "axis"  || type == "datum_axis"  || type == "ref_axis" ||
        type == "point" || type == "datum_point" || type == "ref_point" ||
        type == "csys"  || type == "coordinate_system" || type == "ref_csys")
        return p.axisZ;
    if (type == "import" || type == "component" || type == "transform")
        return p.textDim;
    if (type == "script") return p.statusWarn;
    if (type.size() >= 3 && type[0] == 's' && type[1] == 'm' && type[2] == '_')
        return p.statusWarn;
    if (type.size() >= 5 && type[0] == 'w' && type[1] == 'e' && type[2] == 'l' &&
        type[3] == 'd' && type[4] == '_')
        return p.simLoad;
    if (type.size() >= 5 && type[0] == 's' && type[1] == 'u' && type[2] == 'r' &&
        type[3] == 'f' && type[4] == '_')
        return p.axisY;
    // Solids (add / cut) — extrude family + primitives.
    if (type == "extrude" || type == "revolve" || type == "sweep" || type == "loft" ||
        type == "box" || type == "cylinder" || type == "sphere" ||
        type == "cone" || type == "torus")
        return isCut ? p.statusErr : p.accent;
    return p.textDim;
}
// RGB (0..1) view of featureKindTint for coloredRowWash. Alpha is ignored (wash owns it).
static void categoryColorRGB(const std::string& type, bool isCut, float out[3],
                             bool isError = false) {
    const float* t = featureKindTint(type, isCut, isError);
    out[0] = t[0]; out[1] = t[1]; out[2] = t[2];
}

// Frame-local: when true, the next expandable TreeNodeEx in Features is forced closed
// (first-sight seed for a doc). Cleared after the Features walk finishes that frame.
static bool s_fmCollapseSeed = false;
static void fmSeedCollapsed() {
    if (s_fmCollapseSeed)
        ImGui::SetNextItemOpen(false, ImGuiCond_Always);
}

// User-created datum features that belong under FM Reference (Planes/Axes/Points), not Features.
static bool isUserRefGeomType(const std::string& type) {
    return type == "plane" || type == "datum_plane" || type == "ref_plane" ||
           type == "axis"  || type == "datum_axis"  || type == "ref_axis"  ||
           type == "point" || type == "datum_point" || type == "ref_point" ||
           type == "csys"  || type == "coordinate_system" || type == "ref_csys";
}
static bool isUserPlaneType(const std::string& type) {
    return type == "plane" || type == "datum_plane" || type == "ref_plane";
}
static bool isUserAxisType(const std::string& type) {
    return type == "axis" || type == "datum_axis" || type == "ref_axis";
}
static bool isUserPointType(const std::string& type) {
    return type == "point" || type == "datum_point" || type == "ref_point" ||
           type == "csys" || type == "coordinate_system" || type == "ref_csys";
}

// Collect feature-ids of user datums referenced from a feature's refs (and plane/axis-ish params).
// Used to nest those datums under the consuming feature's FM dropdown while keeping the primary
// listing under Reference.
static void collectUserDatumRefsFromFeature(
    const nlohmann::json& feature,
    const std::unordered_set<std::string>& userDatumIds,
    std::unordered_set<std::string>& out) {
    if (!feature.is_object() || userDatumIds.empty()) return;
    std::function<void(const nlohmann::json&, const std::string&)> walk =
        [&](const nlohmann::json& node, const std::string& key) {
        if (node.is_string()) {
            const std::string v = node.get<std::string>();
            if (userDatumIds.count(v)) out.insert(v);
            return;
        }
        if (node.is_array()) {
            for (const auto& e : node) walk(e, key);
            return;
        }
        if (!node.is_object()) return;
        // Selector objects may name the owner feature / datum id.
        for (const char* k : {"feature", "owner_feature", "datum", "id", "plane", "axis",
                              "point", "csys", "base", "second", "a", "b", "tool",
                              "start_ref", "neutral", "pull"}) {
            auto it = node.find(k);
            if (it != node.end() && it->is_string()) {
                const std::string v = it->get<std::string>();
                if (userDatumIds.count(v)) out.insert(v);
            }
        }
        for (auto it = node.begin(); it != node.end(); ++it)
            walk(it.value(), it.key());
    };
    auto refs = feature.find("refs");
    if (refs != feature.end()) walk(*refs, std::string());
    // Params sometimes hold a bare plane/axis id (mirror, split, etc.).
    auto params = feature.find("params");
    if (params != feature.end() && params->is_object()) {
        for (auto it = params->begin(); it != params->end(); ++it) {
            const std::string& k = it.key();
            const bool planeish =
                k.find("plane") != std::string::npos || k.find("axis") != std::string::npos ||
                k.find("point") != std::string::npos || k == "base" || k == "second" ||
                k == "tool" || k == "csys" || k == "datum";
            if (!planeish) continue;
            walk(it.value(), k);
        }
    }
}


// Resolve a component source relative to the active assembly. Nested assembly sources are already
// normalized by feature_tree_nested.cpp and pass through unchanged.
static std::string resolveComponentPath(const std::string& srcPath) {
    if (srcPath.empty()) return {};
    namespace fs = std::filesystem;
    fs::path path(srcPath);
    if (path.is_absolute()) return path.lexically_normal().string();
    if (s_docPath.empty()) return {};
    return (fs::path(s_docPath).parent_path() / path).lexically_normal().string();
}

// ---- Import features treated as expandable component nodes (item 9) ----
// AI-built multi-body "assemblies" store each external part as type=import (with
// params.source_path) and a type=transform Place_* that moves that body. The tree
// collapses those into ONE expandable component row so the name is not duplicated
// (import name + Place_X), and expands into the source part's feature history.
static const nlohmann::json* featureParamsObj(const nlohmann::json& f) {
    auto it = f.find("params");
    if (it != f.end() && it->is_object()) return &(*it);
    return nullptr;
}
static std::string featureParamString(const nlohmann::json& f, const char* key) {
    if (const nlohmann::json* p = featureParamsObj(f)) {
        auto it = p->find(key);
        if (it != p->end() && it->is_string()) return it->get<std::string>();
    }
    return jfld(f, key);
}
// Absolute/resolved source path for an import feature, or empty if none / unresolvable.
static std::string importFeatureSourceAbs(const nlohmann::json& f) {
    if (jfld(f, "type") != "import") return {};
    std::string src = featureParamString(f, "source_path");
    if (src.empty()) return {};
    return resolveComponentPath(src);
}
// Prefer the human part name (original_name) over a Place_* / instance-counter label.
static std::string importDisplayName(const nlohmann::json& f) {
    std::string n = jfld(f, "name");
    std::string orig = featureParamString(f, "original_name");
    if (!orig.empty()) {
        // Keep a trailing instance counter from the feature name when present
        // ("CD-201_Mag_Wheel_2" stays distinguishable from the first instance).
        if (n.size() > orig.size() && n.compare(0, orig.size(), orig) == 0 &&
            n[orig.size()] == '_')
            return n;
        return orig;
    }
    return n.empty() ? std::string("Imported Part") : n;
}
// Canonical group key for shared-source instance grouping (assembly components use the
// same idea; multi-body import features group here). Empty path => no grouping.
static std::string sourcePathGroupKey(const std::string& absPath) {
    if (absPath.empty()) return {};
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path want = fs::weakly_canonical(fs::path(absPath), ec);
    if (ec) want = fs::path(absPath).lexically_normal();
    return want.string();
}
// Strip a trailing "_123" instance counter so "Wheel_1"/"Wheel_2" share "Wheel".
static std::string stripTrailingInstanceCounter(std::string name) {
    if (name.size() < 3) return name;
    size_t i = name.size();
    while (i > 0 && name[i - 1] >= '0' && name[i - 1] <= '9') --i;
    if (i > 0 && i < name.size() && name[i - 1] == '_' && i >= 2)
        return name.substr(0, i - 1);
    return name;
}
// Shared part name for a multi-import group folder (no per-instance counter).
static std::string importSharedDisplayName(const nlohmann::json& f) {
    std::string orig = featureParamString(f, "original_name");
    if (!orig.empty()) return stripTrailingInstanceCounter(orig);
    std::string n = jfld(f, "name");
    if (n.empty()) n = "Imported Part";
    return stripTrailingInstanceCounter(n);
}
// True when this transform merely places/copies an import body (absorbed under the import row).
static bool isImportPlacementTransform(
    const nlohmann::json& f,
    const std::unordered_map<std::string, const nlohmann::json*>& importById) {
    if (jfld(f, "type") != "transform") return false;
    const std::string body = featureParamString(f, "body");
    return !body.empty() && importById.count(body) > 0;
}

// Open a referenced document in a normal tab, focusing an existing tab for the same
// canonical path. Accepts an absolute path or an assembly-relative source path.
void openComponentPart(AppState& st, const std::string& absPath) {
    // resolveComponentPath accepts absolute paths unchanged (lexically_normal) and resolves
    // assembly-relative ones against s_docPath; callers typically pass the absolute form.
    // Also accept already-absolute paths when s_docPath is empty (Open Part from an unsaved
    // host doc still works for absolute source_path on import features).
    std::string abs = resolveComponentPath(absPath);
    if (abs.empty() && !absPath.empty()) {
        namespace fs = std::filesystem;
        fs::path p(absPath);
        if (p.is_absolute()) abs = p.lexically_normal().string();
        else if (!s_docPath.empty())
            abs = (fs::path(s_docPath).parent_path() / p).lexically_normal().string();
        else
            abs = p.lexically_normal().string();  // last-ditch: treat relative as CWD-relative
    }
    if (abs.empty()) {
        st.toast(ui::ToastLevel::Warn, "Referenced file has no resolvable path");
        return;
    }
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path want = fs::weakly_canonical(fs::path(abs), ec);
    if (ec) want = fs::path(abs).lexically_normal();
    abs = want.string();
    // Prefer an already-open non-handoff tab for the same canonical path.
    for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i) {
        const AppState::DocTab& tab = st.docTabs[i];
        if (tab.aiHandoff || tab.path.empty()) continue;
        std::error_code tabEc;
        fs::path have = fs::weakly_canonical(fs::path(tab.path), tabEc);
        if (tabEc) have = fs::path(tab.path).lexically_normal();
        if (have == want) {
            st.switchToTab(i);
            return;
        }
    }
    // Missing file: still request open so the open path can surface a clear error toast.
    st.requestOpenPath = abs;
}

// A nested row starts an in-context component edit, then waits until the part document is active
// before opening its real PropertyManager form. The PM owns debounced previews and DocMirror
// mutation; AppState's component-edit lifecycle keeps the assembly as a ghost and forces its
// component cache + mesh to refresh when the user exits the edit.
struct PendingNestedEdit {
    std::string componentId;
    std::string sourcePath;
    std::string featureId;
    int waitFrames = 0;
};
static PendingNestedEdit s_pendingNestedEdit;

static void beginNestedFeatureEdit(AppState& st, const std::string& componentId,
                                   const std::string& sourcePath,
                                   const ui::NestedFeature& feature) {
    if (sourcePath.empty() || feature.id.empty() || st.inComponentEdit()) return;
    const std::string lockReason = nestedAgentLockReason(st, sourcePath);
    if (!lockReason.empty()) { st.toast(ui::ToastLevel::Warn, lockReason); return; }
    s_pendingNestedEdit = {componentId, sourcePath, feature.id, 0};
    st.enterComponentEdit(componentId, sourcePath);
}

static void servicePendingNestedEdit(AppState& st) {
    if (s_pendingNestedEdit.featureId.empty()) return;
    const std::string lockReason = nestedAgentLockReason(st, s_pendingNestedEdit.sourcePath);
    if (!lockReason.empty()) {
        st.toast(ui::ToastLevel::Warn, lockReason);
        s_pendingNestedEdit = {};
        return;
    }
    if (++s_pendingNestedEdit.waitFrames > 900) {
        st.toast(ui::ToastLevel::Warn, "Referenced feature did not become editable");
        s_pendingNestedEdit = {};
        return;
    }
    if (!st.inComponentEdit()) return;
    const nlohmann::json& tree = st.docMirror.tree();
    const auto features = tree.is_object() ? tree.find("features") : tree.end();
    if (!tree.is_object() || features == tree.end() || !features->is_array()) return;
    const nlohmann::json* target = nullptr;
    for (const auto& feature : *features) {
        if (feature.is_object() &&
            feature.value("id", std::string()) == s_pendingNestedEdit.featureId) {
            target = &feature;
            break;
        }
    }
    if (!target) return;

    const std::string featureId = s_pendingNestedEdit.featureId;
    const std::string type = target->value("type", std::string());
    s_pendingNestedEdit = {};
    bool opened = false;
    if (type == "sketch" && st.sketchMode) {
        opened = st.sketchMode->enterForFeature(st, featureId);
    } else if (type == "sketch3d" && st.sketch3dMode) {
        opened = st.sketch3dMode->enterForFeature(st, featureId);
    } else if (st.propertyManager) {
        opened = st.propertyManager->openForEdit(st, featureId);
        if (opened) st.leftPaneTab = AppState::LP_PropertyManager;
    }
    if (!opened)
        st.toast(ui::ToastLevel::Info, "This referenced feature has no editable form");
}

// Collect sketch references so a consumed profile remains nested under its owning feature, matching
// the normal part tree rather than presenting a second foreign-tree convention.
static void componentScanSketchRefs(const nlohmann::json& node, const std::string& key,
                                    const std::unordered_set<std::string>& sketchIds,
                                    std::unordered_set<std::string>& seen) {
    if (node.is_object()) {
        if (jfld(node, "kind") == "region") {
            const std::string sketch = jfld(node, "sketch");
            if (!sketch.empty() && sketchIds.count(sketch)) seen.insert(sketch);
        }
        for (auto it = node.begin(); it != node.end(); ++it)
            componentScanSketchRefs(it.value(), it.key(), sketchIds, seen);
    } else if (node.is_array()) {
        for (const auto& value : node)
            componentScanSketchRefs(value, key, sketchIds, seen);
    } else if (node.is_string()) {
        const bool sketchKey = key.find("sketch") != std::string::npos || key == "profile";
        const std::string value = node.get<std::string>();
        if (sketchKey && sketchIds.count(value)) seen.insert(value);
    }
}

// `picked`: this row owns the latest assembly viewport pick (engine owner_feature) — drawn with
// the shared selected style; `reveal` (one-shot, cleared here) scrolls it into view.
static bool drawComponentFeatureRow(AppState& st, const std::string& idSuffix,
                                    const ui::NestedFeature& feature, bool expandable,
                                    bool nested, const std::string& topComponentId,
                                    const std::string& sourcePath, bool editable,
                                    bool picked = false, bool* reveal = nullptr) {
    const float nestIndent = 14.0f * ui::uiScale;
    if (nested) ImGui::Indent(nestIndent);
    const bool isCut = false;  // nested history has no live cut-mode bit; wash by type only
    const float* kindTint = featureKindTint(feature.type, isCut);
    FmRowRect rect = fmRowPill(picked, kindTint);
    ImGuiTreeNodeFlags flags =
        ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding;
    if (expandable) flags |= ImGuiTreeNodeFlags_OpenOnArrow;
    else flags |= ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen;
    const char* icon = fmFeatureIcon(feature.type, &feature.raw);
    if (expandable) {
        treeIcon(icon, "[f]", 16.0f);
        ImGui::SameLine(0, 4 * ui::uiScale);
    }
    const ImVec2 rowPos = ImGui::GetCursorScreenPos();
    const std::string name = feature.name.empty()
        ? (feature.type.empty() ? std::string("Feature") : feature.type) : feature.name;
    if (expandable) fmSeedCollapsed();
    const bool open = ImGui::TreeNodeEx((name + "##" + idSuffix).c_str(), flags);
    if (picked && reveal && *reveal) {
        ImGui::SetScrollHereY(0.5f);
        *reveal = false;
    }
    if (!expandable) treeIconInArrowSlot(rowPos, icon, "[f]");
    fmRowPillEnd(rect, picked);
    const std::string lockReason = nestedAgentLockReason(st, sourcePath);
    editable = editable && lockReason.empty();
    const bool activate = editable && ImGui::IsItemHovered() &&
                          ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem(("##nested_feature_" + idSuffix).c_str())) {
        if (!lockReason.empty()) ImGui::TextWrapped("%s", lockReason.c_str());
        if (!editable) ImGui::BeginDisabled();
        if (ImGui::MenuItem("Edit in Assembly"))
            beginNestedFeatureEdit(st, topComponentId, sourcePath, feature);
        if (!editable) ImGui::EndDisabled();
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
    if (activate) beginNestedFeatureEdit(st, topComponentId, sourcePath, feature);
    if (nested) ImGui::Unindent(nestIndent);
    return expandable && open;
}

// `pickedFeature`: the feature owning the latest assembly pick in THIS component (see
// drawComponentFeatureRow); a pending `reveal` also opens the folder that holds it.
static void drawComponentFeatureTree(AppState& st, const std::string& idPrefix,
                                     const std::string& topComponentId,
                                     const std::string& sourcePath,
                                     const std::vector<ui::NestedFeature>& features,
                                     bool editable, const std::string& pickedFeature = {},
                                     bool* reveal = nullptr) {
    auto isReference = [](const ui::NestedFeature& feature) {
        return feature.type == "plane" || feature.type == "datum_plane" ||
               feature.type == "axis" || feature.type == "point" ||
               feature.folder == "Reference";
    };
    // Non-Reference folders (Dress-up / Patterns / Sketches / user folders) nest the same
    // way the part-mode FeatureManager does — contiguous folder tags collapse under one header.
    auto isFolderFeature = [&](const ui::NestedFeature& feature) {
        return !feature.folder.empty() && feature.folder != "Reference" && !isReference(feature);
    };
    std::unordered_set<std::string> sketchIds;
    for (const auto& feature : features)
        if (feature.type == "sketch") sketchIds.insert(feature.id);

    std::unordered_map<std::string, int> consumeCount;
    std::unordered_map<std::string, std::string> consumer;
    for (const auto& feature : features) {
        if (feature.type == "sketch") continue;
        const auto refs = feature.raw.find("refs");
        if (refs == feature.raw.end()) continue;
        std::unordered_set<std::string> seen;
        componentScanSketchRefs(*refs, {}, sketchIds, seen);
        for (const auto& sketch : seen) {
            ++consumeCount[sketch];
            consumer[sketch] = feature.id;
        }
    }
    std::unordered_map<std::string, std::vector<std::string>> childrenOf;
    std::unordered_set<std::string> nestedSketches;
    for (const auto& item : consumer) {
        if (consumeCount[item.first] == 1) {
            childrenOf[item.second].push_back(item.first);
            nestedSketches.insert(item.first);
        }
    }
    auto findFeature = [&](const std::string& id) -> const ui::NestedFeature* {
        for (const auto& feature : features)
            if (feature.id == id) return &feature;
        return nullptr;
    };

    auto drawFeatureWithSketches = [&](const ui::NestedFeature& feature) {
        if (feature.type == "sketch" && nestedSketches.count(feature.id)) return;
        const auto children = childrenOf.find(feature.id);
        const bool hasChildren = children != childrenOf.end() && !children->second.empty();
        const bool open = drawComponentFeatureRow(
            st, idPrefix + "_" + feature.id, feature, hasChildren, false,
            topComponentId, sourcePath, editable,
            !pickedFeature.empty() && feature.id == pickedFeature, reveal);
        if (open && hasChildren) {
            for (const std::string& sketchId : children->second) {
                if (const ui::NestedFeature* sketch = findFeature(sketchId))
                    drawComponentFeatureRow(st, idPrefix + "_" + sketch->id, *sketch, false,
                                            true, topComponentId, sourcePath, editable);
            }
            ImGui::TreePop();
        }
    };

    std::vector<const ui::NestedFeature*> references;
    for (const auto& feature : features)
        if (isReference(feature)) references.push_back(&feature);
    if (!references.empty()) {
        FmRowRect rect = fmRowPill();
        treeIcon("body_folder", "[F]", 16.0f);
        ImGui::SameLine(0, 4 * ui::uiScale);
        // Reference folder inside a nested component stays DefaultOpen (Reference section
        // behaviour unchanged by the Features default-collapse rule).
        const bool open = ImGui::TreeNodeEx(("Reference##cref_" + idPrefix).c_str(),
            ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_SpanFullWidth |
            ImGuiTreeNodeFlags_FramePadding);
        fmRowPillEnd(rect);
        if (open) {
            for (const auto* feature : references)
                drawComponentFeatureRow(st, idPrefix + "_" + feature->id, *feature, false,
                                        false, topComponentId, sourcePath, editable);
            ImGui::TreePop();
        }
    }

    // History walk with Dress-up/Patterns (and any other folder tag) collapsed into folders —
    // same contiguous-folder convention the part tree uses.
    std::string openFolder;
    bool folderOpen = false;
    // A pending reveal opens the folder holding the picked feature (runs of one folder name
    // share one ImGui id, so opening the name opens its run).
    std::string revealFolder;
    if (reveal && *reveal && !pickedFeature.empty())
        if (const ui::NestedFeature* target = findFeature(pickedFeature))
            if (isFolderFeature(*target)) revealFolder = target->folder;
    for (const auto& feature : features) {
        if (isReference(feature)) continue;
        if (feature.type == "sketch" && nestedSketches.count(feature.id)) continue;

        const std::string folder = isFolderFeature(feature) ? feature.folder : std::string();
        if (folder != openFolder) {
            if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
            openFolder = folder;
            if (!folder.empty()) {
                FmRowRect fldRect = fmRowPill();
                treeIcon("body_folder", "[F]", 16.0f);
                ImGui::SameLine(0, 4 * ui::uiScale);
                fmSeedCollapsed();
                if (!revealFolder.empty() && folder == revealFolder) ImGui::SetNextItemOpen(true);
                folderOpen = ImGui::TreeNodeEx(
                    (folder + "##cfld_" + idPrefix + "_" + folder).c_str(),
                    ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding |
                    ImGuiTreeNodeFlags_OpenOnArrow);
                fmRowPillEnd(fldRect);
            }
        }
        if (!openFolder.empty() && !folderOpen) continue;
        drawFeatureWithSketches(feature);
    }
    if (folderOpen) ImGui::TreePop();
}

// Material display name only — no "Material: " prefix, no <> brackets, no part filename.
static std::string nestedMaterialLabel(const std::string& key) {
    if (key.empty()) return "not specified";
    for (int i = 0; i < kPhysicalMaterialsCount; ++i)
        if (key == kPhysicalMaterials[i].key)
            return kPhysicalMaterials[i].name;
    return key;
}

static void drawNestedInfoRow(const std::string& id, const std::string& label,
                              const char* icon, const char* fallback,
                              const std::string& tooltip = {}) {
    FmRowRect rect = fmRowPill();
    const ImVec2 rowPos = ImGui::GetCursorScreenPos();
    // Clip the label to the row width so long names never overrun the pill.
    const float iconReserve = ImGui::GetTreeNodeToLabelSpacing() + 8.0f * ui::uiScale;
    const float maxW = std::max(8.0f, ImGui::GetContentRegionAvail().x - iconReserve);
    const std::string clipped = ui::kit::elide(label.c_str(), maxW);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
               fmPalette().textDim[2], 0.72f));
    ImGui::TreeNodeEx((clipped + "##" + id).c_str(),
        ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
        ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
    ImGui::PopStyleColor();
    treeIconInArrowSlot(rowPos, icon, fallback, 0.72f);
    fmRowPillEnd(rect);
    if ((!tooltip.empty() || clipped != label) && ImGui::IsItemHovered()) {
        const std::string tip = tooltip.empty() ? label : tooltip;
        ImGui::SetTooltip("%s", tip.c_str());
    }
}

static void drawNestedDocumentContents(AppState& st, const std::string& idPrefix,
                                       const std::string& topComponentId,
                                       const std::string& sourcePath,
                                       const ui::NestedDocument& document,
                                       std::unordered_set<std::string>& pathStack,
                                       int depth) {
    // Material-only info row (no part filename — the parent component row already owns that).
    // Right-click Assign Material writes the component PART file (not the assembly): open by path,
    // set_material + derived set_appearance, save, close. Unresolved path stays display-only.
    {
        const std::string matKey = document.materialKey;
        const std::string mat = nestedMaterialLabel(matKey);
        std::string tip = "Material: " + mat;
        if (!document.path.empty()) tip += "\n" + document.path;
        else if (!document.fileName.empty()) tip += "\n" + document.fileName;
        const std::string rowId = idPrefix + "_info";
        // Prefer NestedDocument.path (absolute source) over the caller's sourcePath when both set.
        const std::string componentPath =
            !document.path.empty() ? document.path : sourcePath;
        const bool canAssign = !componentPath.empty() &&
            document.state == ui::NestedDocumentState::Ready &&
            st.workerStarted;

        FmRowRect rect = fmRowPill();
        const ImVec2 rowPos = ImGui::GetCursorScreenPos();
        const float iconReserve = ImGui::GetTreeNodeToLabelSpacing() + 8.0f * ui::uiScale;
        const float maxW = std::max(8.0f, ImGui::GetContentRegionAvail().x - iconReserve);
        const std::string clipped = ui::kit::elide(mat.c_str(), maxW);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
                   fmPalette().textDim[2], 0.72f));
        ImGui::TreeNodeEx((clipped + "##" + rowId).c_str(),
            ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
            ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        ImGui::PopStyleColor();
        treeIconInArrowSlot(rowPos, "material", "[i]", 0.72f);
        fmRowPillEnd(rect);

        if (canAssign) {
            // Mirror the part Material node menu (feature_tree ~3703): curated keys + Remove.
            // Selection is deferred via queueNestedMaterial — flush after the tree walk.
            ui::pushContextMenuStyle();
            if (ImGui::BeginPopupContextItem(("##nmat_ctx_" + rowId).c_str())) {
                ImGui::TextDisabled("Assign Material");
                ImGui::Separator();
                const std::string lockReason = nestedAgentLockReason(st, componentPath);
                if (!lockReason.empty()) ImGui::TextWrapped("%s", lockReason.c_str());
                ImGui::BeginDisabled(!lockReason.empty());
                for (int i = 0; i < kPhysicalMaterialsCount; ++i) {
                    const auto& pm = kPhysicalMaterials[i];
                    const bool cur = (matKey == pm.key);
                    if (ImGui::MenuItem(pm.name, nullptr, cur))
                        queueNestedMaterial(componentPath, pm.key, /*remove=*/false);
                }
                if (!matKey.empty()) {
                    ImGui::Separator();
                    if (ImGui::MenuItem("Remove Material"))
                        queueNestedMaterial(componentPath, {}, /*remove=*/true);
                }
                ImGui::EndDisabled();
                ImGui::EndPopup();
            }
            ui::popContextMenuStyle();
        }

        if (ImGui::IsItemHovered()) {
            if (canAssign && matKey.empty())
                ImGui::SetTooltip("No material assigned — right-click to assign\n%s",
                                 tip.c_str());
            else
                ImGui::SetTooltip("%s", tip.c_str());
        }
    }

    // Nested children (feature history + sub-components) sit one indent step under the parent
    // component so the hierarchy reads visually (Hunter nesting indentation).
    const float nestIndent = 14.0f * ui::uiScale;
    ImGui::Indent(nestIndent);

    if (document.state != ui::NestedDocumentState::Ready) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
                   fmPalette().textDim[2], 0.72f));
        ImGui::TextWrapped("%s", document.error.empty() ? "No feature history" : document.error.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(nestIndent);
        return;
    }
    if (!document.features.empty()) {
        // Only the top component's OWN history can hold the engine's owner_feature (it names a
        // feature of that component's part, never of a nested sub-assembly's children).
        AsmPickOwner* picked = depth == 0 ? asmPickOwnerFor(st, topComponentId) : nullptr;
        drawComponentFeatureTree(st, idPrefix, topComponentId, sourcePath, document.features, true,
                                 picked ? picked->feature : std::string(),
                                 picked ? &picked->reveal : nullptr);
    }

    if (depth >= 16 || document.components.empty()) {
        ImGui::Unindent(nestIndent);
        return;
    }
    for (const ui::NestedComponent& component : document.components) {
        const std::string childId = idPrefix + "_component_" + component.id;
        const bool resolved = !component.sourceAbsPath.empty();
        const ui::NestedDocument& child = ui::featureTreeNestedDocument(component.sourceAbsPath);
        const bool unavailable = !resolved ||
            child.state == ui::NestedDocumentState::Missing ||
            child.state == ui::NestedDocumentState::Unreadable;
        std::string label = (component.fixed ? "(f) " : "") + component.name;
        if (component.suppressed) label += " [S]";
        if (unavailable || component.hidden)
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
                       fmPalette().textDim[2], unavailable ? 0.55f : 0.72f));
        FmRowRect rect = fmRowPill(false, featureKindTint("import", false));
        treeIcon(child.schema == "aicad.asm/1" ? "assembly" : "part",
                 child.schema == "aicad.asm/1" ? "[A]" : "[P]", 16.0f,
                 unavailable ? 0.5f : 1.0f);
        ImGui::SameLine(0, 4 * ui::uiScale);
        fmSeedCollapsed();
        const bool open = ImGui::TreeNodeEx((label + "##" + childId).c_str(),
            ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding |
            ImGuiTreeNodeFlags_OpenOnArrow);
        fmRowPillEnd(rect);
        if (unavailable || component.hidden) ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            if (unavailable)
                ImGui::SetTooltip("%s", resolved ? child.error.c_str() : "Component has no source path");
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !unavailable)
                openComponentPart(st, component.sourceAbsPath);
        }
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopupContextItem(("##nested_component_" + childId).c_str())) {
            if (unavailable) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Open Part"))
                openComponentPart(st, component.sourceAbsPath);
            if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                revealInFileManager(component.sourceAbsPath);
            if (unavailable) ImGui::EndDisabled();
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();
        if (open) {
            if (!resolved) {
                ImGui::TextDisabled("(unresolved source)");
            } else if (pathStack.count(component.sourceAbsPath)) {
                ImGui::TextDisabled("(cyclic component reference)");
            } else {
                pathStack.insert(component.sourceAbsPath);
                drawNestedDocumentContents(st, childId, topComponentId,
                                           component.sourceAbsPath, child, pathStack, depth + 1);
                pathStack.erase(component.sourceAbsPath);
            }
            ImGui::TreePop();
        }
    }
    ImGui::Unindent(nestIndent);
}

// A pending viewport reveal (st.treeScrollToSelection) whose target row lives in the Features
// section — so the section header is forced open for it. Datum picks also request a scroll, but
// their rows live under Reference and must not pop Features open.
static bool fmRevealTargetsFeatures(AppState& st, const nlohmann::json& tree) {
    if (!st.treeScrollToSelection || !tree.is_object()) return false;
    if (jfld(tree, "schema") == "aicad.asm/1") {
        for (const auto& item : st.selection.items())
            if (!asmPickComponentId(st, tree, item.hit.body_id).empty()) return true;
        return false;
    }
    if (st.selectedFeatureId.empty()) return false;
    auto feats = tree.find("features");
    if (feats == tree.end() || !feats->is_array()) return false;
    for (const auto& f : *feats)
        if (f.is_object() && jfld(f, "id") == st.selectedFeatureId)
            return !isUserRefGeomType(jfld(f, "type"));
    return false;
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

    // ---- components (instance-grouped by resolved source path) ----
    // Same identity key the BOM uses: absolute/canonical source.path. Empty/unresolved paths
    // stay as ungrouped singletons. Groups of >=2 render one collapsible folder
    // "<DisplayName> xN" (DisplayName = first instance name with trailing _<digits> stripped);
    // children keep the existing per-instance row (eye, context menu, expansion, selection).
    // Folder order follows the first occurrence of each source path.
    struct AsmCompInst {
        const nlohmann::json* node = nullptr;
        std::string cid;
        std::string cname;
        std::string srcPath;
        std::string absPath;
        std::string groupKey;   // empty key => singleton (never groups)
        bool fixed = false;
        bool suppressed = false;
        bool hidden = false;
    };
    auto stripInstanceSuffix = [](std::string name) {
        // Strip a trailing "_123" instance counter so "Wheel_1"/"Wheel_2" share "Wheel".
        if (name.size() < 3) return name;
        size_t i = name.size();
        while (i > 0 && name[i - 1] >= '0' && name[i - 1] <= '9') --i;
        if (i > 0 && i < name.size() && name[i - 1] == '_' && i >= 2)
            return name.substr(0, i - 1);
        return name;
    };
    auto componentGroupKey = [](const std::string& absPath) -> std::string {
        if (absPath.empty()) return {};   // empty => ungrouped singleton
        namespace fs = std::filesystem;
        std::error_code ec;
        fs::path want = fs::weakly_canonical(fs::path(absPath), ec);
        if (ec) want = fs::path(absPath).lexically_normal();
        return want.string();
    };

    std::vector<AsmCompInst> insts;
    if (tree.contains("components") && tree["components"].is_array()) {
        insts.reserve(tree["components"].size());
        for (const auto& c : tree["components"]) {
            if (!c.is_object()) continue;
            AsmCompInst inst;
            inst.node = &c;
            inst.cid = jfld(c, "id");
            inst.cname = jfld(c, "name");
            if (inst.cname.empty()) inst.cname = inst.cid;
            inst.fixed = jftbool(c, "fixed");
            inst.suppressed = jftbool(c, "suppressed");
            inst.hidden = jftbool(c, "hidden");
            if (c.contains("source") && c["source"].is_object())
                inst.srcPath = jfld(c["source"], "path");
            inst.absPath = resolveComponentPath(inst.srcPath);
            inst.groupKey = componentGroupKey(inst.absPath);
            insts.push_back(std::move(inst));
        }
    }

    // Viewport -> tree: every component under the live viewport selection lights its row; the
    // latest pick's component is the one-shot reveal target (featureTreeRevealPick asked for it).
    std::unordered_set<std::string> pickedComps;
    std::string revealComp;
    for (const auto& item : st.selection.items()) {
        std::string pcid = asmPickComponentId(st, tree, item.hit.body_id);
        if (pcid.empty()) continue;
        revealComp = pcid;
        pickedComps.insert(std::move(pcid));
    }
    if (!st.treeScrollToSelection) revealComp.clear();

    // Build ordered groups: first-seen key order; empty keys are unique per instance.
    struct AsmCompGroup {
        std::string key;                 // shared key, or empty for singleton
        std::string absPath;             // shared source abs path (may be empty)
        std::vector<size_t> indices;     // into insts
    };
    std::vector<AsmCompGroup> groups;
    std::unordered_map<std::string, size_t> groupIndexByKey;
    for (size_t i = 0; i < insts.size(); ++i) {
        const AsmCompInst& inst = insts[i];
        if (inst.groupKey.empty()) {
            groups.push_back(AsmCompGroup{{}, inst.absPath, {i}});
            continue;
        }
        auto it = groupIndexByKey.find(inst.groupKey);
        if (it == groupIndexByKey.end()) {
            groupIndexByKey[inst.groupKey] = groups.size();
            groups.push_back(AsmCompGroup{inst.groupKey, inst.absPath, {i}});
        } else {
            groups[it->second].indices.push_back(i);
        }
    }

    // Draw one component instance row (eye / context menu / nested expansion). Used both for
    // ungrouped singletons and for children under an instance-group folder.
    auto drawComponentInstance = [&](const AsmCompInst& inst) {
        const std::string& cid = inst.cid;
        const std::string& cname = inst.cname;
        const std::string& absPath = inst.absPath;
        const bool fixed = inst.fixed;
        const bool suppressed = inst.suppressed;
        const bool hidden = inst.hidden;
        const ui::NestedDocument& nestedDoc = ui::featureTreeNestedDocument(absPath);
        const bool unavailable = absPath.empty() ||
            nestedDoc.state == ui::NestedDocumentState::Missing ||
            nestedDoc.state == ui::NestedDocumentState::Unreadable;
        const bool isAsm = nestedDoc.schema == "aicad.asm/1";
        FStatus fs = getFeatureStatus(statusArr, cid);

        const bool compSel = pickedComps.count(cid) > 0;
        FmRowRect compRect = fmRowPill(compSel, featureKindTint("import", false));
        // SolidWorks marks fixed components with a "(f)" prefix.
        std::string label = (fixed ? "(f) " : "") + cname;
        if (suppressed) label += " [S]";
        // Missing/unreadable references and hidden components are visibly dimmed. The source
        // error stays attached to the row as a tooltip instead of masquerading as "(no features)".
        if (hidden || unavailable) ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2],
                   unavailable ? 0.50f : 0.55f));
        ImGuiTreeNodeFlags lf = ImGuiTreeNodeFlags_SpanFullWidth |
            ImGuiTreeNodeFlags_FramePadding | ImGuiTreeNodeFlags_OpenOnArrow;
        treeIcon(isAsm ? "assembly" : "part", isAsm ? "[A]" : "[P]", 16.0f,
                 hidden || unavailable ? 0.5f : 1.0f);
        ImGui::SameLine(0, 4 * ui::uiScale);
        ImVec2 compRowPos = ImGui::GetCursorScreenPos();
        // Elide against the eye column (+ the status dot featureBadge appends below, if any) so a
        // long component name never runs under the visibility toggle.
        const std::string shownLabel = fmFitRowLabel(label,
            fmEyeColumnW() + (fmBadgeShown(fs.ok, fs.dangling, false, fs.warning) ? fmBadgeW() : 0.0f));
        fmSeedCollapsed();
        // The engine named the picked face's owning feature inside this component: open the row
        // so drawComponentFeatureRow can light and scroll to it.
        if (AsmPickOwner* owner = asmPickOwnerFor(st, cid); owner && owner->openRow) {
            ImGui::SetNextItemOpen(true);
            owner->openRow = false;
        }
        const bool compOpen = ImGui::TreeNodeEx((shownLabel + "##comp_" + cid).c_str(), lf);
        if (compSel && cid == revealComp) {
            ImGui::SetScrollHereY(0.5f);
            st.treeScrollToSelection = false;
        }
        if (hidden || unavailable) ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            if (shownLabel != label) ImGui::SetTooltip("%s", label.c_str());
            if (unavailable) {
                const std::string why = absPath.empty()
                    ? "Component source path is unresolved"
                    : nestedDoc.error;
                ImGui::SetTooltip("%s", why.c_str());
            }
            // Component double-click is the explicit pop-out action. In-context feature editing
            // starts from a nested feature row, preserving a clear distinction between the two.
            if (!unavailable && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
                !ImGui::IsMouseHoveringRect(compRowPos,
                    ImVec2(compRowPos.x + ImGui::GetFontSize() * 1.5f,
                           compRowPos.y + ImGui::GetFontSize() * 1.5f)))
                openComponentPart(st, absPath);
        }

        // ---- component visibility ---------------------------------------------------------
        // Persisted (asm_edit_component `hidden`), so undo/save work; a hidden component still
        // solves mates / counts in BOM (only the body draw is skipped) — unlike suppress. Route
        // the toggle through applyTreeOps like the mate suppress; the load path re-derives the
        // renderer body mask from the flag. Both the right-edge eye and the right-click item
        // drive the SAME op so they stay consistent (existing feature-row idiom).
        ImVec2 cNodeMin = ImGui::GetItemRectMin();
        ImVec2 cNodeMax = ImGui::GetItemRectMax();
        float  cEyeSz   = 14.0f * ui::uiScale;
        float  cEyePad  = 6.0f  * ui::uiScale;
        ImVec2 cEyeMin(cNodeMax.x - cEyeSz - cEyePad,
                       cNodeMin.y + (cNodeMax.y - cNodeMin.y - cEyeSz) * 0.5f);
        ImVec2 cEyeMax(cEyeMin.x + cEyeSz, cEyeMin.y + cEyeSz);
        bool   overCompEye = ImGui::IsMouseHoveringRect(cEyeMin, cEyeMax);
        drawEyeGlyph(ImGui::GetWindowDrawList(), cEyeMin, cEyeMax, !hidden, overCompEye, fmSurf());
        auto toggleCompHidden = [&](bool newHidden) {
            if (refuseAgentLockedTreeDoc(st, st.docMirror.docId())) return;
            applyTreeOps(st, nlohmann::json::array({
                {{"op","asm_edit_component"},{"id",cid},{"hidden",newHidden}}}), "component");
        };
        if (overCompEye) {
            const std::string reason = st.agentLockReason(st.docMirror.docId());
            ImGui::SetTooltip("%s", reason.empty() ? (hidden ? "Show Component" : "Hide Component")
                                                   : reason.c_str());
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) toggleCompHidden(!hidden);
        }
        // Wave4: armed cube — LMB on component (not the eye) toggles chat context ref.
        if (st.contextPickActive && ImGui::IsItemClicked(ImGuiMouseButton_Left) && !overCompEye)
            pushComponentContextRef(st, cid, cname);
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopupContextItem(("##cctx_" + cid).c_str())) {
            const std::string lockReason = nestedAgentLockReason(st, absPath);
            if (!lockReason.empty()) ImGui::TextWrapped("%s", lockReason.c_str());
            ImGui::BeginDisabled(st.activeDocLockedByAgent());
            // "Edit" = edit this component's PLACEMENT / mates in the assembly (the common action):
            // route through the request seam AsmPanels::draw() consumes to open + focus the Mate
            // panel for this component. Placed first (above "Edit Model") as the default edit.
            if (ImGui::MenuItem("Edit"))
                st.requestEditComponentMates = cid;
            ImGui::EndDisabled();
            // Whole-model editing remains available, while an individual nested feature's
            // Edit in Assembly action opens the real parameter form directly.
            const bool editableModel =
                !unavailable && nestedDoc.state == ui::NestedDocumentState::Ready &&
                lockReason.empty();
            if (!editableModel) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Edit Model"))
                st.enterComponentEdit(cid, absPath);
            if (!editableModel) ImGui::EndDisabled();
            ImGui::Separator();
            if (unavailable) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Open Part"))
                openComponentPart(st, absPath);
            if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                revealInFileManager(absPath);
            if (unavailable) ImGui::EndDisabled();
            ImGui::Separator();
            if (ImGui::MenuItem("Add to AI context")) pushComponentContextRef(st, cid, cname);
            if (ImGui::MenuItem(hidden ? "Show Component" : "Hide Component", nullptr, false,
                                !st.activeDocLockedByAgent()))
                toggleCompHidden(!hidden);
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();

        fmRowPillEnd(compRect, compSel);
        featureBadge(fs.ok, !fs.error.empty(), fs.error, fs.dangling, false, fs.warning);

        // Cached recursive document tree: metadata, editable feature history, and nested
        // assembly references. The cache keys on mtime+size, so a saved in-context edit is
        // visible on the next frame without reparsing steady-state components.
        if (compOpen) {
            if (absPath.empty()) {
                drawNestedInfoRow("component_" + cid + "_info",
                                  "not specified",
                                  "material", "[i]", "Material: not specified");
                ImGui::TextDisabled("(component has no resolvable source path)");
            } else {
                std::unordered_set<std::string> pathStack;
                pathStack.insert(absPath);
                drawNestedDocumentContents(st, "component_" + cid, cid, absPath,
                                           nestedDoc, pathStack, 0);
            }
            ImGui::TreePop();
        }
    };

    for (const AsmCompGroup& group : groups) {
        if (group.indices.size() < 2) {
            // Singleton — render exactly as before (no folder wrapper).
            drawComponentInstance(insts[group.indices[0]]);
            continue;
        }

        // Multi-instance folder: "<DisplayName> xN" at the first instance's tree position.
        const AsmCompInst& first = insts[group.indices[0]];
        const std::string displayName = stripInstanceSuffix(first.cname);
        const std::string folderLabel =
            displayName + " x" + std::to_string(group.indices.size());
        // Stable ImGui id from the group key (canonical path).
        const std::string folderId = "asmgrp_" + group.key;

        FmRowRect grpRect = fmRowPill();
        treeIcon("body_folder", "[F]", 16.0f);
        ImGui::SameLine(0, 4 * ui::uiScale);
        fmSeedCollapsed();
        if (!revealComp.empty() &&
            std::any_of(group.indices.begin(), group.indices.end(),
                        [&](size_t idx) { return insts[idx].cid == revealComp; }))
            ImGui::SetNextItemOpen(true);   // the picked instance sits in this folder
        const bool groupOpen = ImGui::TreeNodeEx(
            (folderLabel + "##" + folderId).c_str(),
            ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding |
            ImGuiTreeNodeFlags_OpenOnArrow);
        // Group-folder context menu: Open Part once for the shared source.
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopupContextItem(("##gctx_" + folderId).c_str())) {
            const bool canOpen = !group.absPath.empty();
            if (!canOpen) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Open Part"))
                openComponentPart(st, group.absPath);
            if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                revealInFileManager(group.absPath);
            if (!canOpen) ImGui::EndDisabled();
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();
        if (ImGui::IsItemHovered() && !group.absPath.empty())
            ImGui::SetTooltip("%s", group.absPath.c_str());
        fmRowPillEnd(grpRect);

        if (groupOpen) {
            const float nestIndent = 14.0f * ui::uiScale;
            ImGui::Indent(nestIndent);
            for (size_t idx : group.indices)
                drawComponentInstance(insts[idx]);
            ImGui::Unindent(nestIndent);
            ImGui::TreePop();
        }
    }

    ImGui::Separator();

    // ---- Mates folder ----
    FmRowRect matesRect = fmRowPill();
    treeIcon("mates_folder", "[M]", 16.0f);
    ImGui::SameLine(0, 4 * ui::uiScale);   // #4.5
    // Features-section item: start collapsed on first doc display (seed), then user choice sticks.
    fmSeedCollapsed();
    ImGuiTreeNodeFlags ff = ImGuiTreeNodeFlags_SpanFullWidth |
                            ImGuiTreeNodeFlags_FramePadding |
                            ImGuiTreeNodeFlags_OpenOnArrow;
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

                ui::pushContextMenuStyle();
                if (ImGui::BeginPopupContextItem(("##mctx_" + mid).c_str())) {
                    const std::string lockReason = st.agentLockReason(st.docMirror.docId());
                    if (!lockReason.empty()) ImGui::TextWrapped("%s", lockReason.c_str());
                    ImGui::BeginDisabled(st.activeDocLockedByAgent());
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
                    ImGui::EndDisabled();
                    ImGui::EndPopup();
                }
                ui::popContextMenuStyle();
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
// Rail-tab chrome (frosted pill + accent wash + high-contrast glyph). The previous ultra-light
// wash left only a dash/dot smudge under the FeatureManager title over light glass.
static bool toolbarIconBtn(const char* id, const char* iconName, const char* tooltip,
                           bool enabled) {
    const float S = ui::uiScale;
    const float btn = 24.0f * S;
    ImGui::PushID(id);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + btn, p0.y + btn);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    bool clicked = ImGui::InvisibleButton("##tb", ImVec2(btn, btn)) && enabled;
    bool hov = ImGui::IsItemHovered();

    // Opaque-enough frosted chip so the control never dissolves into the glass column. Stronger
    // on hover; selection glow only while hovered+enabled so resting state stays calm.
    const float wash = !enabled ? 0.32f : ((hov) ? 0.55f : 0.42f);
    ui::glassRowBackground(dl, p0, p1, 6.0f, false, /*selected=*/false, wash);
    const float tintStrength = !enabled ? 0.10f : ((hov) ? 0.30f : 0.18f);
    ui::coloredRowWash(dl, p0, p1, 6.0f, fmPalette().accent, tintStrength,
                       /*selected=*/hov && enabled);

    const float iz = 15.0f * S;
    const ui::AdaptiveSurface& surf = fmSurf();
    // Enabled icons always use primary ink (secondary was too close to the light glass wash and
    // read as the "weird" faint marks under the title). Disabled stays clearly muted, not ghosted.
    ImVec4 tintV = enabled
        ? surf.textPrimary
        : ImVec4(surf.textTertiary.x, surf.textTertiary.y,
                 surf.textTertiary.z, 0.55f);
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

static void tickOpenProgress(AppState& st);   // long-open progress readout (defined below)

void fmPaneHeaderChip(AppState& st, const char* title, const char* titleIcon, float paneCenterX) {
    // The pane header is drawn every frame on every left-pane tab, so a long open's elapsed
    // readout keeps advancing even while the FeatureManager body itself is not the visible tab.
    tickOpenProgress(st);
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

// Save filter: native Chudware part only (a silent Save / first-save always writes .step+).
// Save As writes the native .step+ ONLY — non-native formats live under File > Export (the Win32
// dialog appends the default extension regardless of the chosen filter, so a multi-format Save As
// silently wrote .step+ anyway).
static const char* kStepPlusFilter = "Chudware part (*.step+)\0*.step+\0All files (*.*)\0*.*\0";

// F4: Open/Import filter — every format the engine can import (see core/doc/formats.cpp
// format_registry() / doc_formats RPC, the future dynamic source). A leading aggregate entry lets
// the user see all CAD+mesh files at once, then grouped entries narrow by family, then All files.
// Win32 common-dialog layout: NUL-separated "label\0patterns\0"... pairs, double-NUL terminated
// (the C string literal's own trailing NUL supplies the final one).
static const char* kImportFilter =
    "All CAD, drawing & mesh files\0*.draw+;*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.sat;*.sab;*.dxf;*.dwg;*.stl;*.3mf;*.obj;*.ply;*.gltf;*.glb;*.3dm;*.sldprt;*.sldasm;*.x_t;*.x_b\0"
    "Chudware drawing (*.draw+)\0*.draw+\0"
    "Chudware part (*.step+)\0*.step+\0"
    "STEP (*.step;*.stp)\0*.step;*.stp\0"
    "IGES (*.iges;*.igs)\0*.iges;*.igs\0"
    "BREP (*.brep)\0*.brep\0"
    "ACIS (*.sat;*.sab)\0*.sat;*.sab\0"
    "AutoCAD (*.dwg;*.dxf)\0*.dwg;*.dxf\0"
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
// CHUDWARE_NO_FILE_DIALOGS set the bottom strip keeps the old raw-path InputText fallback and
// the dialog-needing paths refuse politely instead of blocking a headless run.
static bool fileDialogsDisabled() {
    static const bool disabled = std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr;
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

    // T-PERF4: never block GL on doc_save (large STEP+ embeds drawings + cache).
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    const std::string docId = st.docMirror.docId();
    const std::string pathCopy = path;
    st.toast(ui::ToastLevel::Info, "Saving " + pathBaseName(pathCopy) + "…");
    ++st.rebuildsInFlight;
    st.rpc.call("doc_save",
                nlohmann::json{{"doc_id", docId}, {"path", pathCopy}},
                [&st, pathCopy, docId](const core::RpcResult& r) {
                    --st.rebuildsInFlight;
                    if (st.docMirror.docId() != docId) {
                        st.addLog("[doc] save for " + docId + " ignored — active doc changed");
                        return;
                    }
                    if (!r.ok) {
                        st.addLog("[err] save: " + r.err);
                        st.toast(ui::ToastLevel::Error, "Save failed: " + r.err);
                        return;
                    }
                    if (r.result.is_object() && r.result.contains("code")) {
                        std::string msg = jfld(r.result, "message");
                        st.addLog("[err] save: " + msg);
                        st.toast(ui::ToastLevel::Error, "Save failed: " + msg);
                        return;
                    }
                    s_docPath = pathCopy;
                    st.clearActiveTabDirty();
                    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                        st.docTabs[st.activeTab].path = pathCopy;
                    st.clearDrawingDirtyForSource(docId);
                    ui::boxMirrorNotifySave(st, pathCopy);
                    st.addLog("[doc] saved: " + pathCopy);
                    st.toast(ui::ToastLevel::Info, "Saved " + pathBaseName(pathCopy));
                }, 120000);
}

// ---- long-open progress -----------------------------------------------------------------------
// doc_open and its optional mesh doc_activate run in hostd's long watchdog tier: a .step+ whose
// saved build cache can't be used is recompiled from its features, which for a big part takes
// minutes. While such a call is in flight the tree shows a spinner row and the status line an
// elapsed-seconds readout, and the finished open says whether the saved cache served it — so a
// slow open reads as "still building", never as a hang or an `Open failed`. Each open holds a
// ticket; its completion callbacks end their own ticket (several opens may overlap).
struct OpenProgress {
    uint64_t ticket = 0;
    std::string name;         // file basename
    bool meshPhase = false;   // doc_open landed; the mesh doc_activate is still running
    std::chrono::steady_clock::time_point started;
};
static std::vector<OpenProgress> s_openProgress;
static uint64_t s_openTicketSeq = 0;
// The st.status text this readout last wrote. The per-frame tick only rewrites st.status while it
// still shows that text, so a sim / agent status posted mid-open is never clobbered.
static std::string s_openStatusText;

static double secondsSince(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
}

static std::string formatOpenSeconds(double s) {
    char buf[32];
    std::snprintf(buf, sizeof buf, s < 10.0 ? "%.1f s" : "%.0f s", s);
    return buf;
}

// "Opening part.step+… 42 s" for the oldest in-flight open (+N more); empty when none.
static std::string openProgressLabel() {
    if (s_openProgress.empty()) return {};
    const OpenProgress& o = s_openProgress.front();
    char secs[32];
    std::snprintf(secs, sizeof secs, "%.0f s", secondsSince(o.started));
    std::string label = std::string(o.meshPhase ? "Loading mesh for " : "Opening ") + o.name +
                        "\xe2\x80\xa6 " + secs;
    if (s_openProgress.size() > 1)
        label += "  (+" + std::to_string(s_openProgress.size() - 1) + " more)";
    return label;
}

static void setOpenStatus(AppState& st, std::string text, int kind) {
    st.status = text;
    st.statusKind = kind;
    s_openStatusText = std::move(text);
}

// Per frame (tree body + pane header): advance the elapsed readout in the status line.
static void tickOpenProgress(AppState& st) {
    if (s_openProgress.empty() || st.status != s_openStatusText) return;
    std::string label = openProgressLabel();
    if (label != s_openStatusText) setOpenStatus(st, std::move(label), 0);
}

static uint64_t beginOpenProgress(AppState& st, const std::string& path) {
    OpenProgress o;
    o.ticket = ++s_openTicketSeq;
    o.name = pathBaseName(path);
    o.started = std::chrono::steady_clock::now();
    s_openProgress.push_back(std::move(o));
    setOpenStatus(st, openProgressLabel(), 0);   // the user's own action claims the status line
    return s_openTicketSeq;
}

static void setOpenMeshPhase(uint64_t ticket) {
    for (OpenProgress& o : s_openProgress)
        if (o.ticket == ticket) o.meshPhase = true;
}

// Drop the ticket; returns its elapsed seconds (-1 for ticket 0 / unknown, e.g. the sync path).
static double endOpenProgress(uint64_t ticket) {
    for (auto it = s_openProgress.begin(); it != s_openProgress.end(); ++it) {
        if (it->ticket != ticket) continue;
        const double secs = secondsSince(it->started);
        s_openProgress.erase(it);
        return secs;
    }
    return -1.0;
}

// How the engine served the doc_open just adopted: "loaded from saved build cache", or "rebuilt"
// plus the engine's cache_miss_reason. Empty when the engine doesn't report cache use at all.
static std::string openCacheNote(const core::DocMirror& m) {
    if (!m.lastOpenedCacheReported()) return {};
    if (m.lastOpenedCacheHit()) return "loaded from saved build cache";
    std::string why = m.lastOpenedCacheMissReason();
    std::string note = "rebuilt from features";
    if (!why.empty()) {
        std::replace(why.begin(), why.end(), '_', ' ');
        note += " \xe2\x80\x94 saved cache not used: " + why;
    }
    // contracts/stepplus.md §17: the rebuild is saved into the file, so the next open is warm.
    if (m.lastOpenedWriteback() > 0) {
        note += "; build saved to the file";
    } else if (m.lastOpenedWriteback() < 0) {
        std::string refused = m.lastOpenedWritebackReason();
        std::replace(refused.begin(), refused.end(), '_', ' ');
        note += "; build not saved to the file (" + refused + ")";
    }
    return note;
}

// The open finished (mesh on screen or nothing to load): end the ticket and say how long it took
// and whether the saved cache served it. `quiet` (st.handoffQuietOpen captured when the open was
// applied — a live MCP-handoff refresh would toast every build step) only logs.
static void announceOpened(AppState& st, const std::string& base, const std::string& cacheNote,
                           uint64_t ticket, bool quiet) {
    const double secs = endOpenProgress(ticket);
    std::string msg = "Opened " + base;
    if (!cacheNote.empty()) msg += " \xe2\x80\x94 " + cacheNote;
    if (secs >= 0.0) msg += " (" + formatOpenSeconds(secs) + ")";
    st.addLog("[open] " + msg);
    if (quiet) return;
    st.toast(ui::ToastLevel::Info, msg);
    setOpenStatus(st, msg, 0);
}

// A doc_open that failed (transport error or engine refusal): end the ticket, report with timing.
static void reportOpenFailed(AppState& st, const std::string& path, const std::string& err,
                             uint64_t ticket) {
    const double secs = endOpenProgress(ticket);
    std::string msg = "Open failed: " + pathBaseName(path) + " \xe2\x80\x94 " + err;
    if (secs >= 0.0) msg += " (after " + formatOpenSeconds(secs) + ")";
    st.addLog("[err] open: " + msg);
    st.toast(ui::ToastLevel::Error, msg);
    setOpenStatus(st, msg, 2);
}

// Apply a JUST-OPENED document (DocMirror::open already succeeded, so the mirror holds the new
// doc id / revision / tree). Factored out so BOTH open paths (native dialog and the
// CHUDWARE_NO_FILE_DIALOGS path-strip fallback) take the identical route.
//
// Geometry refresh: DocMirror::adoptOpenResponse keeps the doc_open response's own tess block,
// so the common case loads that mesh directly. Only when the response carried no mesh AND the
// engine did not report its session warm (older engines) do we fetch the mesh with doc_activate
// — async via st.rpc to keep the GL thread live; it never bumps the revision, so the mirror stays
// in step. A warm response with no mesh is a doc with nothing to display: no follow-up call, since
// any activation would only repeat work the engine already did. The callback runs on the GL
// thread (AsyncRpc::drain), so loadDocMesh / toast / resync are all safe there.
// `ticket` (beginOpenProgress) is ended here or in the mesh callback; 0 = untracked sync open.
static void applyOpenedDoc(AppState& st, const std::string& path, uint64_t ticket = 0) {
    st.clearViewportForNewDoc();   // resets viewport + tree transients (clears s_docPath too)
    // Re-derive default reference-plane/axis/origin visibility from the just-opened doc's tree.
    // docMirror.open() already ran applyDocDrivenVisibility once (via its notify() callback)
    // BEFORE this function was called, but clearViewportForNewDoc() just wiped autoHiddenPlanes /
    // hiddenFeatures back to "all visible" empty-doc defaults. The fast mesh-load path below
    // (the common case) never mutates the doc tree again, so nothing else would re-trigger the
    // docMirror listener — a solid part opened from disk showed its reference planes stuck
    // visible forever. Re-run it here so the DEFAULT (hidden on >=1 solid body, visible on an
    // empty/sketch-only doc) is correct from frame 0. planesUserToggled/axesUserToggled/
    // originUserToggled were just reset to false by the clear, so this only sets the default —
    // a later manual toggle still sticks for the rest of the session.
    if (st.docMirror.hasDoc())
        st.applyDocDrivenVisibility(st.docMirror.tree());
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
    // W2-ref: one-shot Zoom-to-Fit after the next mesh commit for a real file open.
    // Skipped for AI handoff quiet reopens (watching the AI build must not re-frame every step).
    // Sibling RefVisibility consumes + clears the flag once geometry with a known radius exists.
    if (!st.handoffQuietOpen)
        st.fitViewOnNextMeshCommit = true;
    st.addLog("[doc] opened: " + path + "  rev=" + std::to_string(st.docMirror.revision()));
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
    const std::string base = pathBaseName(path);
    const std::string cacheNote = openCacheNote(st.docMirror);
    const bool quiet = st.handoffQuietOpen;
    // FAST PATH: the doc_open response itself carried the mesh (DocMirror::open now keeps
    // tess.aicm_path + volume + feature_status/owner_map instead of discarding them). Load it
    // directly and skip the engine activation below — the forced-rebuild variant recompiled
    // whenever the open was served from the .step+'s embedded cache, i.e. every AI-handoff
    // refresh paid for the part twice. Fallback (empty aicm) keeps the historical path.
    if (!st.docMirror.lastOpenedAicm().empty()) {
#ifdef __EMSCRIPTEN__
        // Web: sync loadDocMesh runs on the main/GL thread and wedges under ASYNCIFY
        // (same class as doc_tabs switch). Route through the loader pthread.
        const std::string aicmFast = st.docMirror.lastOpenedAicm();
        st.loadDocMeshAsync(aicmFast);
        if (std::abs(st.docMirror.lastOpenedVolume()) < 1e-6) {
            st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = false;
            st.planesUserToggled = true;
            st.yaw = 0.0f; st.pitch = 89.5f; st.zoom = 1.0f;
            st.addLog("[open] flat 2D import — hid datum planes + oriented Top");
        }
        announceOpened(st, base, cacheNote, ticket, quiet);
        return;
#else
        std::string lerr;
        if (st.loadDocMesh(st.docMirror.lastOpenedAicm(), lerr)) {
            if (std::abs(st.docMirror.lastOpenedVolume()) < 1e-6) {
                // Flat 2D import (DXF/DWG): same plane + colour as the datum planes washes the
                // drawing out — hide the planes and face it Top-on (mirrors the async branch).
                st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = false;
                st.planesUserToggled = true;
                st.yaw = 0.0f; st.pitch = 89.5f; st.zoom = 1.0f;
                st.addLog("[open] flat 2D import — hid datum planes + oriented Top");
            }
            announceOpened(st, base, cacheNote, ticket, quiet);
            return;
        }
        st.addLog("[err] loadDocMesh(open fast path): " + lerr + " — falling back to rebuild");
#endif
    } else if (st.docMirror.lastOpenedWarm()) {
        // Warm engine session and no mesh in the response: the doc has nothing to display (empty
        // part / sketch-only). An activation would hand back the same empty state.
        announceOpened(st, base, cacheNote, ticket, quiet);
        return;
    }
    // Multi-document: bind this open-refresh to the doc it targets so a tab switch before the async
    // result drains can't load THIS doc's mesh / tree into whatever doc is foreground by then.
    const std::string openDocId = st.docMirror.docId();
    setOpenMeshPhase(ticket);
    ++st.rebuildsInFlight;
    st.rpc.call("doc_activate", {{"doc_id", openDocId}},
        [&st, base, cacheNote, openDocId, ticket, quiet](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (st.docMirror.docId() != openDocId) {
                st.addLog("[open] mesh refresh for " + openDocId + " ignored — active doc changed");
                announceOpened(st, base, cacheNote, ticket, quiet);
                return;
            }
            if (!r.ok || r.result.contains("code")) {
                std::string msg = !r.ok ? r.err : jfld(r.result, "message");
                const double secs = endOpenProgress(ticket);
                st.addLog("[err] open mesh refresh: " + msg);
                std::string warn = "Opened " + base + " \xe2\x80\x94 mesh refresh failed: " + msg;
                if (secs >= 0.0) warn += " (after " + formatOpenSeconds(secs) + ")";
                st.toast(ui::ToastLevel::Warn, warn);
                setOpenStatus(st, warn, 1);
                return;
            }
            std::string aicm;
            if (r.result.contains("tess") && r.result["tess"].is_object())
                aicm = jfld(r.result["tess"], "aicm_path");
            if (!aicm.empty()) {
#ifdef __EMSCRIPTEN__
                st.loadDocMeshAsync(aicm);
#else
                std::string lerr;
                if (!st.loadDocMesh(aicm, lerr) && !lerr.empty())
                    st.addLog("[err] loadDocMesh: " + lerr);
#endif
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
            announceOpened(st, base, cacheNote, ticket, quiet);
        }, core::kLongEngineCallTimeoutMs);
}

// User-facing open: doc_open on AsyncRpc so the GL thread stays live, with the long-tier deadline
// (a cache-miss rebuild may take minutes — failure is reported only when the engine says so) and
// an elapsed-time readout in the tree + status line until the mesh is on screen. Non-modal: tabs
// already open keep switching from their local snapshots while the engine builds.
static void startAsyncOpen(AppState& st, const std::string& path, bool asSolid) {
    nlohmann::json params = {{"path", path}};
    if (asSolid) params["as_solid"] = true;
    st.toast(ui::ToastLevel::Info, "Opening " + pathBaseName(path) + "\xe2\x80\xa6");
    const uint64_t ticket = beginOpenProgress(st, path);
    ++st.rebuildsInFlight;
    st.rpc.call("doc_open", std::move(params),
                [&st, path, ticket](const core::RpcResult& r) {
                    --st.rebuildsInFlight;
                    if (!r.ok) {
                        reportOpenFailed(st, path, r.err, ticket);
                        return;
                    }
                    std::string err;
                    if (!st.docMirror.adoptOpenResponse(r.result, err)) {
                        reportOpenFailed(st, path, err, ticket);
                        return;
                    }
                    applyOpenedDoc(st, path, ticket);
                    // Import lanes (DWG/DXF/SAT/STEP...) name every item they could not bring in;
                    // show each one so a partial import never looks complete.
                    const auto warnings = r.result.find("warnings");
                    if (warnings != r.result.end() && warnings->is_array())
                        for (const auto& w : *warnings) {
                            const std::string text = w.is_string() ? w.get<std::string>() : w.dump();
                            st.toast(ui::ToastLevel::Warn, pathBaseName(path) + ": " + text);
                            st.addLog("[warn] [open] " + pathBaseName(path) + ": " + text);
                        }
                }, core::kLongEngineCallTimeoutMs);
}

// Native-dialog Open: pick a .step+ file, open it in the worker, apply it.
// T-PERF4: doc_open runs on AsyncRpc so STEP import never freezes the GL thread.
static void doOpenDoc(AppState& st) {
    if (fileDialogsDisabled()) {
        st.toast(ui::ToastLevel::Info,
                 "File dialogs disabled — use the path field below the tree");
        return;
    }
    std::string path = openFileDialog(kImportFilter);
    if (path.empty()) return;  // canceled
    std::string openExt = std::filesystem::path(path).extension().string();
    for (char& ch : openExt) ch = (char)std::tolower((unsigned char)ch);
    if (openExt == ".draw+") {
        featureTreeOpenDrawingPath(st, path);
        return;
    }
    // Mesh files: ask whether to import as a solid (vs. graphics mesh). Non-mesh formats never prompt.
    bool asSolid = false;
    if (isMeshImportFormat(path)) {
        int choice = ui::promptImportMeshAsSolid();
        if (choice < 0) return;        // canceled
        asSolid = (choice == 1);
    }
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Error, "Open failed: worker not started");
        return;
    }
    startAsyncOpen(st, path, asSolid);
}

// Features section header — the SAME centred-label + twin-hairline + far-right chevron row as the
// document / Reference headers (fmSectionHeaderRow), plus the Add Folder glass button in the lead
// slot at the far left (Hunter red-pen placement); the left hairline starts just right of it.
// Returns the (possibly toggled) open state; sets *addFolder on the frame its button is clicked.
// addEn grays the button out (it still swallows its own clicks so a miss never collapses the list).
static bool fmFeaturesHeader(bool* open, bool addEn, bool* addFolder, const char* disabledReason) {
    const float S = ui::uiScale;
    const float btn = 18.0f * S;
    bool addClicked = false;
    fmSectionHeaderRow("Features", open, nullptr, btn + 6.0f * S, [&](ImVec2 p, float rowH) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 bMin(p.x + 4.0f * S, p.y + (rowH - btn) * 0.5f);
        const ImVec2 bMax(bMin.x + btn, bMin.y + btn);
        ImGui::SetCursorScreenPos(bMin);
        const bool clicked = ImGui::InvisibleButton("##add_folder", ImVec2(btn, btn));
        const bool hov = addEn && ImGui::IsItemHovered();
        addClicked = addEn && clicked;
        // Resting glass pill (same language as fmSectionDividerAddButton's "+") so it reads as a
        // control; the glyph is sized like the toolbar's (~0.7 of the chip), not a speck.
        ui::glassRowBackground(dl, bMin, bMax, 3.0f, false, false, hov ? 0.40f : 0.22f);
        const ImVec2 bc((bMin.x + bMax.x) * 0.5f, (bMin.y + bMax.y) * 0.5f);
        const float* dis = ui::palette.textDisabled;
        const ImU32 col = addEn ? ImGui::ColorConvertFloat4ToU32(fmSurf().textPrimary)
                                : ImGui::ColorConvertFloat4ToU32(ImVec4(dis[0], dis[1], dis[2], 0.5f));
        ui::drawIconGlass(dl, "folder_new", bc, btn * 0.72f, col, fmHalo(fmSurf()));
        if (ImGui::IsItemHovered()) {
            if (!addEn && disabledReason && *disabledReason)
                ImGui::SetTooltip("%s", disabledReason);
            else if (hov)
                ImGui::SetTooltip("Add Folder\n(groups the selected features, or drops an empty folder "
                                  "to drag features into)");
        }
    }, 0.0f, [](ImVec2, float) {});
    if (addFolder) *addFolder = addClicked;
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
        const char* rbLbl = "rolled back";
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
    const bool canNavigate = st.workerStarted && !st.anyRunActive();
    bool canDoc = canNavigate && !st.activeDocLockedByAgent();
    const std::string lockReason = st.agentLockReason(st.docMirror.docId());
    const float S = ui::uiScale;

    // W5C: the old tree/Properties/Configurations stub tabs were removed here — they were visual
    // decoration that duplicated the real vertical tab strip in the LeftPane container (layout.cpp
    // drawLeftPane). This strip now hosts only the functional document toolbar.
    // --- centered document toolbar ---
    // glassRowAtCursor is draw-only and does not advance the layout cursor. Without an explicit
    // row reservation the full-width filter InputText below was placed at the same Y and stole
    // clicks from undo/redo (and the rest of the toolbar).
    // Six buttons: new part / new assembly / open / save / undo / redo.
    const float bw = 24.0f * S, gap = 4.0f * S, nBtns = 6.0f;
    const float toolW = nBtns * bw + (nBtns - 1) * gap;
    ImVec2 toolRowMin = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    // Reserve the toolbar row in the layout FIRST (glass/icons are draw-list only). A zero-height
    // Dummy previously left only a Spacing gap under the header, so the row read as a faint
    // dash/dot smudge crushed against the filter field.
    ImGui::Dummy(ImVec2(availW, bw));
    float bx = toolRowMin.x + std::max(0.0f, (availW - toolW) * 0.5f);
    const float by = toolRowMin.y;
    auto toolBtn = [&](const char* id, const char* icon, const char* tip, bool en) -> bool {
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        bool hit = toolbarIconBtn(id, icon, !en && !lockReason.empty() ? lockReason.c_str() : tip, en);
        bx += bw + gap;
        return hit;
    };
    ImGui::PushID("fm_toolbar");
    if (toolBtn("fm_new", "new_doc", "New Part", canNavigate) && canNavigate) {
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
    if (toolBtn("fm_newasm", "assembly", "New Assembly", canNavigate) && canNavigate) {
        std::string err;
        st.suppressTabDirty = true;
        bool ok = st.docMirror.newAssembly(st.worker, err);
        st.suppressTabDirty = false;
        if (ok) {
            st.addLog("[doc] new assembly  rev=" + std::to_string(st.docMirror.revision()));
            st.clearViewportForNewDoc();   // C34
            st.addDocTab(/*isAssembly=*/true);
            st.showRibbon = true; st.ribbonTab = AppState::RT_Features;
        } else st.addLog("[err] new assembly: " + err);
    }
    if (toolBtn("fm_open", "open", "Open...", canNavigate) && canNavigate)
        doOpenDoc(st);   // C41: native dialog -> DocMirror::open -> viewport refresh
    bool canSave = canNavigate && st.docMirror.hasDoc();
    if (toolBtn("fm_save", "save", "Save  (Shift-click: Save As...)", canSave) && canSave)
        doSaveDoc(st, /*forceDialog=*/ImGui::GetIO().KeyShift);   // C41
    // W1-B (item 4): route through the shared AppState helper so the toolbar buttons and the global
    // Ctrl+Z / Ctrl+Y keys take the identical path (mesh refresh + owner map + confirmation toast).
    // Only enable when the active tab is the CAD doc currently bound in DocMirror — Drawing/ECAD
    // tabs leave the previous part loaded behind the sheet, and async tab rebind can lag activeTab.
    const bool canHistory = canDoc && st.docMirror.hasDoc() &&
        !(st.propertyManager && st.propertyManager->active()) &&
        !(st.sketch3dMode && st.sketch3dMode->active()) &&
        !st.activeTabIsDrawing() && !st.activeTabIsEcad() &&
        st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        st.docTabs[st.activeTab].docId == st.docMirror.docId() &&
        !st.docTabs[st.activeTab].historyCleared;
    if (toolBtn("fm_undo", "undo", "Undo", canHistory) && canHistory) st.docUndo();
    if (toolBtn("fm_redo", "redo", "Redo", canHistory) && canHistory) st.docRedo();
    ImGui::PopID();
    // Restore the layout cursor after absolute-positioned buttons (they leave it mid-row).
    ImGui::SetCursorScreenPos(ImVec2(toolRowMin.x, toolRowMin.y + bw));
    ImGui::Dummy(ImVec2(0, 0));

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
    // No Separator() under the filter — the hairline read as another orphaned mark under the
    // title/filter stack on light glass. Spacing alone separates filter from the tree body.
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
    servicePendingNestedEdit(st);

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
    // A doc_open / mesh activation still building (long cache-miss rebuild): spinner row with the
    // elapsed time, above the tree of whatever doc is on screen meanwhile.
    tickOpenProgress(st);
    if (!s_openProgress.empty()) {
        const std::string label = openProgressLabel();
        ui::kit::spinner(label.c_str());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("The engine is building this file. Other open tabs stay usable;\n"
                              "it opens in its own tab when the build finishes.");
    }

    // ---- document title ----
    const nlohmann::json& tree = st.docMirror.tree();
    std::string docName = "Part1";
    if (tree.is_object()) docName = jfld(tree, "name");
    if (docName.empty()) docName = "Part1";

    // C37-E (item 2): the SAME guard the top toolbar uses (st.workerStarted && !st.anyRunActive()).
    // Every mutating tree interaction — context menu, drag-reorder, Ctrl+Up/Down, rollback drag —
    // is gated on this so no op fires while an agent run is in flight.
    bool canDocTree = st.workerStarted && !st.anyRunActive() && !st.activeDocLockedByAgent();
    const std::string agentLockReason = st.agentLockReason(st.docMirror.docId());

    {
        // Filename / References / Features are three INDEPENDENT collapsible sections (Hunter):
        // collapsing the filename hides only its equations/material/solid-bodies, leaving the
        // References and Features sections in place. All three use the same section-header look.
        //
        // Auto-collapse (Wave 1): the part-name (file) + Reference sections start OPEN on a fresh
        // empty part/assembly (no solid body yet) so Equations/Material/Origin/planes are one click
        // away for the first sketch. The moment the doc gains a solid body (first extrude, or open
        // of an assembly that already has bodies) both auto-collapse — unless the user has already
        // toggled that header, after which their choice sticks (same sticky pattern as
        // planesUserToggled / originUserToggled). Features stays user-controlled only.
        // State is keyed by doc id so multi-doc tab switches don't bleed open/closed + toggle
        // flags across documents (the previous pure-statics lived forever across every doc).
        const float kSecIndent = 8.0f * ui::uiScale;
        struct SecCollapseState {
            bool fileOpen = true;
            bool refOpen  = true;
            bool featOpen = true;
            bool fileUserToggled = false;
            bool refUserToggled  = false;
            bool lastHasMesh = false;
            bool seeded = false;
        };
        static std::unordered_map<std::string, SecCollapseState> s_secByDoc;
        const std::string secDocKey = st.docMirror.hasDoc()
            ? st.docMirror.docId()
            : std::string();   // empty key covers the no-doc / pre-open panel state
        SecCollapseState& sec = s_secByDoc[secDocKey];
        const bool hasMesh = st.hasDocMesh;
        if (!sec.seeded) {
            // First sight of this doc: open only while no solid body yet; assemblies (and parts
            // reopened with geometry) land collapsed so the feature history is the focus.
            const bool wantOpen = !hasMesh;
            sec.fileOpen = wantOpen;
            sec.refOpen  = wantOpen;
            sec.featOpen = true;   // Features HEADER stays expanded
            sec.lastHasMesh = hasMesh;
            sec.seeded = true;
            // Force every expandable item *inside* Features collapsed on first display.
            // User expansions after this frame stick via ImGui tree storage.
            s_fmCollapseSeed = true;
        } else if (!sec.lastHasMesh && hasMesh) {
            // false→true transition: first solid body just appeared — auto-collapse both unless
            // the user already took control of that header.
            if (!sec.fileUserToggled) sec.fileOpen = false;
            if (!sec.refUserToggled)  sec.refOpen  = false;
            sec.lastHasMesh = hasMesh;
        } else {
            sec.lastHasMesh = hasMesh;
        }
        bool& s_fileSecOpen = sec.fileOpen;
        bool& s_refSecOpen  = sec.refOpen;
        bool& s_featSecOpen = sec.featOpen;
        // A viewport pick revealing a Features row opens the section it lives in.
        const bool featuresReveal = fmRevealTargetsFeatures(st, tree);
        if (featuresReveal) s_featSecOpen = true;
        bool fileRightClicked = false;
        const bool fileOpenBefore = s_fileSecOpen;
        // PLM: revision badge + a jump-to-this-part's-links button. They ride in the header's
        // trailing slot when the full doc name still fits beside them; on a narrow pane they drop
        // to their own right-aligned row below instead of overlapping the name and chevron.
        std::string plmRev, plmLife; bool plmDirty = false;
        const bool hasPlm = ui::plmActiveDocPlmState(st, plmRev, plmLife, plmDirty);
        const ImGuiStyle& fmStyle = ImGui::GetStyle();
        float plmW = 0.0f;
        if (hasPlm) {
            const std::string badgeText =
                plmRev.empty() ? plmLife : plmRev + " \xc2\xb7 " + plmLife;
            plmW = ImGui::CalcTextSize("links").x + fmStyle.FramePadding.x * 2.0f +
                   fmStyle.ItemSpacing.x + ImGui::CalcTextSize(badgeText.c_str()).x;
        }
        auto drawPlmControls = [&](ImVec2 at) {
            ImGui::SetCursorScreenPos(at);
            if (ImGui::SmallButton("links##fm_graph")) {
                st.activeWorkspace = AppState::Workspace::PLM;
                g_plmHub.page = PlmPage::Graph;
                ui::plmGraphFocus(docName);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show this part's links in the project graph");
            ImGui::SameLine();
            ui::plmLifecycleBadge(plmLife, plmRev);
        };
        // Header geometry mirrors fmSectionHeaderRow: 4dp inset, name, 6+8dp gaps, slot, 6dp, 18dp chevron.
        const float plmS = ui::uiScale;
        const float plmInline = ImGui::CalcTextSize(docName.c_str()).x + plmW + 44.0f * plmS;
        if (hasPlm && plmInline <= ImGui::GetContentRegionAvail().x) {
            fmSectionHeaderRow(docName.c_str(), &s_fileSecOpen, &fileRightClicked, 0.0f,
                               [](ImVec2, float) {}, plmW, [&](ImVec2 slot, float rowH) {
                drawPlmControls(ImVec2(slot.x, slot.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f));
            });
        } else {
            fmSectionDividerCollapse(docName.c_str(), &s_fileSecOpen, &fileRightClicked);
            if (hasPlm) {
                const ImVec2 row = ImGui::GetCursorScreenPos();
                const float avail = ImGui::GetContentRegionAvail().x;
                drawPlmControls(ImVec2(row.x + std::max(0.0f, avail - plmW - 24.0f * plmS), row.y));
            }
        }
        if (s_fileSecOpen != fileOpenBefore) sec.fileUserToggled = true;

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
                ui::pushContextMenuStyle();
                if (ImGui::BeginPopup("##root_ctx")) {
                    if (!agentLockReason.empty()) ImGui::TextWrapped("%s", agentLockReason.c_str());
                    ImGui::BeginDisabled(st.activeDocLockedByAgent());
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
                    ImGui::EndDisabled();
                    ImGui::EndPopup();
                }
                ui::popContextMenuStyle();
                // Picker popup lives OUTSIDE the context menu (a sibling) to avoid nested-popup
                // ordering pitfalls; it is opened by the menu item via s_openRootPicker.
                if (s_openRootPicker) { ImGui::OpenPopup("##appearance_root"); s_openRootPicker = false; }
                if (appearanceColorPicker("##appearance_root", s_rootColor,
                                          &s_rootMetallic, &s_rootRoughness,
                                          agentLockReason.empty() ? nullptr : agentLockReason.c_str())) {
                    applyTreeOps(st, nlohmann::json::array({
                        {{"op", "set_appearance"}, {"id", "b1"},
                         {"color", {s_rootColor[0], s_rootColor[1], s_rootColor[2]}},
                         {"metallic", s_rootMetallic}, {"roughness", s_rootRoughness}}}),
                        "set_appearance");
                }
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
                        ui::pushContextMenuStyle();
                        if (ImGui::BeginPopupContextItem("##mat_ctx")) {
                            ImGui::TextDisabled("Assign Material");
                            ImGui::Separator();
                            if (!agentLockReason.empty())
                                ImGui::TextWrapped("%s", agentLockReason.c_str());
                            ImGui::BeginDisabled(st.activeDocLockedByAgent());
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
                            ImGui::EndDisabled();
                            ImGui::EndPopup();
                        }
                        ui::popContextMenuStyle();
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
                            ui::pushContextMenuStyle();
                            if (ImGui::BeginPopupContextItem(ctxId)) {
                                if (ImGui::MenuItem("Add to AI context"))
                                    pushBodyContextRef(st, bid, bname);
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
                            ui::popContextMenuStyle();
                            if (rowClicked) {
                                if (st.contextPickActive) {
                                    pushBodyContextRef(st, bid, bname);
                                } else {
                                    setBodyShown(st, bi, hidden);
                                }
                            }
                            if (rowHovered)
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", bname);
                        }
                        ImGui::TreePop();
                    }
                }
            }
            ImGui::Unindent(kSecIndent);
        }   // end filename section

        // ---- References section (Points / Planes / Axes folders) — its own collapse,
        // independent of the filename above and the Features below. ----
        // Keep auto-axis FM labels current ("Plane1 X Axis") even if inject still seeds
        // the legacy "Axis A ∩ B" string into axisAutoCache / axisDisplayNames.
        st.rebuildAxisDisplayNames(tree);

        // User-created datum features (plane/axis/point/csys) for Reference folders AND for
        // nesting under consuming Features rows. Collected outside the section collapse so
        // Features still skips/nests them when Reference is collapsed.
        struct UserRefFeat { std::string id; std::string name; std::string type; };
        std::vector<UserRefFeat> userPlanes, userAxes, userPoints;
        std::unordered_set<std::string> userDatumIds;
        if (tree.is_object()) {
            auto rfi = tree.find("features");
            if (rfi != tree.end() && rfi->is_array()) {
                for (const auto& f : *rfi) {
                    if (!f.is_object()) continue;
                    const std::string ty = jfld(f, "type");
                    if (!isUserRefGeomType(ty)) continue;
                    const std::string id = jfld(f, "id");
                    if (id.empty()) continue;
                    userDatumIds.insert(id);
                    UserRefFeat ur;
                    ur.id = id;
                    ur.type = ty;
                    ur.name = jfld(f, "name");
                    if (ur.name.empty()) ur.name = ty + "·" + id;
                    if (isUserPlaneType(ty))      userPlanes.push_back(ur);
                    else if (isUserAxisType(ty))  userAxes.push_back(ur);
                    else if (isUserPointType(ty)) userPoints.push_back(ur);
                }
            }
        }

        const bool refOpenBefore = s_refSecOpen;
        fmSectionDividerCollapse("Reference", &s_refSecOpen);
        if (s_refSecOpen != refOpenBefore) sec.refUserToggled = true;
        if (s_refSecOpen) {
            ImGui::Indent(kSecIndent);

            // ---- Points folder (origin point + user ref-point/csys features) -----------------
            // Folder header mirrors Axes (body_folder + seed-collapsed + cascade eye). The origin
            // point is a leaf child so selection / PM point-well routing stay on the point row.
            // Future intersection-created point features (type point/datum_point) list here too.
            {
                // Persistent origin visibility (hiddenFeatures) — NOT the viewport-published
                // originVisEff, which lags the FM by a frame on fresh docs (same fix as planes/axes).
                // Folder-level cascade eye: origin + every user point/csys. anyShown drives the
                // glyph (dim when all hidden); click flips the whole set and sticks control.
                const bool originShown = st.hiddenFeatures.count("point.origin") == 0;
                bool anyPointShown = originShown;
                for (const auto& up : userPoints)
                    if (st.hiddenFeatures.count(up.id) == 0) anyPointShown = true;
                FmRowRect originFldRect = fmRowPill();
                treeIcon("body_folder", "[F]", 16.0f);
                ImGui::SameLine(0, 4 * ui::uiScale);
                if (!anyPointShown)
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                fmSeedCollapsed();
                const bool originFldOpen = ImGui::TreeNodeEx(
                    (fmFitRowLabel("Points", fmEyeColumnW()) + "##ref_points_folder").c_str(),
                    ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
                if (!anyPointShown) ImGui::PopStyleColor();
                {
                    ImVec2 oFldMin = ImGui::GetItemRectMin();
                    ImVec2 oFldMax = ImGui::GetItemRectMax();
                    float  oEyeSz = 14.0f * ui::uiScale, oEyePad = 6.0f * ui::uiScale;
                    ImVec2 oEyeMin(oFldMax.x - oEyeSz - oEyePad,
                                   oFldMin.y + (oFldMax.y - oFldMin.y - oEyeSz) * 0.5f);
                    ImVec2 oEyeMax(oEyeMin.x + oEyeSz, oEyeMin.y + oEyeSz);
                    bool   oOverEye = ImGui::IsMouseHoveringRect(oEyeMin, oEyeMax);
                    drawEyeGlyph(ImGui::GetWindowDrawList(), oEyeMin, oEyeMax,
                                 anyPointShown, oOverEye, fmSurf());
                    if (oOverEye) {
                        ImGui::SetTooltip(anyPointShown ? "Hide Points" : "Show Points");
                        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                            const bool show = !anyPointShown;
                            if (show) {
                                st.hiddenFeatures.erase("point.origin");
                            } else {
                                st.hiddenFeatures.insert("point.origin");
                                if (st.selectedFeatureId == "point.origin")
                                    st.selectedFeatureId.clear();
                            }
                            st.originUserToggled = true;
                            for (const auto& up : userPoints) {
                                if (show) st.hiddenFeatures.erase(up.id);
                                else {
                                    st.hiddenFeatures.insert(up.id);
                                    if (st.selectedFeatureId == up.id)
                                        st.selectedFeatureId.clear();
                                }
                                st.refGeomUserToggled.insert(up.id);
                            }
                            st.applyDatumVisibility();
                        }
                    }
                }
                fmRowPillEnd(originFldRect);
                if (originFldOpen) {
                    // Origin point leaf — selectable (selectedFeatureId = "point.origin");
                    // per-row eye keeps sticky control in sync with the folder cascade eye.
                    bool originSelected = (st.selectedFeatureId == "point.origin");
                    ImGuiTreeNodeFlags of = ImGuiTreeNodeFlags_Leaf |
                                            ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                            ImGuiTreeNodeFlags_SpanFullWidth |
                                            ImGuiTreeNodeFlags_FramePadding;
                    FmRowRect originRect = fmRowPill(originSelected, ui::palette.textDim);
                    if (!originShown)
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                    ImVec2 originRowPos = ImGui::GetCursorScreenPos();
                    ImGui::TreeNodeEx(
                        (fmFitRowLabel("Origin", fmEyeColumnW()) + "##origin_pt").c_str(), of);
                    treeIconInArrowSlot(originRowPos, "origin", "[O]",
                                        originShown ? 1.0f : 0.45f);
                    if (!originShown) ImGui::PopStyleColor();
                    fmRowPillEnd(originRect, originSelected);

                    ImVec2 oNodeMin = ImGui::GetItemRectMin();
                    ImVec2 oNodeMax = ImGui::GetItemRectMax();
                    float  oEyeSz = 14.0f * ui::uiScale, oEyePad = 6.0f * ui::uiScale;
                    ImVec2 oEyeMin(oNodeMax.x - oEyeSz - oEyePad,
                                   oNodeMin.y + (oNodeMax.y - oNodeMin.y - oEyeSz) * 0.5f);
                    ImVec2 oEyeMax(oEyeMin.x + oEyeSz, oEyeMin.y + oEyeSz);
                    bool   oOverEye = ImGui::IsMouseHoveringRect(oEyeMin, oEyeMax);
                    {
                        ImDrawList* odl = ImGui::GetWindowDrawList();
                        drawEyeGlyph(odl, oEyeMin, oEyeMax, originShown, oOverEye, fmSurf());
                        if (oOverEye) {
                            ImGui::SetTooltip(originShown ? "Hide Origin" : "Show Origin");
                            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                if (originShown) {
                                    st.hiddenFeatures.insert("point.origin");
                                    if (originSelected) st.selectedFeatureId.clear();
                                } else {
                                    st.hiddenFeatures.erase("point.origin");
                                }
                                st.originUserToggled = true;
                                st.applyDatumVisibility();
                            }
                        }
                    }
                    // Body-click (not on eye) → select the origin point; route into PM point well.
                    if (!oOverEye && ImGui::IsItemClicked()) {
                        st.selection.clear();
                        st.selectedFeatureId = "point.origin";
                        if (st.propertyManager && st.propertyManager->active() &&
                            st.propertyManager->activeWellWantsPoint())
                            st.propertyManager->routeDatumPoint("point.origin", "Origin");
                    }

                    // User ref-point / csys features (intersection points, etc.).
                    for (const auto& up : userPoints) {
                        const bool pSel = (st.selectedFeatureId == up.id);
                        const bool pHidden = st.hiddenFeatures.count(up.id) > 0;
                        ImGuiTreeNodeFlags uf = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth |
                                                ImGuiTreeNodeFlags_FramePadding;
                        FmRowRect urect = fmRowPill(pSel, ui::palette.textDim);
                        if (pHidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                        ImVec2 uRowPos = ImGui::GetCursorScreenPos();
                        const std::string upShown = fmFitRowLabel(up.name, fmEyeColumnW());
                        ImGui::TreeNodeEx((upShown + "##refpt_" + up.id).c_str(), uf);
                        if (upShown != up.name && ImGui::IsItemHovered())
                            ImGui::SetTooltip("%s", up.name.c_str());
                        const char* icon = (up.type == "csys" || up.type == "coordinate_system" ||
                                            up.type == "ref_csys") ? "ref_csys" : "ref_point";
                        const char* fb = (up.type == "csys" || up.type == "coordinate_system" ||
                                          up.type == "ref_csys") ? "[CS]" : "[Pt]";
                        treeIconInArrowSlot(uRowPos, icon, fb, pHidden ? 0.45f : 1.0f);
                        if (pHidden) ImGui::PopStyleColor();
                        fmRowPillEnd(urect, pSel);
                        ImVec2 uMin = ImGui::GetItemRectMin();
                        ImVec2 uMax = ImGui::GetItemRectMax();
                        float  uEyeSz = 14.0f * ui::uiScale, uEyePad = 6.0f * ui::uiScale;
                        ImVec2 uEyeMin(uMax.x - uEyeSz - uEyePad,
                                       uMin.y + (uMax.y - uMin.y - uEyeSz) * 0.5f);
                        ImVec2 uEyeMax(uEyeMin.x + uEyeSz, uEyeMin.y + uEyeSz);
                        bool   uOverEye = ImGui::IsMouseHoveringRect(uEyeMin, uEyeMax);
                        drawEyeGlyph(ImGui::GetWindowDrawList(), uEyeMin, uEyeMax,
                                     !pHidden, uOverEye, fmSurf());
                        if (uOverEye) {
                            ImGui::SetTooltip(pHidden ? "Show %s" : "Hide %s", up.name.c_str());
                            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                if (pHidden) st.hiddenFeatures.erase(up.id);
                                else {
                                    st.hiddenFeatures.insert(up.id);
                                    if (pSel) st.selectedFeatureId.clear();
                                }
                                st.refGeomUserToggled.insert(up.id);
                                st.applyDatumVisibility();
                            }
                        } else if (ImGui::IsItemClicked()) {
                            st.selection.clear();
                            st.selectedFeatureId = up.id;
                            selectFeatureInViewport(st, up.id);
                            if (st.propertyManager && st.propertyManager->active() &&
                                st.propertyManager->activeWellWantsPoint())
                                st.propertyManager->routeDatumPoint(up.id, up.name);
                        }
                    }
                    ImGui::TreePop();
                }
            }

            // ---- Planes folder (X/Y, X/Z, Y/Z) ----------------------------------------------
            // Mirrors the Axes folder: body_folder + seed-collapsed + cascade eye. Per-row eyes,
            // planeEmphasis selection, and sketch/PM pick routing are unchanged.
            {
                static const char* kPlanes[] = {"X/Z", "X/Y", "Y/Z"};
                static const int   kOrder[3] = {1, 0, 2};   // display slots -> plane index
                static const char* kPlaneId[] = {"plane.front", "plane.top", "plane.right"};
                auto takePlaneControl = [&st]() {
                    if (st.planesUserToggled) return;
                    for (int i = 0; i < 3; ++i) st.showPlane[i] = st.planeVisEff[i];
                    st.planesUserToggled = true;
                };
                // Eye state from the PERSISTENT plane visibility (showPlane / autoHiddenPlanes) —
                // NOT planeVisEff, which the viewport publishes AFTER the FM draws, so a fresh doc
                // showed a stale "hidden" glyph for one frame. Axes read persistent hiddenFeatures
                // the same way. Force-shows (sketch/PM) are transient and not reflected in the eye.
                auto planeShownPersist = [&st](int pi) -> bool {
                    return st.planesUserToggled ? st.showPlane[pi]
                                                : (st.autoHiddenPlanes.count(kPlaneId[pi]) == 0);
                };
                const bool allPlanesShown =
                    planeShownPersist(0) && planeShownPersist(1) && planeShownPersist(2);
                const bool anyPlaneShown =
                    planeShownPersist(0) || planeShownPersist(1) || planeShownPersist(2);

                FmRowRect planesFldRect = fmRowPill();
                treeIcon("body_folder", "[F]", 16.0f);
                ImGui::SameLine(0, 4 * ui::uiScale);
                if (!anyPlaneShown)
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                fmSeedCollapsed();
                const bool planesOpen = ImGui::TreeNodeEx(
                    (fmFitRowLabel("Planes", fmEyeColumnW()) + "##ref_planes_folder").c_str(),
                    ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
                if (!anyPlaneShown) ImGui::PopStyleColor();
                // Folder cascade eye: any/all shown → hide all; all hidden → show all.
                {
                    ImVec2 pFldMin = ImGui::GetItemRectMin();
                    ImVec2 pFldMax = ImGui::GetItemRectMax();
                    float  pEyeSz = 14.0f * ui::uiScale, pEyePad = 6.0f * ui::uiScale;
                    ImVec2 pEyeMin(pFldMax.x - pEyeSz - pEyePad,
                                   pFldMin.y + (pFldMax.y - pFldMin.y - pEyeSz) * 0.5f);
                    ImVec2 pEyeMax(pEyeMin.x + pEyeSz, pEyeMin.y + pEyeSz);
                    bool   pOverEye = ImGui::IsMouseHoveringRect(pEyeMin, pEyeMax);
                    drawEyeGlyph(ImGui::GetWindowDrawList(), pEyeMin, pEyeMax,
                                 allPlanesShown, pOverEye, fmSurf());
                    if (pOverEye) {
                        ImGui::SetTooltip(anyPlaneShown ? "Hide Planes" : "Show Planes");
                        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                            takePlaneControl();
                            const bool show = !anyPlaneShown;
                            for (int i = 0; i < 3; ++i) st.showPlane[i] = show;
                            if (!show) {
                                st.planeEmphasis = -1;
                                if (st.selectedFeatureId == "plane.front" ||
                                    st.selectedFeatureId == "plane.top" ||
                                    st.selectedFeatureId == "plane.right")
                                    st.selectedFeatureId.clear();
                            }
                        }
                    }
                }
                fmRowPillEnd(planesFldRect);
                if (planesOpen) {
                    for (int slot = 0; slot < 3; ++slot) {
                        int pi = kOrder[slot];
                        ImGuiTreeNodeFlags pf = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth |
                                                ImGuiTreeNodeFlags_FramePadding;
                        // T-G18: selection is shown by fmRowPill's glass card (like feature rows);
                        // the ImGui Selected/Header paint on top read heavier than a feature row.
                        bool hidden = !planeShownPersist(pi);
                        // Pill FIRST (at the row's true left edge — drawing it after the icon left
                        // a misaligned slab starting right of the indent), then the node, then the
                        // icon into the leaf arrow slot. Hidden planes keep a readable dim state.
                        // Z-up normals: Front=Y green, Top=Z blue, Right=X red (match axis washes).
                        const float* planeTint = (pi == 0) ? ui::palette.axisY
                                              : (pi == 1) ? ui::palette.axisZ
                                                          : ui::palette.axisX;
                        FmRowRect planeRect = fmRowPill(st.planeEmphasis == pi, planeTint);
                        if (hidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                        ImVec2 planeRowPos = ImGui::GetCursorScreenPos();
                        ImGui::TreeNodeEx((fmFitRowLabel(kPlanes[pi], fmEyeColumnW()) + "##plane" +
                                           std::to_string(pi)).c_str(), pf);
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
                                    takePlaneControl();
                                    st.showPlane[pi] = hidden;
                                    if (!st.showPlane[pi]) {
                                        if (st.planeEmphasis == pi) st.planeEmphasis = -1;
                                        if (st.selectedFeatureId == kPlaneId[pi])
                                            st.selectedFeatureId.clear();
                                    }
                                }
                            }
                        }
                        if (!pOverEye && ImGui::IsItemClicked()) {
                            if (st.sketchPlanePickActive && st.sketchMode) {
                                st.sketchPlanePickActive = false;
                                st.sketchMode->consumePlanePick(st, kPlaneId[pi]);
                            } else if (st.sketchMode && st.sketchMode->smartDimPicking()) {
                                st.sketchMode->addPlaneDimRef(st, pi);
                            } else {
                                // Row click = SELECT only (toggle). Visibility is owned solely by
                                // the eye toggle (Hunter): row click must not hide/show the plane.
                                bool wasSel = (st.selectedFeatureId == kPlaneId[pi]);
                                if (wasSel) {
                                    st.planeEmphasis = -1;
                                    st.selectedFeatureId.clear();
                                } else {
                                    st.planeEmphasis = pi;
                                    st.selection.clear();
                                    st.selectedFeatureId = kPlaneId[pi];
                                }
                            }
                        }
                        if (ImGui::IsItemHovered() && !pOverEye)
                            ImGui::SetTooltip(st.sketchPlanePickActive ? "Sketch on %s"
                                              : ((st.sketchMode && st.sketchMode->smartDimPicking())
                                                     ? "Dimension to %s"
                                                     : (hidden ? "Show %s" : "Hide %s")), kPlanes[pi]);
                    }
                    // User-created datum planes under Reference > Planes.
                    for (const auto& up : userPlanes) {
                        const bool pSel = (st.selectedFeatureId == up.id);
                        const bool pHidden = st.hiddenFeatures.count(up.id) > 0 ||
                                            st.autoHiddenPlanes.count(up.id) > 0;
                        ImGuiTreeNodeFlags uf = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth |
                                                ImGuiTreeNodeFlags_FramePadding;
                        FmRowRect urect = fmRowPill(pSel, featureKindTint(up.type, false));
                        if (pHidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                        ImVec2 uRowPos = ImGui::GetCursorScreenPos();
                        const std::string upShown = fmFitRowLabel(up.name, fmEyeColumnW());
                        ImGui::TreeNodeEx((upShown + "##refpl_" + up.id).c_str(), uf);
                        if (upShown != up.name && ImGui::IsItemHovered())
                            ImGui::SetTooltip("%s", up.name.c_str());
                        treeIconInArrowSlot(uRowPos, "ref_plane", "[P]", pHidden ? 0.45f : 1.0f);
                        if (pHidden) ImGui::PopStyleColor();
                        fmRowPillEnd(urect, pSel);
                        ImVec2 uMin = ImGui::GetItemRectMin();
                        ImVec2 uMax = ImGui::GetItemRectMax();
                        float  uEyeSz = 14.0f * ui::uiScale, uEyePad = 6.0f * ui::uiScale;
                        ImVec2 uEyeMin(uMax.x - uEyeSz - uEyePad,
                                       uMin.y + (uMax.y - uMin.y - uEyeSz) * 0.5f);
                        ImVec2 uEyeMax(uEyeMin.x + uEyeSz, uEyeMin.y + uEyeSz);
                        bool   uOverEye = ImGui::IsMouseHoveringRect(uEyeMin, uEyeMax);
                        drawEyeGlyph(ImGui::GetWindowDrawList(), uEyeMin, uEyeMax,
                                     !pHidden, uOverEye, fmSurf());
                        if (uOverEye) {
                            ImGui::SetTooltip(pHidden ? "Show %s" : "Hide %s", up.name.c_str());
                            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                if (pHidden) {
                                    st.hiddenFeatures.erase(up.id);
                                    st.autoHiddenPlanes.erase(up.id);
                                } else {
                                    st.hiddenFeatures.insert(up.id);
                                    st.autoHiddenPlanes.insert(up.id);
                                    if (pSel) st.selectedFeatureId.clear();
                                }
                                st.datumPlanesUserToggled.insert(up.id);
                                st.applyDatumVisibility();
                            }
                        } else if (ImGui::IsItemClicked()) {
                            if (st.sketchPlanePickActive && st.sketchMode) {
                                st.sketchPlanePickActive = false;
                                st.sketchMode->consumePlanePick(st, nlohmann::json(up.id));
                                st.selectedFeatureId = up.id;
                                selectFeatureInViewport(st, up.id);
                            } else if (st.propertyManager && st.propertyManager->active() &&
                                       st.propertyManager->activeWellWantsPlane()) {
                                st.propertyManager->routeDatumPlane(up.id, up.name);
                                st.selectedFeatureId = up.id;
                                selectFeatureInViewport(st, up.id);
                            } else {
                                if (pSel) {
                                    st.selectedFeatureId.clear();
                                    clearFeatureHighlight(st);
                                } else {
                                    st.selection.clear();
                                    st.selectedFeatureId = up.id;
                                    selectFeatureInViewport(st, up.id);
                                }
                            }
                        }
                    }
                    ImGui::TreePop();
                }
            }

            // ---- Axes folder (default X/Y/Z + auto/user axes) -------------------------------
            // Eyes write hiddenFeatures; body click selects / routes into an active axis well.
            // Axes no longer appear in Features. Names come from st.axisDisplayNames (rebuilt
            // on inject when membership changes) — no per-frame tree.features scan.
            {
                struct AxisRow { std::string id; std::string name; };
                std::vector<AxisRow> axes;
                axes.reserve(st.axisDatumIds.size() + 3);
                auto displayName = [&](const std::string& id, const char* fallback) -> std::string {
                    auto it = st.axisDisplayNames.find(id);
                    if (it != st.axisDisplayNames.end() && !it->second.empty()) return it->second;
                    return fallback ? std::string(fallback) : id;
                };
                axes.push_back({"axis.x", displayName("axis.x", "X Axis")});
                axes.push_back({"axis.y", displayName("axis.y", "Y Axis")});
                axes.push_back({"axis.z", displayName("axis.z", "Z Axis")});
                // Auto + user axes currently injected (skip the three defaults already listed).
                for (const auto& aid : st.axisDatumIds) {
                    if (aid == "axis.x" || aid == "axis.y" || aid == "axis.z") continue;
                    axes.push_back({aid, displayName(aid, aid.c_str())});
                }
                // Union axisAutoCache so listing works even if axisDatumIds is briefly stale.
                for (const auto& ent : st.axisAutoCache) {
                    if (ent.id.empty() || ent.id == "axis.x" || ent.id == "axis.y" ||
                        ent.id == "axis.z")
                        continue;
                    bool seen = false;
                    for (const auto& ar : axes)
                        if (ar.id == ent.id) { seen = true; break; }
                    if (!seen) {
                        const char* fb = ent.name.empty() ? ent.id.c_str() : ent.name.c_str();
                        axes.push_back({ent.id, displayName(ent.id, fb)});
                    }
                }
                // Union the renderer's own axis datums: whatever can DRAW must be listable,
                // even when the DocMirror-listener inject never ran for this load path
                // (cached-AICM opens, instant tab binds, pre-auto-axis files).
                if (st.renderer) {
                    for (const auto& rd : st.renderer->refDatums()) {
                        if (rd.kind != Renderer::RefDatum::Kind::Axis || rd.id.empty() ||
                            rd.id == "axis.x" || rd.id == "axis.y" || rd.id == "axis.z")
                            continue;
                        bool seen = false;
                        for (const auto& ar : axes)
                            if (ar.id == rd.id) { seen = true; break; }
                        if (!seen) {
                            const char* fb = rd.name.empty() ? rd.id.c_str() : rd.name.c_str();
                            axes.push_back({rd.id, displayName(rd.id, fb)});
                        }
                    }
                }
                // User axis features not yet in axisDatumIds (pre-inject / edge cases).
                for (const auto& ua : userAxes) {
                    bool seen = false;
                    for (const auto& ar : axes)
                        if (ar.id == ua.id) { seen = true; break; }
                    if (!seen) axes.push_back({ua.id, displayName(ua.id, ua.name.c_str())});
                }
                // Stable order: defaults first (already), then auto, then others by id.
                std::stable_sort(axes.begin() + 3, axes.end(),
                    [](const AxisRow& a, const AxisRow& b) { return a.id < b.id; });

                // Build the listed set once so the folder cascade eye and the child loop agree.
                std::unordered_set<std::string> userAxisIds;
                for (const auto& ua : userAxes) userAxisIds.insert(ua.id);
                std::unordered_set<std::string> autoCacheIds;
                for (const auto& ent : st.axisAutoCache)
                    if (!ent.id.empty()) autoCacheIds.insert(ent.id);
                std::unordered_set<std::string> rendererAxisIds;
                if (st.renderer)
                    for (const auto& rd : st.renderer->refDatums())
                        if (rd.kind == Renderer::RefDatum::Kind::Axis && !rd.id.empty())
                            rendererAxisIds.insert(rd.id);
                std::vector<AxisRow> listedAxes;
                listedAxes.reserve(axes.size());
                for (const auto& ar : axes) {
                    const bool isDefault =
                        ar.id == "axis.x" || ar.id == "axis.y" || ar.id == "axis.z";
                    const bool inDatum = st.axisDatumIds.count(ar.id) > 0;
                    const bool isUserAxis = userAxisIds.count(ar.id) > 0;
                    const bool inAutoCache = autoCacheIds.count(ar.id) > 0;
                    const bool inRenderer = rendererAxisIds.count(ar.id) > 0;
                    // Defaults always; injected auto/user axes; cache/renderer fallback; user features.
                    if (isDefault || isUserAxis || inDatum || inAutoCache || inRenderer)
                        listedAxes.push_back(ar);
                }
                bool anyAxisShown = false;
                bool allAxesShown = !listedAxes.empty();
                for (const auto& ar : listedAxes) {
                    const bool shown = st.hiddenFeatures.count(ar.id) == 0;
                    if (shown) anyAxisShown = true;
                    else       allAxesShown = false;
                }
                if (listedAxes.empty()) { anyAxisShown = false; allAxesShown = false; }

                // Axes folder header — same body_folder + fmRowPill pattern as Planes / Points.
                // Seed-collapsed on first doc display so folder-level cascade eye is primary.
                FmRowRect axesFldRect = fmRowPill();
                treeIcon("body_folder", "[F]", 16.0f);
                ImGui::SameLine(0, 4 * ui::uiScale);
                if (!anyAxisShown)
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                fmSeedCollapsed();
                const bool axesOpen = ImGui::TreeNodeEx(
                    (fmFitRowLabel("Axes", fmEyeColumnW()) + "##ref_axes_folder").c_str(),
                    ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
                if (!anyAxisShown) ImGui::PopStyleColor();
                {
                    ImVec2 aFldMin = ImGui::GetItemRectMin();
                    ImVec2 aFldMax = ImGui::GetItemRectMax();
                    float  aEyeSz = 14.0f * ui::uiScale, aEyePad = 6.0f * ui::uiScale;
                    ImVec2 aEyeMin(aFldMax.x - aEyeSz - aEyePad,
                                   aFldMin.y + (aFldMax.y - aFldMin.y - aEyeSz) * 0.5f);
                    ImVec2 aEyeMax(aEyeMin.x + aEyeSz, aEyeMin.y + aEyeSz);
                    bool   aOverEye = ImGui::IsMouseHoveringRect(aEyeMin, aEyeMax);
                    drawEyeGlyph(ImGui::GetWindowDrawList(), aEyeMin, aEyeMax,
                                 allAxesShown, aOverEye, fmSurf());
                    if (aOverEye) {
                        ImGui::SetTooltip(anyAxisShown ? "Hide Axes" : "Show Axes");
                        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                            const bool show = !anyAxisShown;
                            for (const auto& ar : listedAxes) {
                                if (show) {
                                    st.hiddenFeatures.erase(ar.id);
                                } else {
                                    st.hiddenFeatures.insert(ar.id);
                                    if (st.selectedFeatureId == ar.id)
                                        st.selectedFeatureId.clear();
                                }
                                st.axesUserToggledIds.insert(ar.id);
                            }
                            st.axesUserToggled = true;
                            st.applyDatumVisibility();
                        }
                    }
                }
                fmRowPillEnd(axesFldRect);
                if (axesOpen) {
                    for (const auto& ar : listedAxes) {
                        ImGuiTreeNodeFlags af = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth |
                                                ImGuiTreeNodeFlags_FramePadding;
                        const bool axisSel = (st.selectedFeatureId == ar.id);
                        const bool axisHidden = st.hiddenFeatures.count(ar.id) > 0;
                        // Layers-style full-row wash: default X/Y/Z get palette.axis* tints
                        // (F.Cu red / B.Cu blue parity). Auto/user axes keep the neutral accent.
                        const float* axisTint = nullptr;
                        if (ar.id == "axis.x")      axisTint = ui::palette.axisX;
                        else if (ar.id == "axis.y") axisTint = ui::palette.axisY;
                        else if (ar.id == "axis.z") axisTint = ui::palette.axisZ;
                        FmRowRect axisRect = fmRowPill(axisSel, axisTint);
                        if (axisHidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.85f));
                        ImVec2 axisRowPos = ImGui::GetCursorScreenPos();
                        // Full name already shows in this row's hover tooltip (below).
                        ImGui::TreeNodeEx(
                            (fmFitRowLabel(ar.name, fmEyeColumnW()) + "##refaxis_" + ar.id).c_str(), af);
                        treeIconInArrowSlot(axisRowPos, "ref_axis", "[A]",
                                            axisHidden ? 0.45f : 1.0f);
                        if (axisHidden) ImGui::PopStyleColor();
                        fmRowPillEnd(axisRect, axisSel);

                        ImVec2 aNodeMin = ImGui::GetItemRectMin();
                        ImVec2 aNodeMax = ImGui::GetItemRectMax();
                        float  aEyeSz = 14.0f * ui::uiScale, aEyePad = 6.0f * ui::uiScale;
                        ImVec2 aEyeMin(aNodeMax.x - aEyeSz - aEyePad,
                                       aNodeMin.y + (aNodeMax.y - aNodeMin.y - aEyeSz) * 0.5f);
                        ImVec2 aEyeMax(aEyeMin.x + aEyeSz, aEyeMin.y + aEyeSz);
                        bool   aOverEye = ImGui::IsMouseHoveringRect(aEyeMin, aEyeMax);
                        {
                            ImDrawList* adl = ImGui::GetWindowDrawList();
                            drawEyeGlyph(adl, aEyeMin, aEyeMax, !axisHidden, aOverEye, fmSurf());
                            if (aOverEye) {
                                ImGui::SetTooltip(axisHidden ? "Show %s" : "Hide %s", ar.name.c_str());
                                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                    if (axisHidden) st.hiddenFeatures.erase(ar.id);
                                    else {
                                        st.hiddenFeatures.insert(ar.id);
                                        if (axisSel) st.selectedFeatureId.clear();
                                    }
                                    st.axesUserToggled = true;
                                    st.axesUserToggledIds.insert(ar.id);
                                    st.applyDatumVisibility();
                                }
                            }
                        }
                        if (!aOverEye && ImGui::IsItemClicked()) {
                            st.selection.clear();
                            st.selectedFeatureId = ar.id;
                            if (st.propertyManager && st.propertyManager->active() &&
                                st.propertyManager->activeWellWantsAxis())
                                st.propertyManager->routeDatumAxis(st, ar.id, ar.name);
                        }
                        if (ImGui::IsItemHovered() && !aOverEye)
                            ImGui::SetTooltip("%s", ar.name.c_str());
                    }
                    ImGui::TreePop();
                }
            }

            ImGui::Unindent(kSecIndent);
        }   // end References section

        // ---- Features section — its own collapse; the header hosts Add Folder. ----
        {
            const bool isPartDoc = tree.is_object() && jfld(tree, "schema") != "aicad.asm/1";
            // Add Folder no longer requires a selection: with features selected it groups them; with
            // nothing selected it drops a blank folder at the bottom to drag features into (Hunter).
            const bool addEn = canDocTree && isPartDoc;
            bool addClicked = false;
            fmFeaturesHeader(&s_featSecOpen, addEn, &addClicked, agentLockReason.c_str());
            if (addClicked) createFolderFromSelection(st);
        }
        if (s_featSecOpen) {
            ImGui::Indent(kSecIndent);
            // Isolate ImGui tree-node open-state per document so tabs don't share expansions
            // and a reopened doc re-seeds collapsed cleanly.
            ImGui::PushID(secDocKey.c_str());

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
                        // W2-ref: default/auto/user axes listed under References (axisDatumIds).
                        bool isRefAxis = st.axisDatumIds.count(selectedFeatureId) > 0 ||
                                         selectedFeatureId == "axis.x" ||
                                         selectedFeatureId == "axis.y" ||
                                         selectedFeatureId == "axis.z" ||
                                         selectedFeatureId.rfind("axis.auto:", 0) == 0;
                        if (!selectedFeatureId.empty() && !live.count(selectedFeatureId) &&
                            !isOriginPlane && !isOriginPoint && !isRefAxis) {
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
                    // feature nests under that consumer; unconsumed / multiply-consumed sketches
                    // stay flat. core::consumedSketches is the one definition of consumption,
                    // shared with the viewport's ghost sketches so tree and view agree.
                    std::unordered_map<std::string, int> consumeCount;   // sketch fid -> #consumers
                    std::unordered_map<std::string, std::string> consumer;  // sketch fid -> consumer
                    std::unordered_map<std::string, std::vector<std::string>> childrenOf;  // consumer -> sketches
                    {
                        const std::unordered_set<std::string> sketchIds = core::sketchIdsOf(*feat);
                        for (const auto& f : *feat) {
                            if (!f.is_object()) continue;
                            const std::string fid = jfld(f, "id");
                            for (const auto& sk : core::consumedSketches(f, sketchIds)) {
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
                    // User datums also nest under every feature that references them (AND stay
                    // under Reference). Unlike sketches, multi-consumer is fine — list under each.
                    if (!userDatumIds.empty()) {
                        for (const auto& f : *feat) {
                            if (!f.is_object()) continue;
                            const std::string fid = jfld(f, "id");
                            const std::string fty = jfld(f, "type");
                            if (fid.empty() || isUserRefGeomType(fty) || fty == "sketch") continue;
                            std::unordered_set<std::string> drefs;
                            collectUserDatumRefsFromFeature(f, userDatumIds, drefs);
                            for (const auto& did : drefs) {
                                auto& kids = childrenOf[fid];
                                if (std::find(kids.begin(), kids.end(), did) == kids.end())
                                    kids.push_back(did);
                            }
                        }
                    }
                    // A sketch is nested (hidden from the flat list, shown under its consumer) iff
                    // it landed in childrenOf (one consumer, and that consumer actually builds).
                    // User datums always skip the flat Features list via isUserRefGeomType.
                    std::unordered_set<std::string> nestedSketchIds;
                    {
                        std::unordered_set<std::string> sketchIdsOnly;
                        for (const auto& f : *feat)
                            if (f.is_object() && jfld(f, "type") == "sketch")
                                sketchIdsOnly.insert(jfld(f, "id"));
                        for (const auto& kv : childrenOf)
                            for (const auto& childId : kv.second)
                                if (sketchIdsOnly.count(childId))
                                    nestedSketchIds.insert(childId);
                    }
                    auto isNestedSketch = [&](const std::string& fid) {
                        return nestedSketchIds.count(fid) != 0;
                    };

                    // Item 9: import features act as expandable component nodes. Map each
                    // import id -> feature json, and collect Place_*/transform rows that
                    // reference an import body so they nest under the component (not as a
                    // second duplicated sibling).
                    std::unordered_map<std::string, const nlohmann::json*> importById;
                    std::unordered_map<std::string, std::vector<const nlohmann::json*>> placeChildrenOf;
                    std::unordered_set<std::string> nestedPlacementIds;
                    for (const auto& f : *feat) {
                        if (!f.is_object()) continue;
                        if (jfld(f, "type") == "import") {
                            const std::string id = jfld(f, "id");
                            if (!id.empty()) importById[id] = &f;
                        }
                    }
                    for (const auto& f : *feat) {
                        if (!f.is_object()) continue;
                        if (!isImportPlacementTransform(f, importById)) continue;
                        const std::string body = featureParamString(f, "body");
                        const std::string pid = jfld(f, "id");
                        if (body.empty() || pid.empty()) continue;
                        placeChildrenOf[body].push_back(&f);
                        nestedPlacementIds.insert(pid);
                    }
                    auto importHasExpandableContent = [&](const nlohmann::json& f) -> bool {
                        const std::string id = jfld(f, "id");
                        if (placeChildrenOf.count(id)) return true;
                        const std::string abs = importFeatureSourceAbs(f);
                        if (abs.empty()) return false;
                        const ui::NestedDocument& nd = ui::featureTreeNestedDocument(abs);
                        if (nd.state == ui::NestedDocumentState::Ready &&
                            (!nd.features.empty() || !nd.components.empty()))
                            return true;
                        // Still expand so Material / File / source status is reachable.
                        return !abs.empty();
                    };
                    // Wave 6: group import features that share the same resolved source_path
                    // under one collapsible folder "<PartName> xN" (count >= 2 only). Key =
                    // weakly_canonical(import.params.source_path); empty path stays singleton.
                    // Followers of a multi-instance group are skipped in the flat walk and
                    // rendered under the leader's folder so the shared name is not repeated.
                    struct ImportSrcGroup {
                        std::string key;
                        std::string absPath;
                        std::string sharedName;
                        std::vector<const nlohmann::json*> members;  // first-seen order
                    };
                    std::vector<ImportSrcGroup> importSrcGroups;
                    std::unordered_map<std::string, size_t> importSrcGroupIndex;  // key -> groups idx
                    std::unordered_set<std::string> importGroupFollowers;  // non-leader import fids
                    std::unordered_map<std::string, std::string> importInstanceLabel;  // fid -> label
                    std::unordered_map<std::string, size_t> importGroupLeaderIdx;  // leader fid -> groups idx
                    for (const auto& f : *feat) {
                        if (!f.is_object() || jfld(f, "type") != "import") continue;
                        const std::string id = jfld(f, "id");
                        if (id.empty()) continue;
                        const std::string abs = importFeatureSourceAbs(f);
                        const std::string key = sourcePathGroupKey(abs);
                        if (key.empty()) continue;  // unresolved → never group
                        auto it = importSrcGroupIndex.find(key);
                        if (it == importSrcGroupIndex.end()) {
                            importSrcGroupIndex[key] = importSrcGroups.size();
                            ImportSrcGroup g;
                            g.key = key;
                            g.absPath = abs;
                            g.sharedName = importSharedDisplayName(f);
                            g.members.push_back(&f);
                            importSrcGroups.push_back(std::move(g));
                        } else {
                            importSrcGroups[it->second].members.push_back(&f);
                        }
                    }
                    for (size_t gi = 0; gi < importSrcGroups.size(); ++gi) {
                        ImportSrcGroup& g = importSrcGroups[gi];
                        if (g.members.size() < 2) continue;  // singleton stays a normal row
                        for (size_t mi = 0; mi < g.members.size(); ++mi) {
                            const nlohmann::json* mf = g.members[mi];
                            if (!mf) continue;
                            const std::string mid = jfld(*mf, "id");
                            if (mid.empty()) continue;
                            // Instance label: shared name + " k" (1-based, no brackets) — folder owns the base name.
                            // Selection/lookup keys on feature id, never this display label.
                            importInstanceLabel[mid] =
                                g.sharedName + " " + std::to_string(mi + 1);
                            if (mi == 0)
                                importGroupLeaderIdx[mid] = gi;
                            else
                                importGroupFollowers.insert(mid);
                        }
                    }



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

                    // Viewport -> tree reveal: while a pick's scroll is pending, force open the user
                    // folder / import-instance group / import row (Place_* transforms nest under it)
                    // holding the selected feature so its row draws, lights and scrolls into view.
                    std::string revealFolder, revealParent;
                    if (st.treeScrollToSelection && !selectedFeatureId.empty()) {
                        for (const auto& f : *feat)
                            if (f.is_object() && jfld(f, "id") == selectedFeatureId) {
                                revealFolder = jfld(f, "folder");
                                break;
                            }
                        if (nestedPlacementIds.count(selectedFeatureId))
                            for (const auto& kv : placeChildrenOf)
                                for (const nlohmann::json* pf : kv.second)
                                    if (pf && jfld(*pf, "id") == selectedFeatureId)
                                        revealParent = kv.first;
                    }

                    // ---- render one feature row (shared by flat + nested-sketch paths) ----
                    int rowIdx = 0;   // visual stripe index (stable across nesting)
                    // Returns whether the row is expanded (leaf/child rows always true) so the caller
                    // renders consumed-sketch children only when the parent's arrow is open.
                    auto drawOneRow = [&](const nlohmann::json& f, bool nested) -> bool {
                        std::string fid    = jfld(f, "id");
                        std::string fname  = jfld(f, "name");
                        std::string ftype  = jfld(f, "type");
                        // W2-ref: user datums live under References (and nest under consumers).
                        // Skip the flat Features history listing for plane/axis/point/csys.
                        // Nested=true rows (under a consumer) still draw via this path.
                        if (!nested && isUserRefGeomType(ftype)) return true;
                        // Item 9: show the component/part name, not Place_* or a raw instance id.
                        // Multi-source groups use "Name k" so the folder owns the base name.
                        if (ftype == "import") {
                            auto il = importInstanceLabel.find(fid);
                            fname = (il != importInstanceLabel.end()) ? il->second
                                                                      : importDisplayName(f);
                        }
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
                        // Frosted glass base + stripe only; selection paints last (below).
                        ui::glassRowBackground(rowDl, rowMin, rowMax, 3.5f, rowIdx % 2 == 0, false,
                                               sel ? 0.42f : 0.26f);
                        // Color-coded row wash dyed the feature's CATEGORY color (sketch=blue,
                        // solid-add=teal, cut=orange, dress-up=purple, pattern=cyan, datum=slate,
                        // ...) — the same Layers-style tinted background used across the app.
                        // Replaces the old 3px left color bar.
                        {
                            const bool catIsCut = featureModeIsCut(&f);
                            const bool catIsErr = !fs.ok && (ftype == "sketch" || ftype == "sketch3d");
                            float catTint[3]; categoryColorRGB(ftype, catIsCut, catTint, catIsErr);
                            // Same outer rect as the base — coloredRowWash self-insets.
                            ui::coloredRowWash(rowDl, rowMin, rowMax, 3.5f, catTint, 0.28f, false);
                        }
                        if (suppressed) {
                            // Inset + rounded to match the pill (square full-rect fill poked
                            // bright corners past the rounded outline).
                            const float sIn = 2.0f * S;
                            rowDl->AddRectFilled(
                                ImVec2(rowMin.x + sIn, rowMin.y + sIn),
                                ImVec2(rowMax.x - sIn, rowMax.y - sIn),
                                IM_COL32((int)(pal.statusWarn[0] * 255),
                                         (int)(pal.statusWarn[1] * 255),
                                         (int)(pal.statusWarn[2] * 255), 8),
                                3.5f * S);
                        }
                        if (sel) fmRowSelectedPaint(rowDl, rowMin, rowMax);
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
                        // Imported components render as part nodes (not the generic "open" glyph).
                        ctx.iconName = (ftype == "import") ? "part" : fmFeatureIcon(ftype, &f);
                        ctx.iconFallback = (ftype == "import") ? "[P]" : featureFallback(ftype);
                        ctx.suppressed = suppressed; ctx.selected = sel;
                        ctx.rolledBack = rolledBack; ctx.fs = fs;
                        ctx.ownerBodyCount = bodyCount(fid);
                        ctx.ownedBodies = ownedBodies(fid);
                        ctx.nested = nested;
                        // Expandable: consumed-sketch children OR import component history/placements.
                        const bool importExpand = (ftype == "import") && !nested &&
                                                 importHasExpandableContent(f);
                        ctx.expandable = (!nested && childrenOf.count(fid) > 0) || importExpand;
                        ctx.forceOpen = !revealParent.empty() && fid == revealParent;

                        ctx.canDoc = canDocTree;
                        ctx.barAtEnd = barAtEnd;
                        ctx.firstOwnedBody = firstBodyKey(fid);
                        ctx.convertLabel = convertLabel; ctx.convertCb = convertCb;
                        ctx.reorderCb = reorderCb;
                        ctx.editCallback = &editCallback;

                        RowResult rr = featureRow(ctx, st);
                        // Viewport -> tree reveal: centre the row just submitted (SetScrollHereY
                        // targets the previous line, so it must follow the row, not precede it).
                        if (sel && selectedFeatureId == fid && st.treeScrollToSelection) {
                            ImGui::SetScrollHereY(0.5f);
                            st.treeScrollToSelection = false;
                        }
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
                        if (rr.clicked && ftype == "plane" && st.sketchPlanePickActive &&
                            st.activeDocLockedByAgent()) {
                            st.toast(ui::ToastLevel::Warn, agentLockReason);
                        } else if (rr.clicked && canDocTree && ftype == "plane" &&
                                   st.sketchPlanePickActive && st.sketchMode) {
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
                            if (st.activeDocLockedByAgent() && ftype != "import") {
                                st.toast(ui::ToastLevel::Warn, agentLockReason);
                            } else if (ftype == "sketch") {
                                if (st.sketchMode) st.sketchMode->enterForFeature(st, fid);
                            } else if (ftype == "sketch3d") {
                                if (st.sketch3dMode) st.sketch3dMode->enterForFeature(st, fid);
                            } else if (ftype == "import") {
                                // Double-click an imported component opens its source part
                                // (same action as RMB Open Part).
                                const std::string src = importFeatureSourceAbs(f);
                                if (!src.empty()) openComponentPart(st, src);
                                else if (editCallback &&
                                         !refuseAgentLockedTreeDoc(st, st.docMirror.docId()))
                                    editCallback(fid);
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
                            if (armRename && st.activeDocLockedByAgent()) {
                                st.toast(ui::ToastLevel::Warn, agentLockReason);
                            } else if (armRename && canDocTree) {
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

                        // Wave4: context-cube armed → also push this row into pendingContextRefs.
                        if (st.contextPickActive &&
                            (rr.clicked || rr.ctrlClicked) &&
                            ftype != "plane") {
                            pushFeatureContextRef(st, fid, fname, ftype);
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

                    // Expand one import row: nested source history + absorbed Place_* transforms.
                    auto expandImportRow = [&](const nlohmann::json& imp, bool rowIsOpen) {
                        if (!rowIsOpen) return;
                        const std::string impId = jfld(imp, "id");
                        const std::string absPath = importFeatureSourceAbs(imp);
                        if (!absPath.empty()) {
                            const ui::NestedDocument& nestedDoc =
                                ui::featureTreeNestedDocument(absPath);
                            std::unordered_set<std::string> pathStack;
                            pathStack.insert(absPath);
                            // Reuse the assembly nested renderer: material/file info +
                            // feature history when the source is a native .step+ with
                            // document.json. Dumb STEP/mesh imports show the info row +
                            // "no editable feature history" (NestedDocumentState::External).
                            drawNestedDocumentContents(st, "import_" + impId, impId, absPath,
                                                       nestedDoc, pathStack, 0);
                        } else {
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1],
                                       pal.textDim[2], 0.72f));
                            ImGui::TextUnformatted("  (no source path on import)");
                            ImGui::PopStyleColor();
                        }
                        auto places = placeChildrenOf.find(impId);
                        if (places != placeChildrenOf.end()) {
                            for (const nlohmann::json* pf : places->second) {
                                if (pf) drawOneRow(*pf, /*nested=*/true);
                            }
                        }
                    };

                    for (int i = 0; i < (int)feat->size(); ++i) {
                        const auto& f = (*feat)[i];
                        if (!f.is_object()) continue;
                        std::string fid    = jfld(f, "id");
                        std::string ftype  = jfld(f, "type");
                        std::string folder = jfld(f, "folder");

                        // Skip nested sketches in the flat pass — they render under their consumer.
                        if (ftype == "sketch" && isNestedSketch(fid)) continue;
                        // User datums list under Reference (and nest under consumers when referenced).
                        if (isUserRefGeomType(ftype)) continue;
                        // Item 9: Place_*/import-placement transforms nest under their import body.
                        if (nestedPlacementIds.count(fid)) continue;
                        // Wave 6: non-leader members of a multi-import source group render under
                        // the leader's folder (not as siblings that re-state the same part name).
                        if (importGroupFollowers.count(fid)) continue;

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
                                fmSeedCollapsed();
                                if (!revealFolder.empty() && folder == revealFolder)
                                    ImGui::SetNextItemOpen(true);
                                folderOpen = ImGui::TreeNodeEx(
                                    (folder + "##fld_" + folder).c_str(),
                                    ImGuiTreeNodeFlags_SpanFullWidth |
                                    ImGuiTreeNodeFlags_FramePadding |
                                    ImGuiTreeNodeFlags_OpenOnArrow);
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


                        // Wave 6 multi-import source group: one folder "<Name> xN" wrapping every
                        // instance that shares the same resolved source_path. Open Part on the
                        // folder targets the shared source; each instance keeps its own row/menu.
                        auto leaderIt = importGroupLeaderIdx.find(fid);
                        if (leaderIt != importGroupLeaderIdx.end() &&
                            leaderIt->second < importSrcGroups.size()) {
                            const ImportSrcGroup& g = importSrcGroups[leaderIt->second];
                            const std::string folderLabel =
                                g.sharedName + " x" + std::to_string(g.members.size());
                            const std::string folderId = "impgrp_" + g.key;

                            FmRowRect grpRect = fmRowPill();
                            treeIcon("body_folder", "[F]", 16.0f);
                            ImGui::SameLine(0, 4 * ui::uiScale);
                            fmSeedCollapsed();
                            if (st.treeScrollToSelection &&
                                std::any_of(g.members.begin(), g.members.end(),
                                    [&](const nlohmann::json* mf) {
                                        if (!mf) return false;
                                        const std::string mid = jfld(*mf, "id");
                                        return mid == selectedFeatureId ||
                                               (!revealParent.empty() && mid == revealParent);
                                    }))
                                ImGui::SetNextItemOpen(true);
                            const bool groupOpen = ImGui::TreeNodeEx(
                                (folderLabel + "##" + folderId).c_str(),
                                ImGuiTreeNodeFlags_SpanFullWidth |
                                ImGuiTreeNodeFlags_FramePadding |
                                ImGuiTreeNodeFlags_OpenOnArrow);
                            // Folder context: Open Part once for the shared source file.
                            ui::pushContextMenuStyle();
                            if (ImGui::BeginPopupContextItem(("##igctx_" + folderId).c_str())) {
                                const bool canOpen = !g.absPath.empty();
                                if (!canOpen) ImGui::BeginDisabled();
                                if (ImGui::MenuItem("Open Part"))
                                    openComponentPart(st, g.absPath);
                                if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                                    revealInFileManager(g.absPath);
                                if (!canOpen) ImGui::EndDisabled();
                                ImGui::EndPopup();
                            }
                            ui::popContextMenuStyle();
                            if (ImGui::IsItemHovered() && !g.absPath.empty())
                                ImGui::SetTooltip("%s", g.absPath.c_str());
                            // Double-click folder → open the shared part (same as Open Part).
                            if (ImGui::IsItemHovered() &&
                                ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
                                !g.absPath.empty())
                                openComponentPart(st, g.absPath);
                            fmRowPillEnd(grpRect);

                            if (groupOpen) {
                                const float nestIndent = 14.0f * ui::uiScale;
                                ImGui::Indent(nestIndent);
                                for (const nlohmann::json* mf : g.members) {
                                    if (!mf) continue;
                                    const std::string mfid = jfld(*mf, "id");
                                    const bool rowOpen = drawOneRow(*mf, /*nested=*/false);
                                    // Nested-sketch children under this instance (rare on imports).
                                    auto kids = childrenOf.find(mfid);
                                    if (rowOpen && kids != childrenOf.end()) {
                                        for (const auto& skId : kids->second) {
                                            for (const auto& sf : *feat)
                                                if (sf.is_object() && jfld(sf, "id") == skId) {
                                                    drawOneRow(sf, /*nested=*/true);
                                                    break;
                                                }
                                        }
                                    }
                                    expandImportRow(*mf, rowOpen);
                                    if (!barFid.empty() && mfid == barFid)
                                        drawRollbackBarLine(st, canDocTree, barFid);
                                }
                                ImGui::Unindent(nestIndent);
                                ImGui::TreePop();
                            }
                            continue;
                        }

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

                        // Item 9: expand imported component → source history + placement transforms.
                        if (ftype == "import")
                            expandImportRow(f, rowOpen);

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

            ImGui::PopID();
            ImGui::Unindent(kSecIndent);
        }   // end Features section
        // Always clear after the Features pass (even if header closed) so a one-shot seed
        // cannot leak into subsequent frames.
        s_fmCollapseSeed = false;
        // Same for a viewport reveal: its forced-open folders get one frame. A target row that never
        // drew (hidden by the name filter) must not pin its folders open or scroll a later frame.
        if (featuresReveal) st.treeScrollToSelection = false;
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
        if (appearanceColorPicker("##appearance_feat", s_apprColor,
                                  &s_apprMetallic, &s_apprRoughness,
                                  agentLockReason.empty() ? nullptr : agentLockReason.c_str())) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_appearance"}, {"id", s_apprBody},
                 {"color", {s_apprColor[0], s_apprColor[1], s_apprColor[2]}},
                 {"metallic", s_apprMetallic}, {"roughness", s_apprRoughness}}}),
                "set_appearance");
            s_apprBody.clear();
        }
    }

    // C25: apply ops queued during drawing — safe now, nothing reads the tree json below.
    flushPendingTreeOps(st);
    // Wave 4C: run any copy/cut/paste requested from a row context menu (deferred to here because
    // the copy's DocMirror call replaces the tree json the menu was drawn over).
    flushPendingClipboard(st);
    // Nested component Assign Material: open/apply/save/close the component part by path (or apply
    // on the live mirror when Edit Model / Open Part has that part active). Deferred like tree ops.
    flushPendingNestedMaterial(st);

    ImGui::PopStyleVar();  // compact ItemSpacing (pushed at top of body)
}

// ---- Wave 3: public Save/Open wrappers (the File menu + File Explorer reuse these) ----
// The actual RPC/dialog/mesh-refresh logic lives in the file-local doSaveDoc / doOpenDoc /
// applyOpenedDoc above; these thin wrappers expose it without duplicating any of it, and keep
// s_docPath (the active doc's on-disk path) as the single source of truth.

// Open a file by absolute path (no dialog). Used by the File Explorer double-click and the
// Recent-files menu. Returns true when the open was started (or an existing tab was focused);
// toasts + logs on failure (never throws). Async doc_open on all platforms — matches doOpenDoc.
bool featureTreeOpenDrawingPath(AppState& st, const std::string& path) {
    if (path.empty()) return false;
    namespace fs = std::filesystem;
    std::error_code wantedEc;
    fs::path wanted = fs::weakly_canonical(fs::path(path), wantedEc);
    if (wantedEc) wanted = fs::path(path).lexically_normal();
    for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i) {
        const AppState::DocTab& tab = st.docTabs[i];
        if (tab.kind != AppState::DocTab::TabKind::Drawing || tab.path.empty())
            continue;
        std::error_code haveEc;
        fs::path have = fs::weakly_canonical(fs::path(tab.path), haveEc);
        if (haveEc) have = fs::path(tab.path).lexically_normal();
        if (have == wanted) {
            st.switchToTab(i);
            return true;
        }
    }
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Error, "Open failed: worker not started");
        return false;
    }
    st.toast(ui::ToastLevel::Info, "Opening " + pathBaseName(path) + "\xe2\x80\xa6");
    ++st.rebuildsInFlight;
    st.rpc.call("drawing_open_standalone", {{"path", path}},
                [&st, path](const core::RpcResult& r) {
                    --st.rebuildsInFlight;
                    if (!r.ok || (r.result.is_object() && r.result.contains("code"))) {
                        const std::string msg = !r.ok ? r.err
                            : r.result.value("message", std::string("engine error"));
                        st.addLog("[err] drawing open: " + msg);
                        st.toast(ui::ToastLevel::Error,
                                 "Open failed: " + pathBaseName(path) + " — " + msg);
                        return;
                    }
                    const std::string drawingId =
                        r.result.value("drawing_id",
                                       r.result.value("doc_id", std::string()));
                    const std::string name =
                        r.result.value("name", std::string("Drawing"));
                    const std::string sourceId =
                        r.result.contains("source_doc_id") &&
                                r.result["source_doc_id"].is_string()
                            ? r.result["source_doc_id"].get<std::string>()
                            : std::string();
                    if (drawingId.empty()) {
                        st.toast(ui::ToastLevel::Error,
                                 "Open failed: engine returned no drawing id");
                        return;
                    }
                    st.adoptStandaloneDrawingTab(drawingId, name, sourceId, path);
                    const std::string state =
                        r.result.value("source_state", std::string("missing"));
                    if (state == "missing")
                        st.toast(ui::ToastLevel::Warn,
                                 "Source missing — views frozen from last projection");
                    else
                        st.toast(ui::ToastLevel::Info,
                                 "Opened " + pathBaseName(path));
                }, 120000);
    return true;
}

bool featureTreeOpenPath(AppState& st, const std::string& path, bool asSolid) {
    if (path.empty()) return false;
    std::string requestedExt = std::filesystem::path(path).extension().string();
    for (char& ch : requestedExt) ch = (char)std::tolower((unsigned char)ch);
    if (requestedExt == ".draw+")
        return featureTreeOpenDrawingPath(st, path);

    // Dedup: if a non-handoff tab already has this path, switch to it (mirrors openComponentPart).
    // Path compare is lexical (web has no real std::filesystem canonical for server abs paths).
    {
        namespace fs = std::filesystem;
        std::error_code ec;
        fs::path want = fs::weakly_canonical(fs::u8path(path), ec);
        if (ec) want = fs::u8path(path).lexically_normal();
        const std::string wantStr = want.generic_u8string();
        for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i) {
            const AppState::DocTab& tab = st.docTabs[i];
            if (tab.aiHandoff || tab.path.empty()) continue;
            std::error_code tabEc;
            fs::path have = fs::weakly_canonical(fs::u8path(tab.path), tabEc);
            if (tabEc) have = fs::u8path(tab.path).lexically_normal();
            const std::string haveStr = have.generic_u8string();
            if (haveStr == wantStr || tab.path == path) {
                st.switchToTab(i);
                return true;
            }
        }
    }

    if (!st.workerStarted) {
        if (!st.handoffQuietOpen)
            st.toast(ui::ToastLevel::Error, "Open failed: worker not started");
        return false;
    }

    // Handoff quiet path needs the new tab immediately after return (pollAiCadHandoff
    // tags/dedupes the just-created tab). Keep the historical sync DocMirror::open there;
    // all user-facing opens go through async rpc so STEP import never freezes the GL thread.
    if (st.handoffQuietOpen) {
        std::string err;
        if (st.docMirror.open(st.worker, path, err, asSolid)) {
            applyOpenedDoc(st, path);
            return true;
        }
        st.addLog("[err] open: " + err);
        return false;
    }

    startAsyncOpen(st, path, asSolid);
    return true;  // open started (completion via applyOpenedDoc)
}

// Live MCP handoff poll — see the header. The external MCP server (the engine Claude drives)
// publishes the part it's working on to %APPDATA%\Chudware\handoff\ as a NATIVE .step+ doc plus a
// tiny live.json {rev, step}. We open/refresh a single tab from it so the user watches the AI
// build in the viewport. Loading the native .step+ (not bare STEP) brings the whole feature tree.
// One published part the MCP currently has open, parsed from the handoff control file.
struct HandoffDoc { std::string docId, step, name; long long rev = -1; };

void pollAiCadHandoff(AppState& st) {
    if (!st.workerStarted) return;   // worker not up — nothing can open yet
    if (st.agentV2Running) return;   // an in-app agent run owns the doc; don't fight it
    namespace fs = std::filesystem;
    const std::string dir = appdataAppDir() + "/handoff/";
    static const std::string ctrlNew = dir + "docs.json";   // multi-doc (one tab per open part)
    static const std::string ctrlOld = dir + "live.json";   // legacy single-doc fallback

    // Per-doc_id rev already drawn on screen. "" keys the legacy single-mirror (no doc_id).
    static std::map<std::string, long long> shownRev;
    static std::map<std::string, HandoffDoc> pending;   // doc_id -> latest published, awaiting a safe moment
    static std::map<std::string, int> openFails;        // doc_id -> consecutive failed open attempts
    constexpr int kMaxOpenRetries = 30;                 // ~0.5s of frames before giving a part up
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
                    hd.name  = e.value("name", std::string());
                    hd.rev   = e.value("rev", (long long)-1);
                    if (!hd.step.empty()) published.push_back(hd);
                }
            } else {
                // Legacy shape {rev, step, doc_id[, name]} — a single mirror keyed by "".
                HandoffDoc hd;
                hd.docId = std::string();  // stable legacy key
                hd.step  = j.value("step", std::string());
                hd.name  = j.value("name", std::string());
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
                openFails.erase(st.docTabs[i].handoffDocId);
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
                st.docTabs[newIdx].handoffName = hd.name;
                // Prefer advertised part name over path stem / opaque docId on the tab strip.
                if (!hd.name.empty()) st.docTabs[newIdx].title = hd.name;
                openFails.erase(docId);
                // DEDUPE (fixes the doubled-view bug): close any OTHER mirror tab that shows the
                // same part — either the same doc_id (a racing/stale open) OR the same non-empty
                // NAME under a different doc_id (the agent re-created the part from scratch, so its
                // old incarnation lingers as a second tab). Only aiHandoff tabs are touched, never a
                // tab the user opened themselves.
                // NB: never erase from `pending` here — we are iterating it in the outer range-for;
                // shownRev is a separate map so erasing it is safe. A stale tab's own pending entry
                // (if any) is reconciled by the removal pass next frame.
                for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
                    if (i == newIdx || !st.docTabs[i].aiHandoff) continue;
                    const bool sameId = st.docTabs[i].handoffDocId == docId;
                    const bool sameName = !hd.name.empty() && st.docTabs[i].handoffName == hd.name;
                    if (sameId || sameName) {
                        shownRev.erase(st.docTabs[i].handoffDocId);
                        st.closeDocTab(i);
                    }
                }
                if (!takeFocus) {
                    // Background open: return focus to the tab the user was on (re-find it by doc id,
                    // since closeDocTab above may have shifted indices) and pulse the new tab.
                    int back = -1;
                    if (!prevActiveId.empty())
                        for (int i = 0; i < (int)st.docTabs.size(); ++i)
                            if (st.docTabs[i].docId == prevActiveId) { back = i; break; }
                    if (back >= 0) st.activeTab = back;
                    for (int i = 0; i < (int)st.docTabs.size(); ++i)
                        if (st.docTabs[i].handoffDocId == docId) {
                            st.docTabs[i].aiPulse = true;
                            st.docTabs[i].lastAgentEditAt = std::chrono::steady_clock::now();
                            break;
                        }
                }
                shownRev[docId] = hd.rev;
                if (takeFocus)
                    st.toast(ui::ToastLevel::Info, "Chudware: live view of the AI's part — it updates as it builds");
                done.push_back(docId);
            } else if (!opened) {
                // Open failed. This is often TRANSIENT — the agent may have swapped the .step+ a
                // frame ago and the mesh side-file isn't visible yet — so RETRY for a short window
                // instead of dropping the part forever (the "a whole part silently never appears"
                // bug: the old code consumed the rev after ONE failure, and when the agent is done
                // there is no newer rev to trigger a re-attempt). Give up only after kMaxOpenRetries
                // so a genuinely-unopenable part (e.g. an assembly the core declines) can't spin.
                if (++openFails[docId] >= kMaxOpenRetries) {
                    shownRev[docId] = hd.rev;
                    done.push_back(docId);
                    openFails.erase(docId);
                }
                // else: leave it pending (no shownRev, not done) so the next frame retries.
            }
            continue;
        }

        // Protect the human's unsaved edits: the refresh below is a destructive close+reopen of the
        // AI's newer snapshot, which would silently discard any local edit the user made to this
        // mirror (the finished-but-unsaved case that `userMidEdit` — open sketch/PM form — misses).
        // If the tab is dirty, defer and pulse so the user's work is never lost; the AI's update
        // applies once they save or discard (clearing `dirty`). True merge/co-edit is future work —
        // this just guarantees no data loss when both sides touch the same part.
        if (st.docTabs[idx].dirty) {
            st.docTabs[idx].aiPulse = true;
            st.docTabs[idx].lastAgentEditAt = std::chrono::steady_clock::now();
            continue;
        }

        // Existing mirror — only refresh while the user is actually looking at it and not mid-edit.
        // Otherwise leave it pending (pulse the tab so the user sees the AI is working there); it
        // applies the next frame they return, never yanking focus.
        if (idx != st.activeTab || userMidEdit) {
            if (idx != st.activeTab) {
                st.docTabs[idx].aiPulse = true;
                st.docTabs[idx].lastAgentEditAt = std::chrono::steady_clock::now();
            }
            continue;
        }

        // Preserve the user's camera across the in-place refresh so watching the build doesn't reset
        // their orbit/zoom each step.
        const float cy = st.yaw, cp = st.pitch, cz = st.zoom,
                    px = st.pan[0], py = st.pan[1], pz = st.pan[2];

        // OPEN FIRST, CLOSE SECOND. The old close-then-open order had two costs: closing the
        // ACTIVE tab switched the viewport to a NEIGHBOR doc for the frames until the reopen
        // landed (a visible flicker on every AI build step), and a failed reopen left the part
        // gone from the strip entirely for the whole retry window. Opening the new snapshot
        // first appends a tab at the end (old tab untouched at `idx`), then closing the now-
        // BACKGROUND old tab is index-fixup only — no neighbor switch, no blank frames — and a
        // failed open simply leaves the old snapshot on screen while we retry.
        st.handoffQuietOpen = true;
        const bool opened = featureTreeOpenPath(st, hd.step);
        st.handoffQuietOpen = false;

        if (opened && st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
            st.closeDocTab(idx);   // retire the superseded snapshot (background close; activeTab fixed up)
            st.docTabs[st.activeTab].aiHandoff = true;
            st.docTabs[st.activeTab].handoffDocId = docId;
            st.docTabs[st.activeTab].handoffName = hd.name;
            if (!hd.name.empty()) st.docTabs[st.activeTab].title = hd.name;
            st.docTabs[st.activeTab].aiPulse = false;   // user is viewing this refresh — no glow
            st.yaw = cy; st.pitch = cp; st.zoom = cz;
            st.pan[0] = px; st.pan[1] = py; st.pan[2] = pz;
            st.zoomToBBoxRequested = false;                  // keep our restored view; cancel auto-fit
            shownRev[docId] = hd.rev;
            openFails.erase(docId);
            done.push_back(docId);
        } else {
            // Open failed — the OLD tab is still on screen (nothing was closed), so the user never
            // loses the part; just retry next frame, bounded so a genuinely-unopenable snapshot
            // eventually stops trying.
            if (++openFails[docId] >= kMaxOpenRetries) {
                shownRev[docId] = hd.rev;
                openFails.erase(docId);
                done.push_back(docId);
            }
        }
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

// True when the mirrored tree has assembly components or any non-reference feature (extrude,
// revolve, fillet, import, …). Sketches/datums alone do not count — matches the viewport's
// hasSolidContent policy so an empty/sketch-only agent run is refused by requireNonEmpty.
static bool mirrorHasSolidContent(const AppState& st) {
    if (!st.docBodyIds.empty()) return true;  // viewport already shows solid bodies
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return false;
    auto ci = tree.find("components");
    if (ci != tree.end() && ci->is_array() && !ci->empty()) return true;
    auto fi = tree.find("features");
    if (fi == tree.end() || !fi->is_array()) return false;
    auto isRefOnly = [](const std::string& type) {
        return type == "plane" || type == "datum_plane" || type == "ref_plane" ||
               type == "axis" || type == "datum_axis" || type == "ref_axis" ||
               type == "point" || type == "datum_point" || type == "ref_point" ||
               type == "csys" || type == "coordinate_system" || type == "ref_csys" ||
               type == "sketch" || type == "sketch3d";
    };
    for (const auto& f : *fi) {
        if (!f.is_object()) continue;
        if (f.value("suppressed", false)) continue;
        const std::string type = f.value("type", std::string());
        if (type.empty()) continue;
        if (!isRefOnly(type)) return true;
    }
    return false;
}

void featureTreeSaveDocToPath(AppState& st, const std::string& path, bool requireNonEmpty) {
    if (path.empty() || !st.docMirror.hasDoc() || !st.workerStarted) return;
    const std::string docId = st.docMirror.docId();
    if (docId.empty()) return;
    if (requireNonEmpty && !mirrorHasSolidContent(st)) {
        st.toast(ui::ToastLevel::Warn, "Not saving: part is empty");
        st.addLog("[doc] auto-save refused — part is empty");
        return;
    }
    const std::string pathCopy = path;
    st.toast(ui::ToastLevel::Info, "Saving " + pathBaseName(pathCopy) + "\xe2\x80\xa6");
    ++st.rebuildsInFlight;
    st.rpc.call("doc_save",
                nlohmann::json{{"doc_id", docId}, {"path", pathCopy}},
                [&st, pathCopy, docId](const core::RpcResult& r) {
                    --st.rebuildsInFlight;
                    if (st.docMirror.docId() != docId) {
                        st.addLog("[doc] auto-save for " + docId +
                                  " ignored — active doc changed");
                        return;
                    }
                    if (!r.ok) {
                        st.addLog("[err] auto-save: " + r.err);
                        st.toast(ui::ToastLevel::Error, "Save failed: " + r.err);
                        return;
                    }
                    if (r.result.is_object() && r.result.contains("code")) {
                        std::string msg = jfld(r.result, "message");
                        st.addLog("[err] auto-save: " + msg);
                        st.toast(ui::ToastLevel::Error, "Save failed: " + msg);
                        return;
                    }
                    s_docPath = pathCopy;
                    st.clearActiveTabDirty();
                    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                        st.docTabs[st.activeTab].path = pathCopy;
                    st.clearDrawingDirtyForSource(docId);
                    st.pushRecentFile(pathCopy);
                    ui::plmAutoRecordSave(st, pathCopy);
                    ui::boxMirrorNotifySave(st, pathCopy);
                    st.addLog("[doc] auto-saved: " + pathCopy);
                    st.toast(ui::ToastLevel::Info, "Saved " + pathBaseName(pathCopy));
#ifdef __EMSCRIPTEN__
                    // Web: server file is source of truth; offer a browser download of the .step+.
                    ui::afterFileWritten(pathCopy);
#endif
                }, 120000);
}

}  // namespace ui
