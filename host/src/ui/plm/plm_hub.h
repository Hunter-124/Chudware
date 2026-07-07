// PLM Project Hub — internal shared header (not the public API).
//
// Shared state + declarations across the hub page TUs (plm_workspace.cpp shell,
// plm_state.cpp fetchers, plm_pages.cpp page bodies). The single public entry is
// drawPlmWorkspace() in plm_workspace.h. All data is cached on g_plmHub and
// filled by async RPC (no sync worker call in a draw path).
#pragma once

#include <string>
#include <vector>

struct AppState;
namespace core { struct RpcResult; }

namespace ui {

// The worker wraps every dispatched method as a JSON-RPC {ok:true, result:<method dict>};
// a method-level failure lives as {ok:false, reason, hint} INSIDE result. So RpcResult::ok is
// transport-only — real success is r.result["ok"]. plmOk unwraps both: returns true only on a
// genuine method success, else fills *hint* with the plain-language reason.
bool        plmOk(const core::RpcResult& r, std::string& hint);
std::string plmReason(const core::RpcResult& r);   // the machine reason code ("" if none/ok)

enum class PlmPage {
    Overview, Files, History, Releases, Planning, Issues, Team, Settings
};

// ---- cached row shapes (parsed from the plm_* envelopes) ----------------------

struct PlmItemRow {
    std::string itemId, path, kind, docType, partNumber, rev, lifecycle, workingRev;
    bool dirty = false;
};

struct PlmEntryRow {
    std::string commit, kind, scope, authoredAt, author, message, summary;
};

struct PlmTaskRow {
    std::string id, title, status, priority, assignee, milestoneId;
};

struct PlmIssueRow {
    std::string id, title, severity, state, assignee;
};

struct PlmMilestoneRow {
    std::string id, title, status, due;
};

struct PlmOverview {
    int itemsCount = 0, dirtyCount = 0, openTasks = 0, openIssues = 0, openMilestones = 0;
    std::string aiMode;
    std::vector<PlmItemRow>  dirtyItems;
    std::vector<PlmEntryRow> recent;
    std::vector<PlmTaskRow>  tasks;       // open tasks
    std::string nextMilestone;            // title of the next open milestone ("" = none)
};

// ---- the hub state ------------------------------------------------------------

struct PlmHubState {
    PlmPage     page = PlmPage::Overview;
    std::string projectDir;               // the folder currently shown (mirrors plmProjectPath)
    int         generation = 0;           // bumped on any mutation → stale caches refetch

    // overview
    int         ovGen = -1; bool ovLoading = false; std::string ovErr;
    PlmOverview overview;

    // items (Files page)
    int         itemsGen = -1; bool itemsLoading = false; std::string itemsErr;
    std::vector<PlmItemRow> items;
    std::string selectedItem;
    int         kindFilter = 0;           // 0 = all; else index into a filter list

    // history (History page)
    int         histGen = -1; bool histLoading = false; std::string histErr;
    int         histScope = 0;            // 0 design | 1 planning | 2 all
    std::vector<PlmEntryRow> history;
    std::string selectedEntry;

    // planning (Planning/Issues pages)
    int         planGen = -1; bool planLoading = false; std::string planErr;
    std::vector<PlmTaskRow>      allTasks;
    std::vector<PlmIssueRow>     issues;
    std::vector<PlmMilestoneRow> milestones;

    // settings (part-number scheme, ai mode)
    int         setGen = -1; bool setLoading = false; std::string setErr;
    std::string partPrefix, aiMode;

    // transient action state / input buffers
    char        checkpointMsg[256] = {};
    bool        busy = false;             // a mutation is in flight
    std::string actionErr, actionOk;
    char        newTaskTitle[192] = {};
    char        newIssueTitle[192] = {};
    char        trackPath[512] = {};
    char        linkUrl[512] = {};
    char        linkName[128] = {};
    char        linkCheckout[512] = {};  // optional local checkout of the linked repo
    char        prefixBuf[64] = {};
    char        msEditTitle[160] = {};   // milestone inline-edit buffers
    char        msEditDue[32] = {};
    std::string msEditId;                // milestone currently being edited ("" = none)
};

extern PlmHubState g_plmHub;

// ---- shared widgets (defined in plm_workspace.cpp) ----------------------------

void plmRowPill(bool sel = false, float rows = 1.0f);
void plmSectionHeader(const char* label);
void plmEmptyState(const char* icon, const char* msg, const char* sub = nullptr);
void plmSpinner(const char* prefix);
void plmError(const std::string& msg);
void plmLifecycleBadge(const std::string& lifecycle, const std::string& rev = "");
void plmDocTypeTag(const std::string& docType, const std::string& kind);

// ---- state helpers (defined in plm_state.cpp) ---------------------------------

void        plmInvalidate();                 // bump generation → all caches refetch
std::string plmProjectName(AppState& st);    // display name of the current project folder
void        plmEnsureOverview(AppState& st);
void        plmEnsureItems(AppState& st);
void        plmEnsureHistory(AppState& st);
void        plmEnsurePlanning(AppState& st);
void        plmEnsureSettings(AppState& st);
// convenience: build the {"path": …} param object for the current project.

// ---- page bodies (defined in plm_pages.cpp) -----------------------------------

void plmDrawOverviewPage(AppState& st);
void plmDrawFilesPage(AppState& st);
void plmDrawHistoryPage(AppState& st);
void plmDrawReleasesPage(AppState& st);
void plmDrawPlanningPage(AppState& st);
void plmDrawIssuesPage(AppState& st);
void plmDrawTeamPage(AppState& st);
void plmDrawSettingsPage(AppState& st);

}  // namespace ui
