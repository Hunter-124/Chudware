// Parts Library workspace — shared internal state & declarations (TU split, GUI V2 phase A).
// One PartsState singleton (function-local static, defined in parts_workspace.cpp) shared by
// the pane TUs: parts_taxonomy_pane / parts_filters / parts_results / parts_vendors /
// parts_browser_panel / parts_widgets. RPC-callback idiom: capture ids BY VALUE, re-find the
// row in S() by id inside the callback (rows may have been re-created by a refresh meanwhile).
#pragma once

#include <map>
#include <set>
#include <string>
#include <vector>

struct AppState;

namespace ui {
struct Fonts;
namespace parts {

// ---- data rows -------------------------------------------------------------------

struct Category {
    std::string id;
    std::string label;
    std::string parentId;
};

struct PartsItem {
    std::string id;           // part_id, e.g. "fastener:ISO 4762:M8" or "catalog:mcmaster::https://…"
    std::string name;
    std::string vendor;
    std::string partNumber;
    std::string description;
    std::string fileName;     // selected CAD filename for a community listing
    std::string unit;
    std::string material;
    std::string domain;
    std::string standard;
    std::string source;       // "local" | "live"
    std::string datasheet;
    std::string imageUrl;
    std::string license;
    std::vector<std::string> sizes;  // available parametric sizes from the core catalog
    int         score      = 0;     // backend identity relevance (0–100)
    double      lengthMm   = 0.0;   // search-resolved standard screw length
    double      priceUsd   = 0.0;
    bool        downloaded = false;
    bool        placeable  = false;
};

struct VendorEntry {
    std::string id;
    std::string label;
    bool loggedIn        = false;
    bool loginInProgress = false;
    std::string loginError;
};

// One vendor in an in-flight live search: its logo + inline progress state. Shown after the
// "Parts(N)" header while a search fans out. (Logo texture lives in S().thumbs under
// "vendorlogo:"+site; status drives the little per-vendor progress bar.)
struct LiveVendor {
    std::string site;                 // site key (e.g. "mcmaster")
    std::string label;                // friendly name (tooltip)
    std::string domain;               // registrable domain (favicon source)
    std::string status = "queued";    // queued | loading | done | error
};

struct ApiProvider {
    std::string id;
    std::string label;
    std::vector<std::string> fields;
    bool configured = false;
    bool editing    = false;
    bool saving     = false;
    char fieldBuf[2][192] = {{0}, {0}};  // supports up to 2 credential fields
};

struct QueueJob {
    std::string id;
    std::string site;
    std::string query;
    std::string status;       // pending | running | done | error | cancelled
    std::string artifactPath;
    std::string error;
    bool importing = false;
};

struct Thumb {
    unsigned int tex = 0;
    int w = 0, h = 0;
    double lastUse = 0.0;   // ImGui::GetTime() at the last draw lookup — LRU eviction key
};

// parts_search "notices" — per-site skip reasons (login_required etc.) so stubbed vendors
// surface as a hint line instead of blank rows.
struct Notice {
    std::string site, reason, hint;
};

// vendors_health_status result row.
struct HealthRow {
    std::string site;
    bool   ok        = false;
    bool   expected  = false;   // expected failure (sign-in required) → amber, not red
    double latencyMs = 0.0;
    std::string error;
};

// ---- taxonomy / facets --------------------------------------------------------------
// Live end-to-end: `parts_taxonomy` returns the tree, and the server applies `facets`
// authoritatively in services/parts/gui_rpc.py:_filter_items.

struct TaxFacet {
    std::string id, label, type, unit;    // type: "range" | "enum" | "text" (RPC field is "key" → id)
    std::vector<std::string> options;
};

struct TaxNode {
    std::string id, label, icon;
    std::vector<TaxNode> children;
    std::vector<TaxFacet> facets;
};

struct FacetValue {
    float rmin = 0, rmax = 0;
    std::set<std::string> multi;
    char text[64] = {};
    bool active() const {
        return rmin != 0.0f || rmax != 0.0f || !multi.empty() || text[0] != 0;
    }
};

// ---- shared state ----------------------------------------------------------------

struct PartsState {
    bool categoriesLoaded   = false;
    bool categoriesLoading  = false;
    std::vector<Category>  categories;
    std::string            selectedCategory;

    char searchBuf[256] = {};
    bool searching      = false;
    bool searchDone     = false;
    std::vector<PartsItem> results;
    std::string            searchErr;
    bool hasLive = false;
    std::vector<Notice> notices;   // per-site skip reasons from the last parts_search

    // ---- streaming live vendor search (results paint as each vendor lands) ----
    bool        liveSearching = false;  // a live fan-out job is in flight
    bool        livePolling   = false;  // parts_search_live_poll in flight (single-flight)
    std::string liveToken;              // current job token; stale-poll guard
    int         liveCursor    = 0;      // results already drained from the job
    double      lastLivePoll  = 0.0;
    std::vector<LiveVendor> liveVendors; // vendors this search is fanning out to (logos + progress)

    // ---- filters (sent with parts_search) ----
    float priceMin = 0.0f;
    float priceMax = 0.0f;      // 0 = no cap
    char  materialBuf[64] = {};
    char  vendorBuf[64]   = {};
    int   sourceFilter = 0;     // 0=All 1=Local 2=Live
    int   sortMode     = 0;     // 0=Relevance 1=Price low→high 2=Price high→low 3=Name

    bool gridView = false;

    // ---- taxonomy tree + facet filter values ----
    TaxNode taxonomyRoot;
    bool taxonomyLoaded  = false;
    bool taxonomyLoading = false;
    bool taxonomyIsTree  = false;
    // Cold-start recovery. The parts worker is a Python sidecar that can still be booting
    // when the user opens this workspace; both parts_taxonomy and its parts_list_categories
    // fallback then fail on transport (RpcResult::ok == false) and the one-shot `loaded`
    // latch left the pane permanently empty — the "stuck on No catalog data" report.
    // drawPartsLibraryWorkspace re-arms the load on a bounded backoff instead.
    double taxonomyStartedAt = 0.0;   // GetTime() when the in-flight call started
    double taxonomyRetryAt   = 0.0;   // GetTime() after which the next attempt may fire
    int    taxonomyAttempts  = 0;     // consecutive failed attempts; reset on success/Retry
    std::set<std::string> expandedCats;
    std::map<std::string, FacetValue> facetValues;
    double filterDirtyAt = 0;

    // ---- vendors (strip + popup) ----
    bool vendorsLoaded   = false;
    bool vendorsLoading  = false;
    std::vector<VendorEntry> vendors;
    std::vector<ApiProvider> apiProviders;
    double lastVendorPoll = 0.0;
    bool vendorsPopupOpen = false;

    // ---- vendor health check (vendors_health_check / vendors_health_status) ----
    bool   healthSupported = true;   // false once the worker says "unknown method"
    bool   healthStarting  = false;  // vendors_health_check in flight
    bool   healthRunning   = false;  // backend reports running → keep polling
    bool   healthPolling   = false;  // vendors_health_status in flight (single-flight)
    bool   healthChecked   = false;  // at least one status result received
    double lastHealthPoll  = 0.0;
    std::vector<HealthRow> healthResults;

    // True while the embedded vendor-browser wizard owns the results column. Set
    // optimistically by startLoginWizard (so the panel engages without waiting on the
    // RPC) and rolled back on the failure branches that fall back to the headed flow.
    bool browserWizardRequested = false;

    // ---- download queue ----
    std::vector<QueueJob> queue;
    bool   queuePolling  = false;
    double lastQueuePoll = 0.0;
    bool insertBusy = false;
    std::string insertNote;  // in-flight phase or last insert outcome, also shown as a toast
    bool insertError = false;
    bool   queueActive   = false;  // any pending/running job → keep polling
    std::set<std::string> autoImportJobs;  // community downloads requested for immediate import

    // ---- thumbnails ----
    std::map<std::string, Thumb> thumbs;      // part_id → GL texture; LRU-capped in storeThumb
    std::set<std::string>        thumbBusy;   // requests in flight
    std::set<std::string> thumbFailed; // no image available — never retried this session

    // Set every frame by drawPartsLibraryWorkspace so widget TUs can reach the app fonts
    // (sectionHeader h2). Never dereferenced outside a frame.
    const ui::Fonts* fonts = nullptr;

    bool firstFrame = true;
};

// The one shared instance (function-local static in parts_workspace.cpp).
PartsState& S();

// ---- data / RPC functions (parts_workspace.cpp) -----------------------------------
void loadCategories(AppState& st);
void loadTaxonomy(AppState& st);     // parts_taxonomy → S().taxonomyRoot (falls back to loadCategories)
void loadVendors(AppState& st, bool force = false);
void doSearch(AppState& st);
void startLiveSearch(AppState& st);
void pollLiveSearch(AppState& st);
void pollQueue(AppState& st);
void queueDownload(AppState& st, const PartsItem& item);
void importQueuedArtifact(AppState& st, const std::string& id, const std::string& artifactPath);
void insertItem(AppState& st, const PartsItem& item);
void sendToReverseEngineering(AppState& st, const PartsItem& item);
// RE-32: one-click "Generate 3D model" for metadata-only (no-model) parts.
// Calls reveng_make_part_from_library async (seed→internet research→build→attach).
// Hidden/disabled for parts that already have a model (item.placeable == true).
void generateModelFromLibrary(AppState& st, const PartsItem& item);
void startVendorLogin(AppState& st, VendorEntry& v);
void saveApiCreds(AppState& st, ApiProvider& a);

// Find a node by id anywhere in the taxonomy tree (nullptr when absent). Defined in
// parts_taxonomy_pane.cpp; parts_filters.cpp uses it to reach the selected node's facets.
const TaxNode* findTaxNode(const TaxNode& root, const std::string& id);

// ---- panes ------------------------------------------------------------------------
void drawTaxonomyPane(AppState& st, float paneW);   // parts_taxonomy_pane.cpp (left, tree)
void drawFiltersBlock(AppState& st, float paneW);   // parts_filters.cpp (right pane + vendor strip)
void drawResultsList(AppState& st);                 // parts_results.cpp
void drawResultsGrid(AppState& st);                 // parts_results.cpp

// parts_vendors.cpp — compact vendor strip (footer of the filters pane), the floating
// "Vendor Accounts" popup (sign-in list + API creds + download queue + health table), the
// vendor health-check flow, and the phase-C wizard seam.
void drawVendorStrip(AppState& st, float paneW);    // fixed footer inside the filters pane
// Height the filters pane must reserve for drawVendorStrip, given the pane's content width
// (GetContentRegionAvail().x where the strip will start): the secondary buttons stack onto a
// second row when the pane is too narrow for them side by side.
float vendorStripHeight(float contentW);
void drawVendorsPopup(AppState& st);                // floating glass window; no-op when closed
void startHealthCheck(AppState& st);
void pollHealthStatus(AppState& st);

// parts_browser_panel.cpp — embedded vendor-browser panel + login wizard (phase C).
// startLoginWizard: site empty → full multi-vendor wizard (login_wizard_start {}),
// site set → single-vendor wizard ({sites:[site]}). Falls back to the headed
// vendor_login flow when the worker lacks the embed backend.
bool browserPanelActive();
void drawBrowserPanel(AppState& st);
void startLoginWizard(AppState& st, const std::string& site = std::string());

}  // namespace parts
}  // namespace ui
