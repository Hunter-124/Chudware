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
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui/icons.h"      // drawIconGlass — the in-field search-clear glyph
#include "ui_theme.h"

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

// ---- async loaders -------------------------------------------------------------

void loadCategories(AppState& st) {
    if (S().categoriesLoaded || S().categoriesLoading) return;
    S().categoriesLoading = true;
    st.rpc.call("parts_list_categories", {}, [](const core::RpcResult& r) {
        S().categoriesLoading = false;
        S().categoriesLoaded  = true;
        if (!r.ok || !r.result.is_object()) return;
        if (!r.result.value("ok", false) || !r.result.contains("categories")) return;
        S().categories.clear();
        for (auto& c : r.result["categories"]) {
            Category cat;
            cat.id       = c.value("id",        "");
            cat.label    = c.value("label",     "");
            cat.parentId = c.value("parent_id", "");
            S().categories.push_back(std::move(cat));
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
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;

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

// Client-side result sort, shared by the initial local search and each live append
// so the merged list stays ordered as vendor results trickle in. Relevance (0) keeps
// the backend order (local first, then live in arrival order).
static void applyResultSort() {
    auto& v = S().results;
    switch (S().sortMode) {
        case 1: std::stable_sort(v.begin(), v.end(),
                    [](const PartsItem& a, const PartsItem& b) {
                        if ((a.priceUsd > 0) != (b.priceUsd > 0)) return a.priceUsd > 0;
                        return a.priceUsd < b.priceUsd; });
                break;
        case 2: std::stable_sort(v.begin(), v.end(),
                    [](const PartsItem& a, const PartsItem& b) { return a.priceUsd > b.priceUsd; });
                break;
        case 3: std::stable_sort(v.begin(), v.end(),
                    [](const PartsItem& a, const PartsItem& b) { return a.name < b.name; });
                break;
        default: break;
    }
}

// Fill a PartsItem from a backend result object (shared by local + live paths).
static PartsItem itemFromJson(const nlohmann::json& it) {
    PartsItem item;
    item.id          = it.value("id",          "");
    item.name        = it.value("name",        "");
    item.vendor      = it.value("vendor",      "");
    item.partNumber  = it.value("part_number", "");
    item.description = it.value("description", "");
    item.unit        = it.value("unit",        "ea");
    item.material    = it.value("material",    "");
    item.domain      = it.value("domain",      "");
    item.standard    = it.value("standard",    "");
    item.source      = it.value("source",      "local");
    item.datasheet   = it.value("datasheet",   "");
    item.imageUrl    = it.value("image_url",   "");
    item.license     = it.value("license",     "");
    item.priceUsd    = it.value("price_usd",   0.0);
    item.downloaded  = it.value("downloaded",  false);
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
    if (S().searching) return;
    S().searching  = true;
    S().searchDone = false;
    S().searchErr.clear();
    S().results.clear();
    // A fresh query invalidates any in-flight live job (stale polls are dropped) and
    // supersedes every async callback still carrying an older generation.
    S().liveSearching = false;
    S().liveToken.clear();
    S().liveCursor = 0;
    const unsigned gen = ++g_searchGen;
    nlohmann::json p;
    fillSearchParams(p);
    p["live"] = false;   // instant local layer only; live vendors stream separately
    st.rpc.call("parts_search", p, [gen, &st](const core::RpcResult& r) {
        S().searching  = false;
        // Superseded by a newer query while this one was in flight — drop its rows.
        if (gen != g_searchGen) return;
        S().searchDone = true;
        if (!r.ok) { S().searchErr = r.err; return; }
        if (!r.result.is_object()) return;
        // Catalog index build (additive fields; absent on an older worker → false/0). While
        // the index builds these rows are only a prefix of the catalog, so arm an automatic
        // re-search: the user should never have to press Search twice for a cold cache.
        g_indexBuilding = g_indexStatusSupported && r.result.value("index_building", false);
        g_indexCount    = r.result.value("index_count", 0);
        if (g_indexBuilding) g_researchOnIndexReady = true;
        if (!r.result.value("ok", false)) {
            S().searchErr = r.result.value("reason", "search failed");
            return;
        }
        S().hasLive = r.result.value("has_live", false);
        S().results.clear();
        // Per-site skip notices (login_required stubs) — shown as a hint line under the
        // results header instead of blank rows.
        S().notices.clear();
        collectNotices(r.result);
        if (r.result.contains("results"))
            for (auto& it : r.result["results"])
                S().results.push_back(itemFromJson(it));
        // Client-side sort (relevance = backend order). 1 = price low→high (zero-price
        // last), 2 = price high→low, 3 = name.
        applyResultSort();
        // Local results are up — now stream live vendor hits in the background so the
        // slow browser fan-out never blocks the initial paint. Skipped when the user
        // has filtered to Local-only.
        if (S().sourceFilter != 1)
            startLiveSearch(st);
    });
}

// Update S().liveVendors from a live-search start/poll payload's "vendors" array and kick a live
// logo (favicon) fetch for each. Replaces the list each poll so per-vendor status stays current.
static void applyLiveVendors(AppState& st, const nlohmann::json& res) {
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
        requestVendorLogo(st, lv.site, lv.domain);
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

void startLiveSearch(AppState& st) {
    S().liveVendors.clear();       // fresh fan-out — drop the previous search's vendor strip
    g_liveStartedAt = ImGui::GetTime();   // burst-poll window for the first vendor's rows
    nlohmann::json p;
    fillSearchParams(p);
    // The local layer returns long before this fan-out resolves, so S().searching is already
    // clear and a newer query may have started meanwhile: pin the generation and let the
    // loser drop its token instead of hijacking the newer search.
    const unsigned gen = g_searchGen;
    st.rpc.call("parts_search_live_start", p, [gen, &st](const core::RpcResult& r) {
        if (gen != g_searchGen) return;   // superseded by a newer query
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;
        std::string token = r.result.value("token", "");
        if (token.empty() || r.result.value("done", false)) return;  // no live vendors
        S().liveToken     = token;
        S().liveCursor    = 0;
        S().liveSearching = true;
        S().lastLivePoll  = 0.0;   // poll on the next tick
        applyLiveVendors(st, r.result);
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
    st.rpc.call("parts_search_live_poll", p, [token, &st](const core::RpcResult& r) {
        S().livePolling = false;
        // Drop results from a superseded job (a newer query changed the token).
        if (token != S().liveToken) return;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            S().liveSearching = false;   // give up quietly; local results stay
            return;
        }
        bool appended = false;
        if (r.result.contains("results"))
            for (auto& it : r.result["results"]) {
                S().results.push_back(itemFromJson(it));
                S().hasLive = true;
                appended = true;
            }
        collectNotices(r.result);
        applyLiveVendors(st, r.result);
        S().liveCursor = r.result.value("cursor", S().liveCursor);
        if (appended) applyResultSort();
        if (r.result.value("done", false)) {
            S().liveSearching = false;
            S().liveToken.clear();
        }
    });
}

void pollQueue(AppState& st) {
    if (S().queuePolling) return;
    S().queuePolling  = true;
    S().lastQueuePoll = ImGui::GetTime();
    st.rpc.call("parts_queue_status", {}, [](const core::RpcResult& r) {
        S().queuePolling = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;
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
                q.error        = j.value("error", "");
                q.importing    = importing.count(q.id) > 0;
                if (q.status == "pending" || q.status == "running") active = true;
                jobs.push_back(std::move(q));
            }
        S().queue       = std::move(jobs);
        S().queueActive = active;
    });
}

void queueDownload(AppState& st, const PartsItem& item) {
    nlohmann::json p;
    p["item_id"] = item.id;
    p["name"]    = item.name;
    st.rpc.call("parts_download", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok && r.result.value("downloaded", false)) {
            st.toast(ui::ToastLevel::Info, name + ": local library part — use Insert to place it");
            return;
        }
        if (ok) {
            st.toast(ui::ToastLevel::Success, "Queued download: " + name);
            S().queueActive = true;
            pollQueue(st);
        } else {
            std::string why = r.ok ? r.result.value("reason", "download failed") : r.err;
            st.toast(ui::ToastLevel::Error, "Download failed: " + why);
        }
    });
}

void insertItem(AppState& st, const PartsItem& item) {
    nlohmann::json p;
    p["item_id"] = item.id;
    st.rpc.call("parts_insert_into_doc", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            std::string doc = r.result.value("doc_id", "");
            st.toast(ui::ToastLevel::Success,
                     "Inserted " + name + (doc.empty() ? "" : (" → " + doc)));
        } else {
            std::string why = r.ok
                ? r.result.value("reason", std::string("insert failed"))
                : r.err;
            std::string hint = r.ok ? r.result.value("hint", "") : "";
            st.toast(ui::ToastLevel::Error,
                     "Insert failed: " + why + (hint.empty() ? "" : ("  (" + hint + ")")));
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

    st.rpc.call("reveng_start_session", p, [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            st.pendingRevengSessionId = r.result.value("session_id", "");
            st.activeWorkspace = AppState::Workspace::ReverseEng;   // jump to the RE tab
            st.toast(ui::ToastLevel::Success, "Sent to Reverse Engineering: " + name);
        } else {
            std::string why = r.ok
                ? r.result.value("reason", std::string("could not start session"))
                : r.err;
            std::string hint = r.ok ? r.result.value("hint", "") : "";
            st.toast(ui::ToastLevel::Error,
                     "Reverse-engineer failed: " + why + (hint.empty() ? "" : ("  (" + hint + ")")));
        }
    });
}

// RE-32: async one-call "Generate 3D model" for a metadata-only part.
// Calls reveng_make_part_from_library (RE-33 backend: seed→internet
// research→build parametric→attach→placeable), then switches to the RE
// workspace so the seeded session is visible immediately. NEVER a sync call in
// a draw path (GL-freeze history). Hidden for parts that already have a model.
void generateModelFromLibrary(AppState& st, const PartsItem& item) {
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

    st.rpc.call("reveng_make_part_from_library", p,
                [&st, name = item.name](const core::RpcResult& r) {
        bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (ok) {
            st.pendingRevengSessionId = r.result.value("session_id", "");
            st.activeWorkspace = AppState::Workspace::ReverseEng;
            st.toast(ui::ToastLevel::Success, "Generating 3D model for " + name + "…");
        } else {
            std::string why = r.ok
                ? r.result.value("reason", std::string("generate failed"))
                : r.err;
            std::string hint = r.ok ? r.result.value("hint", "") : "";
            st.toast(ui::ToastLevel::Error,
                     "Generate model failed: " + why +
                         (hint.empty() ? "" : ("  (" + hint + ")")));
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

void drawPartsLibraryWorkspace(AppState& st) {
    using namespace parts;
    const float sc = ui::uiScale;

    S().fonts = &st.fonts;   // widget TUs (sectionHeader h2) reach fonts through here

    if (S().firstFrame) {
        S().firstFrame = false;
        S().gridView = st.settings.partsGridView;
        S().sortMode = st.settings.partsSortMode;    // persisted like gridView
        S().expandedCats.clear();
        for (const auto& c : st.settings.partsExpandedCats) S().expandedCats.insert(c);
        if (st.workerStarted) {
            loadTaxonomy(st);   // falls back to loadCategories on method-missing
            loadVendors(st);
            pollQueue(st);
        }
        // RE-32 / CHUDWARE_SHOT=parts: seed a synthetic no-model result so the
        // "Generate 3D model" button is visible in the headless screenshot without
        // needing the worker to be running. The item has placeable=false so all
        // action buttons (including "Generate 3D model") render on hover.
        if (st.wantPartsShot) {
            st.wantPartsShot = false;
            PartsItem demo;
            demo.id          = "catalog:mcmaster::https://www.mcmaster.com/91251A542/";
            demo.name        = "Socket Head Cap Screw M8×30 (demo)";
            demo.vendor      = "McMaster-Carr";
            demo.partNumber  = "91251A542";
            demo.description = "Class 12.9 alloy steel, black-oxide finish";
            demo.material    = "Alloy Steel";
            demo.domain      = "fastener";
            demo.source      = "live";
            demo.priceUsd    = 0.18;
            demo.unit        = "ea";
            demo.downloaded  = false;
            demo.placeable   = false;   // no 3D model → "Generate 3D model" button shows
            S().results.push_back(std::move(demo));
            S().searchDone  = true;
        }
    }
    // Re-try if worker came up after first frame.
    if (st.workerStarted) {
        if (!S().taxonomyLoaded && !S().taxonomyLoading) loadTaxonomy(st);
        if (!S().vendorsLoaded  && !S().vendorsLoading)  loadVendors(st);
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

    // Layout: [taxonomy tree 240px] | [results flex] | [filters + vendor strip 260px].
    // The docked AI Copilot column can squeeze this workspace well below the three columns'
    // natural width, and ImGui reads a negative child width as "available minus |w|" — a
    // silently wrong layout rather than an error. So the columns degrade instead: results
    // are the primary content and keep a hard floor, the two side panes shrink
    // proportionally, and once even a 60 %-width side pane will not fit they drop out.
    const float totalW     = ImGui::GetContentRegionAvail().x;
    const float catFullW   = 240.0f * sc;
    const float filtFullW  = 260.0f * sc;
    const float gapsW      = 12.0f * sc;    // the two SameLine gaps between the columns
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
    const float midW  = std::max(minMidW, totalW - catW - filtW - (sides ? gapsW : 0.0f));

    // ---- three-column body (full height — the search bar lives INSIDE the middle
    // column so Categories/Filters start flush at the top, no dead space) ---------
    if (sides) {
        drawTaxonomyPane(st, catW);
        ImGui::SameLine();
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 4 * sc));
    ImGui::BeginChild("##parts_results", ImVec2(midW, 0), ImGuiChildFlags_None);
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

        // Sort combo width = widest entry + frame padding + arrow button.
        static const char* kSortItems[] = {
            "Relevance", "Price: low to high", "Price: high to low", "Name"};
        float sortW = 0.0f;
        for (const char* it : kSortItems)
            sortW = std::max(sortW, ImGui::CalcTextSize(it).x);
        sortW += ImGui::GetStyle().FramePadding.x * 2.0f + ImGui::GetFrameHeight();

        // The results column has a floor but the fixed items on this row can still eat the
        // whole line — never hand the input a negative width. The reserve is MEASURED from
        // the widgets that actually follow instead of a literal, so growing the view switch
        // from one button to two segments cannot silently push them off the row.
        const float frameH  = ImGui::GetFrameHeight();
        const float gapW    = ImGui::GetStyle().ItemSpacing.x;
        const float searchW = ImGui::CalcTextSize("Search").x +
                              ImGui::GetStyle().FramePadding.x * 2.0f;
        const float segW    = frameH * 1.30f;    // one icon-only view-switch segment
        const float inputW  = std::max(80.0f * sc,
            ImGui::GetContentRegionAvail().x - (gapW * 3.0f + searchW + sortW + segW * 2.0f));

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
        bool hit = ImGui::InputTextWithHint("##parts_q",
                                    "search fasteners, bearings, sections, electronics, vendors…",
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
        ImGui::SameLine();
        ImGui::SetNextItemWidth(sortW);
        parts::glassFieldBegin();
        const bool sortChanged =
            ImGui::Combo("##sort", &S().sortMode, kSortItems, IM_ARRAYSIZE(kSortItems));
        parts::glassFieldEnd(barTint);
        if (sortChanged) {
            st.settings.partsSortMode = S().sortMode;
            saveSettings(st.settings);
            applyResultSort();   // cosmetic — never re-run the local + vendor fan-out
        }
        ImGui::SameLine();

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

        if ((hit || go) && !S().searching)
            doSearch(st);

        ImGui::Spacing();
        {
            // The count rides in the header's trailing slot, not baked into its label — a
            // "Parts  (128)" label makes the count compete with the title for the eye.
            char countBuf[64];
            const char* trailing = nullptr;
            if (!S().results.empty()) {
                std::snprintf(countBuf, sizeof(countBuf), "%d result%s%s",
                              (int)S().results.size(), S().results.size() == 1 ? "" : "s",
                              S().hasLive ? " · incl. live" : "");
                trailing = countBuf;
            }
            sectionHeader("Parts", /*firstInPane=*/true, "parts_all", trailing);
        }
        // While the background vendor fan-out is still running, show each vendor being searched
        // (its live logo + a per-vendor progress bar) inline under the "Parts" header.
        if (!S().liveVendors.empty()) {
            drawLiveVendorStrip(st);
        } else if (S().liveSearching && !S().results.empty()) {
            ImGui::SameLine();
            ImGui::TextDisabled("· loading more from vendors…");
        }
        // Cold catalog: the rows below are only what is indexed so far. Say so with an
        // indeterminate bar — an index build has no honest percentage — and note that the
        // search re-runs itself (see g_researchOnIndexReady) the moment it completes.
        if (g_indexBuilding) {
            ContentInset inset;
            ImDrawList*  dl   = ImGui::GetWindowDrawList();
            const float  barW = ImGui::GetContentRegionAvail().x;
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
        // Subtle one-line hint for per-site skip notices (login_required stubs).
        if (!S().notices.empty()) {
            char hint[128];
            std::snprintf(hint, sizeof(hint), "%d vendor%s need sign-in — see Vendors",
                          (int)S().notices.size(), S().notices.size() == 1 ? "" : "s");
            ImGui::TextDisabled("%s", hint);
            if (ImGui::IsItemHovered()) {
                std::string tip;
                for (const auto& n : S().notices) {
                    if (!tip.empty()) tip += "\n";
                    tip += n.site + ": " + (n.hint.empty() ? n.reason : n.hint);
                }
                ImGui::SetTooltip("%s", tip.c_str());
            }
        }
        if (S().gridView) drawResultsGrid(st);
        else              drawResultsList(st);
    }
    ImGui::EndChild();

    if (sides) {
        ImGui::SameLine();
        drawFiltersBlock(st, filtW);   // right pane: filters + facets + vendor strip footer
    }

    drawVendorsPopup(st);          // floating "Vendor Accounts" window (no-op when closed)
}

}  // namespace ui
