// Configurations panel — ported from host/src/ui/config_panel.cpp (C5).
#include "ui/config_panel.h"

#include <cstdio>
#include <cstring>
#include <string>

#include "imgui.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "ui/appearance_common.h"

namespace ui {

// ---- local JSON helpers (tolerant) ----
static inline std::string cjstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// Format one param_overrides value, which is either a bare scalar or a {value, expr} dim object.
// Lengths route through pmFormatDim so MMGS/IPS display matches the rest of the app; an explicit
// expr (a typed equation) is shown verbatim. Non-numeric scalars (bools/strings) fall back to dump.
static std::string formatOverrideValue(AppState& st, const nlohmann::json& v) {
    if (v.is_object()) {
        std::string expr = cjstr(v, "expr");
        if (!expr.empty()) return expr;
        auto it = v.find("value");
        if (it != v.end() && it->is_number())
            return c5::pmFormatDim(it->get<double>(), c5::ips(st));
        if (it != v.end()) return it->dump();
        return v.dump();
    }
    if (v.is_number()) return c5::pmFormatDim(v.get<double>(), c5::ips(st));
    if (v.is_string()) return v.get<std::string>();
    return v.dump();
}

// True when the active document is a part/asm we can show configs for (not a drawing tab, not the
// empty start state). Drawing tabs leave the underlying part mirror loaded, so gate on the tab kind.
static bool configsApplicable(AppState& st) {
    return st.docMirror.hasDoc() && st.workerStarted && !c5::activeTabIsDrawing(st);
}

// ---------------------------------------------------------------------------

void ConfigPanel::refresh(AppState& st) {
    if (!configsApplicable(st)) { cacheValid_ = false; return; }
    std::string err;
    nlohmann::json res = st.docMirror.configList(st.worker, err);
    if (res.is_null()) {
        if (!err.empty()) c5::addLog(st, "[config] config_list failed: " + err);
        // Leave the previous cache in place; mark invalid so we retry next frame (cheap call).
        cacheValid_ = false;
        return;
    }
    names_.clear();
    if (res.contains("configurations") && res["configurations"].is_array())
        for (const auto& n : res["configurations"])
            if (n.is_string()) names_.push_back(n.get<std::string>());
    active_ = cjstr(res, "active");
    detail_ = (res.contains("detail") && res["detail"].is_array()) ? res["detail"]
                                                                   : nlohmann::json::array();
    // Keep the expanded-row selection sensible: default to the active config.
    selected_ = -1;
    for (int i = 0; i < (int)names_.size(); ++i)
        if (names_[i] == active_) { selected_ = i; break; }
    if (selected_ < 0 && !names_.empty()) selected_ = 0;

    cacheValid_ = true;
    cacheDocId_ = st.docMirror.docId();
    cacheRevision_ = st.docMirror.revision();
}

bool ConfigPanel::runConfigOp(AppState& st, const std::string& method,
                              const nlohmann::json& params) {
    core::OpsResponse r = st.docMirror.callConfig(st.worker, method, params);
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? r.error_code : r.error_message;
        c5::addLog(st, "[config] " + method + " failed: " + msg);
        c5::toast(ToastLevel::Error, "Configuration: " + msg);
        cacheValid_ = false;  // re-pull so the panel reflects the unchanged truth
        return false;
    }
    // The ops-response already refreshed the mirror (tree/revision/owner map) + fired the listener
    // so the FeatureManager repaints. Reload the viewport mesh for the now-active config's geometry.
    if (!r.aicm_path.empty()) {
        std::string le;
        if (!c5::loadDocMesh(st, r.aicm_path, le))
            c5::addLog(st, "[config] mesh reload failed: " + le);
    }
    cacheValid_ = false;  // config set changed — re-pull config_list on the next draw
    return true;
}

void ConfigPanel::drawEmbedded(AppState& st) {
    drawBody(st);
}

void ConfigPanel::drawBody(AppState& st) {
        ImVec4 accentCol = c5::accent(); accentCol.w = 1.f;
    ImVec4 dimCol = c5::txtDim(); dimCol.w = 1.f;

    // Gate: only part/asm docs have configurations. Drawing tabs / no-doc show a hint.
    if (!configsApplicable(st)) {
        ImGui::TextColored(dimCol, "Open a part or assembly to manage configurations.");
        cacheValid_ = false;
        addPromptOpen_ = false;
        return;
    }

    // Auto-invalidate when the mirror moved to a different doc or advanced its revision (an edit,
    // undo/redo, a config op done elsewhere): the cached name/active/detail set may be stale.
    if (cacheValid_ && (cacheDocId_ != st.docMirror.docId() ||
                        cacheRevision_ != st.docMirror.revision()))
        cacheValid_ = false;
    if (!cacheValid_) refresh(st);

    // ---- header ----
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(accentCol, "Configurations");
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "(%d)", (int)names_.size());

    // ---- toolbar: Add / Delete / Activate ----
    const bool busy = c5::busy(st);
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Add...")) {
        addPromptOpen_ = true;
        addNameBuf_[0] = 0;
        focusAddField_ = true;
    }
    ImGui::SameLine();
    {
        // Delete is guarded: never on the last config, and only with a selection.
        const bool canDelete = selected_ >= 0 && selected_ < (int)names_.size() &&
                               names_.size() > 1;
        if (!canDelete) ImGui::BeginDisabled();
        if (ImGui::Button("Delete")) {
            const std::string& name = names_[selected_];
            runConfigOp(st, "config_delete", {{"name", name}});
        }
        if (!canDelete) ImGui::EndDisabled();
        if (ImGui::IsItemHovered() && names_.size() <= 1)
            ImGui::SetTooltip("A document must keep at least one configuration.");
    }
    ImGui::SameLine();
    {
        const bool canActivate = selected_ >= 0 && selected_ < (int)names_.size() &&
                                 names_[selected_] != active_;
        if (!canActivate) ImGui::BeginDisabled();
        if (ImGui::Button("Activate")) {
            const std::string& name = names_[selected_];
            runConfigOp(st, "config_activate", {{"name", name}});
        }
        if (!canActivate) ImGui::EndDisabled();
    }
    if (busy) ImGui::EndDisabled();
    if (busy) { ImGui::SameLine(); ImGui::TextColored(dimCol, "busy..."); }

    // ---- add-config inline prompt ----
    if (addPromptOpen_) {
        ImGui::Separator();
        ImGui::TextColored(dimCol, "New configuration");
        ImGui::SetNextItemWidth(-1);
        if (focusAddField_) { ImGui::SetKeyboardFocusHere(); focusAddField_ = false; }
        const bool entered = ImGui::InputTextWithHint(
            "##cfgname", "name", addNameBuf_, sizeof(addNameBuf_),
            ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll);
        ImGui::Checkbox("Activate after creating", &addActivate_);
        const bool nameOk = addNameBuf_[0] != 0;
        if (!nameOk) ImGui::BeginDisabled();
        const bool create = ImGui::Button("Create") || (entered && nameOk);
        if (!nameOk) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) addPromptOpen_ = false;
        if (create && nameOk) {
            std::string name(addNameBuf_);
            // Seed from the active config so the new one starts as a sibling (SolidWorks behavior).
            nlohmann::json p = {{"name", name}};
            if (!active_.empty()) p["copy_from"] = active_;
            if (runConfigOp(st, "config_add", p)) {
                if (addActivate_) runConfigOp(st, "config_activate", {{"name", name}});
                addPromptOpen_ = false;
            }
        }
    }

    ImGui::Separator();

    // ---- configuration list ----
    if (names_.empty()) {
        ImGui::TextColored(dimCol, "No configurations.");
        return;
    }

    // Frosted-glass card for the config list, matching the PropertyManager form cards so the glass
    // look is consistent across docked panels (text stays legible on glassBg in both themes).
    ImGui::PushStyleColor(ImGuiCol_ChildBg, c5::glassBg());
    ImGui::PushStyleColor(ImGuiCol_Border, c5::glassBorder());
    ImGui::BeginChild("cfglist", ImVec2(0, 0), ImGuiChildFlags_Border);
    for (int i = 0; i < (int)names_.size(); ++i) {
        const std::string& name = names_[i];
        const bool isActive = (name == active_);
        ImGui::PushID(i);

        // Row: a selectable that also drives the override expansion below. The active config is
        // marked with a leading accent dot and bold-ish accent text (SolidWorks bolds the active).
        std::string label = (isActive ? std::string(u8"●  ") : std::string("    ")) + name;
        if (isActive) ImGui::PushStyleColor(ImGuiCol_Text, accentCol);
        const bool clicked = ImGui::Selectable(label.c_str(), selected_ == i,
                                               ImGuiSelectableFlags_AllowDoubleClick);
        if (isActive) ImGui::PopStyleColor();
        if (clicked) {
            selected_ = i;
            // Double-click activates (SolidWorks: double-click a config to switch to it).
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !isActive && !busy)
                runConfigOp(st, "config_activate", {{"name", name}});
        }
        if (isActive) {
            ImGui::SameLine();
            ImGui::TextColored(dimCol, "(active)");
        }

        // Per-config overrides + suppressions, READ-ONLY, shown under the SELECTED row only (keeps
        // the list compact; the active/selected config's deltas are what the user is reasoning about).
        if (selected_ == i) {
            const nlohmann::json* d = nullptr;
            for (const auto& e : detail_)
                if (e.is_object() && cjstr(e, "name") == name) { d = &e; break; }
            ImGui::Indent(16.0f * c5::scale());
            int shown = 0;
            if (d) {
                auto ovIt = d->find("param_overrides");
                if (ovIt != d->end() && ovIt->is_object()) {
                    for (auto it = ovIt->begin(); it != ovIt->end(); ++it) {
                        // key is "fid.param"; value is a scalar or {value,expr}.
                        std::string disp = formatOverrideValue(st, it.value());
                        ImGui::TextColored(dimCol, "%s = %s", it.key().c_str(), disp.c_str());
                        ++shown;
                    }
                }
                auto supIt = d->find("suppressions");
                if (supIt != d->end() && supIt->is_object()) {
                    for (auto it = supIt->begin(); it != supIt->end(); ++it) {
                        if (it.value().is_boolean() && it.value().get<bool>()) {
                            ImGui::TextColored(dimCol, "%s: suppressed", it.key().c_str());
                            ++shown;
                        }
                    }
                }
            }
            if (shown == 0)
                ImGui::TextColored(dimCol, "no overrides");
            ImGui::Unindent(16.0f * c5::scale());
        }
        ImGui::PopID();
    }
    ImGui::EndChild();
    ImGui::PopStyleColor(2);

}
}  // namespace ui
