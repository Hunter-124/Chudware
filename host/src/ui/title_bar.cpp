// AI-CAD title bar — left-group chips (File/Search/Settings) + centered workspace tabs +
// window caption buttons. Publishes fine-grained chrome::HitLayout spans so every
// interactive element is click-through on Windows and Linux; the remaining area is draggable.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette
#include "metrics.h"           // ui::metrics::stroke / titleFramePad tokens
#include "doc_tabs.h"          // ui::openNewDocChooser (File > New)
#include "feature_tree.h"      // ui::featureTreeActivePath (Save vs. Save As gating)
#include "property_manager.h"  // PropertyManager::active() (used in View sub-menu)

#include <filesystem>      // Recent-files menu labels

// ---------------- caption buttons (shared small ui helper) ----------------
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    // C25-W3G: button width tracks uiScale so buttons stay clickable at 1.5x/2.0x.
    const float btnW = h * (1.35f + 0.15f * (ui::uiScale - 1.0f));
    const ImVec2 size(btnW, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered) {
        // Close keeps the Windows-standard caption red (intentional literal); min/max hover
        // uses the palette role so the wash reads on the light theme too (a white wash on the
        // near-white light headerBg was invisible).
        ImU32 bg = kind == BTN_CLOSE ? IM_COL32(232, 17, 35, 255)
                                     : ui::Palette::toU32(ui::palette.buttonHovered);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    // Caption glyph color from palette so it adapts to light/dark themes.
    const unsigned char fgv = (unsigned char)(ui::palette.captionFg * 255.0f + 0.5f);
    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? IM_COL32(255, 255, 255, 255)
                                                    : IM_COL32(fgv, fgv, fgv, 235);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;  // glyph half-size
    const float th = ui::metrics::stroke(1.4f);  // hairline at 2x DPI
    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {  // restore: two stacked squares
            dl->AddRect(ImVec2(c.x - s + 2 * ui::uiScale, c.y - s), ImVec2(c.x + s, c.y + s - 2 * ui::uiScale), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2 * ui::uiScale), ImVec2(c.x + s - 2 * ui::uiScale, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

// Wave 1C: buildDefaultLayout() moved to ui/layout.cpp.

namespace ui {

// Glass v2.1: the title bar stays a SOLID chrome band (locked design) but subtly adopts the
// Global glass group's user tint — mirror of ribbon_chromeTint in widgets/ribbon.cpp.
static ImVec4 titleBar_chromeTint(const float base[4]) {
    const ui::GlassStyleResolved& gs = ui::glassStyleResolved(ui::GlassGroup::Global);
    ImVec4 c(base[0], base[1], base[2], base[3]);
    if (gs.tintStrength <= 0.001f || gs.tintSat <= 0.001f) return c;
    float h, sat, v;
    ImGui::ColorConvertRGBtoHSV(c.x, c.y, c.z, h, sat, v);
    float tr, tg, tb;
    ImGui::ColorConvertHSVtoRGB(gs.tintHue / 360.0f, gs.tintSat, v, tr, tg, tb);
    float m = gs.tintStrength * 0.35f;
    if (m > 1.0f) m = 1.0f;
    c.x += (tr - c.x) * m;
    c.y += (tg - c.y) * m;
    c.z += (tb - c.z) * m;
    return c;
}

// ---- chip drawing helpers -----------------------------------------------
// Draw a chip-/tab-shaped button in the title-bar. Returns true if clicked.
// isActive: bold outline + subtle fill; hovered: lighter fill. Both states use palette roles.
// chipRounding: corner radius in px (0 = square, >=barH*0.4 = fully rounded pill).
static bool drawChip(const char* id, const char* label, bool isActive, float barH,
                     float chipRounding, ImFont* boldFont = nullptr) {
    const float s = ui::uiScale;
    const float padX = 10.0f * s;
    const float padY = 4.0f * s;

    // Measure text (possibly bold for active state).
    ImFont* activeFont = boldFont ? boldFont : ImGui::GetFont();
    ImFont* normalFont = ImGui::GetFont();
    ImFont* drawFont   = isActive ? activeFont : normalFont;
    (void)normalFont;

    // Determine chip size: same width whether active or not (avoid layout shift).
    // Use bold font width as the canonical measure so inactive chips don't narrow.
    float textW;
    if (boldFont)
        textW = boldFont->CalcTextSizeA(boldFont->FontSize, FLT_MAX, 0, label).x;
    else
        textW = ImGui::CalcTextSize(label).x;

    const float chipW  = textW + padX * 2.0f;
    const float chipH  = barH - padY * 2.0f;
    const ImVec2 size(chipW, chipH);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();

    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float rnd = chipRounding;

    if (isActive) {
        // Active: subtle fill + bold outline (Hunter's rule: bold outline, no underline).
        ImU32 fillC = ui::Palette::toU32(ui::palette.buttonActive);
        ImU32 outlineC = ui::Palette::toU32(ui::palette.accent);
        dl->AddRectFilled(p0, ImVec2(p0.x + chipW, p0.y + chipH), fillC, rnd);
        const float th = ui::metrics::stroke(1.8f);
        dl->AddRect(p0, ImVec2(p0.x + chipW, p0.y + chipH), outlineC, rnd, 0, th);
    } else if (hovered) {
        ImU32 fillC = ui::Palette::toU32(ui::palette.buttonHovered);
        dl->AddRectFilled(p0, ImVec2(p0.x + chipW, p0.y + chipH), fillC, rnd);
    }

    // Label text (always aligned to center of chip).
    const ImVec4 textCol(ui::palette.text[0], ui::palette.text[1],
                         ui::palette.text[2], ui::palette.text[3]);
    const float tx = p0.x + (chipW - textW) * 0.5f;
    const float ty = p0.y + (chipH - drawFont->FontSize) * 0.5f;
    if (drawFont != ImGui::GetFont()) {
        ImGui::GetWindowDrawList()->AddText(drawFont, drawFont->FontSize,
                                            ImVec2(tx, ty), ImGui::ColorConvertFloat4ToU32(textCol), label);
    } else {
        dl->AddText(ImVec2(tx, ty), ImGui::ColorConvertFloat4ToU32(textCol), label);
    }
    return clicked;
}

// ---- File menu (self-contained lambda callable from full or collapsed path) ---
// Declared here so it can be used by both collapsed-menu and normal paths.
// We pass the lambda as a template-free callable.
struct FilMenuDrawer {
    AppState& st;
    bool hasDoc;
    GLFWwindow* win;
    void draw() const {
        if (ImGui::BeginMenu("New")) {
            if (ImGui::MenuItem("Part", "Ctrl+N")) ui::createNewDoc(st, 0);
            if (ImGui::MenuItem("Assembly"))       ui::createNewDoc(st, 1);
            const bool canDraw = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                (st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Part ||
                 st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly);
            if (ImGui::MenuItem("Drawing", nullptr, false, canDraw)) ui::createNewDoc(st, 2);
            ImGui::Separator();
            if (ImGui::MenuItem("Schematic"))      ui::createNewDoc(st, 3);
            if (ImGui::MenuItem("PCB"))            ui::createNewDoc(st, 4);
            ImGui::EndMenu();
        }
        if (ImGui::MenuItem("Open...", "Ctrl+O")) st.requestOpenDialog = true;
        if (ImGui::MenuItem("Import...")) st.requestOpenDialog = true;
        ImGui::Separator();
        if (ImGui::MenuItem("Save", "Ctrl+S", false, hasDoc)) st.requestSave = true;
        if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S", false, hasDoc)) st.requestSaveAs = true;
        if (ImGui::MenuItem("Save All", nullptr, false, hasDoc)) st.requestSaveAll = true;
        ImGui::Separator();
        const bool canExport3d = hasDoc && !st.activeTabIsDrawing() && !st.activeTabIsEcad();
        auto openMeshExport = [&](const char* fmt) {
            st.exportTess.open = true;
            st.exportTess.fmt = fmt;
            st.exportTess.estimateDirty = true;
            st.exportTess.estimateValid = false;
        };
        if (ImGui::BeginMenu("Export", canExport3d)) {
            if (ImGui::MenuItem("STEP (.step)...")) st.requestExportFormat = "step";
            if (ImGui::MenuItem("IGES (.iges)...")) st.requestExportFormat = "iges";
            if (ImGui::MenuItem("BREP (.brep)...")) st.requestExportFormat = "brep";
            ImGui::Separator();
            if (ImGui::MenuItem("STL (.stl)..."))   openMeshExport("stl");
            if (ImGui::MenuItem("OBJ (.obj)..."))   openMeshExport("obj");
            if (ImGui::MenuItem("PLY (.ply)..."))   openMeshExport("ply");
            if (ImGui::MenuItem("3MF (.3mf)..."))   openMeshExport("3mf");
            if (ImGui::MenuItem("glTF (.gltf)...")) openMeshExport("gltf");
            if (ImGui::MenuItem("GLB (.glb)..."))   openMeshExport("glb");
            ImGui::EndMenu();
        }
        ImGui::Separator();
        if (ImGui::BeginMenu("Recent Files", !st.recentFiles.empty())) {
            for (const std::string& rp : st.recentFiles) {
                std::string name = std::filesystem::path(rp).filename().string();
                if (ImGui::MenuItem(name.c_str())) st.requestOpenPath = rp;
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", rp.c_str());
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Clear Recent Files")) {
                st.recentFiles.clear();
                st.settings.recentFiles.clear();
                saveSettings(st.settings);
            }
            ImGui::EndMenu();
        }
        ImGui::Separator();
        // ---- Formerly View menu: panel show/hide, theme toggle, layout reset ----
        if (ImGui::BeginMenu("View")) {
            ImGui::MenuItem("Ribbon", nullptr, &st.showRibbon);
            ImGui::Separator();
            auto leftPaneEntry = [&](const char* label, int tab, bool enabled = true) {
                const bool activeEntry = st.showFeatureTree && st.leftPaneTab == tab;
                if (ImGui::MenuItem(label, nullptr, activeEntry, enabled)) {
                    if (activeEntry) {
                        st.showFeatureTree = false;
                    } else {
                        st.showFeatureTree = true;
                        st.leftPaneTab = tab;
                    }
                }
            };
            leftPaneEntry("FeatureManager",     AppState::LP_FeatureManager);
            leftPaneEntry("PropertyManager",    AppState::LP_PropertyManager,
                          st.propertyManager && st.propertyManager->active());
            leftPaneEntry("ConfigurationManager", AppState::LP_Configurations);
            leftPaneEntry("Explorer",           AppState::LP_Explorer);
            ImGui::MenuItem("Design Table",     nullptr, &st.showDesignTablePanel);
            ImGui::MenuItem("Cut List",         nullptr, &st.showCutListPanel);
            ImGui::MenuItem("Equations",        nullptr, &st.showEquations);
            ImGui::MenuItem("AI Panel",         nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport",         nullptr, &st.showViewport);
            ImGui::Separator();
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme (SolidWorks-style)", nullptr, isLight)) {
                ui::Theme next = isLight ? ui::Theme::Dark : ui::Theme::Light;
                ui::applyTheme(next, st.scale);
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                saveSettings(st.settings);
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
            ImGui::EndMenu();
        }
        ImGui::Separator();
        // ---- Formerly Help menu essentials ----
        if (ImGui::MenuItem("AI Help")) {
            st.showAIPanel = true;
            st.aiInteraction = 2;  // Ask
        }
        if (ImGui::MenuItem("About ShopCraft")) st.showAbout = true;
        ImGui::Separator();
        // Repair/install runtime
        if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                            !st.setupRunning.load() && !st.job.running.load()))
            st.startSetup(true);
        ImGui::Separator();
        if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
    }
};

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    // C25-W3G: scale the frame padding so the title bar grows with DPI (metrics tokens).
    float s = ui::uiScale;

    // ---------- custom title bar (our own; the OS title bar is stripped) ----------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ui::metrics::titleFramePadX(), ui::metrics::titleFramePadY()));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    hitLayout.clearSpans();  // reset fine-grained spans each frame

    // Solid chrome band, subtly following the Global glass tint.
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg, titleBar_chromeTint(ui::palette.headerBg));
    if (ImGui::BeginMainMenuBar()) {
        const float bw = barH * (1.35f + 0.15f * (s - 1.0f));  // caption button width
        const float captionZoneW = bw * 3.0f;
        const float winW = ImGui::GetWindowWidth();
        // Available horizontal space for everything except the OS caption buttons.
        const float availW = winW - captionZoneW - 4.0f * s;

        // ---- Brand wordmark / logo (left edge) -----------------------------------
        ImGui::SetCursorPosX(12.0f * s);
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        float logoEndX = 12.0f * s;
        if (wm.id) {
            const float wmH = barH * 0.40f;
            float wmW = wmH * (float)wm.w / wm.h;
            if (ImGui::GetCursorPosX() + wmW + 18.0f * s < availW - 240.0f * s) {
                ImGui::SetCursorPosY((barH - wmH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)wm.id, ImVec2(wmW, wmH));
                logoEndX = ImGui::GetCursorPosX();
                ImGui::SameLine(0, 16.0f * s);
            }
        } else if (st.logoIcon.id) {
            const float iconH = barH * 0.52f;
            if (ImGui::GetCursorPosX() + iconH + 18.0f * s < availW - 240.0f * s) {
                ImGui::SetCursorPosY((barH - iconH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(iconH, iconH));
                logoEndX = ImGui::GetCursorPosX();
                ImGui::SameLine(0, 16.0f * s);
            }
        }
        (void)logoEndX;

        // ---- LEFT GROUP: File (dropdown) | Search chip | Settings chip ----------
        // These are styled as pill-chips but File keeps the standard dropdown behaviour.
        const bool hasDoc = (st.activeTab >= 0);
        const float chipVPad = 4.0f * s;
        const float chipRound = barH * 0.28f;  // moderately rounded

        // Align chips vertically in the bar.
        ImGui::SetCursorPosY(chipVPad);

        // -- File menu (standard ImGui dropdown, styled as a chip) --
        // We draw it as an ImGui::BeginMenu so the full dropdown works. The chip background
        // is drawn via ImGui::PushStyleColor on the button/header roles.
        {
            ImGui::PushStyleColor(ImGuiCol_Header,        ImVec4(ui::palette.buttonActive[0],  ui::palette.buttonActive[1],  ui::palette.buttonActive[2],  ui::palette.buttonActive[3]));
            ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(ui::palette.buttonHovered[0], ui::palette.buttonHovered[1], ui::palette.buttonHovered[2], ui::palette.buttonHovered[3]));
            ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(ui::palette.buttonActive[0],  ui::palette.buttonActive[1],  ui::palette.buttonActive[2],  ui::palette.buttonActive[3]));
            ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, chipRound);
            const float fileMenuX0 = ImGui::GetCursorScreenPos().x;
            if (ImGui::BeginMenu("File")) {
                FilMenuDrawer fmd{st, hasDoc, win};
                fmd.draw();
                ImGui::EndMenu();
            }
            // Register the File menu span for click-through.
            const float fileMenuX1 = ImGui::GetItemRectMax().x;
            hitLayout.addSpan((int)fileMenuX0, (int)fileMenuX1);
            ImGui::PopStyleVar();
            ImGui::PopStyleColor(3);
        }

        ImGui::SameLine(0, 4.0f * s);

        // -- Search chip --
        {
            const float x0 = ImGui::GetCursorScreenPos().x;
            ImGui::SetCursorPosY(chipVPad);
            if (drawChip("##tb_search", "Search", false, barH - chipVPad * 2.0f, chipRound)) {
                st.showCommandSearch = true;
            }
            hitLayout.addSpan((int)x0, (int)ImGui::GetItemRectMax().x);
        }

        ImGui::SameLine(0, 4.0f * s);

        // -- Settings chip --
        {
            const float x0 = ImGui::GetCursorScreenPos().x;
            ImGui::SetCursorPosY(chipVPad);
            if (drawChip("##tb_settings", "Settings", false, barH - chipVPad * 2.0f, chipRound)) {
                st.openSettings = true;
            }
            hitLayout.addSpan((int)x0, (int)ImGui::GetItemRectMax().x);
        }

        // Legacy coarse menuLeft/Right (kept for anything still reading them).
        hitLayout.menuLeft  = (int)ImGui::GetWindowPos().x + (int)(12.0f * s);
        hitLayout.menuRight = (int)ImGui::GetItemRectMax().x;

        // ---- Subtle vertical separator between left group and workspace tabs ----
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float sepX = ImGui::GetCursorScreenPos().x + 8.0f * s;
            const float sepY0 = ImGui::GetWindowPos().y + barH * 0.20f;
            const float sepY1 = ImGui::GetWindowPos().y + barH * 0.80f;
            ImU32 sepCol = ui::Palette::toU32(ui::palette.border);
            // Half-transparent separator so it's a visual hint, not a hard line.
            sepCol = (sepCol & 0x00FFFFFF) | 0x60000000u;
            dl->AddLine(ImVec2(sepX, sepY0), ImVec2(sepX, sepY1), sepCol, 1.5f * s);
        }

        // ---- CENTER GROUP: workspace tabs, centered between left group and OS buttons ----
        // Workspace tab definitions.
        struct WsTab {
            const char* label;
            AppState::Workspace ws;
        };
        static const WsTab kTabs[] = {
            {"Model",        AppState::Workspace::Model},
            {"Drawing",      AppState::Workspace::Drawing},
            {"Simulate",     AppState::Workspace::Simulate},
            {"Electronics",  AppState::Workspace::Electronics},
            {"Manufacture",  AppState::Workspace::Manufacture},
            {"PLM",          AppState::Workspace::PLM},
            {"Parts Library",AppState::Workspace::PartsLibrary},
            {"AI",           AppState::Workspace::AI},
        };
        constexpr int kTabCount = (int)(sizeof(kTabs) / sizeof(kTabs[0]));

        const float tabRound = barH * 0.30f;
        const float tabGapX  = 3.0f * s;    // gap between tabs
        const float tabPadX  = 10.0f * s;   // horizontal text padding inside each tab
        const float tabH     = barH - chipVPad * 2.0f;

        // Measure total width of all tabs (use medium/bold font if available).
        ImFont* boldFont = st.fonts.medium ? st.fonts.medium : nullptr;
        float totalTabsW = 0.0f;
        for (int i = 0; i < kTabCount; ++i) {
            float tw;
            if (boldFont)
                tw = boldFont->CalcTextSizeA(boldFont->FontSize, FLT_MAX, 0, kTabs[i].label).x;
            else
                tw = ImGui::CalcTextSize(kTabs[i].label).x;
            totalTabsW += tw + tabPadX * 2.0f;
            if (i < kTabCount - 1) totalTabsW += tabGapX;
        }

        // The left group ends at the current cursor. Compute available center zone.
        const float leftGroupEndX   = ImGui::GetCursorScreenPos().x + 16.0f * s;  // +sep width
        const float rightGroupStartX = winW - captionZoneW - 4.0f * s;
        const float centerZoneW     = rightGroupStartX - leftGroupEndX;

        // Clamp: if tabs don't fit, start from leftGroupEndX (no overlap with OS buttons).
        float tabsStartX;
        if (totalTabsW + 8.0f * s <= centerZoneW) {
            tabsStartX = leftGroupEndX + (centerZoneW - totalTabsW) * 0.5f;
        } else {
            // Compressed: start immediately after left group.
            tabsStartX = leftGroupEndX + 4.0f * s;
        }

        // Draw tabs using the draw list directly (avoids messing with ImGui cursor flow for
        // many items) + InvisibleButton for interaction.
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float tx = tabsStartX;
        for (int i = 0; i < kTabCount; ++i) {
            const bool isActive = (st.activeWorkspace == kTabs[i].ws);
            ImFont* drawFont = boldFont ? boldFont : ImGui::GetFont();
            float textW;
            if (boldFont)
                textW = boldFont->CalcTextSizeA(boldFont->FontSize, FLT_MAX, 0, kTabs[i].label).x;
            else
                textW = ImGui::CalcTextSize(kTabs[i].label).x;
            const float chipW = textW + tabPadX * 2.0f;

            // Clip tab if it would overlap the OS controls area.
            if (tx + chipW > rightGroupStartX - 2.0f * s) break;

            const ImVec2 p0(tx, ImGui::GetWindowPos().y + chipVPad);
            const ImVec2 p1(tx + chipW, p0.y + tabH);

            // Background + outline.
            if (isActive) {
                ImU32 fillC    = ui::Palette::toU32(ui::palette.buttonActive);
                ImU32 outlineC = ui::Palette::toU32(ui::palette.accent);
                dl->AddRectFilled(p0, p1, fillC, tabRound);
                dl->AddRect(p0, p1, outlineC, tabRound, 0, ui::metrics::stroke(1.8f));
            } else {
                // No background by default (blends into headerBg); hover handled by InvisibleButton check.
            }

            // InvisibleButton for interaction.
            ImGui::SetCursorScreenPos(p0);
            char btnId[32];
            snprintf(btnId, sizeof(btnId), "##ws_%d", i);
            bool clicked = ImGui::InvisibleButton(btnId, ImVec2(chipW, tabH));
            bool hovered = ImGui::IsItemHovered();

            if (!isActive && hovered) {
                dl->AddRectFilled(p0, p1, ui::Palette::toU32(ui::palette.buttonHovered), tabRound);
            }

            // Label.
            const float labelX = tx + (chipW - textW) * 0.5f;
            const float labelY = p0.y + (tabH - drawFont->FontSize) * 0.5f;
            ImVec4 textColV(ui::palette.text[0], ui::palette.text[1],
                            ui::palette.text[2], ui::palette.text[3]);
            ImU32 textU32 = ImGui::ColorConvertFloat4ToU32(textColV);
            dl->AddText(drawFont, drawFont->FontSize, ImVec2(labelX, labelY), textU32, kTabs[i].label);

            // Register span for click-through.
            hitLayout.addSpan((int)p0.x, (int)p1.x);

            if (clicked) {
                st.activeWorkspace = kTabs[i].ws;
            }

            tx += chipW + tabGapX;
        }

        // ---- Window caption buttons (right-aligned, unchanged) ------------------
        const float bx = winW - captionZoneW;
        hitLayout.controlsLeft  = (int)bx;
        hitLayout.controlsRight = (int)(bx + captionZoneW);
        // Register caption buttons as spans too (belt-and-suspenders).
        hitLayout.addSpan((int)bx, (int)(bx + captionZoneW));

        ImGui::SetCursorPos(ImVec2(bx, 0.0f));
        if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
        ImGui::SetCursorPos(ImVec2(bx + bw, 0.0f));
        if (windowButton("##max", BTN_MAX, barH, maximized)) {
            if (maximized) glfwRestoreWindow(win);
            else glfwMaximizeWindow(win);
        }
        ImGui::SetCursorPos(ImVec2(bx + bw * 2.0f, 0.0f));
        if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);

        ImGui::EndMainMenuBar();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();  // FramePadding

    barHOut = barH;
}

}  // namespace ui
