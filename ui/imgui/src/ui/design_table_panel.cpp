// Design Table panel — ported from host/src/ui/design_table_panel.cpp (C5).
#include "ui/design_table_panel.h"
#include "ui/layout.h"   // T-G23: setNextCommandDialogPlacement (overlay-band float anchor)

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>

#include "imgui.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "ui/appearance_common.h"

namespace ui {

// ---- local JSON helpers (tolerant) ----
static inline std::string djstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

// Extract the numeric value from a param_overrides entry (a bare scalar or a {value, expr} dim
// object). Returns true + fills `out` when a number is present; false for non-numeric / missing.
static bool overrideNumber(const nlohmann::json& v, double& out) {
    if (v.is_number()) { out = v.get<double>(); return true; }
    if (v.is_object()) {
        auto it = v.find("value");
        if (it != v.end() && it->is_number()) { out = it->get<double>(); return true; }
    }
    return false;
}

// A param_overrides entry's typed expression ("=d1*2"), if any (so we display the equation rather
// than the cached value, matching the read-only Configurations panel).
static std::string overrideExpr(const nlohmann::json& v) {
    if (v.is_object()) return djstr(v, "expr");
    return std::string();
}

// The base (un-overridden) value of a feature param from the live tree, formatted for display as a
// faint hint placeholder. The param value in the tree is a number or a {value, expr} dim object.
static std::string baseParamHint(AppState& st, const std::string& featureId,
                                 const std::string& param, bool angle) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return {};
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return {};
    for (const auto& f : *fa) {
        if (!f.is_object() || djstr(f, "id") != featureId) continue;
        auto pp = f.find("params");
        if (pp == f.end() || !pp->is_object()) return {};
        auto pv = pp->find(param);
        if (pv == pp->end()) return {};
        double n;
        if (overrideNumber(*pv, n))
            return angle ? (std::to_string((int)n) + "\xc2\xb0") : c5::pmFormatDim(n, c5::ips(st));
        std::string e = overrideExpr(*pv);
        return e;
    }
    return {};
}

// The display name for a feature id from the live tree (its stored "name", or "type·id" fallback,
// matching the FeatureManager's row label). Empty when the feature is gone.
static std::string featureName(AppState& st, const std::string& fid) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return fid;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return fid;
    for (const auto& f : *fa) {
        if (!f.is_object() || djstr(f, "id") != fid) continue;
        std::string nm = djstr(f, "name");
        if (!nm.empty()) return nm;
        std::string ty = djstr(f, "type");
        return ty.empty() ? fid : (ty + "\xc2\xb7" + fid);
    }
    return fid;  // overridden-but-deleted feature: show the raw id so the column is still labelled
}

// True when the active document is a part/asm we can show a design table for (not a drawing tab,
// not the empty start state). Mirrors config_panel::configsApplicable.
static bool dtApplicable(AppState& st) {
    return st.docMirror.hasDoc() && st.workerStarted && !c5::activeTabIsDrawing(st);
}

// ---------------------------------------------------------------------------

const nlohmann::json* DesignTablePanel::detailFor(const std::string& name) const {
    for (const auto& e : detail_)
        if (e.is_object() && djstr(e, "name") == name) return &e;
    return nullptr;
}

void DesignTablePanel::addColumn(const Column& c) {
    for (const auto& e : columns_)
        if (e.suppress == c.suppress && e.featureId == c.featureId && e.param == c.param)
            return;  // already present
    columns_.push_back(c);
}

void DesignTablePanel::refresh(AppState& st) {
    if (!dtApplicable(st)) { cacheValid_ = false; return; }
    std::string err;
    nlohmann::json res = st.docMirror.configList(st.worker, err);
    if (res.is_null()) {
        if (!err.empty()) c5::addLog(st, "[design-table] config_list failed: " + err);
        cacheValid_ = false;  // retry next frame (cheap call)
        return;
    }
    names_.clear();
    if (res.contains("configurations") && res["configurations"].is_array())
        for (const auto& n : res["configurations"])
            if (n.is_string()) names_.push_back(n.get<std::string>());
    active_ = djstr(res, "active");
    detail_ = (res.contains("detail") && res["detail"].is_array()) ? res["detail"]
                                                                   : nlohmann::json::array();

    // On a doc change, drop the previous doc's column set so a fresh doc starts from its own
    // overrides. (Same doc, advancing revision: KEEP the user-added columns.)
    if (columnsForDocId_ != st.docMirror.docId()) {
        columns_.clear();
        columnsForDocId_ = st.docMirror.docId();
        selectedRow_ = -1;
    }

    // Seed/merge columns from EVERY (feature_id.param) any config currently overrides, plus a
    // suppression column for every feature any config suppresses. These are always present so the
    // grid round-trips the current config set even before the user adds anything.
    for (const auto& e : detail_) {
        if (!e.is_object()) continue;
        auto ov = e.find("param_overrides");
        if (ov != e.end() && ov->is_object()) {
            for (auto it = ov->begin(); it != ov->end(); ++it) {
                const std::string& k = it.key();  // "fid.param"
                auto dot = k.rfind('.');
                if (dot == std::string::npos) continue;
                Column c;
                c.suppress  = false;
                c.featureId = k.substr(0, dot);
                c.param     = k.substr(dot + 1);
                // Angle detection: ask the PM form whether this param is an angle dim (so we
                // parse/format in degrees). Falls back to length when no form/field is found.
                std::string ty;
                const nlohmann::json& tree = st.docMirror.tree();
                if (tree.is_object())
                    if (auto fa = tree.find("features"); fa != tree.end() && fa->is_array())
                        for (const auto& f : *fa)
                            if (f.is_object() && djstr(f, "id") == c.featureId)
                                { ty = djstr(f, "type"); break; }
                if (const c5::PMFormDef* form = ty.empty() ? nullptr : c5::pmFindForm(ty))
                    for (const auto& fd : form->fields)
                        if (fd.kind == c5::PMFieldKind::Dim && c.param == fd.key)
                            c.angle = (fd.unitLabel && std::strcmp(fd.unitLabel, " [deg]") == 0);
                c.label = featureName(st, c.featureId) + "." + c.param;
                addColumn(c);
            }
        }
        auto sup = e.find("suppressions");
        if (sup != e.end() && sup->is_object()) {
            for (auto it = sup->begin(); it != sup->end(); ++it) {
                if (!(it.value().is_boolean() && it.value().get<bool>())) continue;
                Column c;
                c.suppress  = true;
                c.featureId = it.key();
                c.label     = "Suppress: " + featureName(st, c.featureId);
                addColumn(c);
            }
        }
    }

    // Refresh labels of existing columns (a feature rename should retitle its column).
    for (auto& c : columns_) {
        if (c.suppress) c.label = "Suppress: " + featureName(st, c.featureId);
        else            c.label = featureName(st, c.featureId) + "." + c.param;
    }

    if (selectedRow_ >= (int)names_.size()) selectedRow_ = -1;
    cacheValid_ = true;
    cacheDocId_ = st.docMirror.docId();
    cacheRevision_ = st.docMirror.revision();
}

bool DesignTablePanel::runConfigOp(AppState& st, const std::string& method,
                                   const nlohmann::json& params) {
    core::OpsResponse r = st.docMirror.callConfig(st.worker, method, params);
    if (!r.ok) {
        std::string msg = r.error_message.empty() ? r.error_code : r.error_message;
        c5::addLog(st, "[design-table] " + method + " failed: " + msg);
        c5::toast(ToastLevel::Error, "Design Table: " + msg);
        cacheValid_ = false;  // re-pull so the grid reflects the unchanged truth
        return false;
    }
    // The ops-response already refreshed the mirror (tree/revision/owner map) + fired the listener
    // so the FeatureManager repaints. Reload the viewport mesh for the now-active config's geometry
    // (editing the active row changes geometry; an inactive-row edit returns the unchanged mesh).
    if (!r.aicm_path.empty()) {
        std::string le;
        if (!c5::loadDocMesh(st, r.aicm_path, le))
            c5::addLog(st, "[design-table] mesh reload failed: " + le);
    }
    cacheValid_ = false;  // config set changed — re-pull config_list on the next draw
    return true;
}

void DesignTablePanel::buildCandidates(AppState& st, std::vector<Candidate>& out) const {
    out.clear();
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto fa = tree.find("features");
    if (fa == tree.end() || !fa->is_array()) return;

    // Quick membership test against the current column set.
    auto haveParam = [&](const std::string& fid, const std::string& p) {
        for (const auto& c : columns_)
            if (!c.suppress && c.featureId == fid && c.param == p) return true;
        return false;
    };
    auto haveSuppress = [&](const std::string& fid) {
        for (const auto& c : columns_)
            if (c.suppress && c.featureId == fid) return true;
        return false;
    };

    for (const auto& f : *fa) {
        if (!f.is_object()) continue;
        std::string fid = djstr(f, "id");
        std::string ty  = djstr(f, "type");
        if (fid.empty()) continue;
        std::string fname = featureName(st, fid);

        // Param dims: the same dim fields the PropertyManager form exposes for this feature type.
        if (const c5::PMFormDef* form = ty.empty() ? nullptr : c5::pmFindForm(ty)) {
            for (const auto& fd : form->fields) {
                if (fd.kind != c5::PMFieldKind::Dim) continue;
                if (fd.comingSoon) continue;        // never-serialised discoverability fields
                std::string param = fd.key ? fd.key : "";
                if (param.empty() || haveParam(fid, param)) continue;
                Candidate cand;
                cand.col.suppress  = false;
                cand.col.featureId = fid;
                cand.col.param     = param;
                cand.col.angle     = (fd.unitLabel && std::strcmp(fd.unitLabel, " [deg]") == 0);
                cand.col.label     = fname + "." + param;
                cand.menuLabel     = fname + " / " + (fd.label && *fd.label ? fd.label : param);
                out.push_back(std::move(cand));
            }
        }
        // Suppression candidate (one per feature; sketches/datum reference geometry included — the
        // engine accepts any feature id and the overlay simply skips it when irrelevant).
        if (!haveSuppress(fid)) {
            Candidate cand;
            cand.col.suppress  = true;
            cand.col.featureId = fid;
            cand.col.label     = "Suppress: " + fname;
            cand.menuLabel     = fname + " / [Suppress feature]";
            out.push_back(std::move(cand));
        }
    }
}

void DesignTablePanel::draw(AppState& st) {
    if (!showDesignTablePanel()) return;
    ui::setNextCommandDialogPlacement();   // T-G23: float in overlay band (docked slot removed)
    if (!ImGui::Begin("Design Table", &showDesignTablePanel())) { ImGui::End(); return; }

        ImVec4 accentCol = c5::accent(); accentCol.w = 1.f;
    ImVec4 dimCol = c5::txtDim(); dimCol.w = 1.f;

    // Gate: only part/asm docs have configurations. Drawing tabs / no-doc show a hint.
    if (!dtApplicable(st)) {
        ImGui::TextColored(dimCol, "Open a part or assembly to edit its design table.");
        cacheValid_ = false;
        addCfgOpen_ = false;
        ImGui::End();
        return;
    }

    // Auto-invalidate when the mirror moved to a different doc or advanced its revision (an edit,
    // undo/redo, a config op done in the Configurations panel): the cached set may be stale.
    if (cacheValid_ && (cacheDocId_ != st.docMirror.docId() ||
                        cacheRevision_ != st.docMirror.revision()))
        cacheValid_ = false;
    if (!cacheValid_) refresh(st);

    // ---- header ----
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(accentCol, "Design Table");
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextColored(dimCol, "(%d config%s, %d column%s)",
                       (int)names_.size(), names_.size() == 1 ? "" : "s",
                       (int)columns_.size(), columns_.size() == 1 ? "" : "s");

    const bool busy = c5::busy(st);

    // ---- toolbar: Add Config / Delete / Activate / Add Column ----
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Add Config...")) {
        addCfgOpen_ = true;
        addCfgName_[0] = 0;
        focusAddCfg_ = true;
    }
    ImGui::SameLine();
    {
        const bool canDelete = selectedRow_ >= 0 && selectedRow_ < (int)names_.size() &&
                               names_.size() > 1;
        if (!canDelete) ImGui::BeginDisabled();
        if (ImGui::Button("Delete Config"))
            runConfigOp(st, "config_delete", {{"name", names_[selectedRow_]}});
        if (!canDelete) ImGui::EndDisabled();
        if (ImGui::IsItemHovered() && names_.size() <= 1)
            ImGui::SetTooltip("A document must keep at least one configuration.");
    }
    ImGui::SameLine();
    {
        const bool canActivate = selectedRow_ >= 0 && selectedRow_ < (int)names_.size() &&
                                 names_[selectedRow_] != active_;
        if (!canActivate) ImGui::BeginDisabled();
        if (ImGui::Button("Activate"))
            runConfigOp(st, "config_activate", {{"name", names_[selectedRow_]}});
        if (!canActivate) ImGui::EndDisabled();
    }
    ImGui::SameLine();
    if (ImGui::Button("Add Column...")) { addOpen_ = true; ImGui::OpenPopup("##addcol"); }
    if (busy) ImGui::EndDisabled();
    if (busy) { ImGui::SameLine(); ImGui::TextColored(dimCol, "busy..."); }

    // ---- add-column popup: candidate dims + suppression entries from the live model ----
    if (ImGui::BeginPopup("##addcol")) {
        std::vector<Candidate> cands;
        buildCandidates(st, cands);
        ImGui::TextColored(dimCol, "Add a column");
        ImGui::Separator();
        if (cands.empty()) {
            ImGui::TextColored(dimCol, "No more columns to add.");
        } else {
            ImGui::BeginChild("##candlist", ImVec2(280.0f * c5::scale(), 320.0f * c5::scale()));
            for (size_t i = 0; i < cands.size(); ++i) {
                ImGui::PushID((int)i);
                if (ImGui::Selectable(cands[i].menuLabel.c_str())) {
                    addColumn(cands[i].col);
                    ImGui::CloseCurrentPopup();
                }
                ImGui::PopID();
            }
            ImGui::EndChild();
        }
        ImGui::EndPopup();
    }

    // ---- add-config inline prompt (mirrors config_panel) ----
    if (addCfgOpen_) {
        ImGui::Separator();
        ImGui::TextColored(dimCol, "New configuration");
        ImGui::SetNextItemWidth(-1);
        if (focusAddCfg_) { ImGui::SetKeyboardFocusHere(); focusAddCfg_ = false; }
        const bool entered = ImGui::InputTextWithHint(
            "##dtcfgname", "name", addCfgName_, sizeof(addCfgName_),
            ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll);
        ImGui::Checkbox("Activate after creating", &addCfgActivate_);
        const bool nameOk = addCfgName_[0] != 0;
        if (!nameOk) ImGui::BeginDisabled();
        const bool create = ImGui::Button("Create") || (entered && nameOk);
        if (!nameOk) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) addCfgOpen_ = false;
        if (create && nameOk) {
            std::string name(addCfgName_);
            // Seed from the active config so the new row starts as a sibling (SolidWorks behavior).
            nlohmann::json p = {{"name", name}};
            if (!active_.empty()) p["copy_from"] = active_;
            if (runConfigOp(st, "config_add", p)) {
                if (addCfgActivate_) runConfigOp(st, "config_activate", {{"name", name}});
                addCfgOpen_ = false;
            }
        }
    }

    ImGui::Separator();

    if (names_.empty()) {
        ImGui::TextColored(dimCol, "No configurations.");
        ImGui::End();
        return;
    }
    if (columns_.empty()) {
        ImGui::TextColored(dimCol,
            "No driven columns yet. Use \"Add Column...\" to drive a feature dimension or");
        ImGui::TextColored(dimCol, "suppression per configuration.");
        // Still show the config-name list so rows are selectable/activatable.
    }

    // ---- the grid: first column = config name; one column per driven param / suppression ----
    const int ncols = 1 + (int)columns_.size();
    const ImGuiTableFlags flags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                                  ImGuiTableFlags_ScrollX | ImGuiTableFlags_ScrollY |
                                  ImGuiTableFlags_Resizable;
    if (ImGui::BeginTable("designtable", ncols, flags)) {
        ImGui::TableSetupScrollFreeze(1, 1);  // freeze the config-name column + header row
        ImGui::TableSetupColumn("Configuration", ImGuiTableColumnFlags_WidthFixed,
                                160.0f * c5::scale());
        for (const auto& c : columns_)
            ImGui::TableSetupColumn(c.label.c_str(), ImGuiTableColumnFlags_WidthFixed,
                                    120.0f * c5::scale());
        ImGui::TableHeadersRow();

        for (int row = 0; row < (int)names_.size(); ++row) {
            const std::string& name = names_[row];
            const bool isActive = (name == active_);
            const nlohmann::json* d = detailFor(name);
            ImGui::PushID(row);
            ImGui::TableNextRow();

            // --- config-name cell: selectable; double-click activates; active row highlighted ---
            ImGui::TableSetColumnIndex(0);
            std::string nameLabel = (isActive ? std::string("\xe2\x97\x8f  ")
                                              : std::string("    ")) + name;
            if (isActive) ImGui::PushStyleColor(ImGuiCol_Text, accentCol);
            const bool clicked = ImGui::Selectable(
                nameLabel.c_str(), selectedRow_ == row,
                ImGuiSelectableFlags_SpanAllColumns | ImGuiSelectableFlags_AllowDoubleClick);
            if (isActive) ImGui::PopStyleColor();
            if (clicked) {
                selectedRow_ = row;
                if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !isActive && !busy)
                    runConfigOp(st, "config_activate", {{"name", name}});
            }

            // --- driven cells ---
            for (int col = 0; col < (int)columns_.size(); ++col) {
                const Column& c = columns_[col];
                ImGui::TableSetColumnIndex(col + 1);
                ImGui::PushID(col);

                if (c.suppress) {
                    // Suppression checkbox: true when this config explicitly suppresses the feature.
                    bool sup = false;
                    if (d) {
                        auto sIt = d->find("suppressions");
                        if (sIt != d->end() && sIt->is_object()) {
                            auto e = sIt->find(c.featureId);
                            if (e != sIt->end() && e->is_boolean()) sup = e->get<bool>();
                        }
                    }
                    if (busy) ImGui::BeginDisabled();
                    if (ImGui::Checkbox("##sup", &sup))
                        runConfigOp(st, "config_set_suppression",
                                    {{"name", name}, {"feature_id", c.featureId},
                                     {"suppressed", sup}});
                    if (busy) ImGui::EndDisabled();
                } else {
                    // Numeric param cell: blank = inherits base; an entered value sets the override;
                    // clearing it reverts to base (config_set_override clear:true).
                    char buf[64] = {0};
                    std::string placeholder;  // faint base-value hint when no override
                    if (d) {
                        auto oIt = d->find("param_overrides");
                        if (oIt != d->end() && oIt->is_object()) {
                            auto e = oIt->find(c.key());
                            if (e != oIt->end()) {
                                std::string expr = overrideExpr(*e);
                                double n;
                                std::string disp =
                                    !expr.empty() ? expr
                                    : overrideNumber(*e, n)
                                          ? (c.angle ? (std::to_string((int)n) + "\xc2\xb0")
                                                     : c5::pmFormatDim(n, c5::ips(st)))
                                          : e->dump();
                                std::snprintf(buf, sizeof(buf), "%s", disp.c_str());
                            }
                        }
                    }
                    if (buf[0] == 0) {
                        std::string h = baseParamHint(st, c.featureId, c.param, c.angle);
                        placeholder = h.empty() ? "(base)" : (h + "  (base)");
                    }
                    const std::string orig(buf);  // displayed override string before this edit
                    ImGui::SetNextItemWidth(-1);
                    if (busy) ImGui::BeginDisabled();
                    const bool committed = ImGui::InputTextWithHint(
                        "##cell", placeholder.c_str(), buf, sizeof(buf),
                        ImGuiInputTextFlags_EnterReturnsTrue);
                    const bool deactivated = ImGui::IsItemDeactivatedAfterEdit();
                    if (busy) ImGui::EndDisabled();
                    // Only write when the buffer actually changed: a bare focus-in/out must NOT
                    // re-issue an override (which would push a redundant undo step + rebuild).
                    if ((committed || deactivated) && !busy && orig != std::string(buf)) {
                        std::string text(buf);
                        // Trim surrounding whitespace.
                        size_t a = text.find_first_not_of(" \t");
                        size_t b = text.find_last_not_of(" \t");
                        text = (a == std::string::npos) ? std::string()
                                                        : text.substr(a, b - a + 1);
                        if (text.empty()) {
                            // Empty cell => clear the override (revert to base).
                            runConfigOp(st, "config_set_override",
                                        {{"name", name}, {"feature_id", c.featureId},
                                         {"param", c.param}, {"clear", true}});
                        } else if (!text.empty() && text[0] == '=') {
                            // Leading '=' => an expression binding ({value:0, expr}); the engine
                            // re-evaluates it on rebuild. Mirror the PM's expr handling.
                            runConfigOp(st, "config_set_override",
                                        {{"name", name}, {"feature_id", c.featureId},
                                         {"param", c.param},
                                         {"value", {{"value", 0.0}, {"expr", text}}}});
                        } else {
                            double mm = 0.0;
                            std::string perr;
                            const double scale = c5::ips(st) ? 25.4 : 1.0;
                            if (c5::pmParseDim(text, mm, perr, c.angle, scale)) {
                                runConfigOp(st, "config_set_override",
                                            {{"name", name}, {"feature_id", c.featureId},
                                             {"param", c.param}, {"value", mm}});
                            } else {
                                c5::toast(ToastLevel::Error,
                                         "Design Table: " + (perr.empty() ? "bad value" : perr));
                                cacheValid_ = false;  // re-pull to restore the displayed value
                            }
                        }
                    }
                }
                ImGui::PopID();
            }
            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::End();
}
}  // namespace ui
