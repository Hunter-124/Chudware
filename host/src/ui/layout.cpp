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
#include "property_manager.h"   // PropertyManager::drawBody
#include "icons.h"              // getIcon / IconUV

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
    auto clampf = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
    float leftPx  = clampf(258.0f * sc, 180.0f * sc, 0.34f * w);  float leftFrac  = leftPx  / w;
    float rightPx = clampf(300.0f * sc, 200.0f * sc, 0.36f * w);  float rightFrac = rightPx / w;
    ImGuiID main = root, left, right, rightBottom;
    // Left column = LeftPane task pane (full height).
    left  = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left,  leftFrac,  nullptr, &main);
    // Right column = Task Pane (the consolidated AI panel), full height like SolidWorks.
    right = ImGui::DockBuilderSplitNode(main, ImGuiDir_Right, rightFrac / (1.0f - leftFrac),
                                        nullptr, &main);
    // Bottom slice of the right pane holds the command dialogs as docked tabs.
    rightBottom = ImGui::DockBuilderSplitNode(right, ImGuiDir_Down, 0.42f, nullptr, &right);

    // Wave 1D: the left slot now hosts the tabbed task-pane window "LeftPane" (was "FeatureManager").
    ImGui::DockBuilderDockWindow("LeftPane", left);
    // One task-pane window: Design / Activity / Console live as internal tabs.
    ImGui::DockBuilderDockWindow("AI", right);
    // Wave N/O: ECAD shares the central node with the Viewport (tabbed). Dock ECAD FIRST so the
    // Viewport is the default-active tab (3D is the primary view); the ECAD tab pops forward on its
    // own (SetNextWindowFocus) when an ECAD document is loaded.
    ImGui::DockBuilderDockWindow("ECAD", main);
    ImGui::DockBuilderDockWindow("Viewport", main);
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
    ImGui::TextUnformatted(u8"\xe2\x97\x8f  Default");   // bullet + name
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "[Active]");

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::PushTextWrapPos(ImGui::GetContentRegionAvail().x);
    ImGui::TextColored(dimCol,
        "Named configurations and design tables are coming in a later wave. The "
        "Configurations / Design Table windows (View menu) cover config editing for now.");
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

    if (!ImGui::Begin("LeftPane")) { ImGui::End(); return; }

    const auto& pal = ui::palette;
    const float S = ui::uiScale;

    // ---- vertical icon-tab strip (SolidWorks style: small icons stacked on the left edge) ----
    struct TabDef { int id; const char* icon; const char* fallback; const char* tip; };
    static const TabDef kTabs[] = {
        { kLeftPaneExplorer,        "body_folder", "[E]", "Explorer" },
        { kLeftPaneFeatureManager,  "history",     "[F]", "FeatureManager" },
        { kLeftPanePropertyManager, "material",    "[P]", "PropertyManager" },
        { kLeftPaneConfigurations,  "settings",    "[C]", "Configurations" },
    };

    const float btn = 28.0f * S;          // square tab button (matches FM toolbar button tier)
    const float glyph = 18.0f * S;        // toolbar glyph tier
    const float gap = 2.0f * S;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float stripW = btn + style.WindowPadding.x;   // icon column + a little breathing room

    ImGui::BeginChild("##lp_tabstrip", ImVec2(stripW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    for (const TabDef& t : kTabs) {
        const bool selected = (st.leftPaneTab == t.id);
        if (selected) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        }
        ImGui::PushID(t.id);
        IconUV ic = getIcon(t.icon, 20);
        bool clicked;
        if (ic.valid) {
            clicked = ImGui::ImageButton("##lp_tab", ic.texId, ImVec2(glyph, glyph),
                                         ic.uv0, ic.uv1);
        } else {
            clicked = ImGui::Button(t.fallback, ImVec2(btn, btn));
        }
        if (clicked) st.leftPaneTab = t.id;
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", t.tip);
        ImGui::PopID();
        if (selected) ImGui::PopStyleColor(2);
        ImGui::Dummy(ImVec2(0, gap));
    }
    ImGui::EndChild();

    ImGui::SameLine(0, 0);

    // ---- active tab body ----
    ImGui::BeginChild("##lp_body", ImVec2(0, 0), ImGuiChildFlags_None);
    switch (st.leftPaneTab) {
        case kLeftPaneExplorer:
            // Wave 3 plugs the workspace file browser in here (file_explorer.cpp). Stub for now.
            ImGui::PushTextWrapPos(ImGui::GetContentRegionAvail().x);
            ImGui::TextColored(
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1),
                "Workspace explorer — coming in Wave 3");
            ImGui::PopTextWrapPos();
            break;

        case kLeftPanePropertyManager:
            // PM active -> show the live form body; otherwise the slot must never be blank, so fall
            // back to the feature tree body (matches SolidWorks: the PM tab shows the tree at rest).
            if (pmActive) pm->drawBody(st);
            else          drawFeatureTreeBody(st, editCallback);
            break;

        case kLeftPaneConfigurations:
            drawConfigurationsBody(st);
            break;

        case kLeftPaneFeatureManager:
        default:
            drawFeatureTreeBody(st, editCallback);
            break;
    }
    ImGui::EndChild();

    ImGui::End();
}

}  // namespace ui
