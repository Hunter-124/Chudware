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
    Overview, Files, Versions, Compare, Releases, Planning, Issues, Team, Git, Graph, Settings
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

// ---- Git tab row shapes (parsed from the git_* envelopes) ---------------------

struct PlmGitFileRow {
    std::string path, xy;             // xy = porcelain-v2 status code ("??" untracked)
    bool staged = false, unstaged = false, untracked = false;
};

struct PlmGitCommitRow {
    std::string hash, author, date, subject;
};

struct PlmGitBranchRow {
    std::string name;
    bool current = false, remote = false;
};

struct PlmGitRemoteRow {
    std::string name, url;
};

// One shape reused for both pull requests and issues (number + title + state).
struct PlmGitRefRow {
    int number = 0;
    std::string title, state, author, branch;
};

struct PlmOverview {
    int itemsCount = 0, dirtyCount = 0, openTasks = 0, openIssues = 0, openMilestones = 0;
    std::string aiMode;
    std::vector<PlmItemRow>  dirtyItems;
    std::vector<PlmEntryRow> recent;
    std::vector<PlmTaskRow>  tasks;       // open tasks
    std::string nextMilestone;            // title of the next open milestone ("" = none)
};

struct PlmVersionRefRow {
    std::string type, name;
};

struct PlmVersionAuthorRow {
    std::string kind, id, display;
};

struct PlmVersionAiRow {
    std::string provider, model, runId;
};

struct PlmVersionReleaseRow {
    std::string itemId, rev;
};

struct PlmVersionNodeRow {
    std::string commit, kind, scope, authoredAt, message, summary, thumbnailRef;
    std::vector<std::string> parents;
    PlmVersionAuthorRow author;
    PlmVersionAiRow ai;
    std::vector<PlmVersionRefRow> refs;
    PlmVersionReleaseRow release;
    bool hasRelease = false;
    bool pin = false;
};

struct PlmVersionBranchRow {
    std::string name, head, createdAt, from, mergeBase;
    bool current = false;
    int ahead = 0, behind = 0;
};

struct PlmNamedVersionRow {
    std::string name, commit, createdAt, author, message;
};

// versions (full commit DAG)
struct PlmVersionsState {
    int gen = -1;
    bool loading = false;
    bool available = true;
    bool truncated = false;
    std::string err, head, activeBranch, selected, compareSecond;
    int scope = 2;  // 0 design | 1 planning | 2 all
    char search[192] = {};
    std::vector<PlmVersionNodeRow> nodes;
    std::vector<PlmVersionBranchRow> branches;
    std::vector<PlmNamedVersionRow> versions;
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
    int         sortMode   = 0;           // Files sort: 0 name,1 type,2 rev,3 status,4 unsaved

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
    bool        autoCommit = true;        // record saves to history automatically
    // Fired once per projectDir when the hub first loads it (setActiveProjectDir
    // also fires plm_record_outside; this covers "open PLM tab on already-set dir").

    PlmVersionsState versionsGraph;
    std::string outsideRecordedFor;

    // git (Git page) — real git / GitHub client
    int         gitGen = -1; bool gitLoading = false; std::string gitErr;
    bool        gitIsRepo = false; bool gitMissing = false;
    std::string gitBranch, gitUpstream;
    int         gitAhead = 0, gitBehind = 0; bool gitClean = true;
    std::vector<PlmGitFileRow>    gitFiles;
    std::vector<PlmGitCommitRow>  gitLog;
    int         gitTotalCommits = 0, gitCommits24h = 0;
    std::vector<PlmGitBranchRow>  gitBranches;
    std::vector<PlmGitRemoteRow>  gitRemotes;
    std::vector<std::string>      gitTree;           // tracked files (code browser)
    std::string gitSelectedFile, gitFileContent;     // selected file + its content
    bool        gitFileTruncated = false;
    // GitHub (gh) side
    bool        ghAuthed = false; std::string ghUser;
    std::string ghRepoName, ghRepoOwner, ghRepoVis, ghRepoUrl;
    std::vector<PlmGitRefRow>     gitPrs, gitIssues;
    // transient sign-in flow
    std::string ghLoginCode, ghLoginUrl;
    char        gitCommitMsg[512] = {};
    char        gitNewBranch[128] = {};

    // transient action state / input buffers
    char        checkpointMsg[256] = {};
    bool        busy = false;             // a mutation is in flight
    std::string actionErr, actionOk;
    char        newTaskTitle[192] = {};
    char        newIssueTitle[192] = {};
    int         newIssueSev = 1;          // severity for the next logged issue (0 low..3 crit)
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

// ---- shared widgets ------------------------------------------------------------
//
// The hub's spelling of the app-wide vocabulary in ui/widgets/kit.h. The names and
// signatures are load-bearing (hundreds of call sites, plus feature_tree.cpp), but
// each one now forwards to ui::kit — the Project Hub paints no header, empty state,
// chip or spinner of its own. Defined in plm_workspace.cpp.

// A background-only row pill. Unlike ui::kit::row it does NOT reserve or hit-test the
// row: the caller draws the content from the same cursor and owns the interaction.
void plmRowPill(bool sel = false, float rows = 1.0f);
// Accent title with an optional atlas icon lined up to its left. No underline and no
// baked trailing gap — callers place the gap beneath deliberately.
void plmSectionHeader(const char* label, const char* icon = nullptr);
// `icon` is a contracts/icons.md name; the legacy ASCII stand-ins ("[ ]", "(#)") that
// predate the atlas resolve to no icon.
void plmEmptyState(const char* icon, const char* msg, const char* sub = nullptr);
void plmSpinner(const char* prefix);
void plmError(const std::string& msg);
void plmLifecycleBadge(const std::string& lifecycle, const std::string& rev = "");
void plmDocTypeTag(const std::string& docType, const std::string& kind);

// The ONE liquid-glass action button (defined in plm_pages.cpp): frosted base +
// vertical shine + border, an accent wash when `accent`, and an optional atlas icon
// beside the label. Published here because the Graph tab needs the identical button —
// it used to carry a near-byte copy that was 1 px narrower per side. Returns clicked.
bool plmGlassButton(const char* id, const char* label, const char* icon,
                    bool accent = false, float widthOverride = 0.0f);

// ---- state helpers (defined in plm_state.cpp) ---------------------------------

void        plmInvalidate();                 // bump generation → all caches refetch
std::string plmProjectName(AppState& st);    // display name of the current project folder
void        plmEnsureOverview(AppState& st);
void        plmEnsureItems(AppState& st);
void        plmEnsureHistory(AppState& st);
void        plmEnsureVersionGraph(AppState& st);
void        plmEnsurePlanning(AppState& st);
void        plmEnsureSettings(AppState& st);
void        plmEnsureGit(AppState& st);
// convenience: build the {"path": …} param object for the current project.

// ---- page bodies (defined in plm_pages.cpp) -----------------------------------

void plmDrawOverviewPage(AppState& st);
void plmDrawFilesPage(AppState& st);
void plmDrawHistoryPage(AppState& st);
void plmDrawReleasesPage(AppState& st);
void plmDrawPlanningPage(AppState& st);
void plmDrawIssuesPage(AppState& st);
void plmDrawTeamPage(AppState& st);
void plmDrawGitPage(AppState& st);
void plmDrawSettingsPage(AppState& st);
void plmDrawVersionsPage(AppState& st);

}  // namespace ui
