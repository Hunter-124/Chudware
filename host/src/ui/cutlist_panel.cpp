// Weldment Cut List panel (SolidWorks "Weldment Cut List") — see cutlist_panel.h.
//
// Shows the active part document's weldment cut list: weld_member structural members grouped by
// profile + dimensions + length into rows (Item / Profile / Description / Length / Qty / Total
// Length). Read-only — backed by the engine weld_cut_list RPC via DocMirror::weldCutList. The
// result is cached and re-pulled when the mirror's doc id or revision changes, or on Refresh.
#include "cutlist_panel.h"

#include <string>

#include "imgui.h"

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"        // ui::palette / ui::uiScale
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
    if (!ImGui::Begin("Cut List", &st.showCutListPanel)) { ImGui::End(); return; }

    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    const ImVec4 dimCol(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1);

    // Gate: only part docs have a weldment cut list. Drawing tabs / no-doc show a hint.
    if (!cutListApplicable(st)) {
        ImGui::TextColored(dimCol, "Open a part to see its weldment cut list.");
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

    // ---- header + Refresh ----
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextColored(accentCol, "Weldment Cut List");
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "(%d item%s)", (int)rows_.size(),
                       rows_.size() == 1 ? "" : "s");
    ImGui::SameLine(ImGui::GetWindowWidth() - 90.0f * ui::uiScale);
    if (ImGui::Button("Refresh")) cacheValid_ = false;

    // ---- empty hint: a part with no weld_member features ----
    if (rows_.empty()) {
        ImGui::Separator();
        ImGui::TextColored(dimCol, "No weldment members.");
        ImGui::TextColored(dimCol, "Add a Structural Member (Weldments tab) to populate the cut list.");
        ImGui::End();
        return;
    }

    ImGui::Separator();

    // ---- cut-list table ----
    const ImGuiTableFlags flags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_SizingStretchProp |
                                  ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable;
    if (ImGui::BeginTable("cutlist", 6, flags)) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Item", ImGuiTableColumnFlags_WidthFixed, 40.0f * ui::uiScale);
        ImGui::TableSetupColumn("Profile", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Description", ImGuiTableColumnFlags_WidthStretch, 2.0f);
        ImGui::TableSetupColumn("Length", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Qty", ImGuiTableColumnFlags_WidthFixed, 40.0f * ui::uiScale);
        ImGui::TableSetupColumn("Total Length", ImGuiTableColumnFlags_WidthStretch, 1.2f);
        ImGui::TableHeadersRow();

        const bool ips = st.ips();
        for (const auto& r : rows_) {
            if (!r.is_object()) continue;
            ImGui::TableNextRow();

            ImGui::TableSetColumnIndex(0);
            ImGui::Text("%lld", cjint(r, "item"));

            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(cjstr(r, "profile").c_str());

            ImGui::TableSetColumnIndex(2);
            ImGui::TextUnformatted(cjstr(r, "description").c_str());

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
    ImGui::TextColored(dimCol, "%d member%s total", totalMembers_, totalMembers_ == 1 ? "" : "s");
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "  |  total length %s",
                       pmFormatDim(totalLengthMm_, st.ips()).c_str());

    ImGui::End();
}

}  // namespace ui
