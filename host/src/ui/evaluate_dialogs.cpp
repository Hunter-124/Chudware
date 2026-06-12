// Evaluate dialogs implementation (C19) — see evaluate_dialogs.h.
#include "evaluate_dialogs.h"

#include <cstdio>

#include "imgui.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

// Build the refs array for eval_measure from the current selection's resolved selectors.
static nlohmann::json selectionRefs(AppState& st) {
    nlohmann::json refs = nlohmann::json::array();
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty()) continue;
        try { refs.push_back(nlohmann::json::parse(it.selector)); } catch (...) {}
    }
    return refs;
}

// C39: evals run async on st.rpc — heavy methods (interference, thickness on a dense model)
// used to freeze the window for their full duration. The callback runs on the GL thread
// inside the per-frame drain; gen_ guards against a newer eval superseding a pending one.
void EvaluateDialogs::runEval(AppState& st, const std::string& method,
                              const nlohmann::json& extra) {
    errorText_.clear();
    result_ = nlohmann::json::object();
    pending_ = false;
    if (!st.docMirror.hasDoc()) { errorText_ = "No document loaded."; return; }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    pending_ = true;
    const int gen = ++gen_;
    st.rpc.call(method, params, [this, gen, method](const core::RpcResult& r) {
        if (gen != gen_) return;  // a newer eval replaced this one
        pending_ = false;
        if (!r.ok) {
            errorText_ = r.err.empty() ? ("worker call failed: " + method) : r.err;
            return;
        }
        if (r.result.is_object() && r.result.contains("code")) {
            errorText_ = r.result.value("message", std::string("evaluate error"));
            return;
        }
        result_ = r.result;
    }, 60000);
}

void EvaluateDialogs::runFieldEval(AppState& st, const std::string& method,
                                   const nlohmann::json& extra,
                                   const std::string& legendTitle, const std::string& units) {
    errorText_.clear();
    result_ = nlohmann::json::object();
    pending_ = false;
    title_ = legendTitle;
    if (!st.docMirror.hasDoc()) { errorText_ = "No document loaded."; openWindow_ = "field_err"; return; }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    pending_ = true;
    const int gen = ++gen_;
    // Field results are shown in-viewport; while computing, a small wait window gives the
    // user feedback (the old sync call "gave feedback" by freezing the app instead).
    openWindow_ = "field_wait";
    st.rpc.call(method, params,
        [this, &st, gen, method, legendTitle, units](const core::RpcResult& r) {
            if (gen != gen_) return;
            pending_ = false;
            if (!r.ok) {
                errorText_ = r.err.empty() ? ("worker call failed: " + method) : r.err;
                openWindow_ = "field_err"; return;
            }
            if (r.result.is_object() && r.result.contains("code")) {
                errorText_ = r.result.value("message", std::string("evaluate error"));
                openWindow_ = "field_err"; return;
            }
            result_ = r.result;
            std::string aicm = result_.value("aicm_path", std::string());
            if (aicm.empty()) { errorText_ = "no overlay produced"; openWindow_ = "field_err"; return; }
            std::string le;
            if (!st.loadDocMesh(aicm, le)) {
                errorText_ = "overlay load failed: " + le;
                openWindow_ = "field_err"; return;
            }
            st.colormapOn = true;
            if (st.renderer) st.renderer->setColormapMode(true);
            st.legendOn = true;
            st.legendTitle = legendTitle;
            st.legendUnits = units;
            float vmin = 0, vmax = 1;
            if (st.renderer && st.renderer->scalarRange(vmin, vmax)) { st.legendMin = vmin; st.legendMax = vmax; }
            openWindow_.clear();  // field results are shown in-viewport, not a window
            st.addLog("[eval] " + method + " overlay loaded");
        }, 60000);
}

bool EvaluateDialogs::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "eval_mass_props") { runEval(st, "eval_mass", nlohmann::json::object()); openWindow_ = "mass"; title_ = "Mass Properties"; return true; }
    if (cmdId == "eval_measure") {
        nlohmann::json refs = selectionRefs(st);
        runEval(st, "eval_measure", {{"refs", refs}});
        openWindow_ = "measure"; title_ = "Measure"; return true;
    }
    if (cmdId == "eval_section_props") {
        runEval(st, "eval_section_props", {{"plane", "plane.top"}});
        openWindow_ = "section"; title_ = "Section Properties"; return true;
    }
    if (cmdId == "eval_check" || cmdId == "eval_geometry") {
        runEval(st, "eval_check", nlohmann::json::object());
        openWindow_ = "check"; title_ = "Geometry Check"; return true;
    }
    if (cmdId == "eval_interference") {
        // Single-body parts have no interference; assemblies arrive in C20.
        runEval(st, "eval_interference", nlohmann::json::object());
        openWindow_ = "interference"; title_ = "Interference Detection"; return true;
    }
    if (cmdId == "eval_symmetry") {
        runEval(st, "eval_symmetry", {{"plane", "plane.front"}});
        openWindow_ = "symmetry"; title_ = "Symmetry Check"; return true;
    }
    if (cmdId == "eval_draft")     { runFieldEval(st, "eval_draft", {{"pull", {0,0,1}}, {"angle_deg", 1.0}}, "Draft angle", "deg"); return true; }
    if (cmdId == "eval_thickness") { runFieldEval(st, "eval_thickness", nlohmann::json::object(), "Wall thickness", "mm"); return true; }
    if (cmdId == "eval_curvature") { runFieldEval(st, "eval_curvature", {{"kind", "mean"}}, "Mean curvature", "1/mm"); return true; }
    if (cmdId == "eval_zebra") {
        // Zebra would need a striped-environment shader; deferred — note it and fall back.
        openWindow_ = "zebra"; title_ = "Zebra Stripes"; errorText_ =
            "Zebra striping is a shader feature deferred to a later card.";
        return true;
    }
    return false;
}

void EvaluateDialogs::draw(AppState& st) {
    if (openWindow_.empty()) return;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(340, 0), ImGuiCond_Appearing);
    if (ImGui::Begin(title_.c_str(), &open)) {
        if (pending_) {
            // C39: the eval is in flight on the dispatch thread; the result fills in when
            // it drains. A spinner-ish pulse keeps it obviously alive.
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.6f, 0.6f, 0.6f, 1));
            ImGui::TextUnformatted("Computing");
            ImGui::SameLine(0, 0);
            ImGui::TextUnformatted(&"..."[2 - ((int)(ImGui::GetTime() * 3.0) % 3)]);
            ImGui::PopStyleColor();
        } else if (!errorText_.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.90f, 0.35f, 0.30f, 1));
            ImGui::TextWrapped("%s", errorText_.c_str());
            ImGui::PopStyleColor();
        } else if (openWindow_ == "mass") {
            ImGui::Text("Volume:  %.3f mm3", result_.value("volume_mm3", 0.0));
            ImGui::Text("Mass:    %.3f g", result_.value("mass_g", 0.0));
            if (result_.contains("com") && result_["com"].is_array() && result_["com"].size() == 3)
                ImGui::Text("COM:     %.3f, %.3f, %.3f mm",
                            result_["com"][0].get<double>(), result_["com"][1].get<double>(),
                            result_["com"][2].get<double>());
            if (result_.contains("inertia") && result_["inertia"].is_array() &&
                result_["inertia"].size() == 9) {
                ImGui::SeparatorText("Inertia tensor (g.mm2)");
                const auto& I = result_["inertia"];
                for (int r = 0; r < 3; ++r)
                    ImGui::Text("%12.2f %12.2f %12.2f",
                                I[r*3+0].get<double>(), I[r*3+1].get<double>(), I[r*3+2].get<double>());
            }
        } else if (openWindow_ == "measure") {
            std::string kind = result_.value("kind", std::string("(none)"));
            ImGui::Text("Kind: %s", kind.c_str());
            auto show = [&](const char* k, const char* unit) {
                if (result_.contains(k) && result_[k].is_number())
                    ImGui::Text("%s: %.4f %s", k, result_[k].get<double>(), unit);
            };
            show("distance", "mm"); show("angle", "deg"); show("length", "mm");
            show("area", "mm2"); show("radius", "mm"); show("volume", "mm3");
            if (kind == "(none)" || result_.empty())
                ImGui::TextDisabled("Select 1-2 faces/edges/vertices, then Measure.");
        } else if (openWindow_ == "section") {
            ImGui::Text("Area:     %.3f mm2", result_.value("area_mm2", 0.0));
            if (result_.contains("centroid") && result_["centroid"].is_array() &&
                result_["centroid"].size() == 3)
                ImGui::Text("Centroid: %.3f, %.3f, %.3f mm",
                            result_["centroid"][0].get<double>(),
                            result_["centroid"][1].get<double>(),
                            result_["centroid"][2].get<double>());
            ImGui::Text("Ixx: %.3f  Iyy: %.3f  Ixy: %.3f",
                        result_.value("Ixx", 0.0), result_.value("Iyy", 0.0),
                        result_.value("Ixy", 0.0));
        } else if (openWindow_ == "check") {
            if (result_.contains("issues") && result_["issues"].is_array()) {
                const auto& issues = result_["issues"];
                if (issues.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.30f, 0.80f, 0.45f, 1));
                    ImGui::TextWrapped("No geometry issues found.");
                    ImGui::PopStyleColor();
                } else {
                    ImGui::Text("%d issue(s):", (int)issues.size());
                    for (const auto& it : issues) {
                        std::string sev = it.value("severity", std::string("info"));
                        std::string txt = it.value("text", std::string());
                        ImGui::BulletText("[%s] %s", sev.c_str(), txt.c_str());
                    }
                }
            }
        } else if (openWindow_ == "interference") {
            // Single-body parts: explain. (eval_interference returns a fit report shape.)
            ImGui::TextWrapped("Interference detection runs across assembly components. This is a "
                               "single-body part; open an assembly (C20) to check component clashes.");
            if (result_.contains("summary"))
                ImGui::TextWrapped("%s", result_.value("summary", std::string()).c_str());
        } else if (openWindow_ == "symmetry") {
            bool sym = result_.value("symmetric", false);
            ImGui::Text("Symmetric: %s", sym ? "yes" : "no");
            ImGui::Text("Deviation: %.3f mm3", result_.value("deviation_mm3", 0.0));
        } else if (openWindow_ == "zebra") {
            ImGui::TextWrapped("Zebra striping is deferred to a later card.");
        }
    }
    ImGui::End();
    if (!open) openWindow_.clear();
}

}  // namespace ui
