// PLM Project Hub — full-viewport body for the PLM title-bar tab.
//
// A project = a folder + its hidden vault (contract plm.md §1). This workspace is
// a hub: a left rail of pages (Overview / Files / History / Releases / Planning /
// Issues / Team / Settings) over the one project folder resolved by
// AppState::plmProjectPath(). The basic loop — see files, checkpoint, release —
// is front and centre on Overview; power tools sit one click away.
//
// All backend calls are async (st.rpc + per-frame drain) and pass {"path":
// st.plmProjectPath()} — the vault worker methods are path-addressed. No sync
// worker call ever runs in a draw path (GL-thread rule). No git vocabulary in any
// user-facing string (contract §8.1): Project / History / Checkpoint / Release.
#include "plm_workspace.h"
#include "plm_hub.h"

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui_theme.h"

namespace ui {

PlmHubState g_plmHub;  // shared across the hub page TUs (plm_hub.h)

// ---- shared widgets (used by every page) --------------------------------------

void plmRowPill(bool sel, float rows) {
    float rowH = ImGui::GetFrameHeight() * rows;
    ImVec2 p0  = ImGui::GetCursorScreenPos();
    float  w   = ImGui::GetContentRegionAvail().x;
    ImVec2 p1(p0.x + w, p0.y + rowH);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f,
                           false, sel, sel ? 0.42f : 0.20f);
}

void plmSectionHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::TextUnformatted(label);
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

void plmEmptyState(const char* icon, const char* msg, const char* sub) {
    const float S = ui::uiScale;
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.30f));
    auto centered = [](const char* t, ImVec4 col) {
        float tw = ImGui::CalcTextSize(t).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - tw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(t);
        ImGui::PopStyleColor();
    };
    centered(icon, ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                          palette.textDisabled[2], 0.5f));
    ImGui::Spacing();
    centered(msg, ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    if (sub)
        centered(sub, ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                             palette.textDisabled[2], 0.8f));
    ImGui::Dummy(ImVec2(0, 8.0f * S));
}

void plmSpinner(const char* prefix) {
    static const char* kF[] = {". . .", "o . .", ". o .", ". . o"};
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::Text("%s %s", prefix, kF[(int)(ImGui::GetTime() * 4.0) & 3]);
    ImGui::PopStyleColor();
}

void plmError(const std::string& msg) {
    if (msg.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f));
    ImGui::TextWrapped("%s", msg.c_str());
    ImGui::PopStyleColor();
}

void plmLifecycleBadge(const std::string& lifecycle, const std::string& rev) {
    ImVec4 col;
    const char* label = lifecycle.c_str();
    if (lifecycle == "Released")
        col = ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f);
    else if (lifecycle == "Superseded")
        col = ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                     palette.textDisabled[2], 1.0f);
    else
        col = ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    if (!rev.empty())
        ImGui::Text("%s \xc2\xb7 %s", rev.c_str(), label);
    else
        ImGui::Text("%s", label);
    ImGui::PopStyleColor();
}

// A short, coloured tag for a doc_type / kind — no atlas icons (avoids the
// dual-source atlas staging trap); plain bracket tags read clearly.
void plmDocTypeTag(const std::string& docType, const std::string& kind) {
    struct T { const char* tag; float r, g, b; };
    std::string k = !docType.empty() ? docType : kind;
    T t{"DOC", palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    if (k == "part")            t = {"PART", palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "assembly")   t = {"ASM",  palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "drawing")    t = {"DWG",  palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "ecad")       t = {"PCB",  palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "cam")        t = {"CAM",  palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "spec")       t = {"SPEC", palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2]};
    else if (k == "report")     t = {"RPT",  palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "datasheet")  t = {"DATA", palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "spreadsheet")t = {"XLS",  palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "dataset")    t = {"CSV",  palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "image")      t = {"IMG",  palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "sim_study")  t = {"SIM",  palette.textDim[0], palette.textDim[1], palette.textDim[2]};
    else if (k == "software")   t = {"CODE", palette.accent[0], palette.accent[1], palette.accent[2]};
    else if (k == "linked_repo")t = {"LINK", palette.accent[0], palette.accent[1], palette.accent[2]};
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(t.r, t.g, t.b, 1.0f));
    ImGui::Text("[%s]", t.tag);
    ImGui::PopStyleColor();
}

// ---- rail ---------------------------------------------------------------------

namespace {

struct RailItem { PlmPage page; const char* label; };
static const RailItem kRail[] = {
    {PlmPage::Overview, "Overview"},
    {PlmPage::Files,    "Files"},
    {PlmPage::History,  "History"},
    {PlmPage::Releases, "Releases"},
    {PlmPage::Planning, "Planning"},
    {PlmPage::Issues,   "Issues"},
    {PlmPage::Team,     "Team & Sync"},
    {PlmPage::Settings, "Settings"},
};

static void drawRail(AppState& st, float railW, float bottomMargin) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 10 * S));
    ImGui::BeginChild("##plm_rail", ImVec2(railW, -bottomMargin), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    ImGui::TextUnformatted("PROJECT");
    ImGui::PopStyleColor();
    {
        std::string name = plmProjectName(st);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextWrapped("%s", name.empty() ? "(none open)" : name.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8 * S));

    ImDrawList* dl = ImGui::GetWindowDrawList();
    for (const auto& r : kRail) {
        ImGui::PushID((int)r.page);
        bool sel = (g_plmHub.page == r.page);
        const float rowH = ImGui::GetFrameHeight() * 1.4f;
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        float  w  = ImGui::GetContentRegionAvail().x;

        // Full-row clickable button drives selection + hover (the old row only
        // registered clicks on the label glyphs, so most of the pill was dead).
        if (ImGui::InvisibleButton("##row", ImVec2(w, rowH)))
            g_plmHub.page = r.page;
        bool hovered = ImGui::IsItemHovered();

        // Pill background (brightens on hover even when not selected).
        float bgA = sel ? 0.42f : (hovered ? 0.30f : 0.20f);
        ui::glassRowBackground(dl, p0, ImVec2(p0.x + w, p0.y + rowH), 4.0f,
                               false, sel || hovered, bgA);

        // Label — centered horizontally + vertically inside the pill.
        ImVec4 tc = sel
            ? ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f)
            : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f);
        ImVec2 ts = ImGui::CalcTextSize(r.label);
        dl->AddText(ImVec2(p0.x + (w - ts.x) * 0.5f, p0.y + (rowH - ts.y) * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(tc), r.label);

        // Count badge (from cached overview) pinned to the right edge, centered.
        int badge = 0;
        if (r.page == PlmPage::Files)  badge = g_plmHub.overview.dirtyCount;
        if (r.page == PlmPage::Issues) badge = g_plmHub.overview.openIssues;
        if (badge > 0) {
            char b[16]; std::snprintf(b, sizeof(b), "%d", badge);
            ImVec2 bs = ImGui::CalcTextSize(b);
            dl->AddText(ImVec2(p0.x + w - bs.x - 6 * S, p0.y + (rowH - bs.y) * 0.5f),
                        ImGui::ColorConvertFloat4ToU32(ImVec4(palette.statusWarn[0],
                            palette.statusWarn[1], palette.statusWarn[2], 1.0f)),
                        b);
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
    ImGui::EndChild();
}

// ---- onboarding (no project resolvable) ---------------------------------------

static void drawOnboarding(AppState& st) {
    plmEmptyState("[ + ]", "Start a project",
                  "Open or save a document — history begins automatically.");
    const float S = ui::uiScale;
    float bw = 220 * S;
    ImGui::SetCursorPosX((ImGui::GetContentRegionAvail().x - bw) * 0.5f);
    if (!st.workspaceDir.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        if (ImGui::Button("Use my workspace folder", ImVec2(bw, 0))) {
            st.plmProjectDir = st.workspaceDir;
            plmInvalidate();
        }
        ImGui::PopStyleColor();
    }
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void drawPlmWorkspace(AppState& st) {
    const float S = ui::uiScale;

    if (!st.workerStarted) {
        plmEmptyState("[V]", "Project history unavailable",
                      "The AI-CAD service is not running.");
        return;
    }

    // Follow the resolved project folder; a change resets all caches.
    std::string proj = st.plmProjectPath();
    if (proj != g_plmHub.projectDir) {
        g_plmHub.projectDir = proj;
        plmInvalidate();
    }
    if (proj.empty()) { drawOnboarding(st); return; }

    // keep the dashboard aggregate fresh (drives rail badges + Overview)
    plmEnsureOverview(st);

    // Outer margin so neither the rail nor the page hugs the main window border.
    const float M = 10.0f * S;
    ImGui::Dummy(ImVec2(0, M));   // top margin
    ImGui::Indent(M);            // left margin (rail + page shift right)

    const float railW = 190.0f * S;
    drawRail(st, railW, M);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14 * S, 10 * S));
    // -M width/height leaves a matching right + bottom margin from the window edge.
    ImGui::BeginChild("##plm_page", ImVec2(-M, -M), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    switch (g_plmHub.page) {
        case PlmPage::Overview: plmDrawOverviewPage(st); break;
        case PlmPage::Files:    plmDrawFilesPage(st);    break;
        case PlmPage::History:  plmDrawHistoryPage(st);  break;
        case PlmPage::Releases: plmDrawReleasesPage(st); break;
        case PlmPage::Planning: plmDrawPlanningPage(st); break;
        case PlmPage::Issues:   plmDrawIssuesPage(st);   break;
        case PlmPage::Team:     plmDrawTeamPage(st);     break;
        case PlmPage::Settings: plmDrawSettingsPage(st); break;
    }
    ImGui::EndChild();
    ImGui::Unindent(M);
}

}  // namespace ui
