// AI-CAD dock layout + SolidWorks-faithful tabbed left pane — see layout.h.
//
// 1C: buildDefaultLayout() moved here verbatim from title_bar.cpp (preserving the W4-K px-based
//     dock clamp byte-for-byte); the only behavioral change is docking "LeftPane" — the new tabbed
//     container window — into the left slot instead of "FeatureManager".
// 1D: drawLeftPane() draws the vertical icon-tab strip + the active tab body, routing to the
//     existing feature-tree / PropertyManager / configurations bodies.
#include "layout.h"

#include "imgui.h"
#include "imgui_internal.h"

#include "../core/app_state.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette

#include "feature_tree.h"       // drawFeatureTreeBody
#include "file_explorer.h"      // Wave 3: drawFileExplorerBody (Explorer tab)
#include "property_manager.h"   // PropertyManager::drawBody
#include "config_panel.h"       // ConfigPanel::drawEmbedded (live Configurations tab)
#include "icons.h"              // getIcon / IconUV

#include <algorithm>
#include <cstdio>

// C24: SolidWorks 2025 default arrangement.
//   LEFT   = LeftPane (the SW task pane: Explorer / FeatureManager / PropertyManager /
//            Configurations as a vertical icon-tab strip), full height ribbon->status (~280px). The
//            PropertyManager swaps INTO the FeatureManager slot OF this pane during a command.
//   RIGHT  = Task Pane (~340px): Design (AI panel) + Agent Activity as tabs, like SW's task pane.
//            The floating command dialogs (Insert/Mate/Exploded/Interference/BOM, Mass Properties
//            & friends, Simulation) dock as tabs into the bottom of this pane on first open so they
//            never hide behind the central viewport node (the C20b NoBringToFrontOnFocus debt).
//   BOTTOM = Console, a thin strip below the viewport (collapsed-feeling; SW has no console but we
//            keep ours unobtrusive).
//   CENTER = Viewport, as large as possible.
// "Reset layout" rebuilds exactly this.
void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);

    // Target pixel widths translated to fractions of the dockspace width.
    const float w = size.x > 1.0f ? size.x : 1440.0f;
    // W4-K / GUI audit #2.6: clamp the dock widths in PIXELS, not in fixed fraction bounds. The
    // target px scale by uiScale so the docks track the pre-scaled fonts; clamping the *fraction*
    // with a fixed ceiling (the old 0.34/0.36) silently ate that DPI growth at >=1.5x and clipped
    // tree labels — the very thing W4-K was meant to fix. See docs/GUI_STYLE_GUIDE.md §6.
    const float sc = ui::uiScale;
    // Grow panes for readability at HiDPI, but dampen the growth so the viewport remains the
    // first-read area instead of being squeezed by two linear-DPI sidebars.
    const float panelSc = 1.0f + (sc - 1.0f) * 0.55f;
    auto clampf = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
    const float minCenterPx = clampf(420.0f * panelSc, 320.0f, 0.52f * w);
    float leftPx  = clampf(360.0f * panelSc, 250.0f * panelSc, 0.34f * w);
    float rightPx = clampf(380.0f * panelSc, 270.0f * panelSc, 0.36f * w);
    const float sideMax = w - minCenterPx;
    if (leftPx + rightPx > sideMax && sideMax > 0.0f) {
        const float k = sideMax / (leftPx + rightPx);
        leftPx *= k;
        rightPx *= k;
    }
    float leftFrac  = leftPx  / w;
    float rightFrac = rightPx / w;
    ImGuiID main = root, left, right, rightBottom;
    // Left column = LeftPane task pane (full height).
    left  = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left,  leftFrac,  nullptr, &main);
    // Right column = Task Pane (the consolidated AI panel), full height like SolidWorks.
    right = ImGui::DockBuilderSplitNode(main, ImGuiDir_Right, rightFrac / (1.0f - leftFrac),
                                        nullptr, &main);
    // Bottom slice of the right pane holds the command dialogs as docked tabs.
    rightBottom = ImGui::DockBuilderSplitNode(right, ImGuiDir_Down, 0.42f, nullptr, &right);

    // Wave 1D: visible dock label stays familiar while the internal id is stable.
    ImGui::DockBuilderDockWindow("FeatureManager##LeftPane", left);
    // One task-pane window: Design / Activity / Console live as internal tabs.
    ImGui::DockBuilderDockWindow("AI", right);
    // Wave N/O: ECAD shares the central node with the Viewport (tabbed). Dock ECAD FIRST so the
    // Viewport is the default-active tab (3D is the primary view); the ECAD tab pops forward on its
    // own (SetNextWindowFocus) when an ECAD document is loaded.
    ImGui::DockBuilderDockWindow("ECAD", main);
    ImGui::DockBuilderDockWindow("Viewport", main);
    ImGui::DockBuilderDockWindow("Viewport###Viewport", main);
    // Command dialogs dock into the lower-right slot so they surface as tabs instead of floating
    // behind the central node. They share one node (only one is typically open at a time).
    const char* dialogs[] = {
        "Simulation", "Insert Component", "Mate##asm", "Exploded View",
        "Interference Detection", "Bill of Materials",
        // Evaluate dialogs (titles set in evaluate_dialogs.cpp) — pre-registered so they land in
        // the task pane the first time they open (DockBuilderDockWindow matches by name).
        "Mass Properties", "Measure", "Section Properties", "Geometry Check", "Symmetry Check",
        "Zebra Stripes", "Draft angle", "Wall thickness", "Mean curvature",
        // Configurations (SolidWorks ConfigurationManager) — lands in the task pane the first time
        // it's toggled on so it surfaces as a tab rather than floating over the viewport.
        "Configurations",
        // Design Table — same task-pane slot so the editable config grid surfaces as a tab.
        "Design Table",
        // Weldment Cut List — same task-pane slot so it surfaces as a tab when toggled on.
        "Cut List",
    };
    for (const char* d : dialogs) ImGui::DockBuilderDockWindow(d, rightBottom);

    ImGui::DockBuilderFinish(root);
}

namespace ui {

static ImVec4 roleColor(const float role[4]) {
    return ImVec4(role[0], role[1], role[2], role[3]);
}

static bool drawManagerTabButton(const char* id,
                                 const char* icon,
                                 const char* fallback,
                                 const char* tip,
                                 bool selected,
                                 bool pmAttention,
                                 float btn,
                                 float glyph) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + btn, p0.y + btn);

    ImGui::PushID(id);
    bool clicked = ImGui::InvisibleButton("##lp_tab", ImVec2(btn, btn));
    bool hovered = ImGui::IsItemHovered();

    ImU32 fill = 0;
    if (selected) fill = ui::Palette::toU32(pal.ribbonTabActive);
    else if (hovered) fill = ui::Palette::toU32(pal.buttonHovered);
    if (fill) dl->AddRectFilled(p0, p1, fill, 7.0f * S);

    if (selected) {
        dl->AddRectFilled(ImVec2(p0.x, p0.y + 6.0f * S),
                          ImVec2(p0.x + 3.0f * S, p1.y - 6.0f * S),
                          ui::Palette::toU32(pal.accent), 2.0f * S);
        dl->AddRect(p0, p1, ui::Palette::toU32(pal.accentSoft), 7.0f * S, 0,
                    std::max(1.0f, 1.0f * S));
    }

    IconUV ic = getIcon(icon, 20);
    if (ic.valid) {
        float ix = p0.x + (btn - glyph) * 0.5f;
        float iy = p0.y + (btn - glyph) * 0.5f;
        ImVec4 tint = selected ? roleColor(pal.text) : roleColor(pal.textDim);
        if (pmAttention && !selected) tint = roleColor(pal.accent);
        dl->AddImage(ic.texId, ImVec2(ix, iy), ImVec2(ix + glyph, iy + glyph),
                     ic.uv0, ic.uv1, ImGui::ColorConvertFloat4ToU32(tint));
    } else {
        ImVec2 ts = ImGui::CalcTextSize(fallback);
        ImU32 tc = selected ? ui::Palette::toU32(pal.text) : ui::Palette::toU32(pal.textDim);
        dl->AddText(ImVec2(p0.x + (btn - ts.x) * 0.5f, p0.y + (btn - ts.y) * 0.5f),
                    tc, fallback);
    }

    if (pmAttention) {
        float r = 3.0f * S;
        dl->AddCircleFilled(ImVec2(p1.x - 6.0f * S, p0.y + 6.0f * S), r,
                            ui::Palette::toU32(pal.statusWarn));
    }

    if (hovered) ImGui::SetTooltip("%s", tip);
    ImGui::PopID();
    return clicked;
}

// ---- Configurations tab body (no Begin/End) -------------------------------
// The part-document engine has no named-configuration schema yet (config_* RPCs exist for the
// dockable ConfigPanel, but parts ship a single implicit configuration). Render the implicit
// "Default [Active]" entry plus a short note pointing at the future design-tables work, matching
// the SolidWorks ConfigurationManager's at-rest appearance for a single-config part.
void drawConfigurationsBody(AppState& st) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    const ImVec4 dimCol(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1);

    ImGui::PushFont(st.fonts.medium);
    ImGui::TextColored(accentCol, "Configurations");
    ImGui::PopFont();
    ImGui::Spacing();

    // Implicit single configuration: bold accent dot + "[Active]" tag (SolidWorks bolds the active).
    ImGui::PushStyleColor(ImGuiCol_Text, accentCol);
    ImGui::TextUnformatted("*  Default");
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "[Active]");

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::PushTextWrapPos(ImGui::GetContentRegionAvail().x);
    ImGui::TextColored(dimCol,
        "Use Design Table for editable configuration values. The active configuration is "
        "mirrored here so the left manager stays the single place to inspect model state.");
    ImGui::PopTextWrapPos();
}

// ---- the SolidWorks-faithful tabbed left pane -----------------------------

void drawLeftPane(AppState& st,
                  const std::function<void(const std::string& featureId)>& editCallback) {
    PropertyManager* pm = st.propertyManager;

    // Auto-switch policy (Wave 1D): track the PM's active state across frames.
    //   open-edge  (inactive -> active): jump to the PropertyManager tab so the form is visible.
    //   close-edge (active -> inactive): return to FeatureManager, but ONLY if the user is still on
    //   the PropertyManager tab — never yank a manual mid-edit tab change the user made themselves.
    static bool s_pmWasActive = false;
    const bool pmActive = pm && pm->active();
    if (pmActive && !s_pmWasActive) {
        st.leftPaneTab = kLeftPanePropertyManager;
    } else if (!pmActive && s_pmWasActive) {
        if (st.leftPaneTab == kLeftPanePropertyManager)
            st.leftPaneTab = kLeftPaneFeatureManager;
    }
    s_pmWasActive = pmActive;

    if (!ImGui::Begin("FeatureManager##LeftPane")) { ImGui::End(); return; }

    const auto& pal = ui::palette;
    const float S = ui::uiScale;

    // True horizontal center of the WHOLE pane (icon rail + body), captured BEFORE the rail child so
    // the FeatureManager header can be centered across the full panel. Centering only within the body
    // (right of the rail) left the header sitting slightly left of the panel's true center.
    const ImVec2 paneOrigin  = ImGui::GetCursorScreenPos();
    const float  paneInnerW  = ImGui::GetContentRegionAvail().x;
    const float  paneCenterX = paneOrigin.x + paneInnerW * 0.5f;

    // ---- vertical icon-tab strip (SolidWorks style: small icons stacked on the left edge) ----
    struct TabDef { int id; const char* icon; const char* fallback; const char* tip; };
    static const TabDef kTabs[] = {
        { kLeftPaneExplorer,        "body_folder", "E", "Explorer" },
        { kLeftPaneFeatureManager,  "history",     "F", "Feature Manager" },
        { kLeftPanePropertyManager, "material",    "P", "Property Manager" },
        { kLeftPaneConfigurations,  "settings",    "C", "Configurations" },
    };

    const float btn = 36.0f * S;
    const float glyph = 20.0f * S;
    const float gap = 4.0f * S;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float stripW = btn + 8.0f * S;

    ImGui::BeginChild("##lp_tabstrip", ImVec2(stripW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 rail0 = ImGui::GetWindowPos();
    ImVec2 rail1(rail0.x + stripW, rail0.y + ImGui::GetWindowHeight());
    dl->AddRectFilled(rail0, rail1, ui::Palette::toU32(pal.headerBg));
    ImGui::SetCursorPos(ImVec2(4.0f * S, 6.0f * S));
    for (const TabDef& t : kTabs) {
        const bool selected = (st.leftPaneTab == t.id);
        const bool attention = (t.id == kLeftPanePropertyManager && pmActive);
        char idBuf[16];
        std::snprintf(idBuf, sizeof(idBuf), "%d", t.id);
        if (drawManagerTabButton(idBuf, t.icon, t.fallback, t.tip, selected, attention, btn, glyph))
            st.leftPaneTab = t.id;
        ImGui::Dummy(ImVec2(0, gap));
    }
    ImGui::EndChild();

    // Body sits flush against the rail; the symmetric WindowPadding pushed below supplies EQUAL
    // breathing room on both sides (gap from the rail == gap from the panel's right edge) so every
    // manager tab is evenly spaced instead of crowding the rail or leaning toward the right edge.
    ImGui::SameLine(0, 0);

    // ---- active tab body ----
    const char* title = "Feature Manager";
    const char* titleIcon = "history";
    if (st.leftPaneTab == kLeftPaneExplorer) {
        title = "Explorer";
        titleIcon = "body_folder";
    } else if (st.leftPaneTab == kLeftPanePropertyManager && pmActive) {
        title = "Property Manager";
        titleIcon = "material";
    } else if (st.leftPaneTab == kLeftPaneConfigurations) {
        title = "Configurations";
        titleIcon = "settings";
    }

    // Equal left/right margins via symmetric WindowPadding + a slim scrollbar. When the form
    // overflows, ImGui reserves a vertical scrollbar on the right, which would make the right gap
    // (padding + scrollbar) wider than the left (padding) — exactly the lopsided look on the tall
    // Extrude PropertyManager. We mirror it by indenting the content by the scrollbar width while
    // scrolling, so the gap from the rail equals the gap from the panel edge in BOTH states.
    // s_bodyScrolled samples last frame's scroll state (queried just before EndChild).
    static bool s_bodyScrolled = false;
    const float bodyPad = 12.0f * S;
    const float sbSize  = 8.0f * S;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(bodyPad, ImGui::GetStyle().WindowPadding.y));
    ImGui::PushStyleVar(ImGuiStyleVar_ScrollbarSize, sbSize);
    ImGui::BeginChild("##lp_body", ImVec2(0, 0), ImGuiChildFlags_AlwaysUseWindowPadding);
    const bool bodyIndented = s_bodyScrolled;
    if (bodyIndented) ImGui::Indent(sbSize);   // balance the right-side scrollbar gutter
    IconUV headIcon = getIcon(titleIcon, 20);
    const float iconSz = headIcon.valid ? 20.0f * S : 0.0f;
    const float iconGap = headIcon.valid ? 6.0f * S : 0.0f;
    const float titleW = ImGui::CalcTextSize(title).x;
    const float groupW = iconSz + iconGap + titleW;
    // Center the header at the pane's TRUE horizontal center (captured in screen space above),
    // mapped into this body child's local cursor X; clamp so it never sits left of the body padding.
    const float localCur = ImGui::GetCursorPosX();
    const float titleX = std::max(localCur,
        localCur + (paneCenterX - groupW * 0.5f - ImGui::GetCursorScreenPos().x));
    ImGui::SetCursorPosX(titleX);
    if (headIcon.valid) {
        ImGui::Image(headIcon.texId, ImVec2(iconSz, iconSz), headIcon.uv0, headIcon.uv1);
        ImGui::SameLine(0, iconGap);
    }
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted(title);
    ImGui::PopFont();
    ImGui::Separator();
    switch (st.leftPaneTab) {
        case kLeftPaneExplorer:
            // Wave 3: the workspace file browser (file_explorer.cpp).
            drawFileExplorerBody(st);
            break;

        case kLeftPanePropertyManager:
            // PM active -> show the live form body; otherwise the slot must never be blank, so fall
            // back to the feature tree body (matches SolidWorks: the PM tab shows the tree at rest).
            if (pmActive) pm->drawBody(st);
            else          drawFeatureTreeBody(st, editCallback);
            break;

        case kLeftPaneConfigurations:
            // User chose the live ConfigurationManager (config_* RPCs) for this tab. ConfigPanel is
            // owned by run_gui and borrowed via st.configPanel; drawEmbedded renders its contents
            // without a Begin/End. Fall back to the at-rest single-config body if it's unset.
            if (st.configPanel) st.configPanel->drawEmbedded(st);
            else                drawConfigurationsBody(st);
            break;

        case kLeftPaneFeatureManager:
        default:
            drawFeatureTreeBody(st, editCallback);
            break;
    }
    s_bodyScrolled = ImGui::GetScrollMaxY() > 0.0f;   // remembered for next frame's indent balance
    if (bodyIndented) ImGui::Unindent(sbSize);
    ImGui::EndChild();
    ImGui::PopStyleVar(2);   // WindowPadding + ScrollbarSize pushed before the body child

    ImGui::End();
}

}  // namespace ui
