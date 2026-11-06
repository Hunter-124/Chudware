// Parts Library workspace — full-viewport body for the PartsLibrary title-bar tab.
// Federated catalog browse/search over engine_oracle/parts_hub (fasteners, bearings,
// sections, fittings, hydraulics, ECAD + live vendor catalogs), price/material/source
// filters, thumbnail previews, per-vendor sign-in (headed browser via the worker), API
// provider credentials, and the background scrape/download queue. All RPC calls are
// async (st.rpc) — never blocks the GL thread. Degrades gracefully to empty-state when
// services are not running.
//
// GUI V2 phase A TU split: this file owns the PartsState singleton (S()), the entry point +
// layout + search bar, and the data/RPC functions. Panes live in parts_taxonomy_pane.cpp,
// parts_filters.cpp, parts_results.cpp, parts_vendors.cpp, parts_browser_panel.cpp; shared
// widgets in parts_widgets.cpp. RPC-callback idiom: capture ids by value, re-find rows in
// S() by id.
#include "parts_workspace.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui/asm_panels.h"
#include "ui/icons.h"      // drawIconGlass — the in-field search-clear glyph
#include "ui_theme.h"
#include "ui/metrics.h"      // ui::metrics::U — the search input's last-resort floor
#include "ui/widgets/kit.h"  // ui::kit::elide — the search hint's last-resort fit
#include "core/async_rpc.h"  // core::RpcResult — load-failure reasons
#include "ui/reveng/reveng_workspace.h"  // revengProjectDir — RE sessions belong to the working folder

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

PartsState& S() {
    static PartsState s;
    return s;
}

// Bounded cold-start retries for the category tree (see drawPartsLibraryWorkspace). Eight
// attempts on a 1.5s-doubling backoff capped at 15s covers ~75s of Python-sidecar boot.
constexpr int kTaxonomyMaxAttempts = 8;

// ---- catalog index build state (parts_index_status) ----------------------------
// The Python side keeps a persisted FTS index of the local catalog; on a cold cache it is
// (re)built in the background and `parts_search` answers from whatever is indexed SO FAR,
// reporting the build through two additive response fields. These file-local statics track
// it so the workspace can say so honestly and — crucially — re-run the user's query by
// itself the moment the build lands, instead of showing a short list forever.
static bool   g_indexBuilding        = false;  // backend is still building the index
static int    g_indexCount           = 0;      // parts indexed so far
static bool   g_indexPolling         = false;  // parts_index_status in flight (single-flight)
static double g_lastIndexPoll        = 0.0;
static bool   g_researchOnIndexReady = false;  // re-run doSearch when the build finishes
// Cleared the first time parts_index_status fails. Without this latch an old worker that
// somehow reported `index_building` but had no status RPC would spin: clear-on-failure →
// auto re-search → the search re-arms `index_building` → poll → clear → … Once the RPC is
// known missing the whole feature stands down for the session.
static bool   g_indexStatusSupported = true;

// Live fan-out start time. The first vendor usually lands inside the first couple of
// seconds, and making its rows wait on the steady-state 0.4 s tick was the single biggest
// chunk of perceived latency in a live search — so the poll bursts first, then settles.
static double g_liveStartedAt = 0.0;

// Vendor favicon requests wait behind visible part previews on the shared RPC lane.
namespace {

// Registrable domain per site key, feeding the favicon URL in requestVendorLogo.
//
// Group 1 is transcribed verbatim from the backend's `_SITE_DOMAINS`
// (services/parts/gui_rpc.py:115-125), which `_site_domain` (gui_rpc.py:128-143) consults
// first. Group 2 covers the backend's SECOND step (gui_rpc.py:133-142: import
// services.browser.adapters.<site>, take its BASE_URL netloc, drop "www.") — there is no
// client-side equivalent for that import, so the keys where it disagrees with the
// `<site>.com` fallback are snapshotted here instead. Without group 2, cadenas / maxon /
// thomson — all login-gated vendors the GUI DOES list — would fetch the favicon of an
// unrelated site that happens to own <key>.com, which is worse than no logo.
//
// Anything not listed falls through to `<site>.com`, exactly like gui_rpc.py:143.
struct SiteDomain { const char* key; const char* domain; };
constexpr SiteDomain kSiteDomains[] = {
    // -- group 1: gui_rpc.py:115-125, verbatim ------------------------------------------
    {"mcmaster", "mcmaster.com"}, {"grabcad", "grabcad.com"}, {"traceparts", "traceparts.com"},
    {"amazon", "amazon.com"}, {"jlcpcb", "jlcpcb.com"}, {"pcbway", "pcbway.com"},
    {"misumi", "misumi.com"}, {"lcsc", "lcsc.com"}, {"digikey", "digikey.com"},
    {"snapeda", "snapeda.com"}, {"content3d", "3dcontentcentral.com"},
    {"3dcontentcentral", "3dcontentcentral.com"}, {"homedepot", "homedepot.com"},
    {"lowes", "lowes.com"}, {"acehardware", "acehardware.com"}, {"grainger", "grainger.com"},
    {"fastenal", "fastenal.com"}, {"harborfreight", "harborfreight.com"},
    {"tractorsupply", "tractorsupply.com"}, {"onlinemetals", "onlinemetals.com"},
    {"metalsdepot", "metalsdepot.com"},
    // -- group 2: adapter BASE_URL netloc, for the keys where it != "<key>.com" ----------
    {"cadenas", "3dfindit.com"}, {"cui", "cuidevices.com"},
    {"freecadlibrary", "github.com"}, {"gbstandards", "openstd.samr.gov.cn"},
    {"hdsc", "hdsc.com.cn"}, {"hiwin", "hiwin.us"}, {"maxon", "maxongroup.com"},
    {"rockwest", "rockwestcomposites.com"}, {"thomson", "thomsonlinear.com"},
    {"wch", "wch-ic.com"},
};

std::string vendorDomain(const std::string& id) {
    if (id.empty()) return {};
    // Some ids reach the GUI carrying their transport suffix ("mcmaster_web",
    // "amazon_browser"); the registrable domain belongs to the stem, not the suffix.
    std::string stem = id;
    for (const char* suf : {"_web", "_browser"}) {
        const size_t n = std::strlen(suf);
        if (stem.size() > n && stem.compare(stem.size() - n, n, suf) == 0) {
            stem.erase(stem.size() - n);
            break;
        }
    }
    for (const auto& e : kSiteDomains)
        if (stem == e.key) return e.domain;
    return stem + ".com";
}

}  // namespace

// Site keys whose favicon is wanted but not yet requested. requestVendorLogo funnels through
// the SHARED 4-slot thumbnail budget, so firing all ~16 on the frame the vendor list lands
// would starve the part renders the user is actually looking at. Drained one per frame by
// pumpVendorLogos, and only while the budget is mostly idle.
static std::vector<std::string> g_logoQueue;

// ---- async loaders -------------------------------------------------------------

// One honest sentence for a failed catalog load. The worker's raw text names internals
// (methods, tracebacks), so the pane says WHICH side failed and the raw reason goes to the log.
static std::string catalogFailText(AppState& st, const char* method, const core::RpcResult& r) {
    std::string raw = r.err;
    if (raw.empty() && r.result.is_object())
        raw = r.result.value("reason", r.result.value("error", std::string()));
    st.addLog(std::string("[parts] ") + method + " failed: " +
              (raw.empty() ? std::string("no result") : raw));
    return r.ok ? "The parts catalog could not be read."
                : "The parts service did not respond.";
}

static std::string rpcProblem(const core::RpcResult& r, const char* fallback) {
    if (r.ok && r.result.is_object()) {
        const auto reason = r.result.find("reason");
        if (reason != r.result.end() && reason->is_string() &&
            reason->get<std::string>() == "credentials_required")
            return "Configure API keys in Settings";
        for (const char* key : {"hint", "reason"}) {
            const auto it = r.result.find(key);
            if (it != r.result.end() && it->is_string()) {
                const std::string text = it->get<std::string>();
                if (!text.empty()) return text;
            }
        }
    }
    return r.err.empty() ? fallback : r.err;
}

void loadCategories(AppState& st) {
    if (S().categoriesLoaded || S().categoriesLoading) return;
    S().categoriesLoading = true;
    st.rpc.call("parts_list_categories", {}, [&st](const core::RpcResult& r) {
        S().categoriesLoading = false;
        S().categoriesLoaded  = true;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false) ||
            !r.result.contains("categories")) {
            S().taxonomyError = catalogFailText(st, "parts_list_categories", r);
            return;
        }
        S().categories.clear();
        for (auto& c : r.result["categories"]) {
            Category cat;
            cat.id       = c.value("id",        "");
            cat.label    = c.value("label",     "");
            cat.parentId = c.value("parent_id", "");
            S().categories.push_back(std::move(cat));
        }
        if (!S().categories.empty()) {
            S().taxonomyAttempts = 0;   // recovered on the flat list — stop the backoff
            S().taxonomyError.clear();
        }
    });
}

// parts_taxonomy → S().taxonomyRoot. Facet field arrives as "key" (mapped into TaxFacet.id).
// The tree includes a "vendors" root (Vendors → type groups → site leaves) rendered like any
// folder. On failure / method-missing, falls back to the flat parts_list_categories list.
static void parseTaxNode(const nlohmann::json& j, TaxNode& out) {
    out.id    = j.value("id",    "");
    out.label = j.value("label", "");
    out.icon  = j.value("icon",  "");
    if (j.contains("facets") && j["facets"].is_array())
        for (auto& f : j["facets"]) {
            TaxFacet tf;
            tf.id    = f.value("key",   "");   // NOTE: RPC field is "key", not "id"
            tf.label = f.value("label", tf.id);
            tf.type  = f.value("type",  "text");
            tf.unit  = f.value("unit",  "");
            if (f.contains("values") && f["values"].is_array())
                for (auto& v : f["values"])
                    if (v.is_string()) tf.options.push_back(v.get<std::string>());
            out.facets.push_back(std::move(tf));
        }
    if (j.contains("children") && j["children"].is_array())
        for (auto& c : j["children"]) {
            TaxNode ch;
            parseTaxNode(c, ch);
            out.children.push_back(std::move(ch));
        }
}

void loadTaxonomy(AppState& st) {
    if (S().taxonomyLoaded || S().taxonomyLoading) return;
    S().taxonomyLoading   = true;
    S().taxonomyStartedAt = ImGui::GetTime();
    st.rpc.call("parts_taxonomy", {}, [&st](const core::RpcResult& r) {
        S().taxonomyLoading = false;
        S().taxonomyLoaded  = true;
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false) &&
                  r.result.contains("tree") && r.result["tree"].is_array();
        if (!ok) {
            // Method-missing or failure → flat parts_list_categories 2-level synthesis.
            S().taxonomyIsTree = false;
            S().taxonomyError = catalogFailText(st, "parts_taxonomy", r);
            loadCategories(st);
            return;
        }
        TaxNode root;
        root.id = "";
        root.label = "All Parts";
        for (auto& n : r.result["tree"]) {
            TaxNode ch;
            parseTaxNode(n, ch);
            root.children.push_back(std::move(ch));
        }
        S().taxonomyRoot    = std::move(root);
        S().taxonomyIsTree  = true;
        S().taxonomyAttempts = 0;   // recovered — stop the backoff
        S().taxonomyError.clear();
    });
}

void loadVendors(AppState& st, bool force) {
    if (S().vendorsLoading) return;
    if (S().vendorsLoaded && !force) return;
    S().vendorsLoading = true;
    st.rpc.call("vendor_status", {}, [](const core::RpcResult& r) {
        S().vendorsLoading = false;
        S().vendorsLoaded  = true;
        S().lastVendorPoll = ImGui::GetTime();
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().vendorsError = r.ok ? "Vendor status could not be read."
                                    : "The parts service did not respond.";
            return;
        }
        S().vendorsError.clear();

        std::vector<VendorEntry> vs;
        if (r.result.contains("sources"))
            for (auto& v : r.result["sources"]) {
                VendorEntry e;
                e.id              = v.value("id", "");
                e.label           = v.value("label", e.id);
                e.loggedIn        = v.value("logged_in", false);
                e.loginInProgress = v.value("login_in_progress", false);
                e.loginError      = v.value("login_error", "");
                vs.push_back(std::move(e));
            }
        S().vendors = std::move(vs);

        // Prefetch every known vendor's favicon now that the list has landed. Queued rather
        // than fired: pumpVendorLogos drains it one per frame so the shared thumbnail budget
        // stays available for the part renders on screen.
        g_logoQueue.clear();
        for (const auto& v : S().vendors)
            if (!v.id.empty()) g_logoQueue.push_back(v.id);

        // API providers: preserve open editors across refreshes.
        std::map<std::string, ApiProvider> old;
        for (auto& a : S().apiProviders) old[a.id] = a;
        std::vector<ApiProvider> as;
        if (r.result.contains("api_providers"))
            for (auto& v : r.result["api_providers"]) {
                ApiProvider a;
                a.id         = v.value("id", "");
                a.label      = v.value("label", a.id);
                a.configured = v.value("configured", false);
                if (v.contains("fields"))
                    for (auto& f : v["fields"]) a.fields.push_back(f.get<std::string>());
                auto it = old.find(a.id);
                if (it != old.end()) {
                    a.editing = it->second.editing;
                    a.saving  = it->second.saving;
                    std::memcpy(a.fieldBuf, it->second.fieldBuf, sizeof(a.fieldBuf));
                }
                as.push_back(std::move(a));
            }
        S().apiProviders = std::move(as);
    });
}

// Vendor logos share the worker lane with real part previews. Wait until search
// and the visible previews settle before using spare capacity for favicons.
static void pumpVendorLogos(AppState& st) {
    if (g_logoQueue.empty() || !S().searchDone || S().searching || S().liveStarting) return;
    if (!S().results.empty() && S().visibleThumbs.empty()) return;
    for (const auto& id : S().visibleThumbs)
        if (!S().thumbs.count(id) && !S().thumbFailed.count(id)) return;
    if (S().thumbBusy.size() >= 2) return;
    while (!g_logoQueue.empty()) {
        const std::string site = g_logoQueue.front();
        g_logoQueue.erase(g_logoQueue.begin());
        const std::string key = "vendorlogo:" + site;
        if (S().thumbs.count(key) || S().thumbFailed.count(key)) continue;  // already resolved
        std::string domain = vendorDomain(site);
        for (const auto& live : S().liveVendors)
            if (live.site == site && !live.domain.empty()) { domain = live.domain; break; }
        requestVendorLogo(st, site, domain);
        return;                                                            // exactly one/frame
    }
}

// Queue a site's favicon if it is neither cached, failed, in flight, nor already queued.
// Result rows can come from commerce sites that vendor_status never lists (it only surfaces
// login-gated adapters), so the Sources row tops the queue up from what it actually draws.
static void wantVendorLogo(const std::string& site) {
    if (site.empty()) return;
    const std::string key = "vendorlogo:" + site;
    if (S().thumbs.count(key) || S().thumbFailed.count(key) || S().thumbBusy.count(key)) return;
    if (std::find(g_logoQueue.begin(), g_logoQueue.end(), site) != g_logoQueue.end()) return;
    g_logoQueue.push_back(site);
}


// Fill a PartsItem from a backend result object (shared by local + live paths).
static PartsItem itemFromJson(const nlohmann::json& it) {
    PartsItem item;
    item.id          = it.value("id",          "");
    item.name        = it.value("name",        "");
    item.vendor      = it.value("vendor",      "");
    item.partNumber  = it.value("part_number", "");
    item.description = it.value("description", "");
    item.fileName    = it.value("file_name",   "");
    item.unit        = it.value("unit",        "ea");
    item.material    = it.value("material",    "");
    item.domain      = it.value("domain",      "");
    item.standard    = it.value("standard",    "");
    item.source      = it.value("source",      "local");
    item.datasheet   = it.value("datasheet",   "");
    item.imageUrl    = it.value("image_url",   "");
    item.license     = it.value("license",     "");
    item.currency    = it.value("currency",    "");
    item.kind        = it.value("kind",        "");
    item.mpn         = it.value("mpn",         "");
    item.manufacturer = it.value("manufacturer", "");
    item.family      = it.value("family",      "");
    item.designation = it.value("designation", "");
    const std::string thumbnail = it.value("thumbnail_url", "");
    if (!thumbnail.empty()) item.imageUrl = thumbnail;
    if (it.contains("model_formats") && it["model_formats"].is_array())
        for (const auto& format : it["model_formats"])
            if (format.is_string()) item.modelFormats.push_back(format.get<std::string>());
    if (it.contains("sizes") && it["sizes"].is_array())
        for (const auto& size : it["sizes"])
            if (size.is_string()) item.sizes.push_back(size.get<std::string>());
    item.score       = it.value("score",       0);
    if (it.contains("insert_params") && it["insert_params"].is_object())
        item.lengthMm = it["insert_params"].value("length_mm", 0.0);
    item.priceUsd    = it.value("price_usd",   0.0);
    item.price       = it.value("price", item.priceUsd);
    item.stock       = it.value("stock", -1);
    item.downloaded  = it.value("downloaded",  false);
    item.downloadable = it.value("downloadable", false);
    item.placeable   = it.value("placeable",   false);
    return item;
}

static void collectNotices(const nlohmann::json& res) {
    if (res.contains("notices") && res["notices"].is_array())
        for (auto& n : res["notices"]) {
            Notice nt;
            nt.site   = n.value("site",   "");
            nt.reason = n.value("reason", "");
            nt.hint   = n.value("hint",   "");
            if (nt.reason == "credentials_required") nt.hint = "Configure API keys in Settings";
            // De-dupe against notices already shown (live polls re-send the full set).
            bool dup = false;
            for (const auto& e : S().notices)
                if (e.site == nt.site && e.reason == nt.reason) { dup = true; break; }
            if (!dup) S().notices.push_back(std::move(nt));
        }
}

// Monotonic search generation. Bumped by doSearch and captured BY VALUE by every async
// search callback: the fast local layer clears S().searching long before the slow live
// fan-out resolves, so two queries in quick succession overlap and the loser must drop its
// payload instead of hijacking the winner's list.
static unsigned g_searchGen = 0;
static nlohmann::json g_searchParams;

// The one search-parameter serializer, shared by the fast local `parts_search` and the slow
// `parts_search_live_start` fan-out: query + category + global filters + facets. Single code
// path so the two call sites can never drift apart again.
//
// Facets are scoped to the selected taxonomy node: S().facetValues is a flat map that
// outlives the selection, so a value set under one category would otherwise still be sent
// while browsing another and silently filter every row away. Only keys declared by the
// current node are emitted (no node — "All Parts", flat mode — means no facets at all).
static void fillSearchParams(nlohmann::json& p) {
    p["query"] = S().searchBuf;
    p["limit"] = 100;
    static const char* sortModes[] = {"relevance", "price_asc", "price_desc", "name"};
    p["sort"] = sortModes[std::clamp(S().sortMode, 0, 3)];
    // Tree mode → taxonomy category_id (incl. vendors.*.<site> leaves = browse that vendor);
    // fallback flat mode keeps the legacy "category" param.
    if (S().taxonomyIsTree) p["category_id"] = S().selectedCategory;
    else                    p["category"]    = S().selectedCategory;
    if (S().priceMin > 0.0f) p["price_min"] = S().priceMin;
    if (S().priceMax > 0.0f) p["price_max"] = S().priceMax;
    if (S().materialBuf[0])  p["material"]  = S().materialBuf;
    if (S().vendorBuf[0])    p["vendor"]    = S().vendorBuf;
    if (S().sourceFilter == 1) p["source"] = "local";
    if (S().sourceFilter == 2) p["source"] = "live";
    // Active facet values: range → {min?,max?}, enum → string list, text → substring.
    if (const TaxNode* node = findTaxNode(S().taxonomyRoot, S().selectedCategory)) {
        nlohmann::json facets = nlohmann::json::object();
        for (const auto& f : node->facets) {
            auto it = S().facetValues.find(f.id);
            if (it == S().facetValues.end()) continue;
            const FacetValue& fv = it->second;
            if (!fv.active()) continue;
            if (fv.rmin != 0.0f || fv.rmax != 0.0f) {
                nlohmann::json range = nlohmann::json::object();
                if (fv.rmin != 0.0f) range["min"] = fv.rmin;
                if (fv.rmax != 0.0f) range["max"] = fv.rmax;
                facets[f.id] = std::move(range);
            } else if (!fv.multi.empty()) {
                facets[f.id] = std::vector<std::string>(fv.multi.begin(), fv.multi.end());
            } else if (fv.text[0]) {
                facets[f.id] = fv.text;
            }
        }
        if (!facets.empty()) p["facets"] = std::move(facets);
    }
}

void doSearch(AppState& st) {
    const unsigned gen = ++g_searchGen;
    S().searching = true;
    S().searchDone = false;
    S().searchErr.clear();
    S().moreError.clear();
    S().results.clear();
    S().notices.clear();
    S().localLoaded = S().localVisible = S().localTotal = 0;
    S().totalExact = S().hasMore = S().loadingMore = false;
    S().hasLive = S().liveStarting = S().liveSearching = S().livePolling = false;
    S().liveToken.clear();
    S().liveCursor = 0;
    nlohmann::json p;
    fillSearchParams(p);
    p["offset"] = 0;
    p["live"] = false;
    g_searchParams = p;
    st.rpc.call("parts_search", p, [gen, &st](const core::RpcResult& r) {
        if (gen != g_searchGen) return;
        S().searching = false;
        S().searchDone = true;
        if (!r.ok) { S().searchErr = r.err.empty() ? "Parts service unavailable" : r.err; return; }
        if (!r.result.is_object()) { S().searchErr = "Invalid search response"; return; }
        g_indexBuilding = g_indexStatusSupported && r.result.value("index_building", false);
        g_indexCount = r.result.value("index_count", 0);
        if (g_indexBuilding) g_researchOnIndexReady = true;
        if (!r.result.value("ok", false)) {
            S().searchErr = rpcProblem(r, "Search failed");
            return;
        }
        collectNotices(r.result);
        if (r.result.contains("results") && r.result["results"].is_array())
            for (const auto& it : r.result["results"])
                S().results.push_back(itemFromJson(it));
        S().localLoaded = S().localVisible = static_cast<int>(S().results.size());
        S().localTotal = r.result.value("total", S().localLoaded);
        S().totalExact = r.result.value("total_exact", false);
        S().hasMore = r.result.value("has_more", false);
        if (S().sourceFilter != 1) startLiveSearch(st);
    });
}

void loadMoreParts(AppState& st) {
    if (!S().hasMore || S().searching || S().loadingMore) return;
    S().loadingMore = true;
    S().moreError.clear();
    const unsigned gen = g_searchGen;
    const int offset = S().localLoaded;
    nlohmann::json p = g_searchParams;
    p["offset"] = offset;
    st.rpc.call("parts_search", p, [gen, offset](const core::RpcResult& r) {
        if (gen != g_searchGen) return;
        S().loadingMore = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().moreError = rpcProblem(r, "Could not load more parts");
            return;
        }
        if (r.result.value("offset", offset) != offset) {
            S().moreError = "The parts service returned an unexpected page; retry.";
            return;
        }
        int received = 0;
        if (r.result.contains("results") && r.result["results"].is_array()) {
            for (const auto& entry : r.result["results"]) {
                ++received;
                PartsItem item = itemFromJson(entry);
                auto found = std::find_if(S().results.begin(), S().results.end(),
                    [&item](const PartsItem& existing) { return existing.id == item.id; });
                if (found != S().results.end()) {
                    if (found - S().results.begin() < S().localVisible) continue;
                    S().results.erase(found);
                }
                S().results.insert(S().results.begin() + S().localVisible, std::move(item));
                ++S().localVisible;
            }
        }
        S().hasLive = static_cast<int>(S().results.size()) > S().localVisible;
        S().localLoaded = offset + received;
        S().localTotal = r.result.value("total", S().localTotal);
        S().totalExact = r.result.value("total_exact", false);
        if (received == 0 && r.result.value("has_more", false))
            S().moreError = "Additional results are unavailable; refine the search.";
        S().hasMore = r.result.value("has_more", false) && received > 0;
        collectNotices(r.result);
    });
}

// Update the live vendor progress and queue favicons behind visible part previews.
static void applyLiveVendors(const nlohmann::json& res) {
    if (!res.contains("vendors") || !res["vendors"].is_array()) return;
    std::vector<LiveVendor> next;
    next.reserve(res["vendors"].size());
    for (auto& v : res["vendors"]) {
        LiveVendor lv;
        lv.site = v.value("site", "");
        if (lv.site.empty()) continue;
        lv.label  = v.value("label", lv.site);
        lv.domain = v.value("domain", "");
        lv.status = v.value("status", std::string("queued"));
        wantVendorLogo(lv.site);
        next.push_back(std::move(lv));
    }
    if (!next.empty()) S().liveVendors = std::move(next);
}

// Inline vendor logos + per-vendor progress bars, drawn right after the "Parts(N)" header while a
// live search fans out. Progress is an HONEST 3-state (queued/loading/done/error): the vendor
// scrape is a single blocking fetch with no incremental page-load signal, so "loading" shows an
// animated indeterminate bar rather than a fabricated percentage.
static void drawLiveVendorStrip(AppState& st) {
    if (!S().liveSearching || S().liveVendors.empty()) return;
    const float s = ui::uiScale;
    const auto& pal = ui::palette;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float logo = 18.0f * s, barH = 3.0f * s, gap = 8.0f * s;
    const float cellH = logo + 3.0f * s + barH;   // logo + gap + progress bar
    const float rowH  = cellH + 5.0f * s;          // vertical stride between wrapped rows
    const float tsec = (float)ImGui::GetTime();
    const int   kMaxRows = 3;                       // cap; overflow past this shows a "+N"

    // Capture the pane's line-start x (wrapped rows flush here) BEFORE SameLine moves the cursor
    // up to the header line's right edge (where row 0 begins).
    const float leftEdge = ImGui::GetCursorScreenPos().x;
    ImGui::SameLine();
    const ImVec2 base = ImGui::GetCursorScreenPos();
    const float rightB = base.x + ImGui::GetContentRegionAvail().x;   // pane right content edge
    const float rowY0  = base.y - 3.0f * s;         // row 0 sits with the header text
    float trackC4[4];
    ui::Palette::withAlpha(pal.border, trackC4, 0.45f);
    const ImU32 trackCol = ui::Palette::toU32(trackC4);

    float x = base.x;
    int   row = 0, lastRow = 0;
    const int N = (int)S().liveVendors.size();
    for (int i = 0; i < N; ++i) {
        auto& lv = S().liveVendors[i];
        // Wrap when this logo would cross the right edge — but never wrap a row's first cell.
        const float rowLeft = (row == 0) ? base.x : leftEdge;
        if (x + logo > rightB && x > rowLeft + 0.5f) {
            if (row + 1 >= kMaxRows) {
                // Out of vertical room within the cap → terminate cleanly with a "+N" count.
                char more[16];
                std::snprintf(more, sizeof(more), "+%d", N - i);
                const float ty = rowY0 + row * rowH + (logo - ImGui::GetTextLineHeight()) * 0.5f;
                dl->AddText(ImVec2(x, ty), ui::Palette::toU32(pal.textDim), more);
                ImGui::SetCursorScreenPos(ImVec2(x, rowY0 + row * rowH));
                ImGui::InvisibleButton("##lv_more",
                                       ImVec2(ImGui::CalcTextSize(more).x + 4.0f * s, cellH));
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%d more vendor%s searching…", N - i,
                                      (N - i) == 1 ? "" : "s");
                break;
            }
            row++;
            x = leftEdge;
        }
        lastRow = row;
        const float y = rowY0 + row * rowH;
        const std::string id = "vendorlogo:" + lv.site;
        const ImVec2 l0(x, y), l1(x + logo, y + logo);
        auto it = S().thumbs.find(id);
        if (it != S().thumbs.end() && it->second.tex) {
            it->second.lastUse = ImGui::GetTime();  // LRU: keep on-screen logos resident
            dl->AddImage((ImTextureID)(intptr_t)it->second.tex, l0, l1);
        } else {
            // Lettered fallback until the favicon loads (or if it never does).
            float bg[4];
            ui::Palette::withAlpha(pal.accent, bg, 0.16f);
            dl->AddRectFilled(l0, l1, ui::Palette::toU32(bg), 4.0f * s);
            char c = lv.label.empty() ? '?' : lv.label[0];
            if (c >= 'a' && c <= 'z') c = (char)(c - 32);
            char L[2] = {c, 0};
            const ImVec2 ts = ImGui::CalcTextSize(L);
            dl->AddText(ImVec2(x + (logo - ts.x) * 0.5f, y + (logo - ts.y) * 0.5f),
                        ui::Palette::toU32(pal.text), L);
        }
        // Progress track + fill.
        const ImVec2 b0(x, y + logo + 3.0f * s), b1(x + logo, y + logo + 3.0f * s + barH);
        dl->AddRectFilled(b0, b1, trackCol, barH * 0.5f);
        if (lv.status == "done") {
            dl->AddRectFilled(b0, b1, ui::Palette::toU32(pal.statusOk), barH * 0.5f);
        } else if (lv.status == "error") {
            dl->AddRectFilled(b0, b1, ui::Palette::toU32(pal.axisX), barH * 0.5f);
        } else if (lv.status == "loading") {
            // Indeterminate sweep (no real % available from a blocking scrape).
            const float w = logo, segW = w * 0.42f;
            float t = tsec * 0.9f;
            t -= (float)(long)t;                       // fractional part (t >= 0)
            const float sx = b0.x - segW + t * (w + segW);
            const float xa = sx < b0.x ? b0.x : sx;
            const float xb = (sx + segW) > b1.x ? b1.x : (sx + segW);
            if (xb > xa)
                dl->AddRectFilled(ImVec2(xa, b0.y), ImVec2(xb, b1.y),
                                  ui::Palette::toU32(pal.accent), barH * 0.5f);
        }  // queued → track only
        ImGui::SetCursorScreenPos(l0);
        ImGui::InvisibleButton((std::string("##lv_") + lv.site).c_str(), ImVec2(logo, cellH));
        if (ImGui::IsItemHovered()) {
            const char* stxt = lv.status == "done" ? "done"
                             : lv.status == "error" ? "failed"
                             : lv.status == "loading" ? "searching…" : "queued";
            ImGui::SetTooltip("%s — %s", lv.label.c_str(), stxt);
        }
        x += logo + gap;
    }
    // Reserve vertical space so the results list flows below the (possibly multi-row) strip.
    const float bottom = rowY0 + lastRow * rowH + cellH;
    ImGui::SetCursorScreenPos(ImVec2(leftEdge, base.y));
    const float reserve = (bottom - base.y) > cellH ? (bottom - base.y) : cellH;
    ImGui::Dummy(ImVec2(1.0f, reserve));
}

// Friendly name for a site key: the live-vendor list first (it carries the backend's label
// for exactly the sites in play), then the account list, else the raw key.
static std::string vendorLabelFor(const std::string& site) {
    for (const auto& lv : S().liveVendors)
        if (lv.site == site && !lv.label.empty()) return lv.label;
    for (const auto& v : S().vendors)
        if (v.id == site && !v.label.empty()) return v.label;
    return site;
}

// Show the sources that contributed live results; queued favicons use spare capacity.
static void drawResultSourcesRow() {
    if (S().results.empty()) return;
    // Live catalog ids are "catalog:<site>::<url>". First-appearance order, capped at 8.
    constexpr size_t kMaxSources = 8;
    std::vector<std::string> sites;
    for (const auto& item : S().results) {
        if (item.id.compare(0, 8, "catalog:") != 0) continue;
        const auto sep = item.id.find("::", 8);
        if (sep == std::string::npos || sep == 8) continue;
        std::string site = item.id.substr(8, sep - 8);
        if (std::find(sites.begin(), sites.end(), site) != sites.end()) continue;
        sites.push_back(std::move(site));
        if (sites.size() >= kMaxSources) break;
    }
    if (sites.empty()) return;

    const float s = ui::uiScale;
    const auto& pal = ui::palette;
    const float logo = 16.0f * s, gap = 6.0f * s;

    ImGui::TextDisabled("Sources");
    ImGui::SameLine();
    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    const ImVec2 start = ImGui::GetCursorScreenPos();
    // Stop short of the pill scrollbar's lane so a logo can never slide under the bar.
    const float  rightB = start.x + ImGui::GetContentRegionAvail().x -
                          ui::kChatScrollbarLane * s;
    const float  y      = start.y + (ImGui::GetTextLineHeight() - logo) * 0.5f;
    float x = start.x;
    for (const auto& site : sites) {
        if (x + logo > rightB && x > start.x + 0.5f) break;   // narrow column — clip cleanly
        const ImVec2 l0(x, y), l1(x + logo, y + logo);
        auto it = S().thumbs.find("vendorlogo:" + site);
        if (it != S().thumbs.end() && it->second.tex) {
            it->second.lastUse = ImGui::GetTime();   // LRU: keep on-screen logos resident
            dl->AddImage((ImTextureID)(intptr_t)it->second.tex, l0, l1);
        } else {
            // Not fetched yet (or a site vendor_status never listed) — queue it and show a
            // lettered tile meanwhile, so the row never collapses to nothing.
            wantVendorLogo(site);
            const std::string lbl = vendorLabelFor(site);
            float bg[4];
            ui::Palette::withAlpha(pal.accent, bg, 0.16f);
            dl->AddRectFilled(l0, l1, ui::Palette::toU32(bg), 4.0f * s);
            char c = lbl.empty() ? '?' : lbl[0];
            if (c >= 'a' && c <= 'z') c = (char)(c - 32);
            const char L[2] = {c, 0};
            const ImVec2 ts = ImGui::CalcTextSize(L);
            dl->AddText(ImVec2(x + (logo - ts.x) * 0.5f, y + (logo - ts.y) * 0.5f),
                        ui::Palette::toU32(pal.text), L);
        }
        const std::string label = vendorLabelFor(site);
        const float textX = x + logo + gap;
        const float textRoom = rightB - textX;
        if (textRoom > 12.0f * s) {
            const std::string shown = ui::kit::elide(label.c_str(), textRoom);
            dl->AddText(ImVec2(textX, start.y), ui::Palette::toU32(pal.textDim), shown.c_str());
            x = textX + ImGui::CalcTextSize(shown.c_str()).x + 12.0f * s;
        } else {
            x += logo + gap;
        }
    }
    // Close the line at its natural height so the results list flows below it.
    ImGui::SetCursorScreenPos(start);
    ImGui::Dummy(ImVec2(x - start.x, logo));
}

void startLiveSearch(AppState& st) {
    S().liveVendors.clear();       // fresh fan-out — drop the previous search's vendor strip
    g_liveStartedAt = ImGui::GetTime();   // burst-poll window for the first vendor's rows
    S().liveStarting = true;
    nlohmann::json p = g_searchParams;
    p.erase("live");
    p.erase("offset");
    p["client"] = "parts-workspace-" +
        std::to_string(reinterpret_cast<std::uintptr_t>(&S()));
    // The local layer returns long before this fan-out resolves, so S().searching is already
    // clear and a newer query may have started meanwhile: pin the generation and let the
    // loser drop its token instead of hijacking the newer search.
    const unsigned gen = g_searchGen;
    st.rpc.call("parts_search_live_start", p, [gen](const core::RpcResult& r) {
        if (gen != g_searchGen) return;
        S().liveStarting = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            Notice n;
            n.site = "Web sources";
            n.reason = "unavailable";
            n.hint = rpcProblem(r, "Web search unavailable");
            S().notices.push_back(std::move(n));
            return;
        }
        collectNotices(r.result);
        std::string token = r.result.value("token", "");
        if (token.empty()) return;
        S().liveToken     = token;
        S().liveCursor    = 0;
        S().liveSearching = true;
        S().lastLivePoll  = 0.0;   // poll on the next tick
        applyLiveVendors(r.result);
    });
}

void pollLiveSearch(AppState& st) {
    if (S().livePolling || S().liveToken.empty()) return;
    S().livePolling  = true;
    S().lastLivePoll = ImGui::GetTime();
    nlohmann::json p;
    p["token"]  = S().liveToken;
    p["cursor"] = S().liveCursor;
    const std::string token = S().liveToken;
    st.rpc.call("parts_search_live_poll", p, [token](const core::RpcResult& r) {
        // Drop results from a superseded job without altering the current poll.
        if (token != S().liveToken) return;
        S().livePolling = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().liveSearching = false;
            Notice n;
            n.site = "Web sources";
            n.reason = "unavailable";
            n.hint = rpcProblem(r, "Web search unavailable");
            S().notices.push_back(std::move(n));
            S().liveToken.clear();
            return;
        }
        if (r.result.contains("results"))
            for (auto& it : r.result["results"]) {
                PartsItem item = itemFromJson(it);
                if (std::none_of(S().results.begin(), S().results.end(),
                                 [&item](const PartsItem& existing) { return existing.id == item.id; })) {
                    S().results.push_back(std::move(item));
                    S().hasLive = true;
                }
            }
        collectNotices(r.result);
        applyLiveVendors(r.result);
        S().liveCursor = r.result.value("cursor", S().liveCursor);
        if (r.result.value("done", false)) {
            S().liveSearching = false;
            S().liveToken.clear();
        }
    });
}

// Standard-part placement and downloaded CAD both yield editable native documents.
// Open either through the same mirror/viewport path, rather than leaving a doc
// silently created in the worker with no tab in the user's workspace.
static void activatePartDocument(AppState& st, const std::string& doc,
                                 const std::string& name, bool standardInsert) {
    st.rpc.call("doc_activate", {{"doc_id", doc}, {"origin", "system"}},
        [&st, name, doc, standardInsert](const core::RpcResult& response) {
            if (standardInsert) S().insertBusy = false;
            if (st.asmPanels) st.asmPanels->closeAllToolForms(st);
            std::string aicm, err;
            const bool activated = response.ok && response.result.is_object() &&
                st.docMirror.adoptActivateResponse(doc, response.result, aicm, err);
            if (!activated) {
                if (!response.ok) err = response.err;
                S().insertNote = "Created " + name + " (" + doc +
                                 "), but could not open it: " + err;
                S().insertError = true;
                st.toast(ui::ToastLevel::Warn, S().insertNote);
                return;
            }
            int existing = -1;
            for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i)
                if (st.docTabs[i].docId == doc) { existing = i; break; }
            if (existing >= 0) {
                st.docTabs[existing].userAdopted = true;
                if (st.activeTab == existing) {
                    st.clearViewportForNewDoc();
                    st.applyDocDrivenVisibility(st.docMirror.tree());
                    if (!aicm.empty()) st.loadDocMeshAsync(aicm);
                } else {
                    st.switchToTab(existing);
                }
            } else {
                st.clearViewportForNewDoc();
                st.applyDocDrivenVisibility(st.docMirror.tree());
                st.addDocTab(false);
                if (!aicm.empty()) st.loadDocMeshAsync(aicm);
            }
            st.activeWorkspace = AppState::Workspace::Model;
            S().insertError = false;
            S().insertNote = (standardInsert ? "Created " : "Imported ") + name + " as a new part";
            st.toast(ui::ToastLevel::Success, S().insertNote);
        });
}

static void updateJobOperation(const std::string& jobId, const std::string& status,
                               const std::string& error, bool busy) {
    for (auto& entry : S().itemOperations) {
        auto& op = entry.second;
        if (op.jobId != jobId || jobId.empty()) continue;
        op.status = status;
        op.error = error;
        op.busy = busy;
    }
}
static void finishJobOperation(const std::string& jobId, const std::string& status,
                               const std::string& error) {
    updateJobOperation(jobId, status, error, false);
    for (auto& entry : S().itemOperations)
        if (entry.second.jobId == jobId) entry.second.jobId.clear();
}


void importQueuedArtifact(AppState& st, const std::string& id, const std::string& artifactPath) {
    updateJobOperation(id, "Importing CAD model…", "", true);
    S().autoImportJobs.erase(id);
    nlohmann::json params = {{"artifact_path", artifactPath}};
    for (auto& job : S().queue) {
        if (job.id != id) continue;
        job.importing = true;
        if (!job.artifactMember.empty()) params["member"] = job.artifactMember;
        if (!job.artifactFormat.empty()) params["format"] = job.artifactFormat;
        break;
    }
    st.rpc.call("parts_import_artifact", params,
        [&st, id](const core::RpcResult& r) {
            std::string label = "CAD model";
            for (auto& job : S().queue)
                if (job.id == id) {
                    job.importing = false;
                    label = job.site + " CAD model";
                }
            const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
            if (!ok) {
                const std::string why = r.ok && r.result.is_object()
                    ? r.result.value("reason", std::string("import failed")) : r.err;
                finishJobOperation(id, "Import failed", why);
                st.toast(ui::ToastLevel::Error, "Import failed: " + why);
                return;
            }
            const std::string doc = r.result.value("doc_id", std::string());
            if (doc.empty()) {
                finishJobOperation(id, "Import failed", "No editable document returned");
                st.toast(ui::ToastLevel::Error, "Import returned no editable document");
                return;
            }
            finishJobOperation(id, "Downloaded and imported CAD model", "");
            activatePartDocument(st, doc, label, false);
        });
}

void pollQueue(AppState& st) {
    if (S().queuePolling) return;
    S().queuePolling  = true;
    S().lastQueuePoll = ImGui::GetTime();
    st.rpc.call("parts_queue_status", {}, [&st](const core::RpcResult& r) {
        S().queuePolling = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().queueError = r.ok && r.result.is_object()
                ? r.result.value("reason", std::string("Queue unavailable"))
                : (r.err.empty() ? "Queue unavailable" : r.err);
            return;
        }
        S().queueError.clear();
        // Preserve per-job "importing" spinners across refreshes.
        std::set<std::string> importing;
        for (auto& j : S().queue)
            if (j.importing) importing.insert(j.id);
        std::vector<QueueJob> jobs;
        bool active = false;
        if (r.result.contains("jobs"))
            for (auto& j : r.result["jobs"]) {
                QueueJob q;
                q.id           = j.value("id", "");
                q.site         = j.value("site", "");
                q.query        = j.value("query", "");
                q.status       = j.value("status", "");
                q.artifactPath = j.value("artifact_path", "");
                if (j.contains("error")) {
                    const auto& e = j["error"];
                    if (e.is_string()) q.error = e.get<std::string>();
                    else if (e.is_object()) {
                        q.error = e.value("hint", std::string());
                        if (q.error.empty()) q.error = e.value("reason", std::string("Download failed"));
                        if (e.value("reason", std::string()) == "credentials_required")
                            q.error = "Configure API keys in Settings";
                        if (e.contains("ask_user")) {
                            const auto& question = e["ask_user"];
                            if (question.is_string()) q.askUser = question.get<std::string>();
                            else if (question.is_object()) {
                                q.askUser = question.value("prompt", std::string());
                                if (question.contains("options") && question["options"].is_array())
                                    for (const auto& option : question["options"]) {
                                        if (!option.is_object()) continue;
                                        const std::string value = option.value("value", std::string());
                                        if (value.empty() || value == "cancel") continue;
                                        q.candidates.push_back(value);
                                        const std::string label = option.value("label", std::string());
                                        q.candidateLabels[value] = label.empty() ? value : label;
                                    }
                            }
                        }
                        if (q.candidates.empty() && e.contains("candidates") && e["candidates"].is_array())
                            for (const auto& candidate : e["candidates"])
                                if (candidate.is_string()) q.candidates.push_back(candidate.get<std::string>());
                        if (q.artifactPath.empty() && e.contains("artifact_path") && e["artifact_path"].is_string())
                            q.artifactPath = e["artifact_path"].get<std::string>();
                        q.canSelect = e.contains("hit") && e["hit"].is_object();
                    }
                }
                if (j.contains("spec_data") && j["spec_data"].is_object()) {
                    q.artifactKind = j["spec_data"].value("artifact_kind", std::string());
                    q.artifactFormat = j["spec_data"].value("format", std::string());
                    q.artifactMember = j["spec_data"].value("member", std::string());
                    q.cadValidated = j["spec_data"].value("cad_validated", false);
                }
                if (j.contains("can_select_member") && j["can_select_member"].is_boolean())
                    q.canSelect = j["can_select_member"].get<bool>();
                if (S().resolvedChoiceJobs.count(q.id)) {
                    q.askUser.clear();
                    q.candidates.clear();
                    q.candidateLabels.clear();
                }
                q.importing    = importing.count(q.id) > 0;
                if (q.status == "pending" || q.status == "running") active = true;
                jobs.push_back(std::move(q));
            }
        S().queue       = std::move(jobs);
        S().queueActive = active;
        for (const auto& job : S().queue) {
            if (job.importing) continue;
            if (job.status == "pending" || job.status == "running")
                updateJobOperation(job.id, job.status == "pending" ? "Download queued" :
                                   "Downloading…", "", true);
            else if (job.status == "done") {
                const std::string status = job.artifactKind == "cad" && !job.cadValidated
                    ? "Downloaded CAD; this format cannot be imported"
                    : (job.artifactKind.empty() ? "Download complete" : "Downloaded " + job.artifactKind);
                updateJobOperation(job.id, status, "", false);
            }
            else if (job.status == "error" || job.status == "cancelled")
                updateJobOperation(job.id, "Download " + job.status, job.error, false);
        }
        for (const auto& job : S().queue) {
            if (!S().autoImportJobs.count(job.id)) continue;
            if (job.status == "done") {
                if (job.artifactKind == "cad" && job.cadValidated && !job.artifactPath.empty())
                    importQueuedArtifact(st, job.id, job.artifactPath);
                else
                    S().autoImportJobs.erase(job.id);
            } else if (job.status == "cancelled" ||
                       (job.status == "error" && job.candidates.empty()))
                S().autoImportJobs.erase(job.id);
        }
    });
}

void dismissQueuedChoice(const std::string& jobId) {
    auto job = std::find_if(S().queue.begin(), S().queue.end(),
        [&jobId](const QueueJob& q) { return q.id == jobId; });
    if (job == S().queue.end() || job->importing) return;
    job->askUser.clear();
    job->candidates.clear();
    job->candidateLabels.clear();
    S().resolvedChoiceJobs.insert(jobId);
    S().autoImportJobs.erase(jobId);
    finishJobOperation(jobId, "Download cancelled", "");
}

void selectQueuedArtifact(AppState& st, const std::string& jobId, const std::string& member) {
    auto job = std::find_if(S().queue.begin(), S().queue.end(),
        [&jobId](const QueueJob& q) { return q.id == jobId; });
    if (job == S().queue.end() || job->importing || !job->canSelect ||
        std::find(job->candidates.begin(), job->candidates.end(), member) == job->candidates.end())
        return;
    job->importing = true;
    updateJobOperation(jobId, "Selecting model…", "", true);
    st.rpc.call("parts_download_select", {{"job_id", jobId}, {"member", member}},
        [&st, jobId](const core::RpcResult& r) {
            for (auto& q : S().queue)
                if (q.id == jobId) q.importing = false;
            const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
            const std::string nextId = ok ? r.result.value("id", std::string()) : std::string();
            if (!ok || nextId.empty()) {
                const std::string why = ok ? "No download job was returned" :
                    rpcProblem(r, "Could not select model");
                updateJobOperation(jobId, "Selection failed", why, false);
                for (auto& q : S().queue)
                    if (q.id == jobId) q.error = why;
                st.toast(ui::ToastLevel::Error, "Selection failed: " + why);
                return;
            }
            S().resolvedChoiceJobs.insert(jobId);
            if (S().autoImportJobs.erase(jobId)) S().autoImportJobs.insert(nextId);
            for (auto& entry : S().itemOperations) {
                auto& op = entry.second;
                if (op.jobId != jobId) continue;
                op.jobId = nextId;
                op.status = "Download queued";
                op.error.clear();
                op.busy = true;
            }
            for (auto& q : S().queue) {
                if (q.id != jobId) continue;
                q.askUser.clear();
                q.candidates.clear();
                q.candidateLabels.clear();
            }
            S().queueActive = true;
            pollQueue(st);
        });
}

void queueDownload(AppState& st, const PartsItem& item) {
    auto& operation = S().itemOperations[item.id];
    if (operation.busy) return;
    operation = {};
    operation.status = "Submitting download…";
    operation.busy = true;
    nlohmann::json p;
    p["item_id"] = item.id;
    p["name"]    = item.name;
    st.rpc.call("parts_download", p, [&st, name = item.name, itemId = item.id, community =
        item.id.rfind("catalog:printables::", 0) == 0 ||
        item.id.rfind("catalog:makerworld::", 0) == 0](const core::RpcResult& r) {
        auto& op = S().itemOperations[itemId];
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok && r.result.value("downloaded", false)) {
            op.busy = false;
            op.status = "Downloaded";
            st.toast(ui::ToastLevel::Info, name + ": local library part — use Insert to place it");
            doSearch(st);
            return;
        }
        const std::string jobId = ok ? r.result.value("id", std::string()) : std::string();
        if (ok && !jobId.empty()) {
            op.jobId = jobId;
            op.status = "Download queued";
            if (community) S().autoImportJobs.insert(jobId);
            st.toast(ui::ToastLevel::Info, "Queued download: " + name);
            S().queueActive = true;
            pollQueue(st);
        } else {
            std::string why = !ok ? rpcProblem(r, "Download failed") : "No download job was returned";
            op.busy = false;
            op.status = "Download failed";
            op.error = why;
            st.toast(ui::ToastLevel::Error, "Download failed: " + why);
        }
    });
}

void insertItem(AppState& st, const PartsItem& item) {
    if (S().insertBusy) return;
    S().insertItemId = item.id;
    const bool intoAssembly = st.activeTab >= 0 &&
        st.activeTab < static_cast<int>(st.docTabs.size()) &&
        st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly;
    const std::string assemblyDoc = intoAssembly ? st.docTabs[st.activeTab].docId : "";
    const std::string projectDir = intoAssembly ? st.plmProjectPath() : "";
    if (intoAssembly && (assemblyDoc.empty() || projectDir.empty() || !st.asmPanels)) {
        S().insertNote = "Cannot insert: the assembly or its project folder is unavailable.";
        S().insertError = true;
        st.toast(ui::ToastLevel::Error, S().insertNote);
        return;
    }
    S().insertBusy = true;
    S().insertError = false;
    S().insertNote = intoAssembly ? "Preparing " + item.name + " for the assembly…"
                                  : "Creating " + item.name + " as a new part…";
    st.toast(ui::ToastLevel::Info, S().insertNote);
    nlohmann::json p;
    p["item_id"] = item.id;
    if (item.lengthMm > 0.0) p["params"] = {{"length_mm", item.lengthMm}};
    if (intoAssembly) {
        p["project_dir"] = projectDir;
        st.rpc.call("parts_prepare_for_assembly", p,
            [&st, name = item.name, assemblyDoc](const core::RpcResult& r) {
                if (st.activeTab < 0 || st.activeTab >= static_cast<int>(st.docTabs.size()) ||
                    st.docTabs[st.activeTab].docId != assemblyDoc) {
                    S().insertBusy = false;
                    S().insertError = true;
                    S().insertNote = "Insertion cancelled: the active assembly changed.";
                    st.toast(ui::ToastLevel::Warn, S().insertNote);
                    return;
                }
                if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                    const std::string why = r.ok && r.result.is_object()
                        ? r.result.value("reason", std::string("could not save part"))
                        : r.err;
                    S().insertBusy = false;
                    S().insertError = true;
                    S().insertNote = "Insert failed: " + why;
                    st.toast(ui::ToastLevel::Error, S().insertNote);
                    return;
                }
                const std::string path = r.result.value("path", std::string());
                if (path.empty()) {
                    S().insertBusy = false;
                    S().insertError = true;
                    S().insertNote = "Insert failed: saved part path was not returned.";
                    st.toast(ui::ToastLevel::Error, S().insertNote);
                    return;
                }
                S().insertNote = "Saved " + name + "; inserting into assembly…";
                st.toast(ui::ToastLevel::Info, S().insertNote);
                st.asmPanels->insertComponentWhenReady(
                    st, path, assemblyDoc,
                    [&st, name](ui::AsmPanels::InsertOutcome outcome, const std::string& error) {
                        S().insertBusy = false;
                        if (outcome == ui::AsmPanels::InsertOutcome::Superseded) {
                            S().insertError = true;
                            S().insertNote = "Insertion cancelled or superseded.";
                            st.toast(ui::ToastLevel::Warn, S().insertNote);
                            return;
                        }
                        S().insertError = outcome == ui::AsmPanels::InsertOutcome::Failure;
                        if (S().insertError) {
                            S().insertNote = "Insert failed: " + error;
                            st.toast(ui::ToastLevel::Error, S().insertNote);
                            return;
                        }
                        if (st.activeWorkspace == AppState::Workspace::PartsLibrary)
                            st.activeWorkspace = AppState::Workspace::Model;
                        S().insertNote = "Inserted " + name + " into the assembly";
                        st.toast(ui::ToastLevel::Success, S().insertNote);
                    });
            });
        return;
    }
    st.rpc.call("parts_insert_into_doc", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            const std::string doc = r.result.contains("doc_id") &&
                                    r.result["doc_id"].is_string()
                ? r.result["doc_id"].get<std::string>() : std::string();
            if (!doc.empty()) {
                // Mechanical placement creates a separate editable document, not an
                // assembly component. Open that document so the result cannot be lost.
                // Activation may compile the new part; do it off the GL thread.
                activatePartDocument(st, doc, name, true);
            } else {
                S().insertBusy = false;
                S().insertNote = "Prepared " + name + "; no new part document was created";
                st.toast(ui::ToastLevel::Info, S().insertNote);
            }
        } else {
            S().insertBusy = false;
            std::string why = r.ok
                ? r.result.value("reason", std::string("insert failed"))
                : r.err;
            std::string hint = r.ok ? r.result.value("hint", "") : "";
            S().insertError = true;
            S().insertNote = "Insert failed: " + why +
                             (hint.empty() ? "" : ("  (" + hint + ")"));
            st.toast(ui::ToastLevel::Error, S().insertNote);
        }
    });
}

// Parts Library → Reverse Engineering delivery. For an un-modeled part (no CAD to
// download), hand its photos/datasheet + identifying info to a fresh RE session so
// the reversing workflow can reconstruct a model. Backend (reveng_start_session):
// seeds any photos/datasheet recorded on the part (part_ref), and the research
// seed (vendor URL → part number → name) drives RE-29's datasheet/web lane so the
// session is populated with no further clicks.
void sendToReverseEngineering(AppState& st, const PartsItem& item) {
    auto& operation = S().itemOperations[item.id];
    if (operation.busy) return;
    operation = {};
    operation.status = "Starting Reverse Engineering…";
    operation.busy = true;
    nlohmann::json p;
    p["name"]     = item.name.empty() ? std::string("Reverse-engineer part") : item.name;
    p["part_ref"] = item.id;   // best-effort seed of on-part assets; harmless if not a hub record
    // Research seed: a vendor listing URL (encoded in a catalog id as
    // "catalog:<site>::<url>") is richest; else the MPN; else the name.
    std::string research;
    const auto up = item.id.find("::http");
    if (up != std::string::npos)      research = item.id.substr(up + 2);
    else if (!item.partNumber.empty()) research = item.partNumber;
    else                               research = item.name;
    if (!research.empty()) p["research"] = research;
    if (const std::string project = reveng::revengProjectDir(st); !project.empty())
        p["project_dir"] = project;

    st.rpc.call("reveng_start_session", p, [&st, name = item.name, id = item.id](const core::RpcResult& r) {
        auto& op = S().itemOperations[id];
        op.busy = false;
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            op.status = "Reverse Engineering session started";
            st.pendingRevengSessionId = r.result.value("session_id", "");
            st.activeWorkspace = AppState::Workspace::ReverseEng;   // jump to the RE tab
            st.toast(ui::ToastLevel::Success, "Sent to Reverse Engineering: " + name);
        } else {
            const std::string why = rpcProblem(r, "Could not start session");
            op.status = "Reverse Engineering failed";
            op.error = why;
            st.toast(ui::ToastLevel::Error, "Reverse-engineer failed: " + why);
        }
    });
}

// RE-32: async one-call "Generate 3D model" for a metadata-only part.
// Calls reveng_make_part_from_library (RE-33 backend: seed→internet
// research→build parametric→attach→placeable), then switches to the RE
// workspace so the seeded session is visible immediately. NEVER a sync call in
// a draw path (GL-freeze history). Hidden for parts that already have a model.
void generateModelFromLibrary(AppState& st, const PartsItem& item) {
    auto& operation = S().itemOperations[item.id];
    if (operation.busy) return;
    operation = {};
    operation.status = "Requesting a 3D model…";
    operation.busy = true;
    nlohmann::json p;
    p["part_ref"] = item.id;
    if (!item.partNumber.empty())  p["part_number"] = item.partNumber;
    // Research seed: same heuristic as sendToReverseEngineering.
    const auto up = item.id.find("::http");
    std::string query;
    if (up != std::string::npos)       query = item.id.substr(up + 2);
    else if (!item.partNumber.empty()) query = item.partNumber;
    else                               query = item.name;
    if (!query.empty()) p["query"] = query;
    if (const std::string project = reveng::revengProjectDir(st); !project.empty())
        p["project_dir"] = project;

    st.rpc.call("reveng_make_part_from_library", p,
                [&st, name = item.name, id = item.id](const core::RpcResult& r) {
        auto& op = S().itemOperations[id];
        op.busy = false;
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            st.pendingRevengSessionId = r.result.value("session_id", "");
            st.activeWorkspace = AppState::Workspace::ReverseEng;
            // The backend searches CAD libraries before building anything; when it found models
            // the session holds them and nothing is being generated.
            const auto found = r.result.find("existing_models");
            op.status = found != r.result.end() && found->is_array() && !found->empty()
                ? "Existing models found" : "Generation started in Reverse Engineering";
            if (found != r.result.end() && found->is_array() && !found->empty())
                st.toast(ui::ToastLevel::Success, "Existing CAD models found for " + name +
                                                      " \xe2\x80\x94 pick one in Reverse Engineering.");
            else
                st.toast(ui::ToastLevel::Success, "Generating 3D model for " + name + "…");
        } else {
            const std::string why = rpcProblem(r, "Could not generate model");
            op.status = "Generate model failed";
            op.error = why;
            st.toast(ui::ToastLevel::Error, "Generate model failed: " + why);
        }
    });
    // Optimistically switch to RE workspace so the user sees the session rail
    // populate as the backend responds (session_id applied in callback above;
    // on failure we stay on RE with an error toast).
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    st.toast(ui::ToastLevel::Info, "Requesting 3D model for " + item.name + "…");
}

void startVendorLogin(AppState& st, VendorEntry& v) {
    v.loginInProgress = true;
    nlohmann::json p;
    p["vendor"] = v.id;
    st.rpc.call("vendor_login", p, [&st, id = v.id](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            st.toast(ui::ToastLevel::Info,
                     "Browser window opened — sign into " + id + ", then close the window.");
        } else {
            std::string why = r.ok ? r.result.value("reason", "login failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Login start failed: " + why);
            for (auto& vv : S().vendors)
                if (vv.id == id) vv.loginInProgress = false;
        }
    });
}

void saveApiCreds(AppState& st, ApiProvider& a) {
    nlohmann::json creds;
    for (size_t i = 0; i < a.fields.size() && i < 2; ++i)
        if (a.fieldBuf[i][0]) creds[a.fields[i]] = a.fieldBuf[i];
    if (creds.empty()) {
        st.toast(ui::ToastLevel::Warn, "Enter the credential fields first.");
        return;
    }
    a.saving = true;
    nlohmann::json p;
    p["provider"] = a.id;
    p["creds"]    = creds;
    st.rpc.call("vendor_creds_set", p, [&st, id = a.id](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        for (auto& aa : S().apiProviders) {
            if (aa.id != id) continue;
            aa.saving = false;
            if (ok) {
                aa.editing = false;
                std::memset(aa.fieldBuf, 0, sizeof(aa.fieldBuf));
            }
        }
        if (ok) {
            st.toast(ui::ToastLevel::Success, "Saved credentials for " + id);
            loadVendors(st, /*force=*/true);
        } else {
            std::string why = r.ok ? r.result.value("reason", "save failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Credential save failed: " + why);
        }
    });
}

}  // namespace parts

// ---- public entry point -------------------------------------------------------

bool partsLibrarySettled(const AppState& st) {
    using namespace parts;
    if (!st.workerStarted) return true;   // the unavailable empty state is final
    const bool catsSettled =
        !S().firstFrame && S().taxonomyLoaded && !S().taxonomyLoading &&
        !S().categoriesLoading && S().taxonomyRetryAt == 0.0 &&
        (S().taxonomyIsTree || !S().categories.empty() ||
         S().taxonomyAttempts >= kTaxonomyMaxAttempts);
    const bool vendorsSettled = S().vendorsLoaded && !S().vendorsLoading;
    const bool resultsSettled = S().searchDone && !S().searching && !S().loadingMore &&
                                !g_indexBuilding && !g_indexPolling && !S().liveStarting &&
                                !S().liveSearching && !S().livePolling;
    static double previewWaitSince = 0.0;
    if (!(catsSettled && vendorsSettled && resultsSettled)) {
        previewWaitSince = 0.0;
        return false;
    }
    if (previewWaitSince == 0.0) previewWaitSince = ImGui::GetTime();
    bool previewsSettled = S().results.empty() || !S().visibleThumbs.empty();
    for (const auto& id : S().visibleThumbs)
        if (!S().thumbs.count(id) && !S().thumbFailed.count(id)) previewsSettled = false;
    return previewsSettled || ImGui::GetTime() - previewWaitSince >= 15.0;
}

void drawPartsLibraryWorkspace(AppState& st) {
    using namespace parts;
    const float sc = ui::uiScale;

    S().fonts = &st.fonts;   // widget TUs (sectionHeader h2) reach fonts through here

    if (S().firstFrame) {
        S().firstFrame = false;
        S().gridView = st.settings.partsGridView;
        if (st.wantPartsShot) {
            if (const char* view = std::getenv("CHUDWARE_PARTS_SHOT_VIEW")) {
                if (std::strcmp(view, "grid") == 0) S().gridView = true;
                else if (std::strcmp(view, "list") == 0) S().gridView = false;
            }
            if (const char* select = std::getenv("CHUDWARE_PARTS_SHOT_SELECT")) {
                char* end = nullptr;
                const long index = std::strtol(select, &end, 10);
                if (end != select && *end == '\0' && index >= 0 && index < 1000000)
                    S().shotSelectIndex = static_cast<int>(index);
            }
        }
        S().sortMode = st.settings.partsSortMode;    // persisted like gridView
        S().expandedCats.clear();
        for (const auto& c : st.settings.partsExpandedCats) S().expandedCats.insert(c);
        if (st.workerStarted) {
            loadTaxonomy(st);   // falls back to loadCategories on method-missing
            loadVendors(st);
            pollQueue(st);
        }
        // The parts screenshot uses the same catalog browse as the real workspace.
        // Never inject a vendor sample into All Parts in place of its available catalog.
        st.wantPartsShot = false;
    }
    // Re-try if worker came up after first frame.
    if (st.workerStarted) {
        if (!S().taxonomyLoaded && !S().taxonomyLoading) loadTaxonomy(st);
        if (!S().vendorsLoaded  && !S().vendorsLoading)  loadVendors(st);
        if (!S().searching && !S().searchDone &&
            (S().taxonomyIsTree || !S().categories.empty() ||
             S().taxonomyAttempts >= kTaxonomyMaxAttempts))
            doSearch(st);   // initial All Parts browse, including a late worker startup
    }
    // Cold-start recovery for the category tree. The parts worker is a Python sidecar that
    // may still be booting when this workspace opens; parts_taxonomy AND its
    // parts_list_categories fallback then both fail on transport, and the one-shot `loaded`
    // latch used to leave the pane stuck on "No catalog data" until the user found Retry.
    // Re-arm on a bounded exponential backoff so it heals itself once the worker answers.
    {
        const double now      = ImGui::GetTime();
        const bool   haveData = S().taxonomyIsTree || !S().categories.empty();
        // A dropped callback would strand taxonomyLoading forever; AsyncRpc's own per-call
        // timeout is 120s, so anything past that is a lost result, not a slow one.
        if (S().taxonomyLoading && now - S().taxonomyStartedAt > 130.0) {
            S().taxonomyLoading = false;
            S().taxonomyLoaded  = true;
            S().taxonomyError   = "The parts service did not respond.";
            st.addLog("[parts] parts_taxonomy produced no result — treating as failed");
        }
        if (st.workerStarted && !haveData && !S().taxonomyLoading &&
            S().taxonomyLoaded && S().taxonomyAttempts < kTaxonomyMaxAttempts) {
            if (S().taxonomyRetryAt == 0.0) {
                // First observed failure — schedule the next attempt.
                ++S().taxonomyAttempts;
                const double delay =
                    std::min(15.0, 1.5 * std::pow(2.0, (double)S().taxonomyAttempts - 1.0));
                S().taxonomyRetryAt = now + delay;
                st.addLog("[parts] category load failed — retry " +
                          std::to_string(S().taxonomyAttempts) + "/" +
                          std::to_string(kTaxonomyMaxAttempts) + " in " +
                          std::to_string((int)delay) + "s");
            } else if (now >= S().taxonomyRetryAt) {
                S().taxonomyRetryAt  = 0.0;
                S().taxonomyLoaded   = false;   // re-arm the one-shot guard
                S().categoriesLoaded = false;
            }
        }
    }

    // Debounced filter → search: any facet/global-filter change stamps filterDirtyAt; fire
    // one doSearch 0.35 s after the last change (no Apply button). Held while a search is
    // already in flight so the dirty edit is never dropped.
    if (S().filterDirtyAt > 0.0 && !S().searching &&
        ImGui::GetTime() - S().filterDirtyAt > 0.35) {
        S().filterDirtyAt = 0.0;
        doSearch(st);
    }

    // Drive the streaming live-vendor poll while a fan-out is in flight so results paint as
    // each vendor lands, without a per-frame RPC. The first two seconds run at ~6.7 Hz (the
    // first vendor typically lands inside that window and its rows should not sit waiting on
    // a 0.4 s tick), then the cadence settles to ~2.5 Hz for the long tail.
    if (S().liveSearching && !S().livePolling) {
        const double now      = ImGui::GetTime();
        const double interval = (now - g_liveStartedAt < 2.0) ? 0.15 : 0.4;
        if (now - S().lastLivePoll > interval) pollLiveSearch(st);
    }

    if (!st.workerStarted) {
        emptyState("parts_all", "Parts Library unavailable",
                   "The Chudware worker service is not running.");
        return;
    }
    // Queue progress must continue even when the filter pane is hidden or replaced by details.
    if (S().queueActive && !S().queuePolling && ImGui::GetTime() - S().lastQueuePoll > 2.5)
        pollQueue(st);


    // Vendor favicons: one queued request per frame, budget permitting (see g_logoQueue).
    pumpVendorLogos(st);

    // Catalog index build → poll ~1 Hz until it lands, then silently re-run the query.
    // Tolerant by construction: transport failure, `ok:false`, or a worker with no
    // parts_index_status at all all resolve to "not building" rather than pinning the
    // workspace in the indexing state forever.
    if (g_indexStatusSupported && g_indexBuilding && !g_indexPolling &&
        ImGui::GetTime() - g_lastIndexPoll > 1.0) {
        g_indexPolling  = true;
        g_lastIndexPoll = ImGui::GetTime();
        st.rpc.call("parts_index_status", {}, [&st](const core::RpcResult& r) {
            g_indexPolling = false;
            if (!(r.ok && r.result.is_object() && r.result.value("ok", false))) {
                g_indexStatusSupported = false;   // stand down for the session
                g_indexBuilding        = false;   // never leave the bar spinning
                g_researchOnIndexReady = false;
                return;
            }
            const bool wasBuilding = g_indexBuilding;
            g_indexBuilding = r.result.value("building", false);
            g_indexCount    = r.result.value("count", g_indexCount);
            if (wasBuilding && !g_indexBuilding && g_researchOnIndexReady) {
                g_researchOnIndexReady = false;
                doSearch(st);   // the query already on screen, now over the full index
            }
        });
    }
    if (S().shotSelectIndex >= 0 && !S().shotSelectApplied && S().searchDone &&
        !S().searching && !g_indexBuilding) {
        if (S().shotSelectIndex < static_cast<int>(S().results.size())) {
            S().selectedId = S().results[S().shotSelectIndex].id;
            S().scrollToSelected = true;
            S().shotSelectApplied = true;
        } else if (S().hasMore && !S().loadingMore && S().moreError.empty()) {
            loadMoreParts(st);
        }
    }
    S().visibleThumbs.clear();


    // Side panes remain available as toolbar drawers when the workspace is narrow.
    const ImVec2 bodyPos = ImGui::GetCursorScreenPos();
    const float bodyH = ImGui::GetContentRegionAvail().y;
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float catFullW   = 240.0f * sc;
    const float filtFullW  = 260.0f * sc;
    // The two SameLine gaps between the columns. MUST match 2 x style ItemSpacing.x (10 px
    // scaled, ui_theme.cpp:327) — under-reserving it pushed the columns wider than the
    // available width, which is what made the side panes read as touching their borders.
    const float gapsW      = 20.0f * sc;
    const float minMidW    = 320.0f * sc;   // results column never narrower than this
    const float kSideFloor = 0.60f;         // shrink the side panes to 60 % before dropping

    float catW = catFullW, filtW = filtFullW;
    const float sidesRoom = totalW - gapsW - minMidW;
    if (sidesRoom < catFullW + filtFullW) {
        const float k = sidesRoom / (catFullW + filtFullW);
        if (k >= kSideFloor) { catW = catFullW * k; filtW = filtFullW * k; }
        else                 { catW = filtW = 0.0f; }
    }
    const bool  sides = catW > 0.0f;
    if (sides && !S().selectedId.empty())
        filtW = std::min(ui::metrics::partsDetailWidth(), totalW - catW - gapsW - minMidW);
    const float midW = std::max(1.0f, totalW - catW - filtW - (sides ? gapsW : 0.0f));

    // ---- three-column body (full height — the search bar lives INSIDE the middle
    // column so Categories/Filters start flush at the top, no dead space) ---------
    if (sides) {
        drawTaxonomyPane(st, catW);
        ImGui::SameLine();
    }

    // Vertical window padding is 8 px, not the frame-tight 4: the search row used to sit
    // welded to the child's top edge.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    // NoScrollbar: ImGui's default bar is replaced by the app-wide pill-on-track scrollbar
    // (ui::drawWindowPillScrollbarV) drawn just before EndChild, like the taxonomy pane and
    // every other list column. parts_results.cpp reserves the matching kChatScrollbarLane on
    // the right of its table/grid arithmetic, so rows never slide under the pill.
    ImGui::BeginChild("##parts_results", ImVec2(midW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    if (browserPanelActive()) {
        drawBrowserPanel(st);
    } else {
        // ---- search bar -----------------------------------------------------------
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * sc, 5 * sc));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,  ImVec2(6 * sc, 6 * sc));

        // Every field on this row wears the workspace accent as its liquid-glass hue; the
        // per-category hues belong to the panes, not to the toolbar.
        const float barTint[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};

        // Short labels on purpose: the combo is sized to the WIDEST of them and shares a row
        // with the search field in a ~430 px results column, so "Price: high to low" cost the
        // field its readable width. Index order is persisted (settings.partsSortMode).
        static const char* kSortItems[] = {
            "Relevance", "Lowest price", "Highest price", "Name"};
        const float fpX     = ImGui::GetStyle().FramePadding.x;
        const float frameH  = ImGui::GetFrameHeight();
        const float gapW    = ImGui::GetStyle().ItemSpacing.x;
        const float searchW = ImGui::CalcTextSize("Search").x + fpX * 2.0f;
        const float segW    = frameH * 1.30f;    // one icon-only view-switch segment
        // Full sort combo = widest entry + frame padding + arrow button.
        float sortFullW = 0.0f;
        for (const char* it : kSortItems)
            sortFullW = std::max(sortFullW, ImGui::CalcTextSize(it).x);
        sortFullW += fpX * 2.0f + frameH;

        // Keep the scrollbar lane clear, and move sort/view to a second row rather
        // than collapsing the search field at narrow widths.
        const float toolbarW = std::max(1.0f, ImGui::GetContentRegionAvail().x -
                                              ui::metrics::px(ui::kChatScrollbarLane));
        const float room = toolbarW - (gapW * 3.0f + searchW + segW * 2.0f);
        const bool splitToolbar = room - frameH < 80.0f * sc;
        const float sortRoom = splitToolbar ? toolbarW - segW * 2.0f - gapW : room;
        const float sortW = splitToolbar
            ? std::max(frameH, std::min(sortFullW, sortRoom))
            : (room - sortFullW >= 120.0f * sc ? sortFullW : frameH);
        const bool sortArrowOnly = sortW < sortFullW;
        const float inputW = splitToolbar
            ? std::max(1.0f, toolbarW - searchW - gapW)
            : std::max(1.0f, room - sortW);

        const ImVec2 qOrigin = ImGui::GetCursorScreenPos();
        const ImVec2 xCenter(qOrigin.x + inputW - 15.0f * sc, qOrigin.y + frameH * 0.5f);

        // The clear affordance is submitted BEFORE the input on purpose. An item submitted
        // AFTER an active InputText cannot be hovered on the mouse-down frame (InputText
        // resets g.ActiveIdAllowOverlap while the button is held), so the first click on it
        // would only blur the field and the second would clear — submitted first it wins the
        // click outright. Its glyph is still painted after the input, below, or the input's
        // frame background would cover it.
        bool clearHit = false, clearHov = false;
        if (S().searchBuf[0]) {
            const float xsz = 20.0f * sc;
            ImGui::SetCursorScreenPos(ImVec2(xCenter.x - xsz * 0.5f, xCenter.y - xsz * 0.5f));
            clearHit = ImGui::InvisibleButton("##parts_q_clear", ImVec2(xsz, xsz));
            clearHov = ImGui::IsItemHovered();
            if (clearHov) ImGui::SetTooltip("Clear search");
            ImGui::SetCursorScreenPos(qOrigin);
        }

        // Both in-field affordances sit in the frame padding: widening FramePadding.x for the
        // input ONLY is the one way to move ImGui's own text/hint origin clear of them.
        parts::glassFieldBegin();
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(26.0f * sc, 5 * sc));
        ImGui::SetNextItemWidth(inputW);
        // ImGui hard-clips a hint at the frame edge ("…electronics, ve"), and an ellipsized
        // one still reads as cut ("search fasteners, …"). Pick the longest WHOLE phrasing that
        // fits the text room between the magnifier inset and the frame's right padding; only
        // the last resort is elided. Re-chosen only when that room (or the font) changes.
        static constexpr const char* kSearchHints[] = {
            "search fasteners, bearings, sections, electronics, vendors\xe2\x80\xa6",
            "search fasteners, bearings, electronics\xe2\x80\xa6",
            "search parts and vendors\xe2\x80\xa6",
            "search parts\xe2\x80\xa6",
            "search",
        };
        static std::string hintShown;
        static float       hintRoom = -1.0f, hintFont = -1.0f;
        const float room4Hint = inputW - 26.0f * sc - fpX;
        if (room4Hint != hintRoom || ImGui::GetFontSize() != hintFont) {
            hintRoom = room4Hint;
            hintFont = ImGui::GetFontSize();
            hintShown.clear();
            for (const char* h : kSearchHints)
                if (ImGui::CalcTextSize(h).x <= room4Hint) { hintShown = h; break; }
            if (hintShown.empty())
                hintShown = ui::kit::elide(kSearchHints[IM_ARRAYSIZE(kSearchHints) - 1],
                                           std::max(1.0f, room4Hint));
        }
        bool hit = ImGui::InputTextWithHint("##parts_q", hintShown.c_str(),
                                            S().searchBuf, sizeof(S().searchBuf),
                                            ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::PopStyleVar();
        {
            ImDrawList*  dl  = ImGui::GetWindowDrawList();
            const ImVec2 mn  = ImGui::GetItemRectMin();
            const ImVec2 mx  = ImGui::GetItemRectMax();
            const ImVec2 gc(mn.x + 13.0f * sc, (mn.y + mx.y) * 0.5f);
            const float  gr  = 4.5f * sc;
            const float  gth = 1.5f * sc;
            const ImU32  gcol = Palette::toU32(palette.textDisabled);
            dl->AddCircle(gc, gr, gcol, 0, gth);
            dl->AddLine(ImVec2(gc.x + gr * 0.72f, gc.y + gr * 0.72f),
                        ImVec2(gc.x + gr * 1.80f, gc.y + gr * 1.80f), gcol, gth);
            if (S().searchBuf[0]) {
                float xc[4];
                Palette::withAlpha(palette.textDim, xc, clearHov ? 1.0f : 0.70f);
                ui::drawIconGlass(dl, "cancel_x", xCenter, 12.0f * sc, Palette::toU32(xc), 0);
            }
        }
        // Closed only HERE, after the magnifier/clear-x decoration: glassFieldBegin splits the
        // window draw list and those two are painted into the widget's channel, so merging
        // earlier would bury them under the field's own gradient.
        parts::glassFieldEnd(barTint);
        if (clearHit) {
            S().searchBuf[0] = '\0';
            doSearch(st);
        }
        ImGui::SameLine();
        // Accent liquid-glass pill. A flat accent Button read as a solid slab next to the
        // glass fields; the wash + hairline border puts it in the same material.
        bool go = false;
        {
            ImDrawList*  dl = ImGui::GetWindowDrawList();
            const ImVec2 p0 = ImGui::GetCursorScreenPos();
            go = ImGui::InvisibleButton("##parts_go", ImVec2(searchW, frameH));
            const bool  hov = ImGui::IsItemHovered();
            const ImVec2 p1(p0.x + searchW, p0.y + frameH);
            ui::glassCardWash(dl, p0, p1, 6.0f, barTint, hov ? 0.48f : 0.34f, 0.35f);
            float br[4];
            Palette::withAlpha(palette.accent, br, hov ? 0.95f : 0.70f);
            dl->AddRect(ImVec2(p0.x + 0.5f, p0.y + 0.5f), ImVec2(p1.x - 0.5f, p1.y - 0.5f),
                        Palette::toU32(br), 6.0f * sc, 0, 1.0f * sc);
            const ImVec2 ts = ImGui::CalcTextSize("Search");
            dl->AddText(ImVec2(p0.x + (searchW - ts.x) * 0.5f, p0.y + (frameH - ts.y) * 0.5f),
                        Palette::toU32(palette.text), "Search");
        }
        if (!splitToolbar) ImGui::SameLine();
        // A one-cell table bounds the kit field to the measured toolbar slot;
        // fieldEnum takes its width from the content region, not SetNextItemWidth.
        const char* sortLabel = kSortItems[std::clamp(S().sortMode, 0,
                                                      (int)IM_ARRAYSIZE(kSortItems) - 1)];
        bool sortChanged = false;
        ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(0, 0));
        if (ImGui::BeginTable("##sort_layout", 1,
                             ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_NoPadOuterX |
                             ImGuiTableFlags_NoPadInnerX, ImVec2(sortW, 0))) {
            ImGui::TableSetupColumn("Sort", ImGuiTableColumnFlags_WidthFixed, sortW);
            ImGui::TableNextColumn();
            sortChanged = ui::kit::fieldEnum(
                "##sort", "", &S().sortMode, kSortItems, IM_ARRAYSIZE(kSortItems),
                ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo)
                    .withPreview(sortArrowOnly ? " " : sortLabel).withTip(sortLabel));
            ImGui::EndTable();
        }
        ImGui::PopStyleVar();
        if (sortChanged) {
            st.settings.partsSortMode = S().sortMode;
            saveSettings(st.settings);
            doSearch(st);   // the worker sorts the complete filtered set before pagination
        }
        if (!splitToolbar || toolbarW >= frameH + segW * 2.0f + gapW) ImGui::SameLine();

        // Two-segment view switch. The old single button was labelled with the mode you were
        // NOT in, so the toolbar never said which view was live; now both affordances are
        // always present and the active one is accent-filled.
        auto viewSegment = [&](const char* id, bool wantGrid, const char* tip) {
            const bool on = (S().gridView == wantGrid);
            // The active segment is an accent liquid-glass tile, painted below from the item
            // rect so it shares the material of the fields on this row; ImGui's own frame
            // goes fully transparent for it. Inactive keeps the stock button surface.
            float bg[4], bgHov[4];
            if (on) {
                bg[0] = bg[1] = bg[2] = bg[3] = 0.0f;
                bgHov[0] = bgHov[1] = bgHov[2] = bgHov[3] = 0.0f;
            } else {
                Palette::withAlpha(palette.button,        bg,    0.80f);
                Palette::withAlpha(palette.buttonHovered, bgHov, 1.0f);
            }
            ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(bg[0], bg[1], bg[2], bg[3]));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(bgHov[0], bgHov[1], bgHov[2], bgHov[3]));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(bgHov[0], bgHov[1], bgHov[2], bgHov[3]));
            const bool clicked = ImGui::Button(id, ImVec2(segW, frameH));
            ImGui::PopStyleColor(3);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);

            // The atlas has no grid/list glyph, so both are drawn: a 2x2 dot matrix and three
            // stacked rules — legible at 20px where a text label would not fit the segment.
            ImDrawList*  dl = ImGui::GetWindowDrawList();
            const ImVec2 a  = ImGui::GetItemRectMin();
            const ImVec2 b  = ImGui::GetItemRectMax();
            if (on) {
                const bool segHov = ImGui::IsItemHovered();
                ui::glassCardWash(dl, a, b, 6.0f, barTint, segHov ? 0.44f : 0.34f, 0.35f);
                float sbr[4];
                Palette::withAlpha(palette.accent, sbr, segHov ? 0.90f : 0.62f);
                dl->AddRect(ImVec2(a.x + 0.5f, a.y + 0.5f), ImVec2(b.x - 0.5f, b.y - 0.5f),
                            Palette::toU32(sbr), 6.0f * sc, 0, 1.0f * sc);
            }
            const ImVec2 c((a.x + b.x) * 0.5f, (a.y + b.y) * 0.5f);
            const ImU32  gcol = Palette::toU32(on ? palette.accent : palette.textDim);
            if (wantGrid) {
                const float d = 3.4f * sc, gap = 2.2f * sc;
                for (int i = 0; i < 4; ++i) {
                    const float ox = ((i & 1) ? 0.5f : -0.5f) * (d + gap);
                    const float oy = ((i & 2) ? 0.5f : -0.5f) * (d + gap);
                    dl->AddRectFilled(ImVec2(c.x + ox - d * 0.5f, c.y + oy - d * 0.5f),
                                      ImVec2(c.x + ox + d * 0.5f, c.y + oy + d * 0.5f),
                                      gcol, 1.0f * sc);
                }
            } else {
                const float w = 10.0f * sc, gap = 3.6f * sc, th = 1.6f * sc;
                for (int i = -1; i <= 1; ++i)
                    dl->AddLine(ImVec2(c.x - w * 0.5f, c.y + i * gap),
                                ImVec2(c.x + w * 0.5f, c.y + i * gap), gcol, th);
            }
            return clicked;
        };
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0.0f, 6 * sc));  // segments touch
        const bool wantGridView = viewSegment("##parts_view_grid", true,  "Grid view");
        ImGui::SameLine();
        const bool wantListView = viewSegment("##parts_view_list", false, "List view");
        ImGui::PopStyleVar();
        if ((wantGridView && !S().gridView) || (wantListView && S().gridView)) {
            S().gridView = !S().gridView;
            st.settings.partsGridView = S().gridView;
            saveSettings(st.settings);
        }
        ImGui::PopStyleVar(2);

        if (hit || go)
            doSearch(st);
        if (!sides) {
            if (chipToggle("##show_categories", "Categories", false))
                ImGui::OpenPopup("##categories_drawer");
            char filtersLabel[48];
            const int active = activeFilterCount();
            if (active) std::snprintf(filtersLabel, sizeof(filtersLabel), "Filters (%d)", active);
            else std::snprintf(filtersLabel, sizeof(filtersLabel), "Filters");
            if (chipWidth("Categories") + chipWidth(filtersLabel) +
                ImGui::GetStyle().ItemSpacing.x <= toolbarW) ImGui::SameLine();
            if (chipToggle("##show_filters", filtersLabel, active > 0))
                ImGui::OpenPopup("##filters_drawer");
            const ImVec2 drawerSize(std::max(1.0f, std::min(320.0f * sc, totalW - 20.0f * sc)),
                                    std::max(1.0f, std::min(600.0f * sc, bodyH - 20.0f * sc)));
            ImGui::SetNextWindowSize(drawerSize, ImGuiCond_Appearing);
            if (ImGui::BeginPopup("##categories_drawer")) {
                drawTaxonomyPane(st, ImGui::GetContentRegionAvail().x);
                ImGui::EndPopup();
            }
            ImGui::SetNextWindowSize(drawerSize, ImGuiCond_Appearing);
            if (ImGui::BeginPopup("##filters_drawer")) {
                drawFiltersBlock(st, ImGui::GetContentRegionAvail().x);
                ImGui::EndPopup();
            }
        }
        handlePartsSelectionKeys(st);

        ImGui::Spacing();
        ImGui::Dummy(ImVec2(0.0f, 3.0f * sc));   // unweld the header from the toolbar
        {
            char countBuf[96];
            const int live = std::max(0, static_cast<int>(S().results.size()) - S().localVisible);
            if (S().totalExact)
                std::snprintf(countBuf, sizeof(countBuf), "%d of %d", S().localVisible, S().localTotal);
            else
                std::snprintf(countBuf, sizeof(countBuf), "%d of %d+", S().localVisible, S().localTotal);
            if (live > 0) {
                const size_t len = std::strlen(countBuf);
                std::snprintf(countBuf + len, sizeof(countBuf) - len, " Library · %d web", live);
            }
            const float headerW = std::max(1.0f, ImGui::GetContentRegionAvail().x -
                                                   ui::metrics::px(ui::kChatScrollbarLane));
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
            ImGui::BeginChild("##parts_header", ImVec2(headerW, 0),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
                              ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
            const bool showCount = S().searchDone && S().searchErr.empty();
            const bool sameLine = ImGui::CalcTextSize(countBuf).x + 100.0f * sc < headerW;
            sectionHeader("Parts", /*firstInPane=*/true, "parts_all",
                          showCount && sameLine ? countBuf : nullptr);
            if (showCount && !sameLine) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
                ImGui::TextWrapped("%s", countBuf);
                ImGui::PopStyleColor();
            }
            ImGui::EndChild();
            ImGui::PopStyleVar();
        }
        // While the background vendor fan-out is still running, show each vendor being searched
        // (its live logo + a per-vendor progress bar) inline under the "Parts" header. Once it
        // ends that strip is gone, so the persistent Sources row takes over and keeps the logos
        // of the vendors that actually contributed rows on screen.
        if (S().liveSearching && !S().liveVendors.empty()) {
            drawLiveVendorStrip(st);
        } else if ((S().liveStarting || S().liveSearching) && !S().results.empty()) {
            ImGui::TextDisabled("Searching web sources…");
        } else {
            drawResultSourcesRow();
        }
        if (S().hasLive) {
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + std::max(1.0f,
                ImGui::GetContentRegionAvail().x - ui::metrics::px(ui::kChatScrollbarLane)));
            ImGui::TextDisabled("Library sorted by %s; web results arrive separately.", sortLabel);
            ImGui::PopTextWrapPos();
        }
        // Cold catalog: the rows below are only what is indexed so far. Say so with an
        // indeterminate bar — an index build has no honest percentage — and note that the
        // search re-runs itself (see g_researchOnIndexReady) the moment it completes.
        if (g_indexBuilding) {
            ContentInset inset;
            ImDrawList*  dl   = ImGui::GetWindowDrawList();
            const float  barW = std::max(1.0f, ImGui::GetContentRegionAvail().x -
                                                ui::metrics::px(ui::kChatScrollbarLane));
            const float  barH = 2.0f * sc;
            const ImVec2 b0   = ImGui::GetCursorScreenPos();
            const ImVec2 b1(b0.x + barW, b0.y + barH);
            float track[4];
            Palette::withAlpha(palette.border, track, 0.45f);
            dl->AddRectFilled(b0, b1, Palette::toU32(track), barH * 0.5f);
            const float segW = barW * 0.28f;
            float t = (float)ImGui::GetTime() * 0.75f;
            t -= (float)(long)t;                       // fractional part (t >= 0)
            const float sx = b0.x - segW + t * (barW + segW);
            const float xa = std::max(sx, b0.x);
            const float xb = std::min(sx + segW, b1.x);
            if (xb > xa)
                dl->AddRectFilled(ImVec2(xa, b0.y), ImVec2(xb, b1.y),
                                  Palette::toU32(palette.accent), barH * 0.5f);
            ImGui::Dummy(ImVec2(barW, barH));
            char idxMsg[96];
            std::snprintf(idxMsg, sizeof(idxMsg), "Indexing catalog… %d part%s so far",
                          g_indexCount, g_indexCount == 1 ? "" : "s");
            if (S().fonts && S().fonts->body) ImGui::PushFont(S().fonts->body);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.95f));
            ImGui::TextUnformatted(idxMsg);
            ImGui::PopStyleColor();
            if (S().fonts && S().fonts->body) ImGui::PopFont();
        }
        if (!S().notices.empty()) {
            int unavailable = 0, signIn = 0, credentials = 0, other = 0;
            bool kicadDeferred = false;
            for (const auto& n : S().notices) {
                if (n.reason == "cef_unavailable") ++unavailable;
                else if (n.reason == "login_required") ++signIn;
                else if (n.reason == "credentials_required") ++credentials;
                else if (n.reason == "native_browse_deferred") kicadDeferred = true;
                else ++other;
            }
            if (unavailable) ImGui::TextDisabled("Some browser sources are unavailable");
            if (signIn) ImGui::TextDisabled("%d vendor%s require sign-in · see Vendors",
                                           signIn, signIn == 1 ? "" : "s");
            if (credentials) ImGui::TextDisabled("Configure API keys in Settings");
            if (kicadDeferred) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
                ImGui::TextWrapped("Select Electronics or search to include installed KiCad parts.");
                ImGui::PopStyleColor();
            }
            if (other) ImGui::TextDisabled("%d source%s could not be searched",
                                          other, other == 1 ? "" : "s");
            if (ImGui::IsItemHovered()) {
                std::string tip;
                for (const auto& n : S().notices) {
                    if (!tip.empty()) tip += "\n";
                    tip += n.site + ": " + (n.hint.empty() ? n.reason : n.hint);
                }
                ImGui::SetTooltip("%s", tip.c_str());
            }
        }
        if (!S().insertNote.empty()) {
            const float* tone = S().insertError ? palette.statusErr : palette.textDim;
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(tone[0], tone[1], tone[2], 1.0f));
            ImGui::TextWrapped("%s", S().insertNote.c_str());
            ImGui::PopStyleColor();
        }
        if (S().gridView) drawResultsGrid(st);
        else              drawResultsList(st);
        if (!S().moreError.empty()) ImGui::TextWrapped("Could not load more: %s", S().moreError.c_str());
        if (S().hasMore) {
            ImGui::BeginDisabled(S().loadingMore);
            if (ImGui::Button(S().loadingMore ? "Loading…" : "Load more")) loadMoreParts(st);
            ImGui::EndDisabled();
        }
        if (S().searchDone && S().searchErr.empty() && !S().totalExact && !S().hasMore)
            ImGui::TextWrapped("Library results may be incomplete; refine the search to narrow them.");
        flushThumbRequests(st);
    }
    // App pill scrollbar on the results column's right edge (no-op unless it overflows).
    // Outside the if/else so the embedded browser panel gets the same bar.
    {
        const ImVec2 cwp = ImGui::GetWindowPos();
        const ImVec2 cws = ImGui::GetWindowSize();
        const ui::AdaptiveSurface& surf = ui::adaptivePanel(
            ui::panelId("parts_results_glass"),
            ImRect(cwp, ImVec2(cwp.x + cws.x, cwp.y + cws.y)));
        ui::drawWindowPillScrollbarV(surf, ui::kChatScrollbar);
    }
    ImGui::EndChild();

    if (sides) {
        ImGui::SameLine();
        if (S().selectedId.empty()) {
            drawFiltersBlock(st, filtW);
        } else {
            ImGui::BeginChild("##part_details", ImVec2(filtW, 0), ImGuiChildFlags_Borders);
            drawPartDetails(st);
            ImGui::EndChild();
        }
    }
    if (!sides && !S().selectedId.empty() && !browserPanelActive()) {
        const float width = std::min(ui::metrics::partsDetailWidth(), totalW);
        ImGui::SetNextWindowPos(ImVec2(bodyPos.x + totalW - width, bodyPos.y), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(width, std::max(1.0f, bodyH)), ImGuiCond_Always);
        const ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
            ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoCollapse;
        if (ImGui::Begin("##parts_detail_drawer", nullptr, flags)) drawPartDetails(st);
        ImGui::End();
    }

    drawVendorsPopup(st);          // floating "Vendor Accounts" window (no-op when closed)
}

}  // namespace ui
