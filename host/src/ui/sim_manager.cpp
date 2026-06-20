// SimManager implementation — see sim_manager.h.
#include "sim_manager.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>

#include "imgui.h"

#include "metrics.h"
#include "icons.h"
#include "property_manager.h"   // PropertyManager::active() — mutual-exclusion pick guard
#include "../core/app_state.h"
#include "../core/sim_client.h"
#include "../bootstrap.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

// ---------------------------------------------------------------- small helpers

// A hover tooltip on the previous item (SW options are ambiguous — every field gets one).
static void simTip(const char* tip) {
    if (tip && *tip && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("%s", tip);
}

static const char* studyTypeKey(StudyType t) {
    return t == StudyType::Static ? "static" : t == StudyType::Modal ? "modal" : "flow";
}
static StudyType studyTypeFromKey(const std::string& k) {
    if (k == "modal") return StudyType::Modal;
    if (k == "flow")  return StudyType::Flow;
    return StudyType::Static;
}
static const char* studyTypeLabel(StudyType t) {
    return t == StudyType::Static ? "Static" : t == StudyType::Modal ? "Frequency" : "Flow";
}

std::string SimWell::label() const {
    if (picks.empty()) return "(empty)";
    return std::to_string(picks.size()) + (picks.size() == 1 ? " face" : " faces");
}

// ---------------------------------------------------------------- materials (singleton)

const std::vector<SimManager::Material>& SimManager::materials() {
    static std::vector<Material> mats;
    static bool loaded = false;
    if (!loaded) {
        loaded = true;
        std::string path = appDir() + "\\engine\\sim\\materials.json";
        std::ifstream f(path, std::ios::binary);
        if (f) {
            std::stringstream ss; ss << f.rdbuf();
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
                    mats.push_back(std::move(m));
                }
            } catch (...) {}
        }
    }
    return mats;
}

int SimManager::materialIndexByKey(const std::string& key) {
    const auto& mats = materials();
    for (int i = 0; i < (int)mats.size(); ++i) if (mats[i].key == key) return i;
    return -1;
}

// ---------------------------------------------------------------- ctor

SimManager::SimManager() {}

// ---------------------------------------------------------------- JSON <-> Study

nlohmann::json SimManager::studyToJson(const Study& s) {
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) fixtures.push_back(p.selector);
    nlohmann::json loads = nlohmann::json::array();
    for (const auto& ld : s.loads) {
        nlohmann::json faces = nlohmann::json::array();
        for (const auto& p : ld.faces.picks) faces.push_back(p.selector);
        nlohmann::json l = {{"faces", faces}};
        if (ld.pressure) { l["type"] = "pressure"; l["magnitude"] = ld.pressureMpa; }
        else { l["type"] = "force"; l["vec"] = {ld.vec[0], ld.vec[1], ld.vec[2]}; }
        loads.push_back(l);
    }
    return nlohmann::json{
        {"id", s.id},
        {"type", studyTypeKey(s.type)},
        {"name", s.name},
        {"material", s.materialKey},
        {"fixtures", fixtures},
        {"loads", loads},
        {"mesh_density", s.meshDensity},
        {"n_modes", s.nModes},
        {"flow", {{"dir", s.flowDir}, {"speed", s.flowSpeed}, {"preset", s.flowPreset}}},
        {"result_summary", s.summary},
    };
}

// Read one face selector back into a SimSel, recovering the viewport identity stashed under "_pick"
// at capture time so a reopened study's faces still highlight.
static SimSel selFromJson(const nlohmann::json& selector) {
    SimSel ss;
    ss.selector = selector;
    if (selector.is_object()) {
        auto it = selector.find("_pick");
        if (it != selector.end() && it->is_object()) {
            ss.pickBody = (uint32_t)it->value("body", 0);
            ss.pickTopo = (uint32_t)it->value("topo", 0);
        }
    }
    return ss;
}

Study SimManager::studyFromJson(const nlohmann::json& j) {
    Study s;
    s.id = j.value("id", std::string());
    s.type = studyTypeFromKey(j.value("type", std::string("static")));
    s.name = j.value("name", std::string("Study"));
    s.materialKey = j.value("material", std::string());
    s.meshDensity = j.value("mesh_density", 0.5);
    s.nModes = j.value("n_modes", 6);
    if (j.contains("flow") && j["flow"].is_object()) {
        const auto& fl = j["flow"];
        s.flowDir = fl.value("dir", 0);
        s.flowSpeed = fl.value("speed", 10.0);
        s.flowPreset = fl.value("preset", 0);
    }
    s.summary = j.value("result_summary", std::string());
    if (j.contains("fixtures") && j["fixtures"].is_array())
        for (const auto& sel : j["fixtures"]) s.fixtures.picks.push_back(selFromJson(sel));
    if (j.contains("loads") && j["loads"].is_array())
        for (const auto& ld : j["loads"]) {
            SimLoad l;
            l.pressure = ld.value("type", std::string("force")) == "pressure";
            if (l.pressure) l.pressureMpa = ld.value("magnitude", 0.1);
            else if (ld.contains("vec") && ld["vec"].is_array() && ld["vec"].size() == 3)
                for (int k = 0; k < 3; ++k) l.vec[k] = ld["vec"][k].get<float>();
            if (ld.contains("faces") && ld["faces"].is_array())
                for (const auto& sel : ld["faces"]) l.faces.picks.push_back(selFromJson(sel));
            s.loads.push_back(std::move(l));
        }
    return s;
}

// ---------------------------------------------------------------- refresh / mirror

void SimManager::refresh(AppState& st) {
    if (!st.docMirror.hasDoc()) { studies_.clear(); sel_ = -1; return; }
    nlohmann::json res; std::string err;
    if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res, err, 15000)) {
        st.addLog("[sim] sim_study_list failed: " + err);
        return;
    }
    if (res.contains("code")) return;
    // Preserve the transient (non-persisted) result fields across a refresh by id.
    std::vector<Study> prev = std::move(studies_);
    studies_.clear();
    if (res.contains("studies") && res["studies"].is_array())
        for (const auto& j : res["studies"]) {
            Study s = studyFromJson(j);
            for (const auto& p : prev)
                if (p.id == s.id) { s.hasResult = p.hasResult; s.banner = p.banner;
                                    s.dragTrace = p.dragTrace;
                                    if (!p.summary.empty()) s.summary = p.summary; break; }
            studies_.push_back(std::move(s));
        }
    lastDocId_ = st.docMirror.docId();
    lastRevision_ = (long)st.docMirror.revision();
    if (sel_ >= (int)studies_.size()) sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
}

void SimManager::persist(AppState& st, Study& s) {
    if (s.id.empty() || !st.docMirror.hasDoc()) return;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                             {"study_id", s.id}, {"study", studyToJson(s)}};
    if (!st.worker.call("sim_study_edit", params, res, err, 15000) || res.contains("code")) {
        st.addLog("[sim] sim_study_edit failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    st.markActiveTabDirty();
}

void SimManager::newStudy(AppState& st, StudyType t) {
    if (!st.docMirror.hasDoc()) {
        st.toast(ui::ToastLevel::Warn, "Open a part before creating a study");
        return;
    }
    refresh(st);
    Study s;
    s.type = t;
    s.name = std::string(studyTypeLabel(t)) + " " + std::to_string((int)studies_.size() + 1);
    if (!materials().empty()) s.materialKey = materials()[0].key;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"study", studyToJson(s)}};
    if (!st.worker.call("sim_study_add", params, res, err, 15000) || res.contains("code")) {
        st.addLog("[sim] sim_study_add failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    st.markActiveTabDirty();
    refresh(st);
    // Select the newly added study (matched by id).
    std::string newId = res.value("study_id", std::string());
    sel_ = -1;
    for (int i = 0; i < (int)studies_.size(); ++i) if (studies_[i].id == newId) { sel_ = i; break; }
    if (sel_ < 0 && !studies_.empty()) sel_ = (int)studies_.size() - 1;
    node_ = SimNode::Study;
    armedWell_ = -1;
    st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::deleteStudy(AppState& st, int i) {
    if (i < 0 || i >= (int)studies_.size() || !st.docMirror.hasDoc()) return;
    std::string id = studies_[i].id;
    nlohmann::json res; std::string err;
    if (!st.worker.call("sim_study_delete", {{"doc_id", st.docMirror.docId()}, {"study_id", id}},
                        res, err, 15000) || res.contains("code")) {
        st.addLog("[sim] sim_study_delete failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    st.markActiveTabDirty();
    clearResultOverlay(st);
    armedWell_ = -1;
    refresh(st);
    if (sel_ >= (int)studies_.size()) sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
    node_ = SimNode::Study;
}

// ---------------------------------------------------------------- ribbon commands

bool SimManager::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "sim_new_static") { newStudy(st, StudyType::Static); return true; }
    if (cmdId == "sim_new_modal")  { newStudy(st, StudyType::Modal);  return true; }
    if (cmdId == "sim_new_flow")   { newStudy(st, StudyType::Flow);   return true; }
    if (cmdId == "sim_results" || cmdId == "sim_material" || cmdId == "sim_mesh") {
        st.leftPaneTab = AppState::LP_Simulation;
        return true;
    }
    refresh(st);
    if (sel_ < 0 || sel_ >= (int)studies_.size()) {
        st.leftPaneTab = AppState::LP_Simulation;
        st.addLog("[sim] create a study first (Simulation tab > New Static/Frequency/Flow)");
        return true;
    }
    Study& s = studies_[sel_];
    if (cmdId == "sim_fixture") {
        captureSelectionInto(st, s.fixtures); persist(st, s);
        node_ = SimNode::Fixtures; armedWell_ = 0; st.leftPaneTab = AppState::LP_Simulation; return true;
    }
    if (cmdId == "sim_load_force") {
        s.loads.push_back(SimLoad{}); captureSelectionInto(st, s.loads.back().faces); persist(st, s);
        node_ = SimNode::Load; loadIdx_ = (int)s.loads.size() - 1; armedWell_ = (int)s.loads.size();
        st.leftPaneTab = AppState::LP_Simulation; return true;
    }
    if (cmdId == "sim_load_pressure") {
        SimLoad l; l.pressure = true; s.loads.push_back(l);
        captureSelectionInto(st, s.loads.back().faces); persist(st, s);
        node_ = SimNode::Load; loadIdx_ = (int)s.loads.size() - 1; armedWell_ = (int)s.loads.size();
        st.leftPaneTab = AppState::LP_Simulation; return true;
    }
    if (cmdId == "sim_run") { st.leftPaneTab = AppState::LP_Simulation; run(st, s); return true; }
    return false;
}

void SimManager::configureActive(AppState& st, float density, const std::string& materialKey) {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    studies_[sel_].meshDensity = density;
    if (!materialKey.empty()) studies_[sel_].materialKey = materialKey;
    persist(st, studies_[sel_]);
}

// ---------------------------------------------------------------- pick capture

void SimManager::captureSelectionInto(AppState& st, SimWell& well) {
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty()) continue;
        try {
            auto sel = nlohmann::json::parse(it.selector);
            if (sel.value("kind", std::string()) != "face") continue;
            // Stash the viewport identity so the face stays highlightable after save/reopen.
            sel["_pick"] = {{"body", it.hit.body_id}, {"topo", it.hit.topo_id}};
            SimSel ss; ss.selector = sel; ss.pickBody = it.hit.body_id; ss.pickTopo = it.hit.topo_id;
            // De-dup by (body,topo).
            bool dup = false;
            for (const auto& p : well.picks)
                if (p.pickBody == ss.pickBody && p.pickTopo == ss.pickTopo) { dup = true; break; }
            if (!dup) well.picks.push_back(std::move(ss));
        } catch (...) {}
    }
}

// ---------------------------------------------------------------- viewport pick mux

bool SimManager::wantsViewportFacePick(AppState& st) const {
    if (st.leftPaneTab != AppState::LP_Simulation) return false;
    if (st.propertyManager && st.propertyManager->active()) return false;  // PM owns picks first
    if (armedWell_ < 0) return false;
    return sel_ >= 0 && sel_ < (int)studies_.size();
}

bool SimManager::routeFacePick(AppState& st, const nlohmann::json& selector, uint32_t body, uint32_t topo) {
    if (!wantsViewportFacePick(st)) return false;
    if (selector.value("kind", std::string()) != "face") return false;
    Study& s = studies_[sel_];
    nlohmann::json sel = selector;
    sel["_pick"] = {{"body", body}, {"topo", topo}};
    SimSel ss; ss.selector = sel; ss.pickBody = body; ss.pickTopo = topo;
    SimWell* well = nullptr;
    if (armedWell_ == 0) well = &s.fixtures;
    else if (armedWell_ - 1 < (int)s.loads.size()) well = &s.loads[armedWell_ - 1].faces;
    if (!well) return false;
    for (const auto& p : well->picks)
        if (p.pickBody == body && p.pickTopo == topo) return true;  // already present (idempotent)
    well->picks.push_back(std::move(ss));
    persist(st, s);
    return true;
}

// ---------------------------------------------------------------- previews

bool SimManager::previewActive(AppState& st) const {
    if (st.leftPaneTab != AppState::LP_Simulation) return false;
    if (st.colormapOn) return false;  // don't stack tints over a result colormap
    return sel_ >= 0 && sel_ < (int)studies_.size();
}

void SimManager::collectFixtureFaces(std::vector<uint64_t>& keys) const {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    for (const auto& p : studies_[sel_].fixtures.picks)
        if (p.pickBody && p.pickTopo)
            keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

void SimManager::collectLoadFaces(std::vector<uint64_t>& keys) const {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    for (const auto& ld : studies_[sel_].loads)
        for (const auto& p : ld.faces.picks)
            if (p.pickBody && p.pickTopo)
                keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

void SimManager::collectLoadArrows(AppState& st, std::vector<LoadArrow>& arrows) const {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    (void)st;
    for (const auto& ld : studies_[sel_].loads) {
        // Anchor at the average of the load faces' selector centroids.
        float c[3] = {0, 0, 0}; int n = 0;
        for (const auto& p : ld.faces.picks) {
            if (!p.selector.is_object()) continue;
            auto it = p.selector.find("centroid");
            if (it != p.selector.end() && it->is_array() && it->size() == 3) {
                for (int k = 0; k < 3; ++k) c[k] += (*it)[k].get<float>();
                ++n;
            }
        }
        if (!n) continue;
        LoadArrow a;
        for (int k = 0; k < 3; ++k) a.at[k] = c[k] / n;
        a.pressure = ld.pressure;
        char buf[48];
        if (ld.pressure) {
            // Pressure: arrow points into the face (use the selector normal if present, else -Z).
            float nrm[3] = {0, 0, -1};
            if (!ld.faces.picks.empty() && ld.faces.picks[0].selector.is_object()) {
                auto it = ld.faces.picks[0].selector.find("normal");
                if (it != ld.faces.picks[0].selector.end() && it->is_array() && it->size() == 3)
                    for (int k = 0; k < 3; ++k) nrm[k] = -(*it)[k].get<float>();
            }
            for (int k = 0; k < 3; ++k) a.dir[k] = nrm[k];
            std::snprintf(buf, sizeof(buf), "%.3g MPa", ld.pressureMpa);
        } else {
            for (int k = 0; k < 3; ++k) a.dir[k] = ld.vec[k];
            std::snprintf(buf, sizeof(buf), "%.0f,%.0f,%.0f N", ld.vec[0], ld.vec[1], ld.vec[2]);
        }
        a.mag = buf;
        arrows.push_back(std::move(a));
    }
}

// ---------------------------------------------------------------- run (ported from SimPanel)

nlohmann::json SimManager::facePointsFor(AppState& st, const nlohmann::json& selector, std::string& err) {
    nlohmann::json result;
    // Strip the host-only "_pick" hint before sending to the engine.
    nlohmann::json sel = selector;
    if (sel.is_object()) sel.erase("_pick");
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"selector", sel}};
    if (!st.worker.call("doc_face_points", params, result, err, 30000)) return {};
    if (result.contains("code")) { err = result.value("message", std::string("face_points error")); return {}; }
    return result;
}

void SimManager::run(AppState& st, Study& s) {
    if (s.type == StudyType::Flow) runFlow(st, s);
    else runStaticOrModal(st, s);
}

void SimManager::runStaticOrModal(AppState& st, Study& s) {
    if (!st.simClient) return;
    auto* sim = st.simClient;
    if (!st.docMirror.hasDoc()) { st.addLog("[sim] no document to analyse"); return; }

    std::string stepPath = appdataDir() + "\\AI-CAD\\cache\\sim_study.step";
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            st.addLog("[sim] export STEP failed: " + (r.contains("code") ? r.value("message", e) : e));
            st.toast(ui::ToastLevel::Error, "Simulation: export failed");
            return;
        }
        lastStepPath_ = r.value("path", stepPath);
    }

    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) {
        std::string e;
        auto fp = facePointsFor(st, p.selector, e);
        if (!fp.is_null() && fp.contains("points")) fixtures.push_back(fp);
    }
    if (fixtures.empty()) {
        st.addLog("[sim] add at least one fixture face before running");
        st.toast(ui::ToastLevel::Warn, "Add a fixture before running");
        return;
    }

    nlohmann::json material = nlohmann::json::object();
    int mi = materialIndexByKey(s.materialKey);
    if (mi >= 0) {
        const auto& m = materials()[mi];
        material = {{"E_mpa", m.E_mpa}, {"nu", m.nu},
                    {"density_g_cm3", m.density_g_cm3}, {"yield_mpa", m.yield_mpa}};
    }

    nlohmann::json study = {
        {"step_path", lastStepPath_}, {"density", s.meshDensity},
        {"material", material}, {"fixtures", fixtures},
    };

    std::string note;
    if (s.type == StudyType::Static) {
        nlohmann::json loads = nlohmann::json::array();
        for (const auto& ld : s.loads) {
            nlohmann::json faces = nlohmann::json::array();
            for (const auto& p : ld.faces.picks) {
                std::string e;
                auto fp = facePointsFor(st, p.selector, e);
                if (!fp.is_null() && fp.contains("points")) {
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
        if (sim->launch("sim_run_static", {{"study", study}}, note, st.docMirror.docId())) {
            running_ = true; runningStudy_ = sel_;
        }
    } else {
        if (sim->launch("sim_run_modal", {{"study", study}, {"n_modes", s.nModes}}, note,
                        st.docMirror.docId())) {
            running_ = true; runningStudy_ = sel_;
        }
    }
    st.addLog(note);
}

void SimManager::runFlow(AppState& st, Study& s) {
    if (!st.simClient) return;
    auto* sim = st.simClient;
    if (!st.docMirror.hasDoc()) { st.addLog("[sim] no document to analyse"); return; }
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
        {"speed_mps", s.flowSpeed}, {"preset", presets[s.flowPreset]},
    };
    s.dragTrace.clear();
    std::string note;
    if (sim->launch("sim_run_flow", params, note, st.docMirror.docId())) {
        running_ = true; runningStudy_ = sel_;
    }
    st.addLog(note);
}

// ---------------------------------------------------------------- tick (drain, owner-gated)

void SimManager::clearResultOverlay(AppState& st) {
    st.colormapOn = false;
    st.legendOn = false;
    st.docMeshHasScalar = false;
    st.docMeshHasDisp = false;
    if (st.renderer) { st.renderer->setColormapMode(false); st.renderer->setDeformScale(0.0f); }
}

void SimManager::tick(AppState& st) {
    if (!st.simClient) return;
    auto* sim = st.simClient;
    auto& job = sim->job();

    // Only consume events/completion for jobs THIS document launched.
    const bool owns = (job.ownerDocId.empty() && running_) ||
                      (st.docMirror.hasDoc() && job.ownerDocId == st.docMirror.docId());
    if (!owns) return;

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
            if (ev.hasTrace && runningStudy_ >= 0 && runningStudy_ < (int)studies_.size())
                studies_[runningStudy_].dragTrace.push_back((float)ev.traceDrag);
        }
    }

    if (job.ready.exchange(false)) {
        running_ = false;
        std::lock_guard<std::mutex> lk(job.mtx);
        int si = runningStudy_;
        if (si < 0 || si >= (int)studies_.size()) return;
        Study& s = studies_[si];
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
            std::snprintf(buf, sizeof(buf), "Drag %.3f N | Cd %.3f | frontal %.0f mm2",
                          res.value("drag_n", 0.0), res.value("cd", 0.0),
                          res.value("frontal_area_mm2", 0.0));
            s.summary = buf;
            s.banner = res.value("reynolds_note", std::string());
            std::string lines = res.value("lines_aicm", std::string());
            if (!lines.empty()) { std::string le; st.loadDocMesh(lines, le); }
            st.addLog("[sim] flow done: " + s.summary);
        } else {
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
        // Cache the summary into the document so it reopens with the last-run headline.
        persist(st, s);
        node_ = SimNode::Results;
    }
}

// ============================================================ tree + form (drawTreeBody)

static const char* kPresetLabels[4] = {
    "Coarse  (~23 s)", "Medium  (~2 min)", "Fine  (~16 min)", "Extra fine  (~73 min)"};
static const char* kDirLabels[6] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};

// A small status dot (teal ok / amber attention). Mirrors the feature tree's badge vocabulary.
static void simBadge(bool ok, const char* tip) {
    const auto& pal = ui::palette;
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, ok
        ? ImVec4(pal.statusOk[0], pal.statusOk[1], pal.statusOk[2], pal.statusOk[3])
        : ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], pal.statusWarn[3]));
    ImGui::TextUnformatted(ok ? u8"•" : "?");
    ImGui::PopStyleColor();
    if (tip && *tip) simTip(tip);
}

void SimManager::drawTreeBody(AppState& st) {
    // Keep the mirror in step with the active document (cheap: re-pull only on a doc/revision change).
    if (st.docMirror.hasDoc() &&
        (lastDocId_ != st.docMirror.docId() || lastRevision_ != (long)st.docMirror.revision()))
        refresh(st);

    const float S = ui::uiScale;

    // ---- New-study buttons (wrap so they never clip in the narrow pane) ----
    const float availW = ImGui::GetContentRegionAvail().x;
    const float bw = (availW - 8.0f * S) / 3.0f;
    if (ImGui::Button("Static##new", ImVec2(bw, 0))) newStudy(st, StudyType::Static);
    simTip("Linear static structural study: apply fixtures + loads, solve for stress, displacement, "
           "and factor of safety.");
    ImGui::SameLine();
    if (ImGui::Button("Frequency##new", ImVec2(bw, 0))) newStudy(st, StudyType::Modal);
    simTip("Modal (frequency) study: solve the natural frequencies and mode shapes. Fixtures optional.");
    ImGui::SameLine();
    if (ImGui::Button("Flow##new", ImVec2(bw, 0))) newStudy(st, StudyType::Flow);
    simTip("External flow (wind tunnel): estimate drag/lift and streamlines for a flow direction.");

    ImGui::Spacing();

    if (!st.docMirror.hasDoc()) {
        ImGui::TextWrapped("Open a part to set up a simulation study.");
        return;
    }
    if (studies_.empty()) {
        ImGui::Separator();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                                    ui::palette.textDim[2], 1));
        ImGui::TextWrapped("No studies yet. Create a study above to add fixtures, loads, and run an "
                           "analysis. Studies are saved inside this part file.");
        ImGui::PopStyleColor();
        return;
    }

    ImGui::Separator();

    // ---- study tree ----
    for (int i = 0; i < (int)studies_.size(); ++i) drawStudyRow(st, i);

    // ---- inline form for the selected study/sub-node ----
    if (sel_ >= 0 && sel_ < (int)studies_.size()) {
        ImGui::Spacing();
        ImGui::Separator();
        ImGui::Spacing();
        drawForm(st, studies_[sel_]);
    }
}

void SimManager::drawStudyRow(AppState& st, int i) {
    Study& s = studies_[i];
    ImGui::PushID(i);
    const bool selStudy = (sel_ == i);

    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth |
                               ImGuiTreeNodeFlags_DefaultOpen;
    if (selStudy && node_ == SimNode::Study) flags |= ImGuiTreeNodeFlags_Selected;

    std::string title = s.name + "  (" + studyTypeLabel(s.type) + ")";
    bool open = ImGui::TreeNodeEx("##study", flags, "%s", title.c_str());
    if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
        sel_ = i; node_ = SimNode::Study; armedWell_ = -1;
    }
    // Right-click: delete (deferred so we never mutate studies_ mid-row-draw).
    bool wantDelete = false;
    if (ImGui::BeginPopupContextItem("##studyctx")) {
        if (ImGui::MenuItem("Delete study")) wantDelete = true;
        ImGui::EndPopup();
    }

    if (open) {
        const float S = ui::uiScale;
        // One leaf row: a 16px atlas icon (feature-manager vocabulary) + a clickable label + badge.
        auto child = [&](const char* icon, const std::string& label, SimNode n, bool ok,
                         const char* tip, int loadI = -1) {
            IconUV iv = getIcon(icon, 20);
            if (iv.valid) {
                ImGui::Image(iv.texId, ImVec2(15.0f * S, 15.0f * S), iv.uv0, iv.uv1);
                ImGui::SameLine(0, 5.0f * S);
            }
            const bool selected = selStudy && node_ == n && (n != SimNode::Load || loadIdx_ == loadI);
            if (ImGui::Selectable(label.c_str(), selected)) {
                sel_ = i; node_ = n; loadIdx_ = loadI;
                armedWell_ = (n == SimNode::Fixtures) ? 0 : (n == SimNode::Load ? loadI + 1 : -1);
            }
            simBadge(ok, tip);
        };

        const std::string matName = [&]{
            int mi = materialIndexByKey(s.materialKey);
            return mi >= 0 ? materials()[mi].name : std::string("(not set)");
        }();
        child("material", "Material: " + matName, SimNode::Material, !s.materialKey.empty(),
              s.materialKey.empty() ? "No material assigned — pick one before running." : "");

        if (s.type != StudyType::Flow) {
            bool hasFix = !s.fixtures.empty();
            child("fixture", "Fixtures (" + s.fixtures.label() + ")", SimNode::Fixtures, hasFix,
                  hasFix ? "" : (s.type == StudyType::Modal
                      ? "No fixtures — frequency study will run free-free."
                      : "A static study needs at least one fixed face."));
            // Loads (each its own row).
            for (int li = 0; li < (int)s.loads.size(); ++li) {
                const SimLoad& ld = s.loads[li];
                std::string lbl = (ld.pressure ? "Pressure " : "Force ") + std::to_string(li + 1) +
                                  " (" + ld.faces.label() + ")";
                child(ld.pressure ? "load_pressure" : "load_force", lbl, SimNode::Load,
                      !ld.faces.empty(),
                      ld.faces.empty() ? "This load has no faces selected." : "", li);
            }
            child("mesh", "Mesh", SimNode::Mesh, true, "Mesh density for the solve.");
        }
        child("results", s.hasResult ? "Results" : "Results (not run)", SimNode::Results,
              s.hasResult, s.hasResult ? "" : "Run the study to generate results.");
        ImGui::TreePop();
    }
    ImGui::PopID();
    if (wantDelete) deleteStudy(st, i);
}

void SimManager::drawMaterialCombo(AppState& st, Study& s) {
    const auto& mats = materials();
    int mi = materialIndexByKey(s.materialKey);
    const char* cur = mi >= 0 ? mats[mi].name.c_str() : "(select material)";
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##material", cur)) {
        for (int i = 0; i < (int)mats.size(); ++i)
            if (ImGui::Selectable(mats[i].name.c_str(), mi == i)) {
                s.materialKey = mats[i].key; persist(st, s);
            }
        ImGui::EndCombo();
    }
    simTip("Material defines stiffness (E), Poisson ratio, density and yield strength used for the "
           "stress / factor-of-safety solve.");
}

void SimManager::drawWellRow(AppState& st, const char* label, SimWell& well, int armId, const char* tip) {
    ImGui::PushID(armId);
    const bool armed = (armedWell_ == armId);
    ImGui::TextUnformatted(label); simTip(tip);
    // Active selection box: armed = accent border + "Selecting…" hint.
    ImGui::PushStyleColor(ImGuiCol_Button, armed
        ? ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 0.85f)
        : ImGui::GetStyleColorVec4(ImGuiCol_Button));
    if (ImGui::Button(armed ? "Selecting faces… (click viewport)" : "Add faces",
                      ImVec2(-FLT_MIN, 0)))
        armedWell_ = armed ? -1 : armId;
    ImGui::PopStyleColor();
    simTip(armed ? "Click faces in the viewport to add them. Click here again to stop."
                 : "Arm this field, then click model faces in the viewport.");
    // Chips: each picked face, right-click to remove.
    if (!well.picks.empty()) {
        for (int k = 0; k < (int)well.picks.size(); ++k) {
            ImGui::PushID(k);
            ImGui::SmallButton(("Face " + std::to_string(k + 1)).c_str());
            if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
                well.picks.erase(well.picks.begin() + k);
                persist(st, studies_[sel_]);
                ImGui::PopID();
                break;
            }
            simTip("Right-click to remove this face.");
            if (k % 4 != 3 && k + 1 < (int)well.picks.size()) ImGui::SameLine();
            ImGui::PopID();
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Clear")) { well.picks.clear(); persist(st, studies_[sel_]); }
        simTip("Remove all faces from this field.");
    }
    ImGui::PopID();
}

void SimManager::drawFixturesForm(AppState& st, Study& s) {
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted(s.type == StudyType::Modal ? "Fixtures (optional)" : "Fixtures");
    ImGui::PopFont();
    ImGui::TextWrapped("Faces that are clamped (zero displacement).");
    ImGui::Spacing();
    drawWellRow(st, "Fixed faces", s.fixtures, 0,
                "Faces held rigidly in place. The reaction to your loads is carried here.");
}

void SimManager::drawLoadsForm(AppState& st, Study& s) {
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Loads");
    ImGui::PopFont();
    const float S = ui::uiScale;
    for (int li = 0; li < (int)s.loads.size(); ++li) {
        SimLoad& ld = s.loads[li];
        ImGui::PushID(li);
        ImGui::Separator();
        std::string hdr = (ld.pressure ? "Pressure " : "Force ") + std::to_string(li + 1);
        ImGui::TextUnformatted(hdr.c_str());
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 22.0f * S);
        if (ImGui::SmallButton("X")) { s.loads.erase(s.loads.begin() + li); persist(st, s);
                                       ImGui::PopID(); break; }
        simTip("Remove this load.");
        drawWellRow(st, "Faces", ld.faces, li + 1, "Faces the load is applied to.");
        if (ImGui::Checkbox("Pressure", &ld.pressure)) persist(st, s);
        simTip("On: a uniform pressure (MPa) normal to the faces. Off: a total force vector (N) "
               "distributed over the faces.");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ld.pressure) {
            if (ImGui::InputFloat("MPa##p", &ld.pressureMpa, 0, 0, "%.4f")) {}
            if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            simTip("Pressure magnitude in megapascals (N/mm^2), pushing into the faces.");
        } else {
            if (ImGui::InputFloat3("N (x,y,z)##f", ld.vec, "%.1f")) {}
            if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            simTip("Total force in newtons along world X/Y/Z, spread evenly over the selected faces.");
        }
        ImGui::PopID();
    }
    ImGui::Spacing();
    if (ImGui::Button("+ Force")) { s.loads.push_back(SimLoad{}); persist(st, s);
                                    node_ = SimNode::Load; loadIdx_ = (int)s.loads.size() - 1;
                                    armedWell_ = (int)s.loads.size(); }
    simTip("Add a force load (newtons) on a set of faces.");
    ImGui::SameLine();
    if (ImGui::Button("+ Pressure")) { SimLoad l; l.pressure = true; s.loads.push_back(l);
                                       persist(st, s); node_ = SimNode::Load;
                                       loadIdx_ = (int)s.loads.size() - 1;
                                       armedWell_ = (int)s.loads.size(); }
    simTip("Add a pressure load (MPa) on a set of faces.");
}

void SimManager::drawResultControls(AppState& st, Study& s) {
    if (!s.hasResult) {
        ImGui::TextWrapped("No results yet. Set up the study and press Run.");
        return;
    }
    ImGui::TextWrapped("%s", s.summary.c_str());
    if (!s.banner.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.70f, 0.20f, 1));
        ImGui::TextWrapped("Qualitative result: %s", s.banner.c_str());
        ImGui::PopStyleColor();
    }
    if (s.type == StudyType::Flow) return;
    ImGui::Spacing();
    if (ImGui::Checkbox("Show colormap", &st.colormapOn)) {
        if (st.renderer) st.renderer->setColormapMode(st.colormapOn);
        st.legendOn = st.colormapOn && st.docMeshHasScalar;
    }
    simTip("Color the model by the result field (von Mises stress / mode amplitude).");
    if (st.docMeshHasDisp && st.renderer) {
        float scale = st.renderer->deformScale();
        float maxScale = st.dispScaleHint > 0 ? st.dispScaleHint * 2.0f : 2.0f;
        ImGui::SetNextItemWidth(-60.0f * ui::uiScale);
        if (ImGui::SliderFloat("Deform", &scale, 0.0f, maxScale, "%.1f"))
            st.renderer->setDeformScale(scale);
        simTip("Exaggerate the deformed shape. 1x is the true scale; higher values amplify it for "
               "visualization only.");
        ImGui::SameLine();
        if (ImGui::SmallButton("1x")) st.renderer->setDeformScale(st.dispScaleHint);
        ImGui::SameLine();
        if (ImGui::SmallButton("0")) st.renderer->setDeformScale(0.0f);
    }
}

void SimManager::drawForm(AppState& st, Study& s) {
    const float S = ui::uiScale;

    // Header: study name (editable).
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted(s.name.c_str());
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextDisabled("(%s)", studyTypeLabel(s.type));

    ImGui::Spacing();

    switch (node_) {
        case SimNode::Material:
            ImGui::TextUnformatted("Material"); simTip("");
            drawMaterialCombo(st, s);
            break;
        case SimNode::Fixtures:
            drawFixturesForm(st, s);
            break;
        case SimNode::Load:
            drawLoadsForm(st, s);
            break;
        case SimNode::Mesh:
            ImGui::TextUnformatted("Mesh density");
            simTip("Lower = coarser/faster mesh; higher = finer/slower but more accurate.");
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (ImGui::SliderFloat("##density", &s.meshDensity, 0.1f, 1.0f, "%.2f")) {}
            if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            simTip("Lower = coarser/faster; higher = finer/slower, more accurate.");
            break;
        case SimNode::Results:
            drawResultControls(st, s);
            break;
        case SimNode::Study:
        default:
            // Study overview: material + (type-specific) key controls + Run.
            if (s.type == StudyType::Flow) {
                ImGui::TextUnformatted("Flow direction");
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::BeginCombo("##dir", kDirLabels[s.flowDir])) {
                    for (int i = 0; i < 6; ++i)
                        if (ImGui::Selectable(kDirLabels[i], s.flowDir == i)) { s.flowDir = i; persist(st, s); }
                    ImGui::EndCombo();
                }
                simTip("World-axis direction the air flows toward.");
                ImGui::TextUnformatted("Speed (m/s)");
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::InputFloat("##speed", &s.flowSpeed, 0, 0, "%.1f")) {}
                if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
                simTip("Free-stream air speed in metres per second.");
                ImGui::TextUnformatted("Resolution");
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::BeginCombo("##preset", kPresetLabels[s.flowPreset])) {
                    for (int i = 0; i < 4; ++i)
                        if (ImGui::Selectable(kPresetLabels[i], s.flowPreset == i)) { s.flowPreset = i; persist(st, s); }
                    ImGui::EndCombo();
                }
                simTip("Voxel resolution / run time. Thin walls need a finer preset or they leak flow.");
            } else {
                ImGui::TextUnformatted("Material"); drawMaterialCombo(st, s);
                ImGui::Spacing();
                if (s.type == StudyType::Modal) {
                    ImGui::TextUnformatted("Modes");
                    ImGui::SetNextItemWidth(-FLT_MIN);
                    if (ImGui::SliderInt("##modes", &s.nModes, 1, 12)) {}
                    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
                    simTip("How many natural frequencies / mode shapes to solve for.");
                }
                ImGui::Spacing();
                ImGui::TextDisabled("Select Fixtures / Loads / Mesh in the tree to edit them.");
            }

            ImGui::Spacing();
            ImGui::Separator();
            bool busy = st.simClient && st.simClient->busy();
            if (busy) ImGui::BeginDisabled();
            if (ImGui::Button(busy ? "Running…" : "Run study", ImVec2(-FLT_MIN, 30.0f * S)))
                run(st, s);
            if (busy) ImGui::EndDisabled();
            simTip("Mesh the part and solve. Results color the model and appear under the Results node.");
            if (busy) {
                ImGui::SameLine();
                if (ImGui::SmallButton("Cancel")) st.simClient->cancel();
            }
            if (!s.dragTrace.empty())
                ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0,
                                 "Drag trace", FLT_MAX, FLT_MAX, ImVec2(-FLT_MIN, 50.0f * S));
            if (s.hasResult) { ImGui::Spacing(); ImGui::TextWrapped("%s", s.summary.c_str()); }
            break;
    }
}

}  // namespace ui
