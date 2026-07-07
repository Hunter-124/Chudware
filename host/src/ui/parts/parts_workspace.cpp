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
#include <cstdio>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

PartsState& S() {
    static PartsState s;
    return s;
}

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
                e.lastLogin       = v.value("last_login", "");
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

void doSearch(AppState& st) {
    if (S().searching) return;
    S().searching  = true;
    S().searchDone = false;
    S().searchErr.clear();
    S().results.clear();
    nlohmann::json p;
    p["query"]    = S().searchBuf;
    p["category"] = S().selectedCategory;
    p["limit"]    = 100;
    if (S().priceMin > 0.0f) p["price_min"] = S().priceMin;
    if (S().priceMax > 0.0f) p["price_max"] = S().priceMax;
    if (S().materialBuf[0])  p["material"]  = S().materialBuf;
    if (S().vendorBuf[0])    p["vendor"]    = S().vendorBuf;
    if (S().sourceFilter == 1) p["source"] = "local";
    if (S().sourceFilter == 2) p["source"] = "live";
    st.rpc.call("parts_search", p, [](const core::RpcResult& r) {
        S().searching  = false;
        S().searchDone = true;
        if (!r.ok) { S().searchErr = r.err; return; }
        if (!r.result.is_object()) return;
        if (!r.result.value("ok", false)) {
            S().searchErr = r.result.value("reason", "search failed");
            return;
        }
        S().hasLive = r.result.value("has_live", false);
        S().results.clear();
        if (!r.result.contains("results")) return;
        for (auto& it : r.result["results"]) {
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
            S().results.push_back(std::move(item));
        }
        // Client-side sort (relevance = backend order). 1 = price low→high (zero-price
        // last), 2 = price high→low, 3 = name.
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
            loadCategories(st);
            loadVendors(st);
            pollQueue(st);
        }
    }
    // Re-try if worker came up after first frame.
    if (st.workerStarted) {
        if (!S().categoriesLoaded && !S().categoriesLoading) loadCategories(st);
        if (!S().vendorsLoaded   && !S().vendorsLoading)    loadVendors(st);
    }

    if (!st.workerStarted) {
        emptyState("parts_all", "Parts Library unavailable",
                   "The AI-CAD worker service is not running.");
        return;
    }

    // Layout: [categories+filters 210px] | [results flex] | [vendors+queue 250px]
    float totalW = ImGui::GetContentRegionAvail().x;
    const float catW  = 210.0f * sc;
    const float vendW = 250.0f * sc;
    const float midW  = totalW - catW - vendW - 12.0f * sc;

    // ---- search bar (above results column only) --------------------------------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * sc, 5 * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,  ImVec2(6 * sc, 6 * sc));
    ImGui::Dummy(ImVec2(catW + 6.0f * sc, 0.0f));  // skip category pane width
    ImGui::SameLine();

    // Sort combo width = widest entry + frame padding + arrow button.
    static const char* kSortItems[] = {
        "Relevance", "Price: low to high", "Price: high to low", "Name"};
    float sortW = 0.0f;
    for (const char* it : kSortItems)
        sortW = std::max(sortW, ImGui::CalcTextSize(it).x);
    sortW += ImGui::GetStyle().FramePadding.x * 2.0f + ImGui::GetFrameHeight();

    ImGui::SetNextItemWidth(midW - 160.0f * sc - sortW);
    bool hit = ImGui::InputTextWithHint("##parts_q",
                                "search fasteners, bearings, sections, electronics, vendors…",
                                S().searchBuf, sizeof(S().searchBuf),
                                ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    bool go = ImGui::Button("Search");
    ImGui::PopStyleColor(2);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(sortW);
    if (ImGui::Combo("##sort", &S().sortMode, kSortItems, IM_ARRAYSIZE(kSortItems))) {
        st.settings.partsSortMode = S().sortMode;
        saveSettings(st.settings);
        doSearch(st);
    }
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.80f));
    if (ImGui::Button(S().gridView ? "List" : "Grid")) {
        S().gridView = !S().gridView;
        st.settings.partsGridView = S().gridView;
        saveSettings(st.settings);
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);

    if ((hit || go) && !S().searching)
        doSearch(st);

    ImGui::Spacing();

    // ---- three-column body ----------------------------------------------------
    drawTaxonomyPane(st, catW);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 4 * sc));
    ImGui::BeginChild("##parts_results", ImVec2(midW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    if (browserPanelActive()) {
        drawBrowserPanel(st);
    } else {
        {
            char hdr[96];
            if (!S().results.empty())
                std::snprintf(hdr, sizeof(hdr), "Parts  (%d%s)", (int)S().results.size(),
                              S().hasLive ? ", incl. live vendor results" : "");
            else
                std::snprintf(hdr, sizeof(hdr), "Parts");
            sectionHeader(hdr, /*firstInPane=*/true);
        }
        if (S().gridView) drawResultsGrid(st);
        else              drawResultsList(st);
    }
    ImGui::EndChild();

    ImGui::SameLine();
    drawVendorsPane(st, vendW);
}

}  // namespace ui
