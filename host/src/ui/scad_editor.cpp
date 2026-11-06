#include "scad_editor.h"

#include <cmath>
#include <string>
#include <nlohmann/json.hpp>
#include "imgui.h"
#include "layout.h"
#include "metrics.h"
#include "widgets/kit_form.h"
#include "../core/app_state.h"
#include "../core/async_rpc.h"
#include "../core/doc_mirror.h"

namespace ui {
namespace {
using json = nlohmann::json;
struct Editor {
    bool open = false, pending = false, inspecting = false, conversion = false, diagnosticError = false;
    unsigned long long serial = 0, inspectSerial = 0, sourceGeneration = 0;
    std::string doc, feature, diagnostics, capability, source;
    int revision = -1;
    json saved, draft, controls = json::array();
};
Editor editor;
int resizeSource(ImGuiInputTextCallbackData* data) {
    if (data->EventFlag == ImGuiInputTextFlags_CallbackResize) {
        auto* source = static_cast<std::string*>(data->UserData);
        source->resize(static_cast<std::size_t>(data->BufTextLen));
        data->Buf = source->data();
    }
    return 0;
}

const json* findFeature(const AppState& st, const std::string& id) {
    const auto& tree = st.docMirror.tree();
    auto it = tree.find("features");
    if (it == tree.end() || !it->is_array()) return nullptr;
    for (const auto& f : *it)
        if (f.is_object() && f.value("id", std::string()) == id &&
            f.value("type", std::string()) == "scad") return &f;
    return nullptr;
}
void inspect(AppState& st) {
    const auto serial = editor.serial, request = ++editor.inspectSerial;
    editor.inspecting = true;
    st.rpc.call("scad_inspect", {{"params", editor.draft}}, [serial, request](const core::RpcResult& r) {
        if (editor.serial != serial || editor.inspectSerial != request) return;
        editor.inspecting = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            editor.diagnosticError = true;
            editor.diagnostics = r.ok && r.result.is_object() ? r.result.value("error", std::string("Unable to inspect source parameters.")) : r.err;
            return;
        }
        editor.controls = r.result.value("controls", json::array());
    });
}
void capability(AppState& st) {
    const auto serial = editor.serial;
    st.rpc.call("scad_status", json::object(), [serial](const core::RpcResult& r) {
        if (editor.serial != serial) return;
        editor.capability = r.ok ? r.result.dump(2) : r.err;
    });
}
void submit(AppState& st, json op) {
    if (editor.pending) return;
    if (st.docMirror.docId() != editor.doc || st.docMirror.revision() != editor.revision) {
        editor.diagnosticError = true;
        editor.diagnostics = "The document changed or is no longer selected. Reopen the editor before applying this draft.";
        return;
    }
    const auto serial = editor.serial;
    const json submitted = editor.draft;
    const bool converting = op.value("op", std::string()) == "scad_convert";
    ++editor.inspectSerial;
    editor.inspecting = false;
    editor.pending = true;
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, json::array({op}),
        [&st, serial, submitted, converting](const core::OpsResponse& r, bool current) {
            --st.rebuildsInFlight;
            if (editor.serial != serial) return;
            editor.pending = false;
            if (!current || !st.isActiveMirrorDoc()) return;
            if (!r.ok) {
                editor.diagnosticError = true;
                editor.diagnostics = !r.error_message.empty() ? r.error_message
                    : !r.error_code.empty() ? r.error_code : "Rebuild failed.";
                if (r.feature_status.is_array() && !r.feature_status.empty())
                    editor.diagnostics += "\n" + r.feature_status.dump(2);
                return;
            }
            editor.revision = r.revision;
            editor.diagnosticError = false;
            editor.saved = submitted;
            if (editor.feature.empty() && r.id_map.is_object())
                editor.feature = r.id_map.value("$0", std::string());
            editor.diagnostics = r.feature_status.is_array() && !r.feature_status.empty()
                ? "Rebuild completed.\n" + r.feature_status.dump(2) : "Rebuild completed.";
            if (!r.aicm_path.empty()) st.loadDocMeshAsync(r.aicm_path, false, true, true);
            if (converting) { editor.open = false; return; }
            inspect(st);
        });
}
}

void openScadEditor(AppState& st, const std::string& docId, const std::string& featureId) {
    if (st.docMirror.docId() != docId) return;
    const json* f = findFeature(st, featureId);
    if (!f) return;
    const auto serial = editor.serial + 1;
    editor = Editor{}; editor.serial = serial; editor.open = true;
    editor.doc = docId; editor.feature = featureId; editor.revision = st.docMirror.revision();
    editor.saved = f->value("params", json::object()); editor.draft = editor.saved;
    editor.source = editor.draft.value("source", std::string());
    inspect(st); capability(st);
}
void openNewScadEditor(AppState& st) {
    if (!st.docMirror.hasDoc()) return;
    const auto serial = editor.serial + 1;
    editor = Editor{}; editor.serial = serial; editor.open = true;
    editor.doc = st.docMirror.docId(); editor.revision = st.docMirror.revision();
    editor.draft = {{"source", "cube([20, 20, 20], center=true);\n"}, {"files", json::array()},
                    {"parameters", json::object()}, {"parameter_schema", json::array()},
                    {"mm_per_source_unit", 1.0}};
    editor.saved = editor.draft;
    editor.source = editor.draft.value("source", std::string());
    inspect(st); capability(st);
}
void openScadConversion(AppState& st) {
    if (!st.docMirror.hasDoc()) return;
    const auto serial = editor.serial + 1;
    editor = Editor{}; editor.serial = serial; editor.open = true; editor.conversion = true;
    editor.doc = st.docMirror.docId(); editor.revision = st.docMirror.revision();
    editor.draft = {{"mm_per_source_unit", 1.0}};
    capability(st);
}
void drawScadEditor(AppState& st) {
    if (!editor.open) return;
    setNextCommandDialogPlacement("SCAD Editor", ImVec2(metrics::px(660), metrics::px(650)),
                                  ImVec2(metrics::px(380), metrics::px(300)));
    if (ImGui::Begin("SCAD Editor", &editor.open)) {
        ImGui::TextWrapped("OpenSCAD uses an optional external renderer and produces faceted geometry; feature history is not recovered.");
        if (st.docMirror.docId() != editor.doc)
            ImGui::TextWrapped("This draft belongs to another document. Return to that document before applying it.");
        double factor = editor.draft.value("mm_per_source_unit", 1.0);
        const char* units[] = {"mm", "cm", "m", "in", "ft", "Custom"};
        const double factors[] = {1.0, 10.0, 1000.0, 25.4, 304.8};
        int unit = 5;
        for (int i = 0; i < 5; ++i) if (std::abs(factor - factors[i]) < 1e-12) unit = i;
        if (kit::fieldEnum("source_unit", "Source coordinate unit", &unit, units, 6) && unit < 5) {
            editor.draft["mm_per_source_unit"] = factor = factors[unit];
            if (!editor.conversion) inspect(st);
        }
        if (kit::fieldNumber("source_factor", "mm per source unit", &factor,
                             kit::FieldOpts().withUnit("mm").withMin(1e-12).withSigFigs(10))) {
            editor.draft["mm_per_source_unit"] = factor;
            if (!editor.conversion) inspect(st);
        }
        if (editor.conversion) {
            ImGui::TextWrapped("Convert a supported native feature graph to managed SCAD. Unsupported features are reported; no features are silently omitted.");
            if (ImGui::Button("Convert native to code") && !editor.pending)
                submit(st, {{"op", "scad_convert"}, {"direction", "to_code"}, {"mm_per_source_unit", factor}});
        } else {
            ImGui::TextUnformatted("Source");
            if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
            ImGui::PushID(static_cast<int>(editor.sourceGeneration));
            if (ImGui::InputTextMultiline("##scad_source", editor.source.data(),
                    editor.source.capacity() + 1, ImVec2(-1, ImGui::GetTextLineHeightWithSpacing() * 10),
                    ImGuiInputTextFlags_CallbackResize, resizeSource, &editor.source))
                editor.draft["source"] = editor.source;
            ImGui::PopID();
            if (st.fonts.mono) ImGui::PopFont();
            if (ImGui::Button("Refresh parameter controls") && !editor.inspecting) inspect(st);
            ImGui::TextUnformatted("Exposed parameters (lengths in mm; angles in degrees)");
            if (!editor.draft.contains("parameters") || !editor.draft["parameters"].is_object())
                editor.draft["parameters"] = json::object();
            if (editor.controls.is_array()) for (const auto& c : editor.controls) {
                if (!c.is_object()) continue;
                const std::string name = c.value("name", std::string());
                if (name.empty()) continue;
                const std::string label = c.value("label", name), kind = c.value("kind", std::string("number"));
                json value = editor.draft["parameters"].value(name, c.value("value", json()));
                if (value.is_object() && value.contains("value")) value = value["value"];
                ImGui::PushID(name.c_str());
                if (value.is_number() && (kind == "number" || kind == "integer" ||
                                          kind == "length" || kind == "angle")) {
                    const char* kinds[] = {"Number", "Integer", "Length (mm)", "Angle (deg)"};
                    const char* keys[] = {"number", "integer", "length", "angle"};
                    int selected = kind == "integer" ? 1 : kind == "length" ? 2 : kind == "angle" ? 3 : 0;
                    if (kit::fieldEnum("kind", "Interpret as", &selected, kinds, 4)) {
                        auto& schema = editor.draft["parameter_schema"];
                        if (!schema.is_array()) schema = json::array();
                        bool found = false;
                        for (auto& entry : schema) {
                            if (entry.is_object() && entry.value("name", std::string()) == name) {
                                entry["kind"] = keys[selected];
                                found = true;
                                break;
                            }
                        }
                        if (!found) schema.push_back({{"name", name}, {"kind", keys[selected]}});
                        inspect(st);
                    }
                }
                if (kind == "boolean" && value.is_boolean()) {
                    bool v = value.get<bool>(); if (kit::fieldCheckbox("value", label.c_str(), &v)) editor.draft["parameters"][name] = v;
                } else if (kind == "string" && value.is_string()) {
                    std::string v = value.get<std::string>(); if (kit::fieldText("value", label.c_str(), &v)) editor.draft["parameters"][name] = v;
                } else if (kind == "integer" && value.is_number()) {
                    int v = value.get<int>(); if (kit::fieldInt("value", label.c_str(), &v)) editor.draft["parameters"][name] = v;
                } else if (value.is_number()) {
                    double v = value.get<double>(); kit::FieldOpts opts;
                    if (kind == "length") opts.withUnit("mm"); else if (kind == "angle") opts.withUnit("deg");
                    if (kit::fieldNumber("value", label.c_str(), &v, opts)) editor.draft["parameters"][name] = v;
                }
                ImGui::PopID();
            }
            if (ImGui::Button("Apply / Rebuild") && !editor.pending) {
                json op = editor.feature.empty() ? json{{"op", "add_feature"}, {"feature", {{"type", "scad"}, {"name", "OpenSCAD"}, {"params", editor.draft}, {"refs", json::object()}}}, {"before", nullptr}}
                    : json{{"op", "edit_feature"}, {"id", editor.feature}, {"params", editor.draft}};
                submit(st, op);
            }
            ImGui::SameLine();
            if (ImGui::Button("Revert draft") && !editor.pending) {
                editor.draft = editor.saved;
                editor.source = editor.draft.value("source", std::string());
                ++editor.sourceGeneration;
                inspect(st);
            }
            ImGui::SameLine();
            if (!editor.feature.empty() && ImGui::Button("Convert managed code to native") && !editor.pending) {
                if (editor.draft != editor.saved) {
                    editor.diagnosticError = true;
                    editor.diagnostics = "Apply or revert the draft before converting the saved feature.";
                } else
                    submit(st, {{"op", "scad_convert"}, {"direction", "to_native"}});
            }
        }
        if (editor.pending) ImGui::TextUnformatted("Rebuilding...");
        if (!editor.diagnostics.empty()) {
            ImGui::Separator();
            const auto split = editor.diagnostics.find('\n');
            const std::string summary = editor.diagnostics.substr(0, split);
            kit::inlineStatus(summary.c_str(), editor.diagnosticError ? kit::Tone::Err : kit::Tone::Neutral);
            if (split != std::string::npos && ImGui::CollapsingHeader("Diagnostic details"))
                ImGui::TextWrapped("%s", editor.diagnostics.c_str() + split + 1);
        }
        if (ImGui::CollapsingHeader("Renderer availability and installation")) {
            ImGui::TextWrapped("Install OpenSCAD on the machine running the CAD engine (server/cell for web). A supported sandbox is also required. No software is installed by this action. See https://openscad.org/downloads.html; then recheck.");
            if (ImGui::Button("Recheck renderer")) capability(st);
            ImGui::TextWrapped("%s", editor.capability.c_str());
        }
    }
    ImGui::End();
}
}
