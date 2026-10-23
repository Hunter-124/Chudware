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
#include "plm_compare.h"
#include "versions_tab.h"

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/file_dialog.h"
#include "ui/icons.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"

namespace ui {

PlmHubState g_plmHub;  // shared across the hub page TUs (plm_hub.h)

// ---- shared widgets (used by every page) --------------------------------------
//
// The hub keeps these NAMES — hundreds of call sites across the PLM TUs (and
// feature_tree.cpp) spell their headers, empty states and spinners this way — but
// the painting belongs to ui::kit now, so the Project Hub, the Copilot, Parts and
// Reverse Engineering all draw one header, one empty state and one spinner. What
// survives in each wrapper is only the PLM default the kit deliberately does not
// carry.

// The kit's resting row wash (kit.cpp kRowBaseA). plmRowPill cannot call the kit's
// row painter at all (see below), so it mirrors the one number that sets the look.
constexpr float kPlmRowWashA = 0.16f;
// Hub empty states own a whole page, so they sit a third of the way down the pane
// instead of hugging its top edge.
constexpr float kPlmEmptyDropFrac = 0.30f;
// A superseded revision is the neutral tone held back — present, no longer current.
constexpr float kSupersededDimA = 0.62f;

// The kit's chip height (kit.cpp chipGeom: one text line plus U(0.5) top and bottom).
// Needed to centre a pill inside a frame-height row; the kit exposes the corner
// radius but not the height.
inline float kPlmChipH() {
    return ImGui::GetTextLineHeight() + ui::metrics::U(1.0f);
}

void plmRowPill(bool sel, float rows) {
    // Background ONLY: the caller draws the row's content from this same cursor and
    // owns its hit test, so this cannot forward to kit::rowBegin — that reserves the
    // row with its own InvisibleButton and would swallow the caller's clicks. It does
    // take the kit's row geometry and wash, so the hub's pills cannot drift from it.
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + ImGui::GetContentRegionAvail().x,
                    p0.y + ImGui::GetFrameHeight() * rows);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1,
                           kit::rowRound() / ui::metrics::scale(), false, sel,
                           kPlmRowWashA);
}

void plmSectionHeader(const char* label, const char* icon) {
    // `first` suppresses the kit's leading section gap: hub headers open glass panels
    // that already carry their own padding, and every caller places the gap above
    // deliberately. Still no underline rule — that decision started here and the kit
    // now carries it app-wide. The kit's headGap under the label replaces the frame
    // padding the old header spent on AlignTextToFramePadding, so the rhythm holds
    // and callers keep adding exactly the gap they want beneath.
    kit::sectionHeader(label, icon, nullptr, -1, /*first=*/true);
}

// Legacy ASCII stand-in art ("[ ]", "(#)", "[ + ]") predates the icon atlas and was
// typed straight into the call sites. The kit draws a real atlas glyph from a NAME,
// so a bracket placeholder resolves to no icon rather than an unknown-name miss.
static const char* plmEmptyIcon(const char* icon) {
    if (!icon || !icon[0]) return nullptr;
    return (icon[0] == '[' || icon[0] == '(') ? nullptr : icon;
}

void plmEmptyState(const char* icon, const char* msg, const char* sub) {
    ImGui::Dummy(ImVec2(0.0f, ImGui::GetContentRegionAvail().y * kPlmEmptyDropFrac));
    kit::emptyState(plmEmptyIcon(icon), msg, sub);
}

void plmSpinner(const char* prefix) {
    kit::spinner(prefix);
}

void plmError(const std::string& msg) {
    kit::inlineStatus(msg.c_str(), kit::Tone::Err);
}

void plmLifecycleBadge(const std::string& lifecycle, const std::string& rev) {
    // Meaning -> colour goes through the kit's tone table, so a released revision
    // reads the same green here, in the feature tree and in the Copilot. Superseded
    // is the neutral tone held back, i.e. "was true, is not any more".
    const bool released   = (lifecycle == "Released");
    const bool superseded = (lifecycle == "Superseded");
    ImGui::PushStyleColor(ImGuiCol_Text,
        kit::toneVec(released ? kit::Tone::Ok : kit::Tone::Neutral,
                     superseded ? kSupersededDimA : 1.0f));
    if (!rev.empty())
        ImGui::Text("%s \xc2\xb7 %s", rev.c_str(), lifecycle.c_str());
    else
        ImGui::Text("%s", lifecycle.c_str());
    ImGui::PopStyleColor();
}

// A short tag for a doc_type / kind, drawn as the app-wide kit pill. The old form
// was bracket text ("[PART]") because the hub had no chip of its own; the kit's pill
// carries the same information with the boundary drawn instead of typed.
void plmDocTypeTag(const std::string& docType, const std::string& kind) {
    struct T { const char* tag; kit::Tone tone; };
    const std::string k = !docType.empty() ? docType : kind;
    T t{"DOC", kit::Tone::Neutral};
    if (k == "part")             t = {"PART", kit::Tone::Accent};
    else if (k == "assembly")    t = {"ASM",  kit::Tone::Accent};
    else if (k == "drawing")     t = {"DWG",  kit::Tone::Accent};
    else if (k == "ecad")        t = {"PCB",  kit::Tone::Accent};
    else if (k == "cam")         t = {"CAM",  kit::Tone::Accent};
    else if (k == "spec")        t = {"SPEC", kit::Tone::Warn};
    else if (k == "report")      t = {"RPT",  kit::Tone::Neutral};
    else if (k == "datasheet")   t = {"DATA", kit::Tone::Neutral};
    else if (k == "spreadsheet") t = {"XLS",  kit::Tone::Neutral};
    else if (k == "dataset")     t = {"CSV",  kit::Tone::Neutral};
    else if (k == "image")       t = {"IMG",  kit::Tone::Neutral};
    else if (k == "sim_study")   t = {"SIM",  kit::Tone::Neutral};
    else if (k == "software")    t = {"CODE", kit::Tone::Accent};
    else if (k == "linked_repo") t = {"LINK", kit::Tone::Accent};
    // Centre the pill in a frame-height row so it sits level with the path text and
    // controls beside it (the pill is shorter than a framed widget).
    const float drop = (ImGui::GetFrameHeight() - kPlmChipH()) * 0.5f;
    if (drop > 0.0f) ImGui::SetCursorPosY(ImGui::GetCursorPosY() + drop);
    kit::chip(t.tag, t.tone);
}

// ---- rail ---------------------------------------------------------------------

namespace {

struct RailItem { PlmPage page; const char* label; const char* icon; };
// The rail's destinations. Files still folds in History + Releases side panels,
// but Releases is ALSO reachable directly — the lifecycle board deserves the full
// pane. Planning folds in Milestones and Issues; Team & Sync has its own tab.
static const RailItem kRail[] = {
    {PlmPage::Overview, "Overview", "app"},
    {PlmPage::Files,    "Files",    "body_folder"},
    {PlmPage::Versions, "Versions", "history"},
    {PlmPage::Compare,  "Compare",  "split"},
    {PlmPage::Releases, "Releases", "ok_badge"},
    {PlmPage::Planning, "Planning", "plan_board"},
    {PlmPage::Team,     "Team",     "team"},
    {PlmPage::Graph,    "Graph",    "mesh"},
    {PlmPage::Git,      "Git",      "git"},
    {PlmPage::Settings, "Settings", "settings"},
};

// One horizontal PROJECT nav button: flat button surface (the same palette.button /
// palette.border face ui::kit's popover triggers use — no drop shadow, so the strip
// matches every other workspace's controls) with a liquid-glass accent wash when
// selected/hovered, icon + label, and an optional count badge in its OWN rounded pill
// inset from the button's right edge. `compact` sheds the label (icon + badge only;
// the label moves to a tooltip) so the strip degrades instead of clipping when the
// pane is narrow. Drives g_plmHub.page on click.
struct NavMetrics {
    float h, iconSz, padX, iconGap, badgePadX, badgeGap, badgeInset;
};

static NavMetrics navMetrics() {
    const float S = ui::uiScale;
    return {ImGui::GetFrameHeight() * 1.16f, 15 * S, 10 * S, 7 * S, 7 * S, 9 * S, 11 * S};
}

static float navBadgePillW(int badge, const NavMetrics& m, char (&bbuf)[16]) {
    bbuf[0] = '\0';
    if (badge <= 0) return 0.0f;
    std::snprintf(bbuf, sizeof(bbuf), "%d", badge);
    return ImGui::CalcTextSize(bbuf).x + m.badgePadX * 2;
}

static float navButtonWidth(const RailItem& r, int badge, bool compact) {
    const NavMetrics m = navMetrics();
    char bbuf[16];
    const float pillW = navBadgePillW(badge, m, bbuf);
    const float body  = compact ? 0.0f : m.iconGap + ImGui::CalcTextSize(r.label).x;
    return m.padX + m.iconSz + body +
           (badge > 0 ? m.badgeGap + pillW + m.badgeInset : m.padX);
}

static void drawNavButton(const RailItem& r, int badge, bool compact) {
    const float S = ui::uiScale;
    ImGui::PushID((int)r.page);
    const bool sel = (g_plmHub.page == r.page);

    const NavMetrics m = navMetrics();
    const float h = m.h;
    char bbuf[16];
    const float badgePillW = navBadgePillW(badge, m, bbuf);
    const float w = navButtonWidth(r, badge, compact);

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + w, p0.y + h);
    if (ImGui::InvisibleButton("##nav", ImVec2(w, h)))
        g_plmHub.page = r.page;
    const bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    if (compact && hov) {
        if (badge > 0) ImGui::SetTooltip("%s (%d)", r.label, badge);
        else           ImGui::SetTooltip("%s", r.label);
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float rnd = 9.0f * S;

    // Flat button face + hairline border — shadowless, like kit buttons.
    dl->AddRectFilled(p0, p1, Palette::toU32(palette.button), rnd);
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
    float borderCol[4];
    Palette::withAlpha(sel ? palette.accent : palette.border, borderCol, sel ? 0.90f : 0.55f);
    dl->AddRect(p0, p1, Palette::toU32(borderCol), rnd, 0, ui::metrics::stroke(1.0f));

    const ImU32 tcu = sel ? Palette::toU32(palette.text)
        : ImGui::ColorConvertFloat4ToU32(ImVec4(palette.textDim[0], palette.textDim[1],
                                                palette.textDim[2], 1.0f));
    float x = p0.x + m.padX;
    ui::drawIconGlass(dl, r.icon, ImVec2(x + m.iconSz * 0.5f, p0.y + h * 0.5f),
                      m.iconSz, tcu, 0);
    if (!compact) {
        x += m.iconSz + m.iconGap;
        ImVec2 ts = ImGui::CalcTextSize(r.label);
        dl->AddText(ImVec2(x, p0.y + (h - ts.y) * 0.5f), tcu, r.label);
    }

    if (badge > 0) {
        ImVec2 bs = ImGui::CalcTextSize(bbuf);
        ImVec2 bp1(p1.x - m.badgeInset, p0.y + (h - bs.y) * 0.5f - 3 * S);
        ImVec2 bp0(bp1.x - badgePillW, bp1.y);
        bp1.y = bp0.y + bs.y + 6 * S;
        // The count sits ON the warn fill, so its ink is derived from that fill rather
        // than assumed dark — the light theme's warn is bright enough to swallow it.
        const ImVec4 fill(palette.statusWarn[0], palette.statusWarn[1],
                          palette.statusWarn[2], 0.90f);
        dl->AddRectFilled(bp0, bp1, ImGui::GetColorU32(fill), (bs.y + 6 * S) * 0.5f);
        dl->AddText(ImVec2(bp0.x + m.badgePadX, bp0.y + 3 * S),
                    ImGui::GetColorU32(ui::maxContrastTextOn(fill)), bbuf);
    }
    ImGui::PopID();
}

// Horizontal PROJECT nav strip above the page body (replaces the old side rail —
// frees the dead vertical column). The active-project folder switcher used to live
// as a right-aligned button on this strip; it now lives in the title bar (a
// persistent, always-visible status control) — see ui::drawTitleBar /
// drawActiveProjectControl in title_bar.cpp — so this strip is rail buttons only.
//
// The strip never clips: when the labelled buttons do not fit `availW`, labels are
// shed right-to-left (the selected page keeps its label longest) until the row fits;
// if even the all-icon row is too wide, buttons wrap onto further rows.
static void drawTopNav(AppState& st, float availW) {
    (void)st;
    const float S = ui::uiScale;
    const float gap = 6 * S;
    constexpr int kN = (int)(sizeof(kRail) / sizeof(kRail[0]));

    int   idx[kN], badge[kN], n = 0;
    bool  compact[kN] = {};
    float fullW[kN], iconW[kN];
    float total = 0.0f;
    for (int i = 0; i < kN; ++i) {
        // Degrade: hide Graph when the worker lacks cad_graph_* RPCs.
        if (kRail[i].page == PlmPage::Graph && !plmGraphAvailable()) continue;
        if (kRail[i].page == PlmPage::Versions && !plmVersionsAvailable()) continue;
        int b = 0;
        if (kRail[i].page == PlmPage::Files)    b = g_plmHub.overview.dirtyCount;
        if (kRail[i].page == PlmPage::Planning) b = g_plmHub.overview.openIssues;
        idx[n] = i; badge[n] = b;
        fullW[n] = navButtonWidth(kRail[i], b, false);
        iconW[n] = navButtonWidth(kRail[i], b, true);
        total += fullW[n] + (n ? gap : 0.0f);
        ++n;
    }
    // Shed labels from the right, sparing the selected page until last.
    for (int pass = 0; pass < 2 && total > availW; ++pass) {
        for (int k = n - 1; k >= 0 && total > availW; --k) {
            if (compact[k]) continue;
            const bool isSel = kRail[idx[k]].page == g_plmHub.page;
            if (pass == 0 && isSel) continue;
            compact[k] = true;
            total -= fullW[k] - iconW[k];
        }
    }

    const float left = ImGui::GetCursorScreenPos().x;
    float x = left;
    for (int k = 0; k < n; ++k) {
        const float w = compact[k] ? iconW[k] : fullW[k];
        if (k) {
            if (x + gap + w - left <= availW) { ImGui::SameLine(0, gap); x += gap; }
            else x = left;   // wrap: next row starts at the strip's left edge
        }
        drawNavButton(kRail[idx[k]], badge[k], compact[k]);
        x += w;
    }
}

// ---- guided tour ----------------------------------------------------------------
//
// A kit card walking the basic loop: choose a project -> save a design ->
// checkpoint -> release. Each step reports its REAL observed state ("Done" only
// when the cached hub data shows it happened) and names any unmet prerequisite —
// the tour never pretends a step happened. Next/Back/Skip only navigate.
static bool s_tourOn = false;
static int  s_tourStep = 0;
constexpr int kTourSteps = 4;

static void drawGuidedTour(AppState& st) {
    auto& H = g_plmHub;
    const bool haveProject = !st.plmProjectPath().empty();
    if (haveProject) plmEnsureItems(st);   // the release step reads item lifecycles

    bool released = false;
    for (const auto& it : H.items)
        if (it.lifecycle == "Released") { released = true; break; }
    bool checkpointed = false;
    for (const auto& e : H.overview.recent)
        if (e.kind != "auto") { checkpointed = true; break; }
    const bool haveFiles = haveProject && H.overview.itemsCount > 0;

    struct Step {
        const char* title; const char* icon; const char* teach;
        bool done;
        const char* prereq;   // unmet prerequisite ("" = ready to do now)
    };
    const Step steps[kTourSteps] = {
        {"Choose or create a project", "body_folder",
         "A project is a folder — pick one with the folder button in the title "
         "bar, or reuse a recent project from the opening page.",
         haveProject, ""},
        {"Save or open a design", "open",
         "Open or save a part, assembly or document under the project folder — "
         "it is tracked from then on (see it on the Files page).",
         haveFiles, haveProject ? "" : "Needs a project first."},
        {"Save a checkpoint", "save",
         "A checkpoint records the whole project as a version you can compare "
         "and restore — use the box on the Overview page.",
         haveProject && checkpointed,
         haveFiles ? "" : "Needs a project with a tracked design or document."},
        {"Release", "ok_badge",
         "Releasing stamps the next revision and freezes it; superseded "
         "revisions stay in History.",
         haveProject && released,
         checkpointed ? "" : "Needs a design with recorded history."},
    };
    s_tourStep = std::min(std::max(s_tourStep, 0), kTourSteps - 1);
    const Step& stp = steps[s_tourStep];

    (void)kit::cardBegin("##plm_tour", "Guided tour", "app");
    char cap[32];
    std::snprintf(cap, sizeof(cap), "Step %d of %d", s_tourStep + 1, kTourSteps);
    kit::progressBar((s_tourStep + 1) / (float)kTourSteps, cap);
    kit::sectionHeader(stp.title, stp.icon, nullptr, -1, /*first=*/true);
    kit::dimText(stp.teach);
    if (stp.prereq[0]) kit::inlineStatus(stp.prereq, kit::Tone::Warn);
    kit::chip(stp.done ? "Done" : "Not done yet",
              stp.done ? kit::Tone::Ok : kit::Tone::Neutral, /*dot=*/true);
    // A shortcut to where this step's action actually lives.
    const char* go = nullptr;
    PlmPage goPage = PlmPage::Overview;
    if (s_tourStep == 1) { go = "Go to Files";    goPage = PlmPage::Files; }
    if (s_tourStep == 2) { go = "Go to Overview"; goPage = PlmPage::Overview; }
    if (s_tourStep == 3) { go = "Go to Releases"; goPage = PlmPage::Releases; }
    if (go && haveProject) {
        ImGui::SameLine();
        if (kit::secondaryButton(go)) H.page = goPage;
    }
    kit::gapRow();
    if (kit::secondaryButton("Back", s_tourStep > 0)) --s_tourStep;
    ImGui::SameLine();
    if (s_tourStep + 1 < kTourSteps) {
        if (kit::primaryButton("Next")) ++s_tourStep;
    } else if (kit::primaryButton("Finish")) {
        s_tourOn = false;
        s_tourStep = 0;
    }
    ImGui::SameLine();
    if (kit::secondaryButton("Skip tour")) { s_tourOn = false; s_tourStep = 0; }
    kit::cardEnd();
}

// ---- onboarding (no project resolvable) ---------------------------------------

static void drawOnboarding(AppState& st) {
    const float S = ui::uiScale;
    const bool haveWorkspace = !st.workspaceDir.empty();
    ImGui::Dummy(ImVec2(0.0f, ImGui::GetContentRegionAvail().y * 0.08f));

    // A centred, readable column instead of rows stretched across the whole pane.
    const float availW = ImGui::GetContentRegionAvail().x;
    const float colW = std::min(availW, 560.0f * S);
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (availW - colW) * 0.5f);
    ImGui::BeginChild("##plm_onboard", ImVec2(colW, 0), ImGuiChildFlags_AutoResizeY,
                      ImGuiWindowFlags_NoScrollbar);

    // The "use my workspace" offer IS the empty state's action, so it rides the
    // kit's centred action slot instead of a hand-tinted button under it.
    if (kit::emptyState(
            "open", "Open a project folder",
            "Pick a folder with the folder button (top-right). A folder that "
            "already holds a project is reused — history is never reinitialized.",
            haveWorkspace ? "Use my workspace folder" : nullptr))
        setActiveProjectDir(st, st.workspaceDir);

    // The whole model, one sentence each.
    kit::gapSection();
    struct Lesson { const char* name; const char* line; };
    static const Lesson kLessons[] = {
        {"Project", "A project is a folder — Chudware keeps its history in a "
                    "hidden vault inside it."},
        {"History", "Every save and checkpoint becomes a version you can compare "
                    "and restore."},
        {"Release", "Releasing stamps the design as a frozen revision; later work "
                    "supersedes it without losing anything."},
    };
    for (const auto& l : kLessons) {
        kit::chip(l.name, kit::Tone::Accent);
        ImGui::SameLine();
        kit::dimText(l.line);
    }

    // Recent projects — one click back into anything opened before.
    if (!st.recentProjects.empty()) {
        kit::sectionHeader("Recent projects", "history");
        std::string activate;   // applied after the loop: activation reorders the list
        for (const auto& p : st.recentProjects) {
            std::string name = p;
            while (!name.empty() && (name.back() == '/' || name.back() == '\\'))
                name.pop_back();
            const size_t cut = name.find_last_of("/\\");
            if (cut != std::string::npos) name = name.substr(cut + 1);
            kit::RowStyle rs;
            rs.icon = "body_folder";
            rs.secondary = p.c_str();
            ImGui::PushID(p.c_str());
            if (kit::row("##recent_proj", name.c_str(), rs)) activate = p;
            ImGui::PopID();
        }
        if (!activate.empty()) setActiveProjectDir(st, activate);
    }

    kit::gapSection();
    if (kit::secondaryButton("Start the guided tour")) {
        s_tourOn = true;
        s_tourStep = 0;
    }
    ImGui::EndChild();
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void plmSelectGitPage() { g_plmHub.page = PlmPage::Git; }
void plmSelectPlanningPage() { g_plmHub.page = PlmPage::Planning; }
void plmSelectTeamPage() { g_plmHub.page = PlmPage::Team; }
void plmSelectOverviewPage() { g_plmHub.page = PlmPage::Overview; }
void plmSelectFilesPage() { g_plmHub.page = PlmPage::Files; }
void plmSelectGraphPage() { g_plmHub.page = PlmPage::Graph; }
void plmSelectVersionsPage() { g_plmHub.page = PlmPage::Versions; }
void plmSelectComparePage() { g_plmHub.page = PlmPage::Compare; }
void plmSelectReleasesPage() { g_plmHub.page = PlmPage::Releases; }

void plmStartGuidedTour() {
    s_tourOn = true;
    s_tourStep = 0;
}

// Whatever page is showing, is it still waiting on data? Only the fetches that
// gate visible content count — this is the headless capture's "settled" signal.
bool plmBusy() {
    const PlmHubState& H = g_plmHub;
    if (H.itemsLoading || H.histLoading || H.versionsGraph.loading || H.outsideLoading) return true;
    switch (H.page) {
        case PlmPage::Overview: return H.ovLoading || H.gitLoading || H.planLoading;
        case PlmPage::Planning:
        case PlmPage::Issues:   return H.planLoading;
        case PlmPage::Releases: return H.gitLoading;
        case PlmPage::Git:      return H.gitLoading || H.gitDetailsLoading;
        case PlmPage::Team:     return H.teamLoading;
        case PlmPage::Compare:  return plmCompareBusy();
        case PlmPage::Versions: return H.versionsGraph.loading;
        default:                return false;
    }
}

void drawPlmWorkspace(AppState& st) {
    const float S = ui::uiScale;

    if (!st.workerStarted) {
        plmEmptyState("warning_badge", "Project history unavailable",
                      "The Chudware service is not running.");
        return;
    }

    // Follow the resolved project folder; a change resets all caches.
    std::string proj = st.plmProjectPath();
    if (proj != g_plmHub.projectDir) {
        g_plmHub.projectDir = proj;
        g_plmHub.outsideLoading = false;
        g_plmHub.outsideErr.clear();
        plmInvalidate();
        plmGraphOnProjectChanged();
        plmVersionsOnProjectChanged();
    }
    // The guided tour rides above whatever the workspace shows — including the
    // onboarding page, where its first step (choose a project) happens.
    if (s_tourOn) {
        const float TM = 10.0f * S;
        ImGui::Dummy(ImVec2(0, 3 * S));
        ImGui::Indent(TM);
        ImGui::BeginChild("##plm_tour_band", ImVec2(-TM, 0),
                          ImGuiChildFlags_AutoResizeY,
                          ImGuiWindowFlags_NoScrollbar |
                          ImGuiWindowFlags_NoScrollWithMouse);
        drawGuidedTour(st);
        ImGui::EndChild();
        ImGui::Unindent(TM);
    }
    if (proj.empty()) { drawOnboarding(st); return; }

    // If an optional RPC is gone, fall back to Overview.
    if ((g_plmHub.page == PlmPage::Graph && !plmGraphAvailable()) ||
        (g_plmHub.page == PlmPage::Versions && !plmVersionsAvailable()))
        g_plmHub.page = PlmPage::Overview;

    // keep the dashboard aggregate fresh (drives rail badges + Overview)
    plmEnsureOverview(st);

    // Outer margin so neither the nav nor the page hugs the main window border.
    const float M = 10.0f * S;
    ImGui::Dummy(ImVec2(0, 3 * S));  // tighter top margin — nav sits closer to the box top
    ImGui::Indent(M);                // left margin

    // Horizontal PROJECT nav above the page views (was a left side rail). It gets
    // the same right margin the page body keeps (-M below) so it never runs past it.
    drawTopNav(st, ImGui::GetContentRegionAvail().x - M);
    ImGui::Dummy(ImVec2(0, 2 * S));  // tighter gap between nav and page body
    if (g_plmHub.outsideLoading)
        kit::spinner("Recording outside changes");
    kit::inlineStatus(g_plmHub.outsideErr.c_str(), kit::Tone::Warn);

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
    // Tighter vertical rhythm across every page body (vertical compression).
    // Keep the horizontal item spacing; only squeeze the y gap.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 3.0f * S));
    switch (g_plmHub.page) {
        case PlmPage::Overview: plmDrawOverviewPage(st); break;
        case PlmPage::Files:    plmDrawFilesPage(st);    break;
        case PlmPage::Versions: plmDrawVersionsPage(st); break;
        case PlmPage::Compare:  plmDrawComparePage(st);  break;
        case PlmPage::Releases: plmDrawReleasesPage(st); break;
        case PlmPage::Planning: plmDrawPlanningPage(st); break;
        case PlmPage::Issues:   plmDrawIssuesPage(st);   break;
        case PlmPage::Team:     plmDrawTeamPage(st);     break;
        case PlmPage::Git:      plmDrawGitPage(st);      break;
        case PlmPage::Graph:    plmDrawGraphPage(st);    break;
        case PlmPage::Settings: plmDrawSettingsPage(st); break;
    }
    // Once the visible page's own fetches have landed, warm the Graph page in
    // the background so switching to it shows the graph instead of a spinner.
    if (g_plmHub.page != PlmPage::Graph && plmGraphAvailable() && !plmBusy())
        plmGraphPrefetch(st);
    ImGui::PopStyleVar();  // ItemSpacing
    ImGui::EndChild();
    ImGui::Unindent(M);
}

}  // namespace ui
