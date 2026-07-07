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
    std::string unit;
    std::string material;
    std::string domain;
    std::string standard;
    std::string source;       // "local" | "live"
    std::string datasheet;
    std::string imageUrl;
    std::string license;
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
    std::string lastLogin;
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
};

// ---- taxonomy / facets (phase B data model — populated by later phases) ----------

struct TaxFacet {
    std::string id, label, type, unit;    // type: "range" | "multi" | "select" | "text"
    std::vector<std::string> options;
    float vmin = 0, vmax = 0;
};

struct TaxNode {
    std::string id, label, icon;
    std::vector<TaxNode> children;
    std::vector<TaxFacet> facets;
};

struct FacetValue {
    float rmin = 0, rmax = 0;
    std::set<std::string> multi;
    int sel = -1;
    char text[64] = {};
    bool active() const {
        return rmin != 0.0f || rmax != 0.0f || !multi.empty() || sel >= 0 || text[0] != 0;
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

    // ---- filters (sent with parts_search) ----
    float priceMin = 0.0f;
    float priceMax = 0.0f;      // 0 = no cap
    char  materialBuf[64] = {};
    char  vendorBuf[64]   = {};
    int   sourceFilter = 0;     // 0=All 1=Local 2=Live
    int   sortMode     = 0;     // 0=Relevance 1=Price low→high 2=Price high→low 3=Name

    bool gridView = false;

    // ---- taxonomy tree + facet filter values (phase B; harmless now) ----
    TaxNode taxonomyRoot;
    bool taxonomyLoaded  = false;
    bool taxonomyLoading = false;
    bool taxonomyIsTree  = false;
    std::set<std::string> expandedCats;
    std::map<std::string, FacetValue> facetValues;
    double filterDirtyAt = 0;

    // ---- vendors (right pane) ----
    bool vendorsLoaded   = false;
    bool vendorsLoading  = false;
    std::vector<VendorEntry> vendors;
    std::vector<ApiProvider> apiProviders;
    double lastVendorPoll = 0.0;

    // ---- download queue ----
    std::vector<QueueJob> queue;
    bool   queuePolling  = false;
    double lastQueuePoll = 0.0;
    bool   queueActive   = false;  // any pending/running job → keep polling

    // ---- thumbnails ----
    std::map<std::string, Thumb> thumbs;      // part_id → GL texture (loaded)
    std::set<std::string>        thumbBusy;   // requests in flight
    std::set<std::string>        thumbFailed; // don't retry this session

    // Set every frame by drawPartsLibraryWorkspace so widget TUs can reach the app fonts
    // (sectionHeader h2). Never dereferenced outside a frame.
    const ui::Fonts* fonts = nullptr;

    bool firstFrame = true;
};

// The one shared instance (function-local static in parts_workspace.cpp).
PartsState& S();

// ---- data / RPC functions (parts_workspace.cpp) -----------------------------------
void loadCategories(AppState& st);
void loadVendors(AppState& st, bool force = false);
void doSearch(AppState& st);
void pollQueue(AppState& st);
void queueDownload(AppState& st, const PartsItem& item);
void insertItem(AppState& st, const PartsItem& item);
void startVendorLogin(AppState& st, VendorEntry& v);
void saveApiCreds(AppState& st, ApiProvider& a);

// ---- panes ------------------------------------------------------------------------
void drawTaxonomyPane(AppState& st, float paneW);   // parts_taxonomy_pane.cpp
void drawFiltersBlock(AppState& st, float paneW);   // parts_filters.cpp
void drawResultsList(AppState& st);                 // parts_results.cpp
void drawResultsGrid(AppState& st);                 // parts_results.cpp
void drawVendorsPane(AppState& st, float paneW);    // parts_vendors.cpp

// parts_browser_panel.cpp — embedded vendor-browser panel (later phase; stub for now).
bool browserPanelActive();
void drawBrowserPanel(AppState& st);

}  // namespace parts
}  // namespace ui
