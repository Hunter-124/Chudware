// Assembly UI panels (C20b) implementation — see asm_panels.h.
#include "asm_panels.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>

#include "imgui.h"

#include "icons.h"
#include "property_manager.h"   // pmParseDim / pmFormatDim (DimField parsing)

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"

namespace ui {

// ---- small theme helpers (mirror feature_tree.cpp / property_manager.cpp) ----

static void pushText(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}
static void asmIcon(const char* name, const char* fallback, float size = 18.0f) {
    IconUV iv = getIcon(name, 20);
    if (iv.valid) ImGui::Image(iv.texId, ImVec2(size, size), iv.uv0, iv.uv1);
    else ImGui::TextUnformatted(fallback);
}

// Classify a resolved selector's surface for the mate auto-suggest.
static MateSurfKind classifySurf(const nlohmann::json& sel) {
    if (!sel.is_object()) return MateSurfKind::Other;
    std::string kind = sel.value("kind", std::string());
    std::string surf = sel.value("surf", std::string());
    if (kind == "face") {
        if (surf == "plane") return MateSurfKind::Plane;
        if (surf == "cylinder" || surf == "cone") return MateSurfKind::Cylinder;
        return MateSurfKind::Other;
    }
    if (kind == "edge") {
        if (surf == "circle") return MateSurfKind::Cylinder;  // axis-bearing
        return MateSurfKind::Other;
    }
    return MateSurfKind::Other;
}

// Map a component k-id to its display name via the doc tree.
static std::string componentName(AppState& st, const std::string& cid) {
    const auto& tree = st.docMirror.tree();
    if (tree.is_object() && tree.contains("components") && tree["components"].is_array())
        for (const auto& c : tree["components"])
            if (c.is_object() && c.value("id", std::string()) == cid)
                return c.value("name", cid);
    return cid;
}

// ===========================================================================
// command dispatch
// ===========================================================================

bool AsmPanels::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "new_assembly") {
        std::string err;
        if (st.docMirror.newAssembly(st.worker, err))
            st.addLog("[asm] new assembly  rev=" + std::to_string(st.docMirror.revision()));
        else
            st.addLog("[err] new assembly: " + err);
        insertOpen_ = true; insertFocus_ = true;  // SolidWorks opens Insert Component on a new assembly
        return true;
    }
    if (cmdId == "asm_insert")  { insertOpen_ = true; insertFocus_ = true; return true; }
    if (cmdId == "asm_mate")    { beginMateForm(); return true; }
    if (cmdId == "asm_explode") { explodeOpen_ = true; explodeFocus_ = true; return true; }
    if (cmdId == "asm_interference") {
        interferenceOpen_ = true; interferenceFocus_ = true; refreshInterference(st); return true;
    }
    if (cmdId == "asm_bom") { bomOpen_ = true; bomFocus_ = true; refreshBom(st); return true; }
    if (cmdId == "asm_move") {
        // Move Component: viewport drag of a selected component = transform preview, then
        // doc_solve_mates on release. The drag itself lives in viewport_panel (not owned here);
        // this command just re-solves the current mates so a moved component re-seats.
        if (st.docMirror.hasDoc()) {
            nlohmann::json res; std::string err;
            if (st.worker.call("doc_solve_mates", {{"doc_id", st.docMirror.docId()}}, res, err)) {
                std::string aicm;
                if (res.contains("tess") && res["tess"].is_object())
                    aicm = res["tess"].value("aicm_path", std::string());
                if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
                st.addLog("[asm] move: re-solved mates");
            } else st.addLog("[err] asm_move: " + err);
        }
        return true;
    }
    if (cmdId == "asm_pattern") {
        // Component patterns are a later-card (C24) item — note it so the command stays observable.
        st.addLog("[asm] component pattern: deferred to C24");
        return true;
    }
    return false;
}

void AsmPanels::draw(AppState& st) {
    drawInsert(st);
    drawMateForm(st);
    drawExplode(st);
    drawInterference(st);
    drawBom(st);
}

// ===========================================================================
// Insert Component
// ===========================================================================

bool AsmPanels::insertComponent(AppState& st, const std::string& path,
                                const nlohmann::json& transform, std::string& aicmOut) {
    aicmOut.clear();
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json op = {{"op", "asm_insert_component"}, {"source", {{"path", path}}}};
    if (!transform.is_null()) op["transform"] = transform;
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) { st.addLog("[err] insert: " + r.error_message); return false; }
    aicmOut = r.aicm_path;
    if (!aicmOut.empty()) { std::string le; st.loadDocMesh(aicmOut, le); }
    return true;
}

void AsmPanels::drawInsert(AppState& st) {
    if (!insertOpen_) return;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(380 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (insertFocus_) { ImGui::SetNextWindowFocus(); insertFocus_ = false; }
    if (ImGui::Begin("Insert Component", &open)) {
        asmIcon("insert_component", "[+]"); ImGui::SameLine(0, 6);
        ImGui::TextUnformatted("Insert a part into the assembly");
        ImGui::Spacing();
        if (!st.docMirror.hasDoc()) {
            pushText(ui::palette.statusWarn);
            ImGui::TextWrapped("No assembly open. Use File > New Assembly first.");
            ImGui::PopStyleColor();
        } else {
            ImGui::TextUnformatted("Source file (.step+ / .step / .stl):");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##asm_insert_path", insertPath_.data(), insertPath_.size());
            const bool hasComps =
                st.docMirror.tree().is_object() &&
                st.docMirror.tree().contains("components") &&
                st.docMirror.tree()["components"].is_array() &&
                !st.docMirror.tree()["components"].empty();
            pushText(ui::palette.textDim);
            ImGui::TextUnformatted(hasComps
                ? "Inserted at the origin (drag/mate to position)."
                : "First component is fixed automatically (SolidWorks behaviour).");
            ImGui::PopStyleColor();
            ImGui::Spacing();
            if (ImGui::Button("Insert", ImVec2(90, 0)) && insertPath_[0]) {
                std::string aicm;
                if (insertComponent(st, insertPath_.data(), nullptr, aicm))
                    st.addLog("[asm] inserted: " + std::string(insertPath_.data()));
            }
            ImGui::SameLine();
            if (ImGui::Button("Close", ImVec2(90, 0))) insertOpen_ = false;
        }
    }
    ImGui::End();
    if (!open) insertOpen_ = false;
}

// ===========================================================================
// Mate PropertyManager form (C17 PM visual style)
// ===========================================================================

void AsmPanels::beginMateForm() {
    mateOpen_ = true;
    mateFocus_ = true;
    activeWell_ = 0;
    picks_[0] = MatePick{};
    picks_[1] = MatePick{};
    mateType_ = 0;
    aligned_ = true;
    std::memset(dimBuf_.data(), 0, dimBuf_.size());
    std::strncpy(dimBuf_.data(), "10", dimBuf_.size() - 1);
    mateError_.clear();
}

void AsmPanels::closeMateForm() { mateOpen_ = false; }

bool AsmPanels::routeAsmPick(const nlohmann::json& selector, const std::string& component,
                             int /*pickKind*/) {
    if (!mateOpen_) return false;
    int w = activeWell_;
    picks_[w].has = true;
    picks_[w].selector = selector;
    picks_[w].component = component;
    picks_[w].surf = classifySurf(selector);
    // Chip label "name · Face/Edge".
    std::string kind = selector.value("kind", std::string("face"));
    std::string kindLabel = kind == "face" ? "Face" : kind == "edge" ? "Edge" : "Vert";
    picks_[w].label = component + " \xC2\xB7 " + kindLabel;  // middle dot
    // Advance to the empty well (SolidWorks fills the second reference next).
    if (!picks_[0].has) activeWell_ = 0;
    else if (!picks_[1].has) activeWell_ = 1;
    else activeWell_ = (w + 1) & 1;
    return true;
}

// Auto-suggested mate types from the two picked surfaces.
// plane+plane -> coincident / parallel / distance ; cyl+cyl -> concentric.
static const char* const kPlanePlane[] = {"coincident", "parallel", "distance"};
static const char* const kCylCyl[]     = {"concentric"};
static const char* const kGeneric[]    = {"coincident", "parallel", "distance", "concentric"};

const char* AsmPanels::suggestedMate() const {
    MateSurfKind a = picks_[0].surf, b = picks_[1].surf;
    if (a == MateSurfKind::Plane && b == MateSurfKind::Plane) {
        int n = mateType_ % 3; return kPlanePlane[n];
    }
    if (a == MateSurfKind::Cylinder && b == MateSurfKind::Cylinder) {
        return kCylCyl[0];
    }
    int n = mateType_ % 4; return kGeneric[n];
}

bool AsmPanels::addMate(AppState& st, const std::string& type, const std::string& align,
                        const nlohmann::json& refs, std::string& aicmOut) {
    aicmOut.clear();
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json mate = {
        {"type", type}, {"align", align}, {"suppressed", false}, {"value", nullptr}, {"refs", refs}};
    // distance / angle / parallel-with-offset carry a value dim.
    if (type == "distance" || type == "angle") {
        double v = 0; std::string perr;
        std::string txt(dimBuf_.data());
        if (!pmParseDim(txt, v, perr)) { mateError_ = "value: " + perr; return false; }
        mate["value"] = {{"value", v}, {"expr", nullptr}};
    }
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
        {{"op", "asm_add_mate"}, {"mate", mate}}}));
    if (!r.ok) { mateError_ = r.error_message; st.addLog("[err] add_mate: " + r.error_message); return false; }
    // Solve the mates so the placed transform updates (asm_add_mate alone re-tessellates but the
    // engine already solves on rebuild; an explicit solve keeps drag/warm state consistent and
    // matches the verified flow). The apply already rebuilt+solved, so reload its aicm.
    aicmOut = r.aicm_path;
    if (!aicmOut.empty()) { std::string le; st.loadDocMesh(aicmOut, le); }
    return true;
}

void AsmPanels::drawMateForm(AppState& st) {
    if (!mateOpen_) return;
    const auto& pal = ui::palette;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(320 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (mateFocus_) { ImGui::SetNextWindowFocus(); mateFocus_ = false; }
    if (ImGui::Begin("Mate##asm", &open)) {
        // header: icon + title + Add / Cancel (PM style)
        asmIcon("mate", "[M]"); ImGui::SameLine(0, 6);
        pushText(pal.text);
        ImGui::TextUnformatted("Mate");
        ImGui::PopStyleColor();
        ImGui::Separator();

        // ---- Mate Selections (two wells) ----
        ImGui::SeparatorText("Mate Selections");
        for (int i = 0; i < 2; ++i) {
            bool active = (activeWell_ == i);
            pushText(active ? pal.accent : pal.textDim);
            ImGui::Text("Reference %d", i + 1);
            ImGui::PopStyleColor();
            ImGui::SameLine();
            // The well: a selectable that, when clicked, becomes the active pick target.
            std::string cell = picks_[i].has ? picks_[i].label : std::string("(pick a face/edge)");
            ImVec4 wellBg = active
                ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2], 0.6f)
                : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Header, wellBg);
            ImGui::PushStyleColor(ImGuiCol_HeaderHovered, wellBg);
            if (ImGui::Selectable(("##well" + std::to_string(i)).c_str(), active,
                                  ImGuiSelectableFlags_None, ImVec2(-1, 0)))
                activeWell_ = i;
            ImGui::PopStyleColor(2);
            ImGui::SameLine(0, 0);
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + 6);
            pushText(picks_[i].has ? pal.text : pal.textDim);
            ImGui::TextUnformatted(cell.c_str());
            ImGui::PopStyleColor();
            // right-click clears
            if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) { picks_[i] = MatePick{}; activeWell_ = i; }
        }

        ImGui::Spacing();
        // ---- Standard Mates (auto-suggested type buttons) ----
        ImGui::SeparatorText("Standard Mates");
        bool bothPlanes = picks_[0].surf == MateSurfKind::Plane && picks_[1].surf == MateSurfKind::Plane;
        bool bothCyl    = picks_[0].surf == MateSurfKind::Cylinder && picks_[1].surf == MateSurfKind::Cylinder;
        const char* const* opts; int nopts;
        if (bothPlanes)   { opts = kPlanePlane; nopts = 3; }
        else if (bothCyl) { opts = kCylCyl;     nopts = 1; }
        else              { opts = kGeneric;    nopts = 4; }
        if (mateType_ >= nopts) mateType_ = 0;
        for (int i = 0; i < nopts; ++i) {
            if (i) ImGui::SameLine();
            bool sel = (mateType_ == i);
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
            std::string label = opts[i];
            if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
            if (ImGui::Button((label + "##mt" + std::to_string(i)).c_str())) mateType_ = i;
            if (sel) ImGui::PopStyleColor();
        }
        std::string type = suggestedMate();

        // ---- Align flip (SolidWorks "Aligned" / "Anti-Aligned") ----
        ImGui::Spacing();
        ImGui::TextUnformatted("Mate alignment:");
        if (ImGui::RadioButton("Aligned", aligned_)) aligned_ = true;
        ImGui::SameLine();
        if (ImGui::RadioButton("Anti-Aligned", !aligned_)) aligned_ = false;
        pushText(pal.textDim);
        ImGui::TextWrapped("Seating one face flat onto another is Anti-Aligned (the normals oppose).");
        ImGui::PopStyleColor();

        // ---- distance / angle DimField ----
        if (type == "distance" || type == "angle") {
            ImGui::Spacing();
            ImGui::TextUnformatted(type == "distance" ? "Distance:" : "Angle (deg):");
            ImGui::SetNextItemWidth(120);
            ImGui::InputText("##asm_mate_dim", dimBuf_.data(), dimBuf_.size());
        }

        if (!mateError_.empty()) {
            ImGui::Spacing();
            pushText(pal.statusErr);
            ImGui::TextWrapped("%s", mateError_.c_str());
            ImGui::PopStyleColor();
        }

        ImGui::Separator();
        bool ready = picks_[0].has && picks_[1].has;
        if (!ready) ImGui::BeginDisabled();
        if (ImGui::Button("Add##asm_mate", ImVec2(90, 0))) {
            nlohmann::json refs = nlohmann::json::array();
            for (int i = 0; i < 2; ++i)
                refs.push_back({{"component", picks_[i].component}, {"selector", picks_[i].selector}});
            std::string aicm;
            if (addMate(st, type, aligned_ ? "aligned" : "anti", refs, aicm)) {
                st.addLog("[asm] mate added: " + type + " (" + (aligned_ ? "aligned" : "anti") + ")");
                // keep the form open for chaining more mates, reset selections
                picks_[0] = MatePick{}; picks_[1] = MatePick{}; activeWell_ = 0; mateError_.clear();
            }
        }
        if (!ready) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Close##asm_mate", ImVec2(90, 0))) closeMateForm();
    }
    ImGui::End();
    if (!open) closeMateForm();
}

// ===========================================================================
// Exploded View
// ===========================================================================

bool AsmPanels::setExplode(AppState& st, bool active, float factor) {
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                             {"active", active}, {"factor", factor}};
    if (!st.worker.call("doc_set_explode_state", params, res, err)) {
        st.addLog("[err] explode: " + err); return false;
    }
    if (res.is_object() && res.contains("code")) {
        st.addLog("[err] explode: " + res.value("message", std::string())); return false;
    }
    // View-only: the engine re-tessellated IN PLACE — reload the (same) aicm path it returns.
    std::string aicm;
    if (res.contains("tess") && res["tess"].is_object())
        aicm = res["tess"].value("aicm_path", std::string());
    if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    explodeActive_ = active;
    explodeFactor_ = factor;
    return true;
}

void AsmPanels::drawExplode(AppState& st) {
    if (!explodeOpen_) return;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(300 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (explodeFocus_) { ImGui::SetNextWindowFocus(); explodeFocus_ = false; }
    if (ImGui::Begin("Exploded View", &open)) {
        asmIcon("exploded_view", "[X]"); ImGui::SameLine(0, 6);
        ImGui::TextUnformatted("Explode factor");
        ImGui::Spacing();
        ImGui::SetNextItemWidth(-1);
        if (ImGui::SliderFloat("##explode_factor", &explodeFactor_, 0.0f, 1.0f, "%.2f")) {
            setExplode(st, explodeFactor_ > 0.001f, explodeFactor_);
        }
        pushText(ui::palette.textDim);
        ImGui::TextWrapped("View-only — drag to slide components apart along their explode vectors.");
        ImGui::PopStyleColor();
        ImGui::Spacing();
        if (ImGui::Button("Collapse", ImVec2(90, 0))) { explodeFactor_ = 0.0f; setExplode(st, false, 0.0f); }
        ImGui::SameLine();
        if (ImGui::Button("Exploded", ImVec2(90, 0))) { explodeFactor_ = 1.0f; setExplode(st, true, 1.0f); }
    }
    ImGui::End();
    if (!open) explodeOpen_ = false;
}

// ===========================================================================
// Interference Detection
// ===========================================================================

// C39: async — pairwise boolean intersections scale with component count and used to freeze
// the window for up to 60s. The window opens immediately showing "Computing..."; the result
// fills in when the call drains (GL thread). A re-refresh while pending supersedes (gen).
void AsmPanels::refreshInterference(AppState& st) {
    interferenceError_.clear();
    interferenceResult_ = nlohmann::json::object();
    interferencePending_ = false;
    if (!st.docMirror.hasDoc()) { interferenceError_ = "No assembly open."; return; }
    interferencePending_ = true;
    const int gen = ++interferenceGen_;
    st.rpc.call("eval_interference", {{"doc_id", st.docMirror.docId()}},
        [this, &st, gen](const core::RpcResult& r) {
            if (gen != interferenceGen_) return;
            interferencePending_ = false;
            if (!r.ok) {
                interferenceError_ = r.err.empty() ? "eval_interference failed" : r.err; return;
            }
            if (r.result.is_object() && r.result.contains("code")) {
                interferenceError_ = r.result.value("message", std::string("interference error"));
                return;
            }
            interferenceResult_ = r.result;
            // Surface a summary in the console so the result is observable even when the window
            // is behind the docked viewport (and useful for scripted shots).
            const auto& res = interferenceResult_;
            int nInter = (res.contains("interferences") && res["interferences"].is_array())
                ? (int)res["interferences"].size() : 0;
            int nClear = (res.contains("clearances") && res["clearances"].is_array())
                ? (int)res["clearances"].size() : 0;
            st.addLog("[asm] interference: " + std::to_string(nInter) + " overlap(s), " +
                      std::to_string(nClear) + " clearance(s)");
        }, 60000);
}

void AsmPanels::drawInterference(AppState& st) {
    if (!interferenceOpen_) return;
    const auto& pal = ui::palette;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(420 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (interferenceFocus_) { ImGui::SetNextWindowFocus(); interferenceFocus_ = false; }
    if (ImGui::Begin("Interference Detection", &open)) {
        asmIcon("interference", "[!]"); ImGui::SameLine(0, 6);
        ImGui::TextUnformatted("Component interference + clearance");
        ImGui::SameLine(ImGui::GetWindowWidth() - 80);
        if (ImGui::Button("Refresh")) refreshInterference(st);
        ImGui::Separator();

        if (interferencePending_) {
            pushText(pal.textDim);
            ImGui::TextUnformatted("Computing interference...");
            ImGui::PopStyleColor();
        } else if (!interferenceError_.empty()) {
            pushText(pal.statusErr);
            ImGui::TextWrapped("%s", interferenceError_.c_str());
            ImGui::PopStyleColor();
        } else {
            const auto& r = interferenceResult_;
            auto inter = r.contains("interferences") && r["interferences"].is_array()
                ? r["interferences"] : nlohmann::json::array();
            auto clear = r.contains("clearances") && r["clearances"].is_array()
                ? r["clearances"] : nlohmann::json::array();
            bool clash = !inter.empty();
            pushText(clash ? pal.statusErr : pal.statusOk);
            ImGui::TextUnformatted(clash ? "Interference detected" : "No interference");
            ImGui::PopStyleColor();
            ImGui::Spacing();
            if (ImGui::BeginTable("##inter_tbl", 3,
                ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) {
                ImGui::TableSetupColumn("Component A");
                ImGui::TableSetupColumn("Component B");
                ImGui::TableSetupColumn("Overlap / Gap");
                ImGui::TableHeadersRow();
                for (const auto& it : inter) {
                    ImGui::TableNextRow();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1.0f));
                    ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("a", std::string()).c_str());
                    ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("b", std::string()).c_str());
                    ImGui::TableNextColumn();
                    ImGui::Text("%.3f mm3 overlap", it.value("volume", 0.0));
                    ImGui::PopStyleColor();
                }
                for (const auto& it : clear) {
                    ImGui::TableNextRow();
                    ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("a", std::string()).c_str());
                    ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("b", std::string()).c_str());
                    ImGui::TableNextColumn(); ImGui::Text("%.3f mm gap", it.value("distance", 0.0));
                }
                ImGui::EndTable();
            }
            if (inter.empty() && clear.empty()) {
                pushText(pal.textDim);
                ImGui::TextWrapped("Fewer than two components, or nothing to compare.");
                ImGui::PopStyleColor();
            }
        }
    }
    ImGui::End();
    if (!open) interferenceOpen_ = false;
}

// ===========================================================================
// BOM (Bill of Materials)
// ===========================================================================

void AsmPanels::refreshBom(AppState& st) {
    bomRows_.clear();
    bomNote_.clear();
    bomPending_ = 0;
    const int gen = ++bomGen_;  // C39: invalidates eval_mass callbacks of an older refresh
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("components") || !tree["components"].is_array()) {
        bomNote_ = "No assembly components.";
        return;
    }
    // Qty rollup by source path; first occurrence holds the name + ordinal (for eval_mass).
    std::map<std::string, int> rowIndexBySource;
    std::vector<int> ordinalForRow;   // 1-based component ordinal of the first occurrence
    int ordinal = 0;
    for (const auto& c : tree["components"]) {
        if (!c.is_object()) continue;
        ++ordinal;
        std::string source = (c.contains("source") && c["source"].is_object())
            ? c["source"].value("path", std::string()) : std::string();
        std::string name = c.value("name", std::string());
        auto it = rowIndexBySource.find(source);
        if (it == rowIndexBySource.end()) {
            BomRow row; row.name = name; row.source = source; row.qty = 1;
            rowIndexBySource[source] = (int)bomRows_.size();
            ordinalForRow.push_back(ordinal);
            bomRows_.push_back(row);
        } else {
            bomRows_[it->second].qty += 1;
        }
    }
    // Mass per distinct source via eval_mass on its component ordinal. C39: the rows draw
    // immediately; each mass fills in as its async call drains (the old sync loop froze the
    // window for rows x call-time). Failures just leave haveMass=false (blank cell), as before.
    for (size_t i = 0; i < bomRows_.size(); ++i) {
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"body", ordinalForRow[i]}};
        ++bomPending_;
        st.rpc.call("eval_mass", params, [this, gen, i](const core::RpcResult& r) {
            if (gen != bomGen_) return;  // a newer refresh rebuilt the rows
            --bomPending_;
            if (r.ok && r.result.is_object() && !r.result.contains("code") &&
                i < bomRows_.size()) {
                bomRows_[i].volMm3 = r.result.value("volume_mm3", 0.0);
                bomRows_[i].massG = r.result.value("mass_g", 0.0);
                bomRows_[i].haveMass = true;
            }
        }, 60000);
    }
}

void AsmPanels::drawBom(AppState& st) {
    if (!bomOpen_) return;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(560 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (bomFocus_) { ImGui::SetNextWindowFocus(); bomFocus_ = false; }
    if (ImGui::Begin("Bill of Materials", &open)) {
        asmIcon("bom", "[B]"); ImGui::SameLine(0, 6);
        ImGui::TextUnformatted("Components");
        ImGui::SameLine(ImGui::GetWindowWidth() - 80);
        if (ImGui::Button("Refresh")) refreshBom(st);
        ImGui::Separator();

        if (!bomNote_.empty()) {
            pushText(ui::palette.textDim);
            ImGui::TextWrapped("%s", bomNote_.c_str());
            ImGui::PopStyleColor();
        }
        if (ImGui::BeginTable("##bom_tbl", 5,
            ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("#", ImGuiTableColumnFlags_WidthFixed, 28);
            ImGui::TableSetupColumn("Part");
            ImGui::TableSetupColumn("Source");
            ImGui::TableSetupColumn("Qty", ImGuiTableColumnFlags_WidthFixed, 40);
            ImGui::TableSetupColumn("Mass (g)", ImGuiTableColumnFlags_WidthFixed, 90);
            ImGui::TableHeadersRow();
            int n = 1;
            for (const auto& row : bomRows_) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::Text("%d", n++);
                ImGui::TableNextColumn(); ImGui::TextUnformatted(row.name.c_str());
                ImGui::TableNextColumn();
                // show just the file stem to keep the column readable; full path on hover
                std::string src = row.source;
                std::string stem = src;
                size_t slash = stem.find_last_of("/\\");
                if (slash != std::string::npos) stem = stem.substr(slash + 1);
                ImGui::TextUnformatted(stem.c_str());
                if (ImGui::IsItemHovered() && !src.empty()) ImGui::SetTooltip("%s", src.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", row.qty);
                ImGui::TableNextColumn();
                if (row.haveMass) ImGui::Text("%.2f", row.massG * row.qty);
                else if (bomPending_ > 0) ImGui::TextDisabled("...");  // C39: eval_mass in flight
                else ImGui::Text("%.0f mm3", row.volMm3 * row.qty);
            }
            ImGui::EndTable();
        }

        ImGui::Spacing();
        ImGui::TextUnformatted("Export CSV to:");
        ImGui::SetNextItemWidth(-90);
        ImGui::InputText("##bom_csv", bomCsvPath_.data(), bomCsvPath_.size());
        ImGui::SameLine();
        if (ImGui::Button("Export", ImVec2(80, 0)) && bomCsvPath_[0]) {
            FILE* f = std::fopen(bomCsvPath_.data(), "wb");
            if (f) {
                std::fprintf(f, "Item,Part,Source,Qty,Mass_g,Volume_mm3\n");
                int idx = 1;
                for (const auto& row : bomRows_) {
                    std::fprintf(f, "%d,%s,%s,%d,%.4f,%.4f\n", idx++, row.name.c_str(),
                                 row.source.c_str(), row.qty,
                                 row.massG * row.qty, row.volMm3 * row.qty);
                }
                std::fclose(f);
                bomNote_ = "Exported to " + std::string(bomCsvPath_.data());
                st.addLog("[asm] BOM CSV -> " + std::string(bomCsvPath_.data()));
            } else {
                bomNote_ = "Could not open CSV path for writing.";
            }
        }
    }
    ImGui::End();
    if (!open) bomOpen_ = false;
}

}  // namespace ui
