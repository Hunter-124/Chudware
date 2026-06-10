// Simulation panel implementation (C19) — see sim_panel.h.
#include "sim_panel.h"

#include <cfloat>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>

#include "imgui.h"

#include "../core/app_state.h"
#include "../core/sim_client.h"
#include "../bootstrap.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

// ---------------------------------------------------------------------- SimWell

std::string SimWell::label() const {
    if (selectors.empty()) return "(no faces)";
    return "Face x" + std::to_string(selectors.size());
}

// ---------------------------------------------------------------------- ctor

SimPanel::SimPanel() {}

void SimPanel::configureActive(float density, int materialIndex) {
    if (active_ < 0 || active_ >= (int)studies_.size()) return;
    studies_[active_].meshDensity = density;
    if (materialIndex >= 0 && materialIndex < (int)materials_.size())
        studies_[active_].materialIndex = materialIndex;
}

int SimPanel::materialIndexByKey(const std::string& key) const {
    for (int i = 0; i < (int)materials_.size(); ++i)
        if (materials_[i].key == key) return i;
    return -1;
}

// ---------------------------------------------------------------------- materials

void SimPanel::ensureMaterials(AppState& st) {
    if (materialsLoaded_) return;
    materialsLoaded_ = true;
    // engine/sim/materials.json relative to appDir() (the worker cwd).
    std::string path = appDir() + "\\engine\\sim\\materials.json";
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        st.addLog("[sim] materials.json not found at " + path);
        return;
    }
    std::stringstream ss;
    ss << f.rdbuf();
    try {
        auto j = nlohmann::json::parse(ss.str());
        for (auto it = j.begin(); it != j.end(); ++it) {
            Material m;
            m.key = it.key();
            const auto& v = it.value();
            m.name = v.value("name", it.key());
            m.E_mpa = v.value("E_mpa", 0.0);
            m.nu = v.value("nu", 0.0);
            m.density_g_cm3 = v.value("density_g_cm3", 0.0);
            m.yield_mpa = v.value("yield_mpa", 0.0);
            materials_.push_back(std::move(m));
        }
    } catch (...) {
        st.addLog("[sim] failed to parse materials.json");
    }
}

// ---------------------------------------------------------------------- commands

void SimPanel::newStudy(AppState& st, StudyType t) {
    Study s;
    s.type = t;
    const char* pre = t == StudyType::Static ? "Static" : t == StudyType::Modal ? "Modal" : "Flow";
    s.name = std::string(pre) + " " + std::to_string((int)studies_.size() + 1);
    studies_.push_back(std::move(s));
    active_ = (int)studies_.size() - 1;
    st.showSimPanel = true;
}

bool SimPanel::handleCommand(AppState& st, const std::string& cmdId) {
    ensureMaterials(st);
    if (cmdId == "sim_new_static") { newStudy(st, StudyType::Static); return true; }
    if (cmdId == "sim_new_modal")  { newStudy(st, StudyType::Modal);  return true; }
    if (cmdId == "sim_new_flow")   { newStudy(st, StudyType::Flow);   return true; }
    if (cmdId == "sim_results" || cmdId == "sim_material" || cmdId == "sim_mesh") {
        st.showSimPanel = true;
        return true;
    }
    if (active_ < 0 || active_ >= (int)studies_.size()) {
        // Most other sim commands need an active study; open the panel and hint.
        st.showSimPanel = true;
        st.addLog("[sim] create a study first (Simulation tab > New Static/Modal/Flow)");
        return true;
    }
    Study& s = studies_[active_];
    if (cmdId == "sim_fixture")       { captureSelectionInto(st, s.fixtures); return true; }
    if (cmdId == "sim_load_force")    { s.loads.push_back(SimLoad{}); captureSelectionInto(st, s.loads.back().faces); return true; }
    if (cmdId == "sim_load_pressure") { SimLoad l; l.pressure = true; s.loads.push_back(l); captureSelectionInto(st, s.loads.back().faces); return true; }
    if (cmdId == "sim_run")           { st.showSimPanel = true; runActive(st); return true; }
    return false;
}

void SimPanel::captureSelectionInto(AppState& st, SimWell& well) {
    // Capture every currently-selected face that has a resolved selector.
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty()) continue;
        try {
            auto sel = nlohmann::json::parse(it.selector);
            if (sel.value("kind", std::string()) == "face")
                well.selectors.push_back(sel);
        } catch (...) {}
    }
}

// ---------------------------------------------------------------------- run

void SimPanel::runActive(AppState& st) {
    if (active_ < 0 || active_ >= (int)studies_.size()) return;
    Study& s = studies_[active_];
    if (s.type == StudyType::Flow) runFlow(st, s);
    else runStaticOrModal(st, s);
}

nlohmann::json SimPanel::facePointsFor(AppState& st, const nlohmann::json& selector,
                                       std::string& err) {
    nlohmann::json result;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"selector", selector}};
    if (!st.worker.call("doc_face_points", params, result, err, 30000)) return {};
    if (result.contains("code")) { err = result.value("message", std::string("face_points error")); return {}; }
    return result;
}

void SimPanel::runStaticOrModal(AppState& st, Study& s) {
    if (!st.simClient) return;
    auto* sim = static_cast<core::SimClient*>(st.simClient);
    if (!st.docMirror.hasDoc()) { st.addLog("[sim] no document to analyse"); return; }
    ensureMaterials(st);

    // 1. Export the live bodies to a temp STEP.
    std::string stepPath = appdataDir() + "\\AI-CAD\\cache\\sim_study.step";
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            st.addLog("[sim] export STEP failed: " + (r.contains("code") ? r.value("message", e) : e));
            return;
        }
        lastStepPath_ = r.value("path", stepPath);
    }

    // 2. Resolve fixtures + loads to surface point clouds via doc_face_points.
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& selj : s.fixtures.selectors) {
        std::string e;
        auto fp = facePointsFor(st, selj, e);
        if (!fp.is_null() && fp.contains("points")) fixtures.push_back(fp);
    }
    if (fixtures.empty()) { st.addLog("[sim] add at least one fixture face before running"); return; }

    // 3. Material.
    nlohmann::json material = nlohmann::json::object();
    if (s.materialIndex >= 0 && s.materialIndex < (int)materials_.size()) {
        const auto& m = materials_[s.materialIndex];
        material = {{"E_mpa", m.E_mpa}, {"nu", m.nu},
                    {"density_g_cm3", m.density_g_cm3}, {"yield_mpa", m.yield_mpa}};
    }

    nlohmann::json study = {
        {"step_path", lastStepPath_},
        {"density", s.meshDensity},
        {"material", material},
        {"fixtures", fixtures},
    };

    if (s.type == StudyType::Static) {
        nlohmann::json loads = nlohmann::json::array();
        for (const auto& ld : s.loads) {
            nlohmann::json faces = nlohmann::json::array();
            for (const auto& selj : ld.faces.selectors) {
                std::string e;
                auto fp = facePointsFor(st, selj, e);
                if (!fp.is_null() && fp.contains("points")) {
                    // The static handler expects a single faceset per load; merge points.
                    if (faces.empty()) faces = fp;
                    else for (auto& pt : fp["points"]) faces["points"].push_back(pt);
                }
            }
            if (faces.empty()) continue;
            nlohmann::json l = {{"faces", faces}};
            if (ld.pressure) { l["type"] = "pressure"; l["magnitude"] = ld.pressureMpa; }
            else { l["type"] = "force"; l["vector"] = {ld.vec[0], ld.vec[1], ld.vec[2]}; }
            loads.push_back(l);
        }
        study["loads"] = loads;
        std::string note;
        if (sim->launch("sim_run_static", {{"study", study}}, note)) running_ = true;
        st.addLog(note);
    } else {  // Modal
        std::string note;
        if (sim->launch("sim_run_modal", {{"study", study}, {"n_modes", s.nModes}}, note))
            running_ = true;
        st.addLog(note);
    }
}

void SimPanel::runFlow(AppState& st, Study& s) {
    if (!st.simClient) return;
    auto* sim = static_cast<core::SimClient*>(st.simClient);
    if (!st.docMirror.hasDoc()) { st.addLog("[sim] no document to analyse"); return; }

    // C07's run_wind_tunnel loads an STL via trimesh — export the bodies to STL (bridge.export_step
    // follows the extension and writes binary STL for a .stl path).
    std::string stlPath = appdataDir() + "\\AI-CAD\\cache\\sim_flow.stl";
    nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stlPath}};
    std::string e;
    if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
        st.addLog("[sim] export STL failed: " + (r.contains("code") ? r.value("message", e) : e));
        return;
    }
    lastStepPath_ = r.value("path", stlPath);

    static const int dirs[6][3] = {{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}};
    static const char* presets[4] = {"coarse", "medium", "fine", "xfine"};
    nlohmann::json params = {
        {"stl_path", lastStepPath_},
        {"direction", {dirs[s.flowDir][0], dirs[s.flowDir][1], dirs[s.flowDir][2]}},
        {"speed_mps", s.flowSpeed},
        {"preset", presets[s.flowPreset]},
    };
    s.dragTrace.clear();
    std::string note;
    if (sim->launch("sim_run_flow", params, note)) running_ = true;
    st.addLog(note);
}

// ---------------------------------------------------------------------- tick (drain)

void SimPanel::tick(AppState& st) {
    if (!st.simClient) return;
    auto* sim = static_cast<core::SimClient*>(st.simClient);
    auto& job = sim->job();

    // Drain progress events -> status text + live drag trace.
    {
        std::vector<core::SimProgress> evs;
        {
            std::lock_guard<std::mutex> lk(job.evMtx);
            if (!job.events.empty()) evs.swap(job.events);
        }
        for (auto& ev : evs) {
            if (!ev.text.empty())
                st.status = "Sim: " + ev.text + (ev.pct ? " (" + std::to_string(ev.pct) + "%)" : "");
            st.statusKind = 0;
            if (ev.hasTrace && active_ >= 0 && active_ < (int)studies_.size())
                studies_[active_].dragTrace.push_back((float)ev.traceDrag);
        }
    }

    if (job.ready.exchange(false)) {
        running_ = false;
        std::lock_guard<std::mutex> lk(job.mtx);
        if (active_ < 0 || active_ >= (int)studies_.size()) return;
        Study& s = studies_[active_];
        if (!job.ok) {
            s.summary = "Run failed: " + job.error;
            st.status = s.summary; st.statusKind = 2;
            st.addLog("[sim] " + s.summary);
            return;
        }
        const auto& res = job.result;
        s.hasResult = true;
        if (s.type == StudyType::Flow) {
            char buf[256];
            std::snprintf(buf, sizeof(buf),
                          "Drag %.3f N | Cd %.3f | frontal %.0f mm2",
                          res.value("drag_n", 0.0), res.value("cd", 0.0),
                          res.value("frontal_area_mm2", 0.0));
            s.summary = buf;
            s.banner = res.value("reynolds_note", std::string());
            // Load streamlines overlay (LINS) if present.
            std::string lines = res.value("lines_aicm", std::string());
            if (!lines.empty()) { std::string le; st.loadDocMesh(lines, le); }
            st.addLog("[sim] flow done: " + s.summary);
        } else {
            // FEA: load overlay + colormap + legend + summary.
            std::string overlay = res.value("overlay_aicm", std::string());
            if (!overlay.empty()) {
                std::string le;
                if (st.loadDocMesh(overlay, le)) {
                    st.colormapOn = true;
                    if (st.renderer) st.renderer->setColormapMode(true);
                    st.legendOn = true;
                    st.legendTitle = (s.type == StudyType::Modal) ? "Mode amplitude" : "von Mises";
                    st.legendUnits = (s.type == StudyType::Modal) ? "" : "MPa";
                    float vmin = 0, vmax = 1;
                    if (st.renderer && st.renderer->scalarRange(vmin, vmax)) {
                        st.legendMin = vmin; st.legendMax = vmax;
                    }
                } else st.addLog("[sim] overlay load failed: " + le);
            }
            if (s.type == StudyType::Modal && res.contains("freqs_hz") && res["freqs_hz"].is_array()) {
                std::string f = "Frequencies (Hz):";
                for (auto& v : res["freqs_hz"]) {
                    char b[32]; std::snprintf(b, sizeof(b), " %.1f", v.get<double>()); f += b;
                }
                s.summary = f;
            } else if (res.contains("summary") && res["summary"].is_object()) {
                const auto& sm = res["summary"];
                char buf[256];
                std::snprintf(buf, sizeof(buf),
                              "max vM %.2f MPa | max disp %.4f mm | min FoS %.2f",
                              sm.value("max_von_mises_mpa", 0.0), sm.value("max_disp_mm", 0.0),
                              sm.value("min_fos", 0.0));
                s.summary = buf;
            }
            st.addLog("[sim] FEA done: " + s.summary);
        }
    }
}

// ---------------------------------------------------------------------- draw

static const char* kPresetLabels[4] = {
    "Coarse  (~23 s)", "Medium  (~2 min)", "Fine  (~16 min)", "Extra fine  (~73 min)"};

void SimPanel::draw(AppState& st) {
    if (!st.showSimPanel) return;
    ensureMaterials(st);
    if (!ImGui::Begin("Simulation", &st.showSimPanel)) { ImGui::End(); return; }

    // Study list (top row of New buttons + tabs).
    if (ImGui::Button("New Static")) newStudy(st, StudyType::Static);
    ImGui::SameLine(); if (ImGui::Button("New Modal")) newStudy(st, StudyType::Modal);
    ImGui::SameLine(); if (ImGui::Button("New Flow"))  newStudy(st, StudyType::Flow);

    if (studies_.empty()) {
        ImGui::Spacing();
        ImGui::TextWrapped("Create a study to set up fixtures, loads, and run an analysis.");
        ImGui::End();
        return;
    }

    // Study tabs (bottom-of-panel SW-style tab bar).
    if (ImGui::BeginTabBar("##studytabs")) {
        for (int i = 0; i < (int)studies_.size(); ++i) {
            bool open = true;
            if (ImGui::BeginTabItem((studies_[i].name + "##s" + std::to_string(i)).c_str(), &open)) {
                active_ = i;
                Study& s = studies_[i];
                ImGui::Separator();
                if (s.type == StudyType::Static)      drawStaticForm(st, s);
                else if (s.type == StudyType::Modal)  drawModalForm(st, s);
                else                                  drawFlowForm(st, s);
                ImGui::EndTabItem();
            }
            if (!open) { studies_.erase(studies_.begin() + i); if (active_ >= (int)studies_.size()) active_ = (int)studies_.size() - 1; break; }
        }
        ImGui::EndTabBar();
    }
    ImGui::End();
}

void SimPanel::drawStaticForm(AppState& st, Study& s) {
    // Material combo.
    if (!materials_.empty()) {
        const char* cur = materials_[s.materialIndex].name.c_str();
        if (ImGui::BeginCombo("Material", cur)) {
            for (int i = 0; i < (int)materials_.size(); ++i)
                if (ImGui::Selectable(materials_[i].name.c_str(), s.materialIndex == i))
                    s.materialIndex = i;
            ImGui::EndCombo();
        }
    }

    // Fixtures well.
    ImGui::SeparatorText("Fixtures");
    ImGui::Text("%s", s.fixtures.label().c_str());
    ImGui::SameLine();
    if (ImGui::SmallButton("Add selected##fix")) captureSelectionInto(st, s.fixtures);
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear##fix")) s.fixtures.selectors.clear();

    // Loads.
    ImGui::SeparatorText("Loads");
    for (int li = 0; li < (int)s.loads.size(); ++li) {
        SimLoad& ld = s.loads[li];
        ImGui::PushID(li);
        ImGui::Text("%s", ld.faces.label().c_str());
        ImGui::SameLine();
        if (ImGui::SmallButton("Add selected")) captureSelectionInto(st, ld.faces);
        ImGui::SameLine();
        if (ImGui::SmallButton("X")) { s.loads.erase(s.loads.begin() + li); ImGui::PopID(); break; }
        ImGui::Checkbox("Pressure", &ld.pressure);
        if (ld.pressure) ImGui::InputFloat("MPa", &ld.pressureMpa);
        else ImGui::InputFloat3("Force N", ld.vec);
        ImGui::PopID();
        ImGui::Separator();
    }
    if (ImGui::Button("+ Force"))    { s.loads.push_back(SimLoad{}); }
    ImGui::SameLine();
    if (ImGui::Button("+ Pressure")) { SimLoad l; l.pressure = true; s.loads.push_back(l); }

    ImGui::SeparatorText("Mesh");
    ImGui::SliderFloat("Density", &s.meshDensity, 0.1f, 1.0f, "%.2f");

    ImGui::Spacing();
    bool busy = st.simClient && static_cast<core::SimClient*>(st.simClient)->busy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Run", ImVec2(120, 0))) runStaticOrModal(st, s);
    if (busy) ImGui::EndDisabled();

    drawResultControls(st, s);
}

void SimPanel::drawModalForm(AppState& st, Study& s) {
    if (!materials_.empty()) {
        const char* cur = materials_[s.materialIndex].name.c_str();
        if (ImGui::BeginCombo("Material", cur)) {
            for (int i = 0; i < (int)materials_.size(); ++i)
                if (ImGui::Selectable(materials_[i].name.c_str(), s.materialIndex == i))
                    s.materialIndex = i;
            ImGui::EndCombo();
        }
    }
    ImGui::SeparatorText("Fixtures (optional)");
    ImGui::Text("%s", s.fixtures.label().c_str());
    ImGui::SameLine();
    if (ImGui::SmallButton("Add selected##mfix")) captureSelectionInto(st, s.fixtures);
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear##mfix")) s.fixtures.selectors.clear();

    ImGui::SliderInt("Modes", &s.nModes, 1, 12);
    ImGui::SliderFloat("Density", &s.meshDensity, 0.1f, 1.0f, "%.2f");

    bool busy = st.simClient && static_cast<core::SimClient*>(st.simClient)->busy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Run", ImVec2(120, 0))) runStaticOrModal(st, s);
    if (busy) ImGui::EndDisabled();

    if (s.hasResult) { ImGui::Separator(); ImGui::TextWrapped("%s", s.summary.c_str()); }
    drawResultControls(st, s);
}

void SimPanel::drawFlowForm(AppState& st, Study& s) {
    static const char* dirLabels[6] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};
    if (ImGui::BeginCombo("Direction", dirLabels[s.flowDir])) {
        for (int i = 0; i < 6; ++i)
            if (ImGui::Selectable(dirLabels[i], s.flowDir == i)) s.flowDir = i;
        ImGui::EndCombo();
    }
    ImGui::InputFloat("Speed m/s", &s.flowSpeed);
    if (ImGui::BeginCombo("Preset", kPresetLabels[s.flowPreset])) {
        for (int i = 0; i < 4; ++i)
            if (ImGui::Selectable(kPresetLabels[i], s.flowPreset == i)) s.flowPreset = i;
        ImGui::EndCombo();
    }
    // Honest thin-wall warning (reports.md §C07): walls thinner than ~1 voxel under-predict drag.
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.70f, 0.20f, 1));
    ImGui::TextWrapped("Note: walls thinner than the voxel size leak flow and under-predict drag. "
                       "Use a finer preset for thin features.");
    ImGui::PopStyleColor();

    bool busy = st.simClient && static_cast<core::SimClient*>(st.simClient)->busy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Run", ImVec2(120, 0))) runFlow(st, s);
    if (busy) ImGui::EndDisabled();

    // Live drag sparkline.
    if (!s.dragTrace.empty()) {
        ImGui::PlotLines("Drag trace", s.dragTrace.data(), (int)s.dragTrace.size(),
                         0, nullptr, FLT_MAX, FLT_MAX, ImVec2(0, 60));
    }

    if (s.hasResult) {
        ImGui::Separator();
        ImGui::TextWrapped("%s", s.summary.c_str());
        // Permanent qualitative-result banner.
        if (!s.banner.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.70f, 0.20f, 1));
            ImGui::TextWrapped("Qualitative result: %s", s.banner.c_str());
            ImGui::PopStyleColor();
        }
    }
}

void SimPanel::drawResultControls(AppState& st, Study& s) {
    if (!s.hasResult || s.type == StudyType::Flow) return;
    ImGui::Separator();
    ImGui::TextWrapped("%s", s.summary.c_str());

    if (ImGui::Checkbox("Colormap", &st.colormapOn)) {
        if (st.renderer) st.renderer->setColormapMode(st.colormapOn);
        st.legendOn = st.colormapOn && st.docMeshHasScalar;
    }
    if (st.docMeshHasDisp && st.renderer) {
        float scale = st.renderer->deformScale();
        // Slider in multiples of the suggested scale hint (0..2x exaggeration).
        float maxScale = st.dispScaleHint > 0 ? st.dispScaleHint * 2.0f : 2.0f;
        if (ImGui::SliderFloat("Deform scale", &scale, 0.0f, maxScale, "%.1f"))
            st.renderer->setDeformScale(scale);
        ImGui::SameLine();
        if (ImGui::SmallButton("1x")) st.renderer->setDeformScale(st.dispScaleHint);
        ImGui::SameLine();
        if (ImGui::SmallButton("0")) st.renderer->setDeformScale(0.0f);
    }
}

}  // namespace ui
