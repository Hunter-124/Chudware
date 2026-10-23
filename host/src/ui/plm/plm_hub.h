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
    std::string change;                  // authoritative working-tree M/A/D/U status
    bool dirty = false;
};

struct PlmEntryRow {
    std::string commit, kind, scope, authoredAt, author, authorKind, message, summary;
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

// One direct consumer from plm_where_used (derived, never stored — services/plm/whereused.py).
struct PlmWhereUsedRow {
    std::string itemId, kind, path, partNumber, via;   // via: "component" | "source"
    int count = 1;                                     // reference multiplicity
};

// ---- Team & Sync row shapes (parsed from plm_status — contracts/plm.md §6) -------

// One shared location the project can sync with (manifest.remotes[] §6.1). The
// optional identity is how THIS machine appears to the team on that location;
// capabilities say what the service can actually do against it.
struct PlmRemoteRow {
    std::string id, kind, url, name;   // kind: folder | ssh | https | plm-server | chudware-cloud
    bool reserved = false;             // chudware-cloud config-only seam (§6.1)
    std::string identityUserId, identityDisplay, identityAccountId;
    bool canSync = false;              // capabilities: "snapshot_sync"
    bool canLock = false;              // capabilities: "advisory_locks"
    bool canLive = false;              // capabilities: "live_coedit" (kind "plm-server") —
                                       // live editing replaces Sync/lock controls entirely
};

// One advisory lock (§6.2) — a courtesy presence signal, never a hard block.
struct PlmLockRow {
    std::string itemId, userId, display, accountId, acquiredAt, expiresAt;
};

// ---- live co-editing session (plm-server profiles — contracts/plm_live.md) -----

// One OTHER participant in the joined live session — the union of the join
// roster and later presence broadcasts, keyed by participant id.
struct PlmLivePeerRow {
    std::string id, display;
};

// The one live item session this GUI holds (at most one — the active document's).
// Ordinary doc_apply_ops on the joined doc are routed through plm_live_propose by
// WorkerClient (the single server order); committed batches come back through the
// existing hostd doc.changed pipeline. This state is presentation + pacing only —
// it is never a second op sender and never patches a tree.
struct PlmLiveState {
    bool        joined  = false;
    bool        joining = false;          // plm_live_join in flight
    bool        leaving = false;          // plm_live_leave in flight
    std::string projectDir, remoteId, itemId;   // session key
    std::string remoteName;               // live remote's display name
    std::string sessionId;
    std::string docId;                    // local worker doc bound to the session
    std::string selfId, selfDisplay;      // this seat's participant identity
    long long   lastSeq = 0;              // authoritative sequence applied locally
    bool        connected = false;        // transport state from the last poll (honest)
    std::string err;                      // last plain-language failure ("" = none)
    std::vector<PlmLivePeerRow> peers;    // other participants (bounded)
    // pacing (ImGui clock) — poll/presence are bounded, async, and never block a frame
    double      lastPollAt = -1.0e9;          bool pollInFlight = false;
    double      lastPresenceCheckAt = -1.0e9; bool presenceInFlight = false;
    double      lastPresenceSentAt = -1.0e9;
    std::string presenceSig;              // last payload sent (skip unchanged)
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
    std::string ovProject;               // project `overview` was answered for ("" = none yet)
    PlmOverview overview;

    // items (Files page)
    int         itemsGen = -1; bool itemsLoading = false; std::string itemsErr;
    std::string itemsProjectDir;         // also consumed by the quickbar outside PLM
    std::vector<PlmItemRow> items;
    std::vector<PlmItemRow> untracked;    // shared discovery result for Explorer
    unsigned long long itemsRevision = 0; // changes only when the row vector is replaced
    unsigned long long filesViewRevision = 0;
    int filesViewFilter = -1, filesViewSort = -1;
    std::vector<size_t> filesView;        // filtered/sorted indices, never dangling pointers
    std::string selectedItem;
    int         kindFilter = 0;           // 0 = all; else index into a filter list
    int         sortMode   = 0;           // Files sort: 0 name,1 type,2 rev,3 status,4 unsaved

    // history (History page)
    int         histGen = -1; bool histLoading = false; std::string histErr;
    int         histScope = 0;            // 0 design | 1 planning | 2 all
    std::string histItemFilter;           // item_id ("" = all items) — refetches server-side
    std::string histAuthorFilter;         // author display name ("" = anyone) — client-side
    int         histActor = 0;            // 0 all | 1 people | 2 AI — client-side
    std::vector<PlmEntryRow> history;
    unsigned long long historyRevision = 0;
    std::string selectedEntry;

    // where-used explorer (Files page) — cached consumers of one item
    std::string wuTarget;                 // item whose consumers are shown ("" = closed)
    std::string wuFor;                    // item the cache was actually fetched for
    int         wuGen = -1; bool wuLoading = false; std::string wuErr;
    std::vector<PlmWhereUsedRow> wuRows;


    // planning (Planning/Issues pages)
    int         planGen = -1; bool planLoading = false; std::string planErr;
    int planPending = 0;
    std::vector<PlmTaskRow>      allTasks;
    std::vector<PlmIssueRow>     issues;
    std::vector<PlmMilestoneRow> milestones;

    // settings (part-number scheme, ai mode)
    int         setGen = -1; bool setLoading = false; std::string setErr;
    std::string partPrefix, aiMode;
    bool        autoCommit = true;        // record saves to history automatically
    // One-shot outside-change recording is scheduled after the first dashboard answer.

    PlmVersionsState versionsGraph;
    std::string outsideRecordedFor;
    bool outsideLoading = false;
    std::string outsideErr;

    // git (Git page) — real git / GitHub client
    int         gitGen = -1; bool gitLoading = false; std::string gitErr;
    int gitPending = 0;
    int gitDetailsGen = -1, gitDetailsPending = 0;
    bool gitDetailsLoading = false;
    bool        gitIsRepo = false; bool gitMissing = false;
    std::string gitBranch, gitUpstream;
    int         gitAhead = 0, gitBehind = 0; bool gitClean = true;
    std::vector<PlmGitFileRow>    gitFiles;
    std::vector<PlmGitCommitRow>  gitLog;
    int         gitTotalCommits = 0, gitCommits24h = 0;
    std::vector<PlmGitBranchRow>  gitBranches;
    std::vector<PlmGitRemoteRow>  gitRemotes;
    std::vector<std::string>      gitTree;           // tracked files (code browser)
    unsigned long long gitTreeRevision = 0;
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

    // team & sync (Team page; the quickbar reads the same cache for presence)
    int         teamGen = -1; bool teamLoading = false; std::string teamErr;
    double      teamFetchedAt = -1.0e9;   // ImGui time of the last fill (locks age out)
    std::vector<PlmRemoteRow> remotes;
    std::vector<PlmLockRow>   locks;
    std::string syncingRemote;            // remote id with a sync in flight ("" = none)
    std::string lastSyncRemote;           // remote id of the last finished sync
    std::string lastSyncMsg;              // plain-language outcome of that sync
    bool        lastSyncOk = false;
    std::vector<std::string> lastSyncCopies;  // names preserved as copies (§6.4)
    char        shareLocation[512] = {};  // Share-this-project dialog buffers
    char        shareName[128] = {};
    char        shareDisplay[128] = {};
    int         shareKind = 0;            // 0 folder | 1 ssh | 2 https (cloud disabled)

    // live co-editing session (plm-server profiles — see PlmLiveState above)
    PlmLiveState live;

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
void        plmEnsureWhereUsed(AppState& st);   // fetches for g_plmHub.wuTarget
void        plmEnsureVersionGraph(AppState& st);
void        plmEnsurePlanning(AppState& st);
void        plmEnsureSettings(AppState& st);
void        plmEnsureGit(AppState& st, bool details = true);
void        plmEnsureTeam(AppState& st);     // remotes + advisory locks (plm_status)
// The shared location that lock/presence actions run against: the first
// non-reserved remote advertising advisory locks (nullptr = none configured).
const PlmRemoteRow* plmTeamLockRemote();
long long   plmIsoToUnix(const std::string& iso);  // "…T…Z" -> unix seconds (0 = unparsable)
bool        plmLockLive(const PlmLockRow& l);      // not yet soft-expired (§6.2)
std::string plmNiceTime(const std::string& iso);   // "2026-07-07T19:54:28Z" -> "2026-07-07  19:54"
// Reason-first plain-language error for sync/lock/share envelopes. Transport or
// tool vocabulary never reaches the user (contract §8.1): unknown reasons whose
// hint reads technical degrade to a generic message.
std::string plmTeamFriendlyError(const std::string& reason, const std::string& hint);
// ---- live co-editing (plm-server profiles — defined in plm_state.cpp) ----------
// The first live-capable shared location (kind "plm-server" advertising
// live_coedit). nullptr = none configured. Live remotes never offer
// Sync/lock controls — the session replaces both.
const PlmRemoteRow* plmTeamLiveRemote();
// True while this GUI holds the live session for that item.
bool plmLiveJoinedItem(const std::string& itemId);
// Join the item's authoritative live session, binding the active mirror doc
// (st.docMirror.docId()). A replacement local_doc_id + open_response in the join
// reply is adopted into the active mirror/tab atomically (DocMirror::
// adoptOpenResponse) BEFORE joined state is presented.
void plmLiveJoin(AppState& st, const PlmRemoteRow& rm, const std::string& itemId);
void plmLiveLeave(AppState& st);
// Per-frame while joined: bounded async event/status poll + bounded best-effort
// presence from the current selection/viewport. Consumes complete envelopes
// only; never patches a tree (commits arrive whole via hostd doc.changed) and
// never blocks a frame.
void plmLiveTick(AppState& st);
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
