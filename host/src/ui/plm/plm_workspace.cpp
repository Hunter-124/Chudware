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
#include "graph_tab.h"

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/file_dialog.h"
#include "ui/icons.h"
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

void plmSectionHeader(const char* label, const char* icon) {
    const float S = ui::uiScale;
    const float h = ImGui::GetFrameHeight();
    if (icon && icon[0]) {
        ImVec2 c = ImGui::GetCursorScreenPos();
        const float isz = 17 * S;
        ui::drawIconGlass(ImGui::GetWindowDrawList(), icon,
                          ImVec2(c.x + isz * 0.5f, c.y + h * 0.5f), isz,
                          Palette::toU32(palette.accent), 0);
        ImGui::Dummy(ImVec2(isz + 9 * S, h));
        ImGui::SameLine();
    }
    // Accent title, no underline — the old "underscore" rule read as clutter and
    // clashed with the boxed glass panels. Icon (if any) is centred on the title.
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    // No trailing Spacing() here — callers add exactly the gap they want (or none),
    // instead of every title carrying an extra baked-in ItemSpacing underneath it.
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
    ImGui::AlignTextToFramePadding();  // sit level with the path text / controls beside it
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(t.r, t.g, t.b, 1.0f));
    ImGui::Text("[%s]", t.tag);
    ImGui::PopStyleColor();
}

// ---- rail ---------------------------------------------------------------------

namespace {

struct RailItem { PlmPage page; const char* label; const char* icon; };
// The hub collapsed to four destinations: Files folds in History + Releases;
// Planning folds in Milestones, Issues and Team & Sync (see plm_pages.cpp).
static const RailItem kRail[] = {
    {PlmPage::Overview, "Overview", "app"},
    {PlmPage::Files,    "Files",    "body_folder"},
    {PlmPage::Planning, "Planning", "plan_board"},
    {PlmPage::Graph,    "Graph",    "mesh"},
    {PlmPage::Git,      "Git",      "git"},
    {PlmPage::Settings, "Settings", "settings"},
};

// One horizontal PROJECT nav button: liquid-glass gradient fill (accent -> accentDim
// top-to-bottom) when selected/hovered over the shared frosted base, icon + label,
// and an optional count badge that sits in its OWN rounded pill inset from the
// button's right edge. Drives g_plmHub.page on click.
static void drawNavButton(const RailItem& r, int badge) {
    const float S = ui::uiScale;
    ImGui::PushID((int)r.page);
    const bool sel = (g_plmHub.page == r.page);

    const float h       = ImGui::GetFrameHeight() * 1.16f;
    const float iconSz  = 15 * S;
    const float padX    = 10 * S;
    const float iconGap = 7 * S;
    const float textW   = ImGui::CalcTextSize(r.label).x;

    // Badge pill metrics — its own background + extra right padding from the edge.
    char bbuf[16] = {};
    float badgePillW = 0.0f;
    const float badgePadX  = 7 * S;   // inside the pill
    const float badgeGap   = 9 * S;   // between label and pill
    const float badgeInset = 11 * S;  // pill -> button right edge ("more padding")
    if (badge > 0) {
        std::snprintf(bbuf, sizeof(bbuf), "%d", badge);
        badgePillW = ImGui::CalcTextSize(bbuf).x + badgePadX * 2;
    }

    const float w = padX + iconSz + iconGap + textW +
                    (badge > 0 ? badgeGap + badgePillW + badgeInset : padX);

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + w, p0.y + h);
    if (ImGui::InvisibleButton("##nav", ImVec2(w, h)))
        g_plmHub.page = r.page;
    const bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float rnd = 9.0f * S;

    // Frosted glass base for every button (translucent + specular + border).
    ui::glassBackground(dl, p0, p1, 9.0f);
    // Liquid-glass gradient wash. The highlight must cover the FULL rounded button
    // (corners included): a rounded accent base, a rounded-top bright strip that
    // owns the top corners, then a full-width vertical sheen below the corner radius
    // (its square edges sit in the interior, never past the rounding).
    if (sel || hov) {
        const float aTop = sel ? (held ? 0.95f : 0.82f) : 0.30f;
        const float aBot = sel ? 0.48f : 0.14f;
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.accentDim[0], palette.accentDim[1], palette.accentDim[2], aBot)), rnd);
        const ImU32 cTop = ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], aTop));
        const ImU32 cFade = ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], 0.0f));
        dl->AddRectFilled(p0, ImVec2(p1.x, p0.y + rnd), cTop, rnd, ImDrawFlags_RoundCornersTop);
        dl->AddRectFilledMultiColor(ImVec2(p0.x, p0.y + rnd),
                                    ImVec2(p1.x, p0.y + h * 0.62f),
                                    cTop, cTop, cFade, cFade);
    }

    const ImU32 tcu = sel ? Palette::toU32(palette.text)
        : ImGui::ColorConvertFloat4ToU32(ImVec4(palette.textDim[0], palette.textDim[1],
                                                palette.textDim[2], 1.0f));
    float x = p0.x + padX;
    ui::drawIconGlass(dl, r.icon, ImVec2(x + iconSz * 0.5f, p0.y + h * 0.5f),
                      iconSz, tcu, 0);
    x += iconSz + iconGap;
    ImVec2 ts = ImGui::CalcTextSize(r.label);
    dl->AddText(ImVec2(x, p0.y + (h - ts.y) * 0.5f), tcu, r.label);

    if (badge > 0) {
        ImVec2 bs = ImGui::CalcTextSize(bbuf);
        ImVec2 bp1(p1.x - badgeInset, p0.y + (h - bs.y) * 0.5f - 3 * S);
        ImVec2 bp0(bp1.x - badgePillW, bp1.y);
        bp1.y = bp0.y + bs.y + 6 * S;
        dl->AddRectFilled(bp0, bp1, ImGui::GetColorU32(ImVec4(palette.statusWarn[0],
            palette.statusWarn[1], palette.statusWarn[2], 0.90f)), (bs.y + 6 * S) * 0.5f);
        dl->AddText(ImVec2(bp0.x + badgePadX, bp0.y + 3 * S), IM_COL32(20, 22, 26, 255), bbuf);
    }
    ImGui::PopID();
}

// Horizontal PROJECT nav strip above the page body (replaces the old side rail —
// frees the dead vertical column). The active-project folder switcher used to live
// as a right-aligned button on this strip; it now lives in the title bar (a
// persistent, always-visible status control) — see ui::drawTitleBar /
// drawActiveProjectControl in title_bar.cpp — so this strip is rail buttons only.
static void drawTopNav(AppState& st) {
    (void)st;
    const float S = ui::uiScale;
    const float gap = 6 * S;
    for (int i = 0; i < (int)(sizeof(kRail) / sizeof(kRail[0])); ++i) {
        // Degrade: hide Graph when the worker lacks cad_graph_* RPCs.
        if (kRail[i].page == PlmPage::Graph && !plmGraphAvailable()) continue;
        if (i) ImGui::SameLine(0, gap);
        int badge = 0;
        if (kRail[i].page == PlmPage::Files)    badge = g_plmHub.overview.dirtyCount;
        if (kRail[i].page == PlmPage::Planning) badge = g_plmHub.overview.openIssues;
        drawNavButton(kRail[i], badge);
    }
}

// ---- onboarding (no project resolvable) ---------------------------------------

static void drawOnboarding(AppState& st) {
    plmEmptyState(
        "[ + ]",
        "Open a project folder",
        "Use the folder button (top-right) to pick an existing project folder. "
        "If the folder already has a .aicadvault, it is reused — history is never "
        "reinitialized. Opening the same folder again is safe and idempotent. "
        "You can also open or save a document under a folder to start history there.");
    const float S = ui::uiScale;
    float bw = 220 * S;
    ImGui::SetCursorPosX((ImGui::GetContentRegionAvail().x - bw) * 0.5f);
    if (!st.workspaceDir.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        if (ImGui::Button("Use my workspace folder", ImVec2(bw, 0))) {
            setActiveProjectDir(st, st.workspaceDir);
        }
        ImGui::PopStyleColor();
    }
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void plmSelectGitPage() { g_plmHub.page = PlmPage::Git; }
void plmSelectPlanningPage() { g_plmHub.page = PlmPage::Planning; }
void plmSelectOverviewPage() { g_plmHub.page = PlmPage::Overview; }
void plmSelectFilesPage() { g_plmHub.page = PlmPage::Files; }
void plmSelectGraphPage() { g_plmHub.page = PlmPage::Graph; }

void drawPlmWorkspace(AppState& st) {
    const float S = ui::uiScale;

    if (!st.workerStarted) {
        plmEmptyState("[V]", "Project history unavailable",
                      "The Chudware service is not running.");
        return;
    }

    // Follow the resolved project folder; a change resets all caches.
    std::string proj = st.plmProjectPath();
    if (proj != g_plmHub.projectDir) {
        g_plmHub.projectDir = proj;
        plmInvalidate();
        plmGraphOnProjectChanged();
    }
    if (proj.empty()) { drawOnboarding(st); return; }

    // If Graph was selected but the RPC is gone, fall back to Overview.
    if (g_plmHub.page == PlmPage::Graph && !plmGraphAvailable())
        g_plmHub.page = PlmPage::Overview;

    // keep the dashboard aggregate fresh (drives rail badges + Overview)
    plmEnsureOverview(st);

    // Outer margin so neither the nav nor the page hugs the main window border.
    const float M = 10.0f * S;
    ImGui::Dummy(ImVec2(0, 3 * S));  // tighter top margin — nav sits closer to the box top
    ImGui::Indent(M);                // left margin

    // Horizontal PROJECT nav above the page views (was a left side rail).
    drawTopNav(st);
    ImGui::Dummy(ImVec2(0, 2 * S));  // tighter gap between nav and page body

    // One generous, uniform content margin for every page body — so the Overview,
    // Planning band, Team & Sync and the board all share the same left edge instead
    // of each element hugging the border by a different amount.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(16 * S, 6 * S));
    // -M width/height leaves a matching right + bottom margin from the window edge.
    // The page body itself never scrolls — overflow lives in each page's inner
    // item-list children (kanban columns, Files/History/Releases lists, etc.).
    ImGui::BeginChild("##plm_page", ImVec2(-M, -M), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    // Tighter vertical rhythm across every page body (Hunter: "we NEED vertical
    // compression"). Keep the horizontal item spacing; only squeeze the y gap.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 3.0f * S));
    switch (g_plmHub.page) {
        case PlmPage::Overview: plmDrawOverviewPage(st); break;
        case PlmPage::Files:    plmDrawFilesPage(st);    break;
        case PlmPage::History:  plmDrawHistoryPage(st);  break;
        case PlmPage::Releases: plmDrawReleasesPage(st); break;
        case PlmPage::Planning: plmDrawPlanningPage(st); break;
        case PlmPage::Issues:   plmDrawIssuesPage(st);   break;
        case PlmPage::Team:     plmDrawTeamPage(st);     break;
        case PlmPage::Git:      plmDrawGitPage(st);      break;
        case PlmPage::Graph:    plmDrawGraphPage(st);    break;
        case PlmPage::Settings: plmDrawSettingsPage(st); break;
    }
    ImGui::PopStyleVar();  // ItemSpacing
    ImGui::EndChild();
    ImGui::Unindent(M);
}

}  // namespace ui
