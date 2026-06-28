// Evaluate dialogs implementation (C19) — see evaluate_dialogs.h.
#include "evaluate_dialogs.h"

#include <cstdio>

#include "imgui.h"

#include "metrics.h"
#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"
#include "property_manager.h"   // pmFormatDim — linear readouts follow the document units

namespace ui {

// Zebra stripe density used by the Zebra Stripes control window. File-scope so it persists across
// frames without adding a member to EvaluateDialogs (the class has a single instance). Matches the
// renderer's default frequency; the slider (4..60) drives it live via Renderer::setZebra().
static float gZebraFreq = 18.0f;

// Format a linear value (canonical mm) for an evaluate readout in the active document units
// (mm in MMGS, inch in IPS). Area/volume/mass/inertia stay in their canonical units below — only
// pure LENGTH measures convert, mirroring SolidWorks (which reformats length, not the derived
// units, on a units toggle).
static std::string evalLen(AppState& st, double mm) { return pmFormatDim(mm, st.ips()); }

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
    // Multi-document: this overlay loads its mesh into the SHARED renderer (st.loadDocMesh + the
    // colormap). Bind it to the doc it was computed on so a tab switch before the async result
    // drains can't paint this field overlay onto a different document's geometry.
    const std::string fieldDocId = st.docMirror.docId();
    // Field results are shown in-viewport; while computing, a small wait window gives the
    // user feedback (the old sync call "gave feedback" by freezing the app instead).
    openWindow_ = "field_wait";
    st.rpc.call(method, params,
        [this, &st, gen, method, legendTitle, units, fieldDocId](const core::RpcResult& r) {
            if (gen != gen_) return;
            if (st.docMirror.docId() != fieldDocId) { pending_ = false; return; }  // active doc changed
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
    if (cmdId == "eval_mass_props") { runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}}); openWindow_ = "mass"; title_ = "Mass Properties"; return true; }
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
        // Zebra surface-continuity inspection: turn the renderer's striped-environment shader on
        // and open a small control window (frequency slider + Turn Off). Closing the window — or
        // pressing Turn Off — disables zebra so the user is never stuck in stripe mode.
        errorText_.clear();
        result_ = nlohmann::json::object();
        pending_ = false;
        if (st.renderer) st.renderer->setZebra(true, gZebraFreq);
        openWindow_ = "zebra"; title_ = "Zebra Stripes";
        return true;
    }
    return false;
}

void EvaluateDialogs::draw(AppState& st) {
    if (openWindow_.empty()) return;
    bool open = true;
    ImGui::SetNextWindowSize(ImVec2(ui::metrics::px(340), 0), ImGuiCond_Appearing);
    // Frosted-glass fill so evaluate result windows float as part of the app-wide glass family
    // (translucent glassBg lets the viewport bleed through subtly; content stays legible).
    const auto& gpal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(gpal.glassBg[0], gpal.glassBg[1], gpal.glassBg[2], gpal.glassBg[3]));
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
            // Material override — density (g/cm3) is forwarded to the engine's eval_mass; changing
            // the material preset or the density re-runs the evaluation. Last preset = Custom.
            static const struct { const char* name; float d; } kMaterials[] = {
                {"Steel", 7.85f}, {"Stainless", 8.00f}, {"Aluminum", 2.70f}, {"Titanium", 4.51f},
                {"Brass", 8.50f}, {"ABS plastic", 1.05f}, {"PLA", 1.24f}, {"Custom", 0.0f},
            };
            const int kCustom = (int)(sizeof(kMaterials) / sizeof(kMaterials[0])) - 1;
            ImGui::SetNextItemWidth(140 * ui::uiScale);
            if (ImGui::Combo("Material", &massMaterial_,
                             "Steel\0Stainless\0Aluminum\0Titanium\0Brass\0ABS plastic\0PLA\0Custom\0")) {
                if (massMaterial_ != kCustom) massDensity_ = kMaterials[massMaterial_].d;
                runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}});
            }
            ImGui::SetNextItemWidth(140 * ui::uiScale);
            if (ImGui::InputFloat("Density (g/cm3)", &massDensity_, 0.0f, 0.0f, "%.3f"))
                massMaterial_ = kCustom;
            if (ImGui::IsItemDeactivatedAfterEdit()) {
                if (massDensity_ < 0.0f) massDensity_ = 0.0f;
                runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}});
            }
            ImGui::Separator();
            ImGui::Text("Volume:  %.3f mm3", result_.value("volume_mm3", 0.0));
            ImGui::Text("Mass:    %.3f g", result_.value("mass_g", 0.0));
            if (result_.contains("com") && result_["com"].is_array() && result_["com"].size() == 3)
                ImGui::Text("COM:     %s, %s, %s",
                            evalLen(st, result_["com"][0].get<double>()).c_str(),
                            evalLen(st, result_["com"][1].get<double>()).c_str(),
                            evalLen(st, result_["com"][2].get<double>()).c_str());
            if (result_.contains("inertia") && result_["inertia"].is_array() &&
                result_["inertia"].size() == 9) {
                ImGui::SeparatorText("Inertia tensor (g.mm2)");
                const auto& I = result_["inertia"];
                for (int r = 0; r < 3; ++r)
                    ImGui::Text("%12.2f %12.2f %12.2f",
                                I[r*3+0].get<double>(), I[r*3+1].get<double>(), I[r*3+2].get<double>());
            }
            // Principal moments of inertia (g.mm2) + their axes (unit vectors at the CoM).
            if (result_.contains("principal_moments") && result_["principal_moments"].is_array() &&
                result_["principal_moments"].size() == 3 &&
                result_.contains("principal_axes") && result_["principal_axes"].is_array() &&
                result_["principal_axes"].size() == 3) {
                ImGui::SeparatorText("Principal axes (g.mm2)");
                const auto& Pm = result_["principal_moments"];
                const auto& Pa = result_["principal_axes"];
                for (int i = 0; i < 3; ++i) {
                    if (!Pa[i].is_array() || Pa[i].size() != 3) continue;
                    ImGui::Text("Px%d %12.2f  [% .3f % .3f % .3f]", i + 1,
                                Pm[i].get<double>(),
                                Pa[i][0].get<double>(), Pa[i][1].get<double>(), Pa[i][2].get<double>());
                }
            }
        } else if (openWindow_ == "measure") {
            std::string kind = result_.value("kind", std::string("(none)"));
            auto numAt = [&](const char* k, double& out) -> bool {
                if (result_.contains(k) && result_[k].is_number()) { out = result_[k].get<double>(); return true; }
                return false;
            };
            // One-line summary of this measurement (for the running History list, SW-style).
            std::string summary; double v;
            if (numAt("distance", v))    summary = "Distance " + evalLen(st, v);
            else if (numAt("length", v)) summary = "Length " + evalLen(st, v);
            else if (numAt("radius", v)) summary = "Radius " + evalLen(st, v) + " (Dia " + evalLen(st, 2.0 * v) + ")";
            else if (numAt("area", v))   { char b[64]; std::snprintf(b, sizeof(b), "Area %.3f mm2", v); summary = b; }
            else if (numAt("volume", v)) { char b[64]; std::snprintf(b, sizeof(b), "Volume %.3f mm3", v); summary = b; }
            double ang;
            if (numAt("angle", ang)) { char b[48]; std::snprintf(b, sizeof(b), "Angle %.3f deg", ang);
                                       summary = summary.empty() ? std::string(b) : summary + ", " + b; }
            // Accumulate: append each completed measurement exactly once (gen_ bumps per eval).
            if (!pending_ && !summary.empty() && gen_ != measureLoggedGen_) {
                measureLoggedGen_ = gen_;
                measureLog_.push_back(summary);
            }

            ImGui::Text("Kind: %s", kind.c_str());
            auto show = [&](const char* k, const char* unit) {
                if (result_.contains(k) && result_[k].is_number())
                    ImGui::Text("%s: %.4f %s", k, result_[k].get<double>(), unit);
            };
            // Linear measures reformat to the document units (mm / inch); angle stays in degrees,
            // and area/volume keep their canonical derived units.
            auto showLen = [&](const char* k) {
                if (result_.contains(k) && result_[k].is_number())
                    ImGui::Text("%s: %s", k, evalLen(st, result_[k].get<double>()).c_str());
            };
            showLen("distance"); show("angle", "deg"); showLen("length");
            show("area", "mm2"); showLen("radius");
            { double r; if (numAt("radius", r)) ImGui::Text("diameter: %s", evalLen(st, 2.0 * r).c_str()); }
            show("volume", "mm3");
            if (kind == "(none)" || result_.empty())
                ImGui::TextDisabled("Select 1-2 faces/edges/vertices, then Measure.");
            // Running history (SolidWorks Measure accumulates) — newest first, with Clear.
            if (!measureLog_.empty()) {
                ImGui::SeparatorText("History");
                if (ImGui::SmallButton("Clear")) { measureLog_.clear(); measureLoggedGen_ = -1; }
                ImGui::BeginChild("##measlog", ImVec2(0, 120 * ui::uiScale), ImGuiChildFlags_Border);
                for (size_t i = measureLog_.size(); i-- > 0; )
                    ImGui::TextUnformatted(measureLog_[i].c_str());
                ImGui::EndChild();
            }
        } else if (openWindow_ == "section") {
            ImGui::Text("Area:     %.3f mm2", result_.value("area_mm2", 0.0));
            if (result_.contains("centroid") && result_["centroid"].is_array() &&
                result_["centroid"].size() == 3)
                ImGui::Text("Centroid: %s, %s, %s",
                            evalLen(st, result_["centroid"][0].get<double>()).c_str(),
                            evalLen(st, result_["centroid"][1].get<double>()).c_str(),
                            evalLen(st, result_["centroid"][2].get<double>()).c_str());
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
            ImGui::TextWrapped("Surface-continuity inspection. Smooth, evenly-spaced stripes = a "
                               "fair surface; stripes that kink = tangency (G1) break; stripes that "
                               "jog/step = curvature (G2) break.");
            ImGui::Spacing();
            if (ImGui::SliderFloat("Stripe density", &gZebraFreq, 4.0f, 60.0f, "%.0f")) {
                if (st.renderer) st.renderer->setZebra(true, gZebraFreq);
            }
            ImGui::Spacing();
            if (ImGui::Button("Turn Off")) {
                if (st.renderer) st.renderer->setZebra(false, gZebraFreq);
                openWindow_.clear();
            }
        }
    }
    ImGui::End();
    ImGui::PopStyleColor();
    if (!open) {
        // Closing the window must also exit stripe mode so the user isn't stuck in zebra shading.
        if (openWindow_ == "zebra" && st.renderer) st.renderer->setZebra(false, gZebraFreq);
        openWindow_.clear();
    }
}

}  // namespace ui
