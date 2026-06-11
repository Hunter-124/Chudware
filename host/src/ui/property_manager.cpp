// PropertyManager implementation (C17).
//
// Draws the active feature form in the FeatureManager dock slot, routes viewport picks into the
// active selector well, debounces live previews through doc_apply_ops (add or edit_feature), and
// commits (OK) / rolls back (Cancel via worker undo of each preview batch).
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "property_manager.h"
#include "icons.h"

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../render/id_pass.h"

namespace ui {

// Debounce window for live preview (card §4).
static const double kDebounceSec = 0.40;

// ---- field enable helpers -------------------------------------------------

// Return the param-value string currently selected by a Combo field with the given key.
static std::string comboValue(const std::vector<PMField>& fields, const char* key) {
    for (const auto& f : fields) {
        if (f.def->kind != PMFieldKind::Combo || std::strcmp(f.def->key, key) != 0) continue;
        std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
        int cur = 0; size_t start = 0;
        while (start <= opts.size()) {
            size_t bar = opts.find('|', start);
            std::string opt = opts.substr(start, bar - start);
            if (cur == f.comboIndex) return opt;
            ++cur;
            if (bar == std::string::npos) break;
            start = bar + 1;
        }
    }
    return {};
}

// Return true when the field's enableKey/enableVals condition is satisfied (always true when
// enableKey is null). comingSoon fields are never enabled for interaction.
static bool fieldEnabled(const PMFieldDef& def, const std::vector<PMField>& fields) {
    if (def.comingSoon) return false;
    if (!def.enableKey)  return true;
    std::string cur = comboValue(fields, def.enableKey);
    std::string allowed = def.enableVals ? def.enableVals : "";
    if (allowed.empty()) return true;
    // Check if cur appears as a token in the pipe-separated allowed list.
    size_t start = 0;
    while (start <= allowed.size()) {
        size_t bar = allowed.find('|', start);
        std::string tok = allowed.substr(start, bar - start);
        if (tok == cur) return true;
        if (bar == std::string::npos) break;
        start = bar + 1;
    }
    return false;
}

// ---- dim parsing ----------------------------------------------------------
// Accepts: "25", "25mm", "1.5in", "3/8in", "3/8" (assumed in when a fraction). Canonical = mm.

static bool parseFraction(const std::string& s, double& out) {
    auto slash = s.find('/');
    if (slash == std::string::npos) return false;
    try {
        double num = std::stod(s.substr(0, slash));
        double den = std::stod(s.substr(slash + 1));
        if (den == 0.0) return false;
        out = num / den;
        return true;
    } catch (...) { return false; }
}

bool pmParseDim(const std::string& raw, double& outMm, std::string& err) {
    err.clear();
    std::string s;
    for (char c : raw) if (!std::isspace((unsigned char)c)) s += c;
    if (s.empty()) { err = "empty"; return false; }
    for (auto& c : s) c = (char)std::tolower((unsigned char)c);

    double scale = 1.0;          // mm per unit
    bool inches = false;
    if (s.size() >= 2 && s.compare(s.size() - 2, 2, "mm") == 0) {
        s.erase(s.size() - 2);
    } else if (s.size() >= 2 && s.compare(s.size() - 2, 2, "in") == 0) {
        s.erase(s.size() - 2); inches = true; scale = 25.4;
    } else if (!s.empty() && s.back() == '"') {
        s.pop_back(); inches = true; scale = 25.4;
    }
    (void)inches;

    double val = 0.0;
    if (s.find('/') != std::string::npos) {
        if (!parseFraction(s, val)) { err = "bad fraction"; return false; }
    } else {
        try { size_t pos = 0; val = std::stod(s, &pos);
              if (pos != s.size()) { err = "bad number"; return false; } }
        catch (...) { err = "bad number"; return false; }
    }
    outMm = val * scale;
    return true;
}

std::string pmFormatDim(double mm, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4gin", mm / 25.4);
    else     std::snprintf(buf, sizeof(buf), "%.4gmm", mm);
    return buf;
}

// A dim value object {value, expr} for an op.
static nlohmann::json dimJson(const PMField& f) {
    nlohmann::json d;
    if (!f.dimExpr.empty()) {
        // Bound to an expression: value carries the last resolved number (or 0); expr is the bind.
        d["value"] = f.dimValue;
        d["expr"]  = f.dimExpr;
    } else {
        d["value"] = f.dimValue;
        d["expr"]  = nullptr;
    }
    return d;
}

// ---- open / close ---------------------------------------------------------

static void initField(PMField& f, const PMFieldDef& d, bool ips) {
    f.def = &d;
    switch (d.kind) {
        case PMFieldKind::Dim: {
            f.dimValue = d.dimDefault;
            f.dimExpr.clear();
            std::string disp = pmFormatDim(d.dimDefault, ips);
            std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
            break;
        }
        case PMFieldKind::Combo:   f.comboIndex = 0; break;
        case PMFieldKind::Check:   f.checkValue = d.checkDefault; break;
        case PMFieldKind::SelectorWell: f.picks.clear(); f.wellActive = false; break;
    }
}

bool PropertyManager::openForCommand(AppState& st, const std::string& cmdId) {
    std::string mode;
    const PMFormDef* form = pmFormForCommand(cmdId, mode);
    if (!form) return false;
    if (active_) close();   // a previous form is replaced (no commit)

    active_ = true;
    editMode_ = false;
    form_ = form;
    mode_ = mode;
    title_ = form->title;
    targetId_.clear();
    previewBatches_ = 0;
    dirty_ = false;
    firstPreviewDone_ = false;
    lastEditTime_ = ImGui::GetTime();

    const bool useIps = false;  // M1: units display follows set_units; default MMGS.
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], useIps);

    // If a mode combo exists, preselect it from the command (boss/cut split button).
    if (!mode_.empty()) {
        for (auto& f : fields_) {
            if (f.def->kind == PMFieldKind::Combo && std::strcmp(f.def->key, "mode") == 0) {
                f.comboIndex = (mode_ == "cut") ? 1 : 0;
            }
        }
    }

    // First selector well becomes active so the user can pick immediately.
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell) { f.wellActive = true; break; }

    // Seed any already-resolved viewport selection into the first well (SolidWorks pre-select).
    PMField* well = activeWell();
    if (well) {
        for (const auto& it : st.selection.items()) {
            if (it.pending_resolve || it.selector.empty()) continue;
            try {
                nlohmann::json selObj = nlohmann::json::parse(it.selector);
                int k = (int)it.hit.kind;
                routePick(selObj, k);
            } catch (...) {}
        }
    }

    st.addLog("[pm] open " + std::string(form_->type) +
              (mode_.empty() ? "" : " (" + mode_ + ")"));
    return true;
}

bool PropertyManager::openForEdit(AppState& st, const std::string& featureId) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return false;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return false;
    const nlohmann::json* target = nullptr;
    for (const auto& f : *feat) {
        if (f.is_object() && f.value("id", std::string()) == featureId) { target = &f; break; }
    }
    if (!target) return false;
    std::string type = target->value("type", std::string());
    const PMFormDef* form = pmFindForm(type);
    if (!form) { st.addLog("[pm] no form for feature type '" + type + "'"); return false; }
    if (active_) close();

    active_ = true;
    editMode_ = true;
    form_ = form;
    mode_.clear();
    title_ = form->title;
    targetId_ = featureId;
    previewBatches_ = 0;
    dirty_ = false;
    firstPreviewDone_ = true;   // the feature already exists; edits go straight to edit_feature
    lastEditTime_ = ImGui::GetTime();

    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], false);

    loadFromTree(st, *target);
    st.addLog("[pm] edit " + featureId + " (" + type + ")");
    return true;
}

void PropertyManager::loadFromTree(AppState& st, const nlohmann::json& feature) {
    (void)st;
    nlohmann::json params = feature.value("params", nlohmann::json::object());
    nlohmann::json refs   = feature.value("refs",   nlohmann::json::object());
    for (auto& f : fields_) {
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                if (params.contains(key) && params[key].is_object()) {
                    const auto& d = params[key];
                    if (d.contains("expr") && d["expr"].is_string()) {
                        f.dimExpr = d["expr"].get<std::string>();
                        if (d.contains("value") && d["value"].is_number())
                            f.dimValue = d["value"].get<double>();
                        std::string disp = "=" + f.dimExpr;
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    } else if (d.contains("value") && d["value"].is_number()) {
                        f.dimValue = d["value"].get<double>();
                        std::string disp = pmFormatDim(f.dimValue, false);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    }
                }
                break;
            }
            case PMFieldKind::Combo: {
                if (params.contains(key) && params[key].is_string()) {
                    std::string val = params[key].get<std::string>();
                    // Resolve combo option index.
                    std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                    int idx = 0, cur = 0; size_t start = 0;
                    while (start <= opts.size()) {
                        size_t bar = opts.find('|', start);
                        std::string opt = opts.substr(start, bar - start);
                        if (opt == val) { idx = cur; break; }
                        ++cur;
                        if (bar == std::string::npos) break;
                        start = bar + 1;
                    }
                    f.comboIndex = idx;
                }
                break;
            }
            case PMFieldKind::Check: {
                if (params.contains(key) && params[key].is_boolean())
                    f.checkValue = params[key].get<bool>();
                break;
            }
            case PMFieldKind::SelectorWell: {
                f.picks.clear();
                if (refs.contains(key)) {
                    const auto& rv = refs[key];
                    auto add = [&](const nlohmann::json& selObj) {
                        if (!selObj.is_object()) return;
                        PMSelEntry e; e.selector = selObj;
                        std::string k = selObj.value("kind", std::string("ref"));
                        e.label = k.empty() ? "ref" : (char)std::toupper((unsigned char)k[0]) + k.substr(1);
                        f.picks.push_back(e);
                    };
                    if (rv.is_array()) for (const auto& s : rv) add(s);
                    else add(rv);
                }
                break;
            }
        }
    }
}

// ---- in-viewport callouts (C27-A) -----------------------------------------

// Pull a world-space anchor from a selector entry: prefer the selector's own "centroid"
// (resolve_pick stamps a world centroid on every face/edge selector); fall back to the
// AppState topo-id centroid caches keyed by (body_id<<32 | topo_id) when absent.
static bool anchorFromSel(AppState& st, const PMSelEntry& e, bool wantEdge, float out[3]) {
    const nlohmann::json& s = e.selector;
    auto c = s.find("centroid");
    if (c != s.end() && c->is_array() && c->size() == 3 &&
        (*c)[0].is_number() && (*c)[1].is_number() && (*c)[2].is_number()) {
        out[0] = (float)(*c)[0].get<double>();
        out[1] = (float)(*c)[1].get<double>();
        out[2] = (float)(*c)[2].get<double>();
        return true;
    }
    // Cache fallback: hint_index is 0-based; cache keys are 1-based AICM topo ids.
    int hint = s.value("hint_index", -1);
    if (hint < 0) return false;
    uint64_t key = (uint64_t)1 << 32 | (uint64_t)(hint + 1);  // body 1 (single-part edit path)
    if (wantEdge) {
        auto it = st.edgeCentroidCache.find(key);
        if (it == st.edgeCentroidCache.end()) return false;
        out[0] = it->second[0]; out[1] = it->second[1]; out[2] = it->second[2];
        return true;
    }
    auto it = st.faceNormalCache.find(key);  // {nx,ny,nz, cx,cy,cz}
    if (it == st.faceNormalCache.end()) return false;
    out[0] = it->second[3]; out[1] = it->second[4]; out[2] = it->second[5];
    return true;
}

std::vector<PMCalloutItem> PropertyManager::calloutItems(AppState& st) const {
    std::vector<PMCalloutItem> out;
    if (!active_ || !form_) return out;

    // Resolve a single shared anchor for the feature: first edge ref's centroid, else first
    // face ref's centroid, else none. Edges win (dress-up features point at an edge).
    float anchor[3] = {0, 0, 0};
    bool hasAnchor = false;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
        if (!f.def->selKinds.edge) continue;
        if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/true, anchor)) { hasAnchor = true; break; }
    }
    if (!hasAnchor) {
        for (const auto& f : fields_) {
            if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
            if (!f.def->selKinds.face) continue;
            if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/false, anchor)) { hasAnchor = true; break; }
        }
    }

    const bool useIps = false;  // M1 default MMGS (mirrors draw())
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim) continue;
        if (!fieldEnabled(*f.def, fields_)) continue;   // skip greyed / coming-soon dims
        PMCalloutItem it;
        it.label = f.def->label;
        it.value = f.dimExpr.empty() ? pmFormatDim(f.dimValue, useIps)
                                     : ("=" + f.dimExpr);
        it.hasAnchor = hasAnchor;
        if (hasAnchor) { it.anchor[0] = anchor[0]; it.anchor[1] = anchor[1]; it.anchor[2] = anchor[2]; }
        out.push_back(std::move(it));
    }
    return out;
}

void PropertyManager::close() {
    active_ = false;
    editMode_ = false;
    form_ = nullptr;
    fields_.clear();
    mode_.clear();
    title_.clear();
    targetId_.clear();
    previewBatches_ = 0;
    dirty_ = false;
    firstPreviewDone_ = false;
}

// ---- selector wells -------------------------------------------------------

PMField* PropertyManager::activeWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive) return &f;
    return nullptr;
}

bool PropertyManager::routePick(const nlohmann::json& selector, int pickKind) {
    PMField* well = activeWell();
    if (!well || !selector.is_object()) return false;
    // Filter by allowed kinds.
    render::PickKind k = (render::PickKind)pickKind;
    const PMSelKinds& ak = well->def->selKinds;
    bool allowed = (k == render::PickKind::Face   && ak.face) ||
                   (k == render::PickKind::Edge   && ak.edge) ||
                   (k == render::PickKind::Vertex && ak.vertex);
    if (!allowed) return false;

    // De-dup by hint_index + centroid (same element picked twice toggles off — SolidWorks).
    auto same = [&](const nlohmann::json& a, const nlohmann::json& b) {
        return a.value("kind", std::string()) == b.value("kind", std::string()) &&
               a.value("hint_index", -1) == b.value("hint_index", -2) &&
               a.value("centroid", nlohmann::json()) == b.value("centroid", nlohmann::json());
    };
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (same(well->picks[i].selector, selector)) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    PMSelEntry e; e.selector = selector;
    std::string kind = selector.value("kind", std::string("ref"));
    e.label = kind.empty() ? "Ref" : std::string(1, (char)std::toupper((unsigned char)kind[0])) + kind.substr(1);
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    return true;
}

// ---- op assembly ----------------------------------------------------------

void PropertyManager::buildOpFeature(nlohmann::json& feature) const {
    feature["type"] = form_->type;
    feature["name"] = std::string(title_) + (targetId_.empty() ? "1" : "");  // overwritten by id_map naming
    nlohmann::json params = nlohmann::json::object();
    nlohmann::json refs   = nlohmann::json::object();
    for (const auto& f : fields_) {
        // comingSoon fields are display-only; never emit them to the engine.
        if (f.def->comingSoon) continue;
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim:
                params[key] = dimJson(f);
                break;
            case PMFieldKind::Combo: {
                std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                int cur = 0; size_t start = 0; std::string chosen;
                while (start <= opts.size()) {
                    size_t bar = opts.find('|', start);
                    std::string opt = opts.substr(start, bar - start);
                    if (cur == f.comboIndex) { chosen = opt; break; }
                    ++cur;
                    if (bar == std::string::npos) break;
                    start = bar + 1;
                }
                if (!chosen.empty()) params[key] = chosen;
                break;
            }
            case PMFieldKind::Check:
                params[key] = f.checkValue;
                break;
            case PMFieldKind::SelectorWell: {
                if (f.def->selMulti) {
                    nlohmann::json arr = nlohmann::json::array();
                    for (const auto& p : f.picks) arr.push_back(p.selector);
                    refs[key] = arr;
                } else if (!f.picks.empty()) {
                    refs[key] = f.picks.front().selector;
                }
                break;
            }
        }
    }
    feature["params"] = params;
    if (!refs.empty()) feature["refs"] = refs;
}

// Minimum refs satisfied so a preview is worth attempting (avoid spamming the worker with
// no-edge fillets). Returns true when every multi-well that the feature *requires* has ≥1 pick.
static bool readyForPreview(const PMFormDef* form, const std::vector<PMField>& fields) {
    // Dress-up + sketched features need their primary selector populated.
    const char* needWell = nullptr;
    std::string t = form->type;
    if (t == "fillet" || t == "chamfer") needWell = "edges";
    else if (t == "extrude" || t == "revolve") needWell = "profile";
    // shell can preview with 0 faces (hollow closed shell); box/cylinder have no wells.
    if (!needWell) return true;
    for (const auto& f : fields)
        if (f.def->kind == PMFieldKind::SelectorWell && std::strcmp(f.def->key, needWell) == 0)
            return !f.picks.empty();
    return true;
}

// ---- preview / commit / cancel --------------------------------------------

void PropertyManager::applyPreview(AppState& st) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    if (!readyForPreview(form_, fields_)) { dirty_ = false; return; }

    nlohmann::json feature;
    buildOpFeature(feature);

    nlohmann::json ops = nlohmann::json::array();
    if (!firstPreviewDone_ && targetId_.empty()) {
        // First preview: add the feature.
        ops.push_back({{"op", "add_feature"}, {"feature", feature}, {"before", nullptr}});
    } else {
        // Subsequent previews / edit-path: edit the existing feature in place.
        if (targetId_.empty()) { dirty_ = false; return; }
        ops.push_back({{"op", "edit_feature"}, {"id", targetId_},
                       {"params", feature.value("params", nlohmann::json::object())},
                       {"refs",   feature.value("refs",   nlohmann::json::object())}});
    }

    auto resp = st.docMirror.applyOps(st.worker, ops);
    if (!resp.ok) {
        st.addLog("[pm] preview failed: " + resp.error_message);
        dirty_ = false;
        return;
    }
    ++previewBatches_;
    // The add-feature flow learns the new feature id from id_map ($0 -> fX).
    if (targetId_.empty() && resp.id_map.is_object()) {
        auto it = resp.id_map.find("$0");
        if (it != resp.id_map.end() && it->is_string()) targetId_ = it->get<std::string>();
    }
    firstPreviewDone_ = true;

    if (!resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[pm] mesh: " + lerr);
    }
    // Reflect any dangling ref back into the wells (red tint).
    if (!targetId_.empty() && resp.feature_status.is_array()) {
        for (const auto& s : resp.feature_status) {
            if (!s.is_object() || s.value("id", std::string()) != targetId_) continue;
            auto dr = s.find("dangling_refs");
            std::vector<std::string> dangling;
            if (dr != s.end() && dr->is_array())
                for (const auto& d : *dr) if (d.is_string()) dangling.push_back(d.get<std::string>());
            for (auto& f : fields_) {
                if (f.def->kind != PMFieldKind::SelectorWell) continue;
                f.wellDangling = false;
                for (const auto& dref : dangling)
                    if (dref.rfind(f.def->key, 0) == 0) f.wellDangling = true;
            }
        }
    }
    dirty_ = false;
}

void PropertyManager::tick(AppState& st) {
    if (!active_ || !dirty_) return;
    double now = ImGui::GetTime();
    if (now - lastEditTime_ < kDebounceSec) return;
    applyPreview(st);
}

void PropertyManager::commit(AppState& st) {
    // Make sure the latest field state is reflected before committing.
    if (dirty_ || !firstPreviewDone_) applyPreview(st);
    st.addLog("[pm] OK -> committed " + (targetId_.empty() ? std::string("(no geometry)") : targetId_));
    // The feature stays in the document; the tree returns on close.
    close();
}

void PropertyManager::cancelEdits(AppState& st) {
    // Undo every preview batch we applied (batch-level worker undo).
    int n = previewBatches_;
    for (int i = 0; i < n; ++i) {
        auto r = st.docMirror.undo(st.worker);
        if (!r.ok) { st.addLog("[pm] cancel undo failed: " + r.error_message); break; }
        if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
    }
    st.addLog("[pm] Cancel -> undid " + std::to_string(n) + " preview batch(es)");
    close();
}

// ---- drawing --------------------------------------------------------------

static void pushTextColor(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}

void PropertyManager::draw(AppState& st) {
    if (!active_) return;
    if (!ImGui::Begin("FeatureManager")) { ImGui::End(); return; }  // same dock slot as the tree

    const auto& pal = ui::palette;

    // ---- header: title + OK (accent ✓) / Cancel (✗) -----------------------
    {
        IconUV ic = getIcon(form_->icon, 20);
        if (ic.valid) { ImGui::Image(ic.texId, ImVec2(18, 18), ic.uv0, ic.uv1); ImGui::SameLine(0, 6); }
        pushTextColor(pal.text);
        ImGui::TextUnformatted(title_.c_str());
        ImGui::PopStyleColor();

        // OK / Cancel buttons, right-aligned. Anchor from the window's right inner
        // edge so a narrow dock never clips the buttons ("OK" -> "OI"). The pair
        // needs btnW*2 + the 4px gap between them; pin its left edge inside the
        // content region. Wrap to a new line if the title leaves no room.
        float btnW = 28.0f;
        float pairW = btnW * 2 + 4;
        float rightX = ImGui::GetWindowContentRegionMax().x - pairW;
        float afterTitleX = ImGui::GetCursorPosX();
        if (rightX > afterTitleX + 6) {
            ImGui::SameLine(0, 0);
            ImGui::SetCursorPosX(rightX);
        } else {
            // Title too wide for same line: drop the buttons to their own row,
            // still right-aligned, so neither overlaps the title nor clips.
            ImGui::SetCursorPosX(rightX < 0 ? 0 : rightX);
        }

        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        bool ok = ImGui::Button("OK##pm", ImVec2(btnW, 0));
        ImGui::PopStyleColor(2);
        ImGui::SameLine(0, 4);
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.button[0], pal.button[1], pal.button[2], 1));
        bool cancel = ImGui::Button("X##pm", ImVec2(btnW, 0));
        ImGui::PopStyleColor();

        if (ImGui::IsItemHovered() && ImGui::IsKeyPressed(ImGuiKey_Escape)) cancel = true;
        if (ImGui::IsKeyPressed(ImGuiKey_Enter, false) && !ImGui::IsAnyItemActive()) ok = true;

        ImGui::Separator();

        if (ok)     { commit(st);      ImGui::End(); return; }
        if (cancel) { cancelEdits(st); ImGui::End(); return; }
    }

    // ---- field rows grouped by `group`, collapsible ------------------------
    const char* curGroup = nullptr;
    bool groupOpen = true;
    bool useIps = false;   // M1 default MMGS

    for (size_t i = 0; i < fields_.size(); ++i) {
        PMField& f = fields_[i];
        // New group header?
        if (!curGroup || std::strcmp(curGroup, f.def->group) != 0) {
            curGroup = f.def->group;
            groupOpen = ImGui::CollapsingHeader(curGroup, ImGuiTreeNodeFlags_DefaultOpen);
        }
        if (!groupOpen) continue;

        // Conditional enable: grey-out when the sibling Combo condition isn't met or comingSoon.
        bool enabled = fieldEnabled(*f.def, fields_);
        if (!enabled) ImGui::BeginDisabled();

        ImGui::PushID((int)i);
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                pushTextColor(pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Not yet supported by engine");
                ImGui::SetNextItemWidth(-1);
                bool changed = ImGui::InputText("##dim", f.dimBuf.data(), f.dimBuf.size(),
                                                ImGuiInputTextFlags_EnterReturnsTrue |
                                                ImGuiInputTextFlags_AutoSelectAll);
                bool deact = ImGui::IsItemDeactivatedAfterEdit();
                if (enabled && (changed || deact)) {
                    std::string txt = f.dimBuf.data();
                    // trim
                    while (!txt.empty() && std::isspace((unsigned char)txt.front())) txt.erase(txt.begin());
                    while (!txt.empty() && std::isspace((unsigned char)txt.back())) txt.pop_back();
                    if (!txt.empty() && txt[0] == '=') {
                        f.dimExpr = txt.substr(1);
                        f.dimError.clear();
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    } else {
                        double mm;
                        std::string err;
                        if (pmParseDim(txt, mm, err)) {
                            f.dimValue = mm; f.dimExpr.clear(); f.dimError.clear();
                            dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        } else {
                            f.dimError = err;
                        }
                    }
                }
                // expr resolved-value + tooltip
                if (enabled && !f.dimExpr.empty()) {
                    pushTextColor(pal.accent);
                    ImGui::Text(u8"= %s  (%s)", f.dimExpr.c_str(),
                                pmFormatDim(f.dimValue, useIps).c_str());
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Bound to equation '%s'", f.dimExpr.c_str());
                }
                if (enabled && !f.dimError.empty()) {
                    pushTextColor(pal.statusErr);
                    ImGui::Text("! %s", f.dimError.c_str());
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::SelectorWell: {
                // Label + activate toggle.
                pushTextColor(f.wellActive ? pal.accent : pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();

                // The well box: click activates (routes picks here); right-click clears.
                std::string chip = f.picks.empty() ? std::string("(pick in viewport)")
                                                   : (f.picks.front().label + u8"×" +
                                                      std::to_string(f.picks.size()));
                ImVec4 border = f.wellDangling
                    ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
                    : (f.wellActive
                        ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                        : ImVec4(pal.border[0], pal.border[1], pal.border[2], pal.border[3]));
                ImGui::PushStyleColor(ImGuiCol_Border, border);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    f.wellActive ? ImVec4(pal.accentSoft[0], pal.accentSoft[1],
                                          pal.accentSoft[2], pal.accentSoft[3])
                                 : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 1));
                ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 1.0f);
                if (ImGui::Button((chip + "##well").c_str(), ImVec2(-1, 0))) {
                    // Activate this well; deactivate others.
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell) o.wellActive = false;
                    f.wellActive = true;
                }
                ImGui::PopStyleVar();
                ImGui::PopStyleColor(2);
                if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
                    f.picks.clear(); f.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                if (f.wellDangling) {
                    pushTextColor(pal.statusErr);
                    ImGui::TextUnformatted("! dangling reference");
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::Combo: {
                pushTextColor(pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Not yet supported by engine");
                // Build display labels (comboLabels if provided, else fall back to comboOptions).
                std::string lblSrc = f.def->comboLabels ? f.def->comboLabels
                                   : (f.def->comboOptions ? f.def->comboOptions : "");
                std::vector<std::string> labels; { size_t s = 0;
                    while (s <= lblSrc.size()) {
                        size_t b = lblSrc.find('|', s);
                        labels.push_back(lblSrc.substr(s, b - s));
                        if (b == std::string::npos) break; s = b + 1;
                    }
                }
                std::string preview = (f.comboIndex < (int)labels.size()) ? labels[f.comboIndex] : "";
                ImGui::SetNextItemWidth(-1);
                if (ImGui::BeginCombo("##combo", preview.c_str())) {
                    for (int n = 0; n < (int)labels.size(); ++n) {
                        bool sel = (f.comboIndex == n);
                        if (ImGui::Selectable(labels[n].c_str(), sel)) {
                            if (enabled) { f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                        }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::EndCombo();
                }
                break;
            }
            case PMFieldKind::Check: {
                bool val = f.checkValue;
                // Render the box with no inline label, then place the label on the
                // same row with wrapping enabled. ImGui's built-in Checkbox label
                // never wraps and gets clipped on a narrow dock (e.g. the long
                // "Tangent propagation (coming soon)" rows) — wrap it ourselves.
                if (ImGui::Checkbox("##chk", &val)) {
                    if (enabled) { f.checkValue = val; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                bool boxHov = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
                ImGui::SameLine(0, ImGui::GetStyle().ItemInnerSpacing.x);
                float wrapX = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x;
                ImGui::PushTextWrapPos(wrapX);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopTextWrapPos();
                if (f.def->comingSoon &&
                    (boxHov || ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)))
                    ImGui::SetTooltip("Not yet supported by engine");
                break;
            }
        }
        ImGui::PopID();

        if (!enabled) ImGui::EndDisabled();
    }

    // ---- live-preview status hint -----------------------------------------
    ImGui::Separator();
    pushTextColor(pal.textDim);
    if (dirty_)            ImGui::TextUnformatted("editing… (live preview)");
    else if (firstPreviewDone_ && !targetId_.empty())
        ImGui::Text("preview: %s", targetId_.c_str());
    else                  ImGui::TextUnformatted("set parameters to preview");
    ImGui::PopStyleColor();

    ImGui::End();
}

}  // namespace ui
