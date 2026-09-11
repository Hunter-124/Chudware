// Weldment Cut List panel (SolidWorks "Weldment Cut List") — see cutlist_panel.h.
//
// Shows the active part document's weldment cut list: weld_member structural members grouped by
// profile + dimensions + length into rows (Item / Profile / Description / Length / Qty / Total
// Length). Read-only — backed by the engine weld_cut_list RPC via DocMirror::weldCutList. The
// result is cached and re-pulled when the mirror's doc id or revision changes, or on Refresh.
#include "cutlist_panel.h"

#include <algorithm>
#include <string>

#include "imgui.h"

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"        // ui::palette / ui::uiScale
#include "layout.h"             // setNextCommandDialogPlacement (floating dialog anchor)
#include "metrics.h"
#include "widgets/kit.h"
#include "property_manager.h"   // pmFormatDim — match the rest of the UI's dim formatting

namespace ui {

// ---- local JSON helpers (tolerant) ----
static inline std::string cjstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline double cjnum(const nlohmann::json& o, const char* k, double dflt = 0.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : dflt;
}
static inline long long cjint(const nlohmann::json& o, const char* k, long long dflt = 0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<long long>() : (long long)cjnum(o, k, (double)dflt);
}

// True when the active document is a PART we can show a cut list for (not a drawing tab, not the
// empty start state). Weldments live in parts; drawing tabs leave the part mirror loaded, so gate
// on the tab kind too.
static bool cutListApplicable(AppState& st) {
    return st.docMirror.hasDoc() && st.workerStarted && !st.activeTabIsDrawing();
}

// ---------------------------------------------------------------------------

void CutListPanel::refresh(AppState& st) {
    if (!cutListApplicable(st)) { cacheValid_ = false; return; }
    std::string err;
    nlohmann::json res = st.docMirror.weldCutList(st.worker, err);
    if (res.is_null()) {
        if (!err.empty()) st.addLog("[cutlist] weld_cut_list failed: " + err);
        // Leave the previous cache in place; mark invalid so we retry next frame (cheap call).
        cacheValid_ = false;
        return;
    }
    rows_ = (res.contains("rows") && res["rows"].is_array()) ? res["rows"]
                                                            : nlohmann::json::array();
    totalMembers_ = (int)cjint(res, "total_members");
    totalLengthMm_ = cjnum(res, "total_length_mm");
    profiles_.clear();
    if (res.contains("profiles") && res["profiles"].is_array())
        for (const auto& p : res["profiles"])
            if (p.is_string()) profiles_.push_back(p.get<std::string>());

    cacheValid_ = true;
    cacheDocId_ = st.docMirror.docId();
    cacheRevision_ = st.docMirror.revision();
}

void CutListPanel::draw(AppState& st) {
    if (!st.showCutListPanel) return;
    ui::setNextCommandDialogPlacement("Cut List",
        ImVec2(ui::metrics::px(680), ui::metrics::px(440)),
        ImVec2(ui::metrics::px(320), ui::metrics::px(260)));
    ImGui::SetNextWindowBgAlpha(1.0f);
    if (!ImGui::Begin("Cut List", &st.showCutListPanel, ImGuiWindowFlags_NoDocking)) {
        ImGui::End();
        return;
    }
    // Gate: only part docs have a weldment cut list. Drawing tabs / no-doc show a hint.
    if (!cutListApplicable(st)) {
        ui::kit::dimText("Open a part to see its weldment cut list.");
        cacheValid_ = false;
        ImGui::End();
        return;
    }

    // Auto-invalidate when the mirror moved to a different doc or advanced its revision (an edit,
    // undo/redo): the cached rows may be stale.
    if (cacheValid_ && (cacheDocId_ != st.docMirror.docId() ||
                        cacheRevision_ != st.docMirror.revision()))
        cacheValid_ = false;
    if (!cacheValid_) refresh(st);

    ui::kit::sectionHeader("Weldment Cut List", nullptr, nullptr, (int)rows_.size(), true);
    if (rows_.empty()) {
        ui::kit::dimText("No weldment members. Add a Structural Member from the Weldments tab to populate the cut list.");
        if (ui::kit::secondaryButton("Refresh")) cacheValid_ = false;
        ImGui::End();
        return;
    }

    ImGui::Separator();

    // ---- cut-list table ----
    const ImGuiTableFlags flags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_SizingFixedFit | ImGuiTableFlags_ScrollX |
                                  ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable;
    const float footerHeight = 2 * ImGui::GetTextLineHeightWithSpacing() +
                               ImGui::GetFrameHeightWithSpacing() + ui::kit::sectionGap();
    const float tableHeight = std::max(ImGui::GetFrameHeightWithSpacing(),
                                      ImGui::GetContentRegionAvail().y - footerHeight);
    if (ImGui::BeginTable("cutlist", 6, flags, ImVec2(0, tableHeight))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Item", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(44));
        ImGui::TableSetupColumn("Profile", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(120));
        ImGui::TableSetupColumn("Description", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(200));
        ImGui::TableSetupColumn("Length", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(100));
        ImGui::TableSetupColumn("Qty", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(44));
        ImGui::TableSetupColumn("Total Length", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(120));
        ImGui::TableHeadersRow();

        const bool ips = st.ips();
        for (const auto& r : rows_) {
            if (!r.is_object()) continue;
            ImGui::TableNextRow();

            ImGui::TableSetColumnIndex(0);
            ImGui::Text("%lld", cjint(r, "item"));

            ImGui::TableSetColumnIndex(1);
            ImGui::TextWrapped("%s", cjstr(r, "profile").c_str());

            ImGui::TableSetColumnIndex(2);
            ImGui::TextWrapped("%s", cjstr(r, "description").c_str());

            ImGui::TableSetColumnIndex(3);
            ImGui::TextUnformatted(pmFormatDim(cjnum(r, "length_mm"), ips).c_str());

            ImGui::TableSetColumnIndex(4);
            ImGui::Text("%lld", cjint(r, "quantity"));

            ImGui::TableSetColumnIndex(5);
            ImGui::TextUnformatted(pmFormatDim(cjnum(r, "total_length_mm"), ips).c_str());
        }
        ImGui::EndTable();
    }

    // ---- footer totals ----
    ImGui::Separator();
    ImGui::TextWrapped("%d member%s total", totalMembers_, totalMembers_ == 1 ? "" : "s");
    ImGui::TextWrapped("Total length: %s", pmFormatDim(totalLengthMm_, st.ips()).c_str());
    if (ui::kit::secondaryButton("Refresh")) cacheValid_ = false;
    ImGui::SameLine();
    if (ui::kit::secondaryButton("Close")) st.showCutListPanel = false;

    ImGui::End();
}

}  // namespace ui
