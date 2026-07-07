// Parts Library workspace — full-viewport body for the PartsLibrary title-bar tab.
// Catalog browse/search over services/parts, vendor panes (McMaster-Carr first),
// and the background download queue. All RPC calls are async (st.rpc) — never blocks
// the GL thread. Degrades gracefully to empty-state when services are not running.
#include "parts_workspace.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

namespace ui {

// ---- local state ----------------------------------------------------------------
namespace {

struct Category {
    std::string id;
    std::string label;
    std::string parentId;
};

struct PartsItem {
    std::string id;
    std::string name;
    std::string vendor;
    std::string partNumber;
    std::string description;
    std::string unit;
    double      priceUsd   = 0.0;
    bool        downloaded = false;
};

struct VendorEntry {
    std::string id;
    std::string label;
    bool loggedIn = false;
    bool checking = false;
};

struct DownloadEntry {
    std::string itemId;
    std::string name;
    std::string vendor;
    float       progress = 0.0f;
    bool        done     = false;
    bool        failed   = false;
};

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

    bool gridView = false;

    bool vendorsLoaded   = false;
    bool vendorsLoading  = false;
    std::vector<VendorEntry>   vendors;
    std::vector<DownloadEntry> queue;

    bool firstFrame = true;
};

static PartsState s_parts;

// ---- helpers -------------------------------------------------------------------

static void partRowPill(bool selected = false, float rows = 1.0f) {
    const float rowH = ImGui::GetFrameHeight() * rows;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    ImVec2 p1(p0.x + w, p0.y + rowH);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f,
                           false, selected, selected ? 0.42f : 0.20f);
}

static void sectionHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::PushFont(nullptr);
    ImGui::TextUnformatted(label);
    ImGui::PopFont();
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

static void emptyState(const char* icon, const char* msg, const char* sub = nullptr) {
    const float S = ui::uiScale;
    float avail = ImGui::GetContentRegionAvail().y;
    ImGui::Dummy(ImVec2(0, avail * 0.35f));

    ImGui::PushFont(nullptr);
    float tw = ImGui::CalcTextSize(icon).x;
    float cx = ImGui::GetCursorPosX() + (ImGui::GetContentRegionAvail().x - tw) * 0.5f;
    ImGui::SetCursorPosX(cx);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 0.5f));
    ImGui::TextUnformatted(icon);
    ImGui::PopStyleColor();
    ImGui::PopFont();
    ImGui::Spacing();

    {
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }
    if (sub) {
        float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - sw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::TextUnformatted(sub);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));
}

static void spinner(const char* prefix) {
    static const char* kFrames[] = {". . .", "o . .", ". o .", ". . o"};
    int f = (int)(ImGui::GetTime() * 4.0) & 3;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::Text("%s %s", prefix, kFrames[f]);
    ImGui::PopStyleColor();
}

// ---- async loaders -------------------------------------------------------------

static void loadCategories(AppState& st) {
    if (s_parts.categoriesLoaded || s_parts.categoriesLoading) return;
    s_parts.categoriesLoading = true;
    st.rpc.call("parts_list_categories", {}, [](const core::RpcResult& r) {
        s_parts.categoriesLoading = false;
        s_parts.categoriesLoaded  = true;
        if (!r.ok || !r.result.is_array()) return;
        s_parts.categories.clear();
        for (auto& c : r.result) {
            Category cat;
            cat.id       = c.value("id",       "");
            cat.label    = c.value("label",     "");
            cat.parentId = c.value("parent_id", "");
            s_parts.categories.push_back(std::move(cat));
        }
    });
}

static void loadVendors(AppState& st) {
    if (s_parts.vendorsLoaded || s_parts.vendorsLoading) return;
    s_parts.vendorsLoading = true;
    // Seed the list immediately; login status filled by RPC.
    s_parts.vendors = {
        {"mcmaster", "McMaster-Carr", false, true},
        {"digikey",  "DigiKey",       false, true},
        {"grainger", "Grainger",      false, true},
        {"misumi",   "MISUMI",        false, true},
    };
    st.rpc.call("vendor_status", {}, [](const core::RpcResult& r) {
        s_parts.vendorsLoading = false;
        s_parts.vendorsLoaded  = true;
        if (!r.ok || !r.result.is_object()) return;
        for (auto& v : s_parts.vendors) {
            v.checking = false;
            if (r.result.contains(v.id))
                v.loggedIn = r.result[v.id].value("logged_in", false);
        }
    });
}

static void doSearch(AppState& st, const std::string& query,
                     const std::string& category) {
    if (s_parts.searching) return;
    s_parts.searching  = true;
    s_parts.searchDone = false;
    s_parts.searchErr.clear();
    s_parts.results.clear();
    nlohmann::json p;
    p["query"]    = query;
    p["category"] = category;
    p["limit"]    = 50;
    st.rpc.call("parts_search", p, [](const core::RpcResult& r) {
        s_parts.searching  = false;
        s_parts.searchDone = true;
        if (!r.ok) { s_parts.searchErr = r.err; return; }
        if (!r.result.is_array()) return;
        s_parts.results.clear();
        for (auto& it : r.result) {
            PartsItem item;
            item.id          = it.value("id",          "");
            item.name        = it.value("name",        "");
            item.vendor      = it.value("vendor",      "");
            item.partNumber  = it.value("part_number", "");
            item.description = it.value("description", "");
            item.unit        = it.value("unit",        "ea");
            item.priceUsd    = it.value("price_usd",   0.0);
            item.downloaded  = it.value("downloaded",  false);
            s_parts.results.push_back(std::move(item));
        }
    });
}

static void queueDownload(AppState& st, const PartsItem& item) {
    for (auto& q : s_parts.queue)
        if (q.itemId == item.id) return;  // already queued
    DownloadEntry e;
    e.itemId = item.id;
    e.name   = item.name;
    e.vendor = item.vendor;
    s_parts.queue.push_back(e);

    nlohmann::json p;
    p["item_id"] = item.id;
    p["vendor"]  = item.vendor;
    st.rpc.call("parts_download", p, [id = item.id](const core::RpcResult& r) {
        for (auto& q : s_parts.queue) {
            if (q.itemId != id) continue;
            q.progress = 1.0f;
            q.done     =  r.ok;
            q.failed   = !r.ok;
            break;
        }
        if (r.ok) {
            for (auto& it : s_parts.results)
                if (it.id == id) { it.downloaded = true; break; }
        }
    });
}

// ---- sub-panels ----------------------------------------------------------------

static void drawCategoryPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_cats", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Categories");

    if (s_parts.categoriesLoading) {
        spinner("Loading");
    } else if (s_parts.categories.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("No catalog data. Start the parts service or check the network.");
        ImGui::PopStyleColor();
    } else {
        // "All" entry.
        {
            bool sel = s_parts.selectedCategory.empty();
            partRowPill(sel);
            if (ImGui::Selectable("All Parts", sel,
                    ImGuiSelectableFlags_SpanAllColumns,
                    ImVec2(0, ImGui::GetFrameHeight())))
                s_parts.selectedCategory.clear();
        }
        ImGui::Spacing();
        for (auto& c : s_parts.categories) {
            if (!c.parentId.empty()) continue;  // top-level only in outer loop
            bool sel = (s_parts.selectedCategory == c.id);
            partRowPill(sel);
            if (ImGui::Selectable(c.label.c_str(), sel,
                    ImGuiSelectableFlags_SpanAllColumns,
                    ImVec2(0, ImGui::GetFrameHeight())))
                s_parts.selectedCategory = sel ? "" : c.id;

            // Children (indented).
            for (auto& ch : s_parts.categories) {
                if (ch.parentId != c.id) continue;
                bool csel = (s_parts.selectedCategory == ch.id);
                ImGui::Indent(14.0f * S);
                partRowPill(csel);
                std::string clbl = "  " + ch.label;
                if (ImGui::Selectable(clbl.c_str(), csel,
                        ImGuiSelectableFlags_SpanAllColumns,
                        ImVec2(0, ImGui::GetFrameHeight())))
                    s_parts.selectedCategory = csel ? "" : ch.id;
                ImGui::Unindent(14.0f * S);
            }
        }
    }
    ImGui::EndChild();
}

static void drawResultsList(AppState& st) {
    const float S = ui::uiScale;

    if (s_parts.searching) { spinner("Searching"); return; }
    if (!s_parts.searchErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Error: %s", s_parts.searchErr.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (s_parts.results.empty()) {
        if (s_parts.searchDone)
            emptyState("(x)", "No results",
                       "Try a different search term or select another category.");
        else
            emptyState("[P]", "Search for parts",
                       "Type in the search bar or select a category.");
        return;
    }

    // Column header.
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::Text("%-40s  %-16s  %-14s  %s", "Name", "Vendor", "Part #", "Price");
    ImGui::PopStyleColor();
    ImGui::Separator();
    ImGui::Spacing();

    for (auto& item : s_parts.results) {
        ImGui::PushID(item.id.c_str());

        partRowPill(false, 1.1f);
        ImGui::BeginGroup();

        // Downloaded badge.
        if (item.downloaded) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1],
                       palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted("[v]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        ImGui::Text("%-40.40s", item.name.c_str());
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::Text("  %-16.16s  %-14.14s", item.vendor.c_str(), item.partNumber.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (item.priceUsd > 0.0)
            ImGui::Text("  $%.2f/%s", item.priceUsd, item.unit.c_str());
        else
            ImGui::TextDisabled("  —");
        ImGui::EndGroup();

        // Hover action buttons (right-aligned).
        if (ImGui::IsItemHovered()) {
            float bx = ImGui::GetContentRegionAvail().x + ImGui::GetCursorPosX()
                       - 170.0f * S;
            ImGui::SameLine();
            ImGui::SetCursorPosX(bx);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], palette.button[3]));
            if (!item.downloaded && ImGui::SmallButton("Download"))
                queueDownload(st, item);
            ImGui::SameLine();
            if (ImGui::SmallButton("Insert")) {
                nlohmann::json p;
                p["item_id"] = item.id;
                p["vendor"]  = item.vendor;
                st.rpc.call("parts_insert_into_doc", p,
                    [](const core::RpcResult&) {});
            }
            ImGui::PopStyleColor();
        }

        if (!item.description.empty() &&
            ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
            ImGui::SetTooltip("%s", item.description.c_str());

        ImGui::Spacing();
        ImGui::PopID();
    }
}

static void drawResultsGrid(AppState& st) {
    const float S     = ui::uiScale;
    const float cardW = 160.0f * S;
    const float cardH = 120.0f * S;
    float availW      = ImGui::GetContentRegionAvail().x;
    int   cols        = std::max(1, (int)(availW / (cardW + 8.0f * S)));

    int i = 0;
    for (auto& item : s_parts.results) {
        if (i % cols != 0) ImGui::SameLine();
        ImGui::PushID(item.id.c_str());

        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + cardW, p0.y + cardH);
        bool hov = ImGui::IsMouseHoveringRect(p0, p1);
        ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 8.0f,
                               false, hov, hov ? 0.32f : 0.20f);

        ImGui::BeginGroup();
        ImGui::Dummy(ImVec2(cardW, 44.0f * S));  // icon placeholder

        auto centre = [&](const char* t) {
            float tw = ImGui::CalcTextSize(t).x;
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (cardW - tw) * 0.5f);
            ImGui::TextUnformatted(t);
        };
        centre(item.name.c_str());
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        centre(item.partNumber.c_str());
        ImGui::PopStyleColor();

        char pbuf[32];
        if (item.priceUsd > 0.0)
            std::snprintf(pbuf, sizeof(pbuf), "$%.2f", item.priceUsd);
        else
            std::snprintf(pbuf, sizeof(pbuf), "—");
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        centre(pbuf);
        ImGui::PopStyleColor();
        ImGui::EndGroup();

        if (hov && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
            nlohmann::json p;
            p["item_id"] = item.id;
            p["vendor"]  = item.vendor;
            st.rpc.call("parts_insert_into_doc", p, [](const core::RpcResult&) {});
        }
        ImGui::PopID();
        ++i;
    }
}

static void drawVendorsPane(AppState& st, float paneW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_vendors", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Vendors");

    if (s_parts.vendorsLoading) {
        spinner("Checking");
    } else {
        for (auto& v : s_parts.vendors) {
            ImGui::PushID(v.id.c_str());
            partRowPill(false, 2.0f);
            ImGui::BeginGroup();

            // Status dot: [+] green = logged in, [ ] red = not.
            ImVec4 dot = v.loggedIn
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.statusErr[0], palette.statusErr[1],
                         palette.statusErr[2], 0.85f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(v.loggedIn ? "[+]" : "[ ]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(v.label.c_str());
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                 ImGui::GetContentRegionAvail().x - 80.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            const char* lbl = v.checking ? "..." : (v.loggedIn ? "Refresh" : "Login");
            if (!v.checking && ImGui::SmallButton(lbl)) {
                v.checking = true;
                nlohmann::json p;
                p["vendor"] = v.id;
                st.rpc.call("vendor_login", p,
                    [id = v.id](const core::RpcResult& r) {
                        for (auto& vv : s_parts.vendors) {
                            if (vv.id != id) continue;
                            vv.checking = false;
                            if (r.ok) vv.loggedIn = r.result.value("logged_in", false);
                            break;
                        }
                    });
            }
            ImGui::PopStyleColor();

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            if (v.checking)
                ImGui::TextUnformatted("  Connecting...");
            else
                ImGui::TextUnformatted(v.loggedIn ? "  Authenticated" : "  Not logged in");
            ImGui::PopStyleColor();

            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Download Queue");

    if (s_parts.queue.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No downloads pending.");
        ImGui::PopStyleColor();
    } else {
        if (ImGui::SmallButton("Clear done")) {
            s_parts.queue.erase(
                std::remove_if(s_parts.queue.begin(), s_parts.queue.end(),
                    [](const DownloadEntry& e){ return e.done || e.failed; }),
                s_parts.queue.end());
        }
        ImGui::Spacing();
        for (auto& q : s_parts.queue) {
            ImGui::PushID(q.itemId.c_str());
            partRowPill(false, 1.6f);
            ImGui::BeginGroup();
            ImGui::Text("%-24.24s", q.name.c_str());
            ImGui::SameLine();
            ImGui::TextDisabled("%s", q.vendor.c_str());
            if (q.done) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusOk[0], palette.statusOk[1],
                           palette.statusOk[2], 1.0f));
                ImGui::TextUnformatted("  Done");
                ImGui::PopStyleColor();
            } else if (q.failed) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusErr[0], palette.statusErr[1],
                           palette.statusErr[2], 1.0f));
                ImGui::TextUnformatted("  Failed");
                ImGui::PopStyleColor();
            } else {
                q.progress = std::min(q.progress + 0.003f, 0.95f);  // animate
                ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 0.80f));
                ImGui::ProgressBar(q.progress, ImVec2(-1.0f, 4.0f * S));
                ImGui::PopStyleColor();
            }
            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    ImGui::EndChild();
}

} // anonymous namespace

// ---- public entry point -------------------------------------------------------

void drawPartsLibraryWorkspace(AppState& st) {
    const float S = ui::uiScale;

    // Lazy init — trigger loads on first visible frame once the worker is up.
    if (s_parts.firstFrame) {
        s_parts.firstFrame = false;
        if (st.workerStarted) {
            loadCategories(st);
            loadVendors(st);
        }
    }
    // Re-try if worker came up after first frame.
    if (st.workerStarted) {
        if (!s_parts.categoriesLoaded && !s_parts.categoriesLoading) loadCategories(st);
        if (!s_parts.vendorsLoaded   && !s_parts.vendorsLoading)    loadVendors(st);
    }

    if (!st.workerStarted) {
        emptyState("[P]", "Parts Library unavailable",
                   "The AI-CAD worker service is not running.");
        return;
    }

    // Layout: [categories 200px] | [results flex] | [vendors+queue 220px]
    float totalW = ImGui::GetContentRegionAvail().x;
    const float catW  = 200.0f * S;
    const float vendW = 220.0f * S;
    const float midW  = totalW - catW - vendW - 12.0f * S;

    // ---- search bar (above results column only) --------------------------------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * S, 5 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,  ImVec2(6 * S, 6 * S));
    ImGui::Dummy(ImVec2(catW + 6.0f * S, 0.0f));  // skip category pane width
    ImGui::SameLine();
    ImGui::SetNextItemWidth(midW - 110.0f * S);
    bool hit = ImGui::InputText("##parts_q", s_parts.searchBuf,
                                sizeof(s_parts.searchBuf),
                                ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    bool go = ImGui::Button("Search");
    ImGui::PopStyleColor(2);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.80f));
    if (ImGui::Button(s_parts.gridView ? "List" : "Grid"))
        s_parts.gridView = !s_parts.gridView;
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);

    if ((hit || go) && !s_parts.searching)
        doSearch(st, s_parts.searchBuf, s_parts.selectedCategory);

    ImGui::Spacing();

    // ---- three-column body ----------------------------------------------------
    drawCategoryPane(st, catW);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##parts_results", ImVec2(midW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    sectionHeader("Parts");
    if (s_parts.gridView) drawResultsGrid(st);
    else                  drawResultsList(st);
    ImGui::EndChild();

    ImGui::SameLine();
    drawVendorsPane(st, vendW);
}

}  // namespace ui
