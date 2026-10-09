// HydraulicPanel side-pane circuit, sizing, simulation, BOM and piping forms.
#include "../hydraulic_panel.h"
#include "hydraulic_internal.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdlib>
#include <utility>

#include "../../core/app_state.h"
#include "../metrics.h"
#include "../sim_manager.h"   // SimManager::unitSys — the Simulation pane's display units

namespace ui {
using namespace hydint;
namespace {

// A 0..1 ratio member as a percentage.
std::string pct(const json& o, const char* k, int sigFigs = 4) { return qty(o, k, "%", 100.0, sigFigs); }

// Valve positions: the closed combo shows the word, the open list the flow paths.
const char* const kPosWords[] = {"Side A", "Side B", "Center", "Proportional"};
const char* const kPosLabels[] = {"Side A: P to A, B to T", "Side B: P to B, A to T",
                                  "Center: the valve's center condition", "Proportional: partial spool opening"};
const char* const kMotorTypes[] = {"Any", "Gear", "Vane", "Piston", "Orbital"};
const char* const kEffClasses[] = {"IE2", "IE3", "IE4"};

// One-line verdict on a simulation: the peak against the relief setting. The peak value itself is
// the Outcome card's chip, so the verdict states only the comparison.
// `relief` is in bar; every quantity is formatted in the pane's display units (`us`).
std::string simVerdict(const json& summary, double relief, bool us, kit::Tone& tone) {
    const double peak = jnum(summary, "peak_pressure_bar");
    const std::string rs = fmtU(relief, "bar", us, 3);
    tone = kit::Tone::Ok;
    if (!(relief > 0.0)) {
        tone = kit::Tone::Neutral;
        return "No relief valve in the circuit to compare the peak with.";
    }
    if (!(peak > relief)) return "The peak stays below the " + rs + " relief setting.";
    const std::string over = kit::fmtQuantity((peak / relief - 1.0) * 100.0, kit::unitText(kit::Unit::Percent), 2);
    if (peak > relief * 1.15) {
        tone = kit::Tone::Warn;
        return "The peak exceeds the " + rs + " relief setting by " + over + ".";
    }
    // Normal behavior, but not an all-clear: an informational note, so it never reads as a
    // green "pass" sitting next to a warning chip.
    tone = kit::Tone::Info;
    return "The peak is " + over + " above the " + rs +
           " relief setting: normal while the relief valve opens (up to 15 %).";
}

}  // namespace

void HydraulicPanel::revealHere(Reveal r) {
    if (reveal_ != r) return;
    reveal_ = Reveal::None;
    ImGui::SetScrollHereY(0.0f);
}

bool HydraulicPanel::drawNativeJson(const char* id, const char* title, const json& value, json& edited,
                                    bool disclosed) {
    ImGui::PushID(id);
    bool applied = false;
    if (disclosed || ImGui::CollapsingHeader(title)) {
        auto& draft = jsonDrafts_[id];
        if (draft.source != value || draft.text.empty()) {
            draft.source = value;
            draft.text = value.dump(2);
            draft.error.clear();
        }
        kit::dimText("Every setting as JSON, in the units each key names. Settings the "
                     "solver does not use are kept; Apply still checks what the solver supports.");
        {
            kit::FieldGroup g("native_json");
            kit::FieldOpts opts;
            opts.lines = 10;
            kit::fieldText("json", "JSON", &draft.text, opts);
        }
        if (kit::secondaryButton("Apply JSON###apply_json", !busy())) {
            json parsed = json::parse(draft.text, nullptr, false);
            if (parsed.is_discarded() || !parsed.is_object())
                draft.error = "Enter a valid JSON object.";
            else {
                edited = std::move(parsed);
                draft.error.clear();
                applied = true;
            }
        }
        ImGui::SameLine();
        if (kit::secondaryButton("Reset draft###reset_json")) {
            draft.text = value.dump(2);
            draft.error.clear();
        }
        if (!draft.error.empty()) kit::inlineStatus(draft.error.c_str(), kit::Tone::Err);
    }
    ImGui::PopID();
    return applied;
}

void HydraulicPanel::drawStart(AppState& st) {
    if (kit::cardBegin("start", "Start", "new_circuit")) {
        kit::dimText("Choose a connected, editable starting circuit. Its simulation setup is included.");
        if (templatesPending_ || !templatesFetched_) kit::spinner("Loading starting circuits\xe2\x80\xa6");
        if (!templatesError_.empty()) kit::inlineStatus(templatesError_.c_str(), kit::Tone::Err);
        for (const auto& entry : templates_) {
            const std::string id = jstr(entry, "id");
            if (id.empty() || id == "blank") continue;
            ImGui::PushID(id.c_str());
            const std::string name = jstr(entry, "name");
            if (kit::cardBegin("template", name.c_str(), "study_hydraulics")) {
                kit::dimText(jstr(entry, "description").c_str());
                const std::string count = sim::countNoun((long long)jnum(entry, "components"), "component") +
                                          " \xc2\xb7 " + sim::countNoun((long long)jnum(entry, "hoses"), "hose");
                kit::dimText(count.c_str());
                const std::string choose = "Use " + name + "###choose";
                if (kit::secondaryButton(choose.c_str(), !busy(), nullptr, ImVec2(-FLT_MIN, 0)))
                    applyTemplate(st, id);
            }
            kit::cardEnd();
            ImGui::PopID();
            kit::gapRow();
        }
        if (kit::secondaryButton("Blank circuit###blank", !busy(), nullptr, ImVec2(-FLT_MIN, 0)))
            applyTemplate(st, "blank");
        kit::itemTooltip("Start with an empty circuit and place your own components.");
    }
    kit::cardEnd();
}

void HydraulicPanel::drawCircuitForm(AppState& st) {
    ImGuiContext& g = *GImGui;
    if (central_ == CentralView::View3D) drawLayout3d(st);
    if (!circuit_.is_object() || (jarr(circuit_, "components").empty() && startVisible_)) {
        drawStart(st);
        return;
    }
    const json& comps = jarr(circuit_, "components");
    if (kit::cardBegin("components", "Components", "insert_component")) {
        {
            kit::FieldGroup g("circuit", "Name");
            std::string name = jstr(circuit_, "name", "Hydraulic circuit");
            if (kit::fieldText("name", "Name", &name, kit::FieldOpts().withHint("Hydraulic circuit"))) {
                circuit_["name"] = name.empty() ? std::string("Hydraulic circuit") : name;
                commitCircuitMutation(st, false);
            }
        }
        for (int i = 0; i < (int)comps.size(); ++i) {
            const auto& component = comps[i];
            const std::string type = typeLabel(jstr(component, "type"));
            const std::string name = jstr(component, "name", type);
            const std::string title = type == name ? name : type + " \xc2\xb7 " + name;
            const auto ports = openRequiredPorts(circuit_, component);
            std::string warning = "Connect required ports:";
            for (const auto& port : ports) warning += " " + port;
            kit::RowStyle row;
            row.icon = "cat_hydraulics";
            row.selected = i == selected_ && selectedHose_ < 0;
            row.tooltip = ports.empty() ? "Select to edit this component." : warning.c_str();
            row.actionsW = ports.empty() ? 0 : ImGui::GetFrameHeight();
            ImGui::PushID(jstr(component, "id").c_str());
            if (kit::row("component", title.c_str(), row)) {
                selected_ = i;
                selectedHose_ = -1;
                focusEditor_ = true;
            }
            IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), title.c_str(), GImGui->LastItemData.StatusFlags);
            if (!ports.empty()) {
                const ImVec2 end = ImGui::GetItemRectMax();
                const ImVec2 saved = ImGui::GetCursorScreenPos();
                ImGui::SetCursorScreenPos(ImVec2(end.x - ImGui::GetFrameHeight() - kit::padX(),
                                                 end.y - ImGui::GetFrameHeight() - kit::padY()));
                kit::chip("", kit::Tone::Warn, true, warning.c_str());
                ImGui::SetCursorScreenPos(saved);
            }
            ImGui::PopID();
        }
        if (comps.empty()) kit::dimText("Place the first component here or drag it onto the schematic.");
        if (kit::secondaryButton(addOpen_ ? "Hide palette###add_component" : "Add component###add_component"))
            addOpen_ = !addOpen_;
    }
    kit::cardEnd();
    if (addOpen_) {
        kit::gapRow();
        drawAddComponent(st);
    }
    kit::gapRow();
    if (selectedHose_ >= 0) drawHoses(st, selectedHose_);
    else if (selected_ >= 0 && selected_ < (int)comps.size()) drawComponentEditor(st);
    else if (!comps.empty()) kit::dimText("Select a component or hose here or on the schematic to edit it.");

    const auto& hoses = jarr(circuit_, "hoses");
    if (kit::cardBegin("hose_list", "Hoses", "sch_wire", &hosesOpen_)) {
        if (hoses.empty()) kit::dimText("Click two ports on the schematic to make a hose.");
        for (int i = 0; i < (int)hoses.size(); ++i) {
            const std::string title = jstr(hoses[i], "net_name", "Hose " + std::to_string(i + 1));
            kit::RowStyle row;
            row.icon = "sch_wire";
            row.selected = i == selectedHose_;
            row.tooltip = "Select to edit hose length, diameter and rating.";
            ImGui::PushID(jstr(hoses[i], "id").c_str());
            if (kit::row("hose", title.c_str(), row)) {
                selected_ = -1;
                selectedHose_ = i;
                focusHose_ = true;
            }
            IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), title.c_str(), GImGui->LastItemData.StatusFlags);
            ImGui::PopID();
        }
    }
    kit::cardEnd();
    if (kit::cardBegin("manual_connection", "Connect manually", "sch_wire", &connectOpen_))
        drawConnect(st);
    kit::cardEnd();
}

void HydraulicPanel::drawLayout3d(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    if (!circuit_.is_object()) return;
    if (kit::cardBegin("hyd_layout3d", "3D layout", "study_hydraulics")) {
        if (kit::secondaryButton(placing3d_ ? "Stop placing###place3d" : "Place in 3D###place3d",
                                 placing3d_ || (selected_ >= 0 && !busy()),
                                 "Select a component to place it on the working plane."))
            armPlacement3d(!placing3d_);
        if (kit::secondaryButton("Frame layout###frame3d")) {
            setCentralView(CentralView::View3D);
            frame3d_ = true;
        }
        kit::FieldGroup g("hyd_place", "Working plane");
        static const char* const planes[] = {"XY", "XZ", "YZ"};
        int plane = (int)placementPlane;
        if (kit::fieldEnum("plane", "Working plane", &plane, planes, 3))
            placementPlane = (simscene::Plane)plane;
        kit::fieldNumber("offset", "Plane offset", &placementOffsetMm, fieldU("mm", us));
        kit::fieldNumber("grid", "Grid spacing", &placementGridMm, fieldU("mm", us));
        kit::fieldCheckbox("snap", "Snap to grid", &placementSnap);
        kit::dimText("Select a component, then click its position on the plane. Tab switches plane; Esc or Enter finishes.");
    }
    kit::cardEnd();
    kit::gapRow();
}

void HydraulicPanel::drawComponentEditor(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    json& comp = circuit_["components"][selected_];
    const std::string id = jstr(comp, "id");
    const std::string type = jstr(comp, "type");
    if (focusEditor_) {
        ImGui::SetScrollHereY(0.1f);
        focusEditor_ = false;
    }
    json& params = comp["params"];
    if (!params.is_object()) {
        kit::inlineStatus("This component's parameters are not a JSON object. Fix them below before editing fields.",
                          kit::Tone::Err);
        json edited;
        if (drawNativeJson(("component_params_" + id).c_str(), "Advanced parameters", params, edited))
            setComponentParams(st, id, edited);
        return;
    }
    const TypeDef* def = typeDef(type);
    // One field group per card section; the params group lines up on its widest label.
    const char* widest = "Position X";
    if (def)
        for (const auto& pd : def->params)
            if (ImGui::CalcTextSize(pd.label).x > ImGui::CalcTextSize(widest).x) widest = pd.label;
    // Scoped by the component's stable id: a field draft never follows the selection elsewhere.
    ImGui::PushID(id.c_str());
    bool removed = false;
    const std::string title = componentName(id);
    if (kit::removableCard("editor", title.c_str(), "settings", nullptr,
                           "Remove this component and its hose ends", &removed)) {
        bool changed = false;
        {
            kit::FieldGroup g("ident", widest);
            std::string name = jstr(comp, "name");
            if (kit::fieldText("name", "Name", &name,
                               kit::FieldOpts().withHint(typeLabel(type).c_str()).withTip("Shown on the schematic and in the BOM."))) {
                comp["name"] = name;
                circuitChanged(st);
            }
            kit::fieldReadout("type", "Type", typeLabel(type).c_str());
            double pos[2] = {0.0, 0.0};
            const json& pj = comp.value("position", json());
            if (pj.is_array() && pj.size() == 2 && pj[0].is_number() && pj[1].is_number()) {
                pos[0] = pj[0].get<double>();
                pos[1] = pj[1].get<double>();
            }
            const kit::FieldOpts po =
                fieldU("mm", us).withTip("Schematic placement; drag the symbol to move it.");
            const bool mx = kit::fieldNumber("pos_x", "Position X", &pos[0], po);
            const bool my = kit::fieldNumber("pos_y", "Position Y", &pos[1], po);
            if (mx || my) {
                comp["position"] = json::array({pos[0], pos[1]});
                commitCircuitMutation(st, false);
            }
        }
        if (kit::secondaryButton("Place in 3D###component_place3d", !busy())) armPlacement3d(true);
        {
            kit::FieldGroup g("layout3d", "3D position X");
            float xyz[3]; componentPosition3d(comp, xyz);
            double p3[3] = {xyz[0], xyz[1], xyz[2]};
            const auto opts = fieldU("mm", us);
            bool moved = kit::fieldNumber("x", "3D position X", &p3[0], opts);
            moved |= kit::fieldNumber("y", "3D position Y", &p3[1], opts);
            moved |= kit::fieldNumber("z", "3D position Z", &p3[2], opts);
            if (moved) {
                for (int k = 0; k < 3; ++k) xyz[k] = (float)p3[k];
                moveComponent3d(st, id, xyz);
            }
        }
        if (def) {
            kit::FieldGroup g("params", widest);
            for (const auto& pd : def->params) {
                kit::FieldOpts o;
                o.tip = pd.tip;
                if (!pd.choices.empty()) {
                    std::vector<std::string> shown;
                    std::vector<const char*> ptrs;
                    for (const char* ch : pd.choices) shown.push_back(prettyChoice(ch));
                    for (const auto& sh : shown) ptrs.push_back(sh.c_str());
                    const int n = (int)pd.choices.size();
                    int idx = 0;
                    if (pd.intChoice) {
                        const std::string cur = std::to_string((int)jnum(params, pd.key, std::atof(pd.choices[0])));
                        idx = std::max(0, indexOf(pd.choices.data(), n, cur));
                    } else {
                        idx = std::max(0, indexOf(pd.choices.data(), n, jstr(params, pd.key, pd.choices[0])));
                    }
                    if (kit::fieldEnum(pd.key, pd.label, &idx, ptrs.data(), n, o)) {
                        if (pd.intChoice)
                            params[pd.key] = std::atoi(pd.choices[idx]);
                        else
                            params[pd.key] = pd.choices[idx];
                        changed = true;
                    }
                    continue;
                }
                double v = jnum(params, pd.key, pd.optional ? 0.0 : pd.def);
                bool present = !pd.optional || jhas(params, pd.key);
                if (pd.unit) {
                    const Disp d = paramDisp(pd, us);
                    o.unit = d.unit;
                    o.scale = d.scale;
                    o.offset = d.offset;
                }
                if (pd.ratio) {
                    o.unit = kit::unitText(kit::Unit::Percent);
                    o.scale = 100.0;
                    o.minV = 0.0;
                    o.maxV = 1.0;
                }
                if (pd.optional) {
                    o.present = &present;
                    o.hint = "engine default";
                }
                if (kit::fieldNumber(pd.key, pd.label, &v, o)) {
                    if (pd.optional && (!present || v == 0.0))
                        params.erase(pd.key);
                    else
                        params[pd.key] = v;
                    changed = true;
                }
            }
        }
        // Parameters another client (an agent) set that this form has no field for: index-keyed
        // under their own scope, so a key spelled like a field above can never collide with it.
        std::vector<std::string> extra;
        for (auto it = params.begin(); it != params.end(); ++it) {
            bool known = false;
            if (def)
                for (const auto& pd : def->params) known = known || it.key() == pd.key;
            if (!known) extra.push_back(it.key());
        }
        if (!extra.empty()) {
            kit::sectionHeader("Other parameters", nullptr, nullptr, (int)extra.size());
            kit::FieldGroup g("extra");
            for (int k = 0; k < (int)extra.size(); ++k) {
                ImGui::PushID(k);
                json& pv = params[extra[k]];
                if (pv.is_number()) {
                    double v = pv.get<double>();
                    if (kit::fieldNumber("v", extra[k].c_str(), &v)) {
                        pv = v;
                        changed = true;
                    }
                } else {
                    kit::fieldReadout("v", extra[k].c_str(), pv.is_string() ? pv.get<std::string>().c_str() : pv.dump().c_str());
                }
                ImGui::PopID();
            }
        }
        if (changed) circuitChanged(st);
        json edited;
        if (drawNativeJson(("component_params_" + id).c_str(), "Advanced parameters", params, edited))
            setComponentParams(st, id, edited);

        // Port connection state: one chip per port, open required ports in Warn.
        kit::chipFlowBegin("ports");
        for (const auto& port : portsOf(comp)) {
            bool on = false;
            for (const auto& h : jarr(circuit_, "hoses"))
                for (const auto& c : jarr(h, "connections"))
                    on = on || (jstr(c, "component_id") == id && jstr(c, "port") == port);
            const bool optional = portOptional(type, port);
            kit::flowValueChip(("Port " + port).c_str(), on ? "connected" : optional ? "optional" : "open",
                               on ? kit::Tone::Ok : optional ? kit::Tone::Neutral : kit::Tone::Warn,
                               on || optional ? nullptr : "Connect this port on the schematic or under Connect manually.");
        }
        kit::chipFlowEnd();

        if (type == "cylinder_linear" && kit::secondaryButton("Size this cylinder###size_cyl")) {
            setCylinderInputs(id, jnum(params, "bore_mm", cylBore_), jnum(params, "rod_mm", cylRod_),
                              reliefPressureBar() > 0.0 ? reliefPressureBar() : cylPressure_,
                              jnum(params, "stroke_mm", cylStroke_), cylVelocity_);
            handleCommand(st, "sim_hyd_size_cyl");
        }
        if (type == "pump") {
            if (kit::secondaryButton("Size this pump###size_pump")) {
                pumpTarget_ = id;
                pumpSpeed_ = jnum(params, "speed_rpm", pumpSpeed_);
                if (reliefPressureBar() > 0.0) pumpPressure_ = reliefPressureBar();
                handleCommand(st, "sim_hyd_size_pump");
            }
            ImGui::SameLine();
            if (kit::secondaryButton("Size its drive###size_drive")) {
                setDriveFromPump(id);
                handleCommand(st, "sim_hyd_drive");
            }
        }
        if (type == "motor_hydraulic" && kit::secondaryButton("Size this motor###size_motor")) {
            motorTarget_ = id;
            if (reliefPressureBar() > 0.0) motPressure_ = reliefPressureBar() * 0.9;
            handleCommand(st, "sim_hyd_size_motor");
        }
    }
    kit::cardEnd();
    ImGui::PopID();
    if (!removed) return;
    if (store_.inflight() > 0)
        setStatus("Wait for the running request to finish, then remove the component.", 2);
    else
        removeComponent(st, selected_);
}

void HydraulicPanel::drawAddComponent(AppState& st) {
    // Palette drops use the canvas transform, while the Add action honors these exact inputs.
    if (kit::cardBegin("add", "Add component", "insert_component")) {
        const auto& defs = typeDefs();
        static const std::vector<std::string> paletteLabels = [] {
            std::vector<std::string> labels;
            const auto& types = typeDefs();
            labels.reserve(types.size());
            for (int i = 0; i < (int)types.size(); ++i)
                labels.push_back(std::string(types[i].label) + "###palette_" + std::to_string(i));
            return labels;
        }();
        kit::dimText("Drag a type onto the central schematic, or define its exact placement below.");
        kit::chipFlowBegin("palette");
        for (int i = 0; i < (int)defs.size(); ++i) {
            kit::chipFlowItem(ImGui::CalcTextSize(defs[i].label).x + 2.0f * ImGui::GetStyle().FramePadding.x);
            if (kit::secondaryButton(paletteLabels[i].c_str(), !busy())) addType_ = i;
            if (!busy() && ImGui::BeginDragDropSource()) {
                const json params = i == addParamsType_ ? addParams_ : defaultParams(defs[i]);
                const std::string payload = json{{"doc_id", store_.activeDocId(st)}, {"study_id", slot_.id},
                                                {"circuit_name", jstr(circuit_, "name")},
                                                {"circuit_generation", circuitGeneration_},
                                                {"type", defs[i].type}, {"name", addName_},
                                                {"params", params}}.dump();
                ImGui::SetDragDropPayload(kHydraulicComponentPayload, payload.c_str(), payload.size() + 1, ImGuiCond_Once);
                kit::dimText(defs[i].label);
                ImGui::EndDragDropSource();
            }
        }
        kit::chipFlowEnd();
        std::vector<const char*> labels;
        for (const auto& d : typeDefs()) labels.push_back(d.label);
        addType_ = std::clamp(addType_, 0, (int)labels.size() - 1);
        kit::FieldGroup g("add", "Type (ISO 1219)");
        kit::fieldEnum("type", "Type (ISO 1219)", &addType_, labels.data(), (int)labels.size(),
                       kit::FieldOpts().withTip("Select the ISO 1219 component type. Drag its palette tile to place it."));
        // The placeholder reads as a default, not as a greyed-out value.
        const std::string hint = std::string(labels[addType_]) + " (default)";
        kit::fieldText("name", "Name", &addName_,
                       kit::FieldOpts().withHint(hint.c_str()).withTip("Optional; defaults to the type."));
        const bool us = SimManager::unitSys(st).us;
        kit::fieldNumber("x", "Position X", &addX_, fieldU("mm", us));
        kit::fieldNumber("y", "Position Y", &addY_, fieldU("mm", us));
        if (addParamsType_ != addType_) {
            addParams_ = defaultParams(typeDefs()[addType_]);
            addParamsType_ = addType_;
        }
        json edited;
        if (drawNativeJson("add_params", "Advanced parameters", addParams_, edited)) {
            std::string error;
            if (validComponentParams(typeDefs()[addType_].type, edited, error)) addParams_ = std::move(edited);
            else setStatus(error, 3);
        }
        if (kit::secondaryButton("Add selected component###add_selected", !busy()))
            addPickedComponent(st);
    }
    kit::cardEnd();
}

void HydraulicPanel::drawHoses(AppState& st, int onlyIndex) {
    json& hoses = circuit_["hoses"];  // an array (syncForDraw)
    const bool us = SimManager::unitSys(st).us;
    if (hoses.empty()) {
        kit::emptyState("sch_wire", "No hoses yet",
                        "Click two ports on the schematic, or use Connect manually.");
        return;
    }
    const ui::Palette& pal = ui::palette;
    int removeIdx = -1;
    bool geomEdited = false;
    const char* kVolumeTip = "Sets the line's oil volume in the simulation and its pressure drop in the piping handoff.";
    // One removable card per hose: its ends as chips, then length, bore and rating. The selected
    // hose (highlighted on the schematic) carries an accent outline.
    for (int i = 0; i < (int)hoses.size(); ++i) {
        if (onlyIndex >= 0 && i != onlyIndex) continue;
        json& h = hoses[i];
        const bool sel = i == selectedHose_;
        if (sel && focusHose_) {
            ImGui::SetScrollHereY(0.2f);
            focusHose_ = false;
        }
        const std::string net = jstr(h, "net_name");
        const std::string title = net.empty() ? "Hose " + std::to_string(i + 1) : net;
        ImGui::PushID(i);
        bool rm = false;
        if (kit::removableCard("hose", title.c_str(), "sch_wire", nullptr, "Remove this hose", &rm)) {
            kit::chipFlowBegin("ends");
            for (const auto& c : jarr(h, "connections"))
                kit::flowChip((componentName(jstr(c, "component_id")) + " \xc2\xb7 " + jstr(c, "port")).c_str());
            kit::chipFlowEnd();
            {
                kit::FieldGroup g("geom", "Inside diameter");
                double len = jnum(h, "length_mm", 1000.0);
                if (kit::fieldNumber("len", "Length", &len,
                                     fieldU("mm", us).withMin(0.0).withTip(kVolumeTip))) {
                    h["length_mm"] = len;
                    geomEdited = true;
                }
                double bore = jnum(h, "id_mm", 12.7);
                if (kit::fieldNumber("bore", "Inside diameter", &bore,
                                     fieldU("mm", us).withMin(0.1).withTip(us ?
                                         "Hose inside diameter (a -8 hose is 1/2 in, a -12 is 3/4 in). It sets the "
                                         "line's oil volume in the simulation and its pressure drop in the piping handoff." :
                                         "Hose inside diameter (1/2\" = 12.7 mm, 3/4\" = 19.0 mm). It sets the line's "
                                         "oil volume in the simulation and its pressure drop in the piping handoff."))) {
                    h["id_mm"] = bore;
                    geomEdited = true;
                }
                bool rated = jhas(h, "rating_bar");
                double rating = jnum(h, "rating_bar", 0.0);
                if (kit::fieldNumber("rating", "Rating", &rating,
                                     fieldU("bar", us).withMin(0.0).withPresent(&rated).withHint(
                                         "not rated").withTip("Working-pressure rating of the hose."))) {
                    if (rated && rating > 0.0)
                        h["rating_bar"] = rating;
                    else
                        h.erase("rating_bar");
                    geomEdited = true;
                }
            }
            json edited;
            const std::string hoseId = jstr(h, "id");
            if (drawNativeJson(("hose_" + hoseId).c_str(), "Advanced hose parameters", h, edited)) {
                std::string error;
                configureHose(st, hoseId, edited, error);
            }
            // Clicking the card selects the hose (highlighted on the schematic).
            if (ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) && ImGui::IsMouseClicked(ImGuiMouseButton_Left))
                selectedHose_ = i;
        }
        kit::cardEnd();
        if (sel)  // the card's child window is the last item
            ImGui::GetWindowDrawList()->AddRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                                                ui::Palette::toU32(pal.accent), kit::rowRound(), 0,
                                                metrics::stroke(1.5f));
        ImGui::PopID();
        if (rm) removeIdx = i;
        kit::gapRow();
    }
    if (geomEdited) circuitChanged(st);
    if (removeIdx >= 0) removeHose(st, removeIdx);
}

void HydraulicPanel::drawConnect(AppState& st) {
    seedConnectDefaults();
    const bool us = SimManager::unitSys(st).us;
    const json& comps = jarr(circuit_, "components");
    if (!endpointComponent_.empty()) {
        const std::string context = componentName(endpointComponent_) + " · " + endpointPort_ +
                                    " selected. Click or drag to the second port.";
        kit::inlineStatus(context.c_str(), kit::Tone::Info);
        if (kit::secondaryButton("Cancel endpoint###cancel_endpoint")) {
            endpointComponent_.clear();
            endpointPort_.clear();
            ends_.clear();
            seedConnectDefaults();
        }
    }
    if (comps.size() < 2) {
        kit::emptyState("sch_wire", "Nothing to connect yet",
                        "Add at least two components, then join their ports here.");
        return;
    }
    // Keyboard/3D connection is self-contained: the button below makes the hose.
    std::vector<std::string> compItems;
    for (const auto& c : comps) compItems.push_back(componentName(jstr(c, "id")) + " \xc2\xb7 " + typeLabel(jstr(c, "type")));
    if (kit::cardBegin("ends", "Ends", "sch_wire")) {
        // One Component + Port pair per end (ends_ is normalised by syncForDraw).
        kit::FieldGroup g("ends", "End 1");
        for (int i = 0; i < (int)ends_.size(); ++i) {
            End& e = ends_[i];
            ImGui::PushID(i);
            const std::string label = "End " + std::to_string(i + 1);
            int ci = std::clamp(e.comp, 0, (int)comps.size() - 1);
            const std::string preview = componentName(jstr(comps[ci], "id"));
            if (kit::fieldCombo("comp", label.c_str(), &ci, compItems, kit::FieldOpts().withPreview(preview.c_str()))) {
                e.comp = ci;
                e.port = 0;
            }
            const std::vector<std::string> ports = portsOf(comps[ci]);
            std::vector<const char*> portPtrs;
            for (const auto& p : ports) portPtrs.push_back(p.c_str());
            // Always a combo: every end's Port row looks the same whatever its port count.
            kit::fieldEnum("port", "Port", &e.port, portPtrs.data(), (int)portPtrs.size(),
                           kit::FieldOpts().withEnumStyle(kit::EnumStyle::Combo).withTip("The port this hose end attaches to."));
            ImGui::PopID();
        }
    }
    if (kit::secondaryButton("+ Tee branch###add_end")) ends_.push_back(End{0, 0});
    kit::itemTooltip("Add a third (or further) end: the hose becomes a tee and all its ports share one pressure node.");
    if (ends_.size() > 2) {
        ImGui::SameLine();
        if (kit::secondaryButton("Remove branch###remove_end")) ends_.pop_back();
    }
    kit::cardEnd();
    kit::gapRow();
    if (kit::cardBegin("new_hose", "Hose", "sch_wire")) {
        kit::FieldGroup g("hose", "Inside diameter");
        kit::fieldText("net", "Name", &hoseNet_,
                       kit::FieldOpts().withHint("From its ends").withTip(
                           "Optional label shown on the schematic and the plots, e.g. \"Pressure line\"."));
        kit::fieldNumber("len", "Length", &hoseLengthMm_,
                         fieldU("mm", us).withMin(0.0).withTip(
                             "Sets the line's oil volume in the simulation and its pressure drop in the piping handoff."));
        kit::fieldNumber("bore", "Inside diameter", &hoseIdMm_,
                         fieldU("mm", us).withMin(0.1).withTip(us ?
                             "Hose inside diameter: a -8 hose is 1/2 in, a -12 is 3/4 in." :
                             "Hose inside diameter: 1/2\" = 12.7 mm, 3/4\" = 19.0 mm."));
        kit::fieldNumber("rating", "Rating", &hoseRatingBar_,
                         fieldU("bar", us).withMin(0.0).withTip(
                             us ? "Hose working-pressure rating (SAE 100R2AT 1/2\" = 4,000 psi)."
                                : "Hose working-pressure rating (SAE 100R2AT 1/2\" = 275 bar)."));
    }
    kit::cardEnd();
    const std::string blocker = connectBlocker();
    if (kit::secondaryButton("Connect ports###connect_manual", blocker.empty(),
                             blocker.empty() ? nullptr : blocker.c_str()))
        connectEnds(st);
    if (!blocker.empty()) kit::inlineStatus(blocker.c_str(), kit::Tone::Warn);
}

// ---- sizing ------------------------------------------------------------------------------------

void HydraulicPanel::drawSizingForm(AppState& st) {
    // The Cylinder / Pump / Motor / Drive switch is pinned under the form tabs (drawSubPages).
    switch (sizeKind_) {
        case SizeKind::Cylinder: drawCylinderSizing(st); break;
        case SizeKind::Pump: drawPumpSizing(st); break;
        case SizeKind::Motor: drawMotorSizing(st); break;
        case SizeKind::Drive: drawDriveSizing(st); break;
    }
}

namespace {
// Field picking a circuit component of `type` (or "(not linked)") into `target`; true on change.
// A circuit that has a component of this type links it automatically (the first one), so Design
// never claims "not linked" while the schematic shows the pump; choosing "(not linked)" by hand
// is remembered for this field. An unlinked sizing says so inline: its result is a calculation.
// The combo sits in its OWN field group: its text width must not stack the numeric fields
// (each sizing card keeps one label-left layout for its numbers).
bool targetField(const char* id, const char* label, const json& circuit, const char* type, std::string& target) {
    std::vector<std::string> ids = {""};
    std::vector<std::string> items = {"(not linked)"};
    for (const auto& c : jarr(circuit, "components"))
        if (jstr(c, "type") == type) {
            ids.push_back(jstr(c, "id"));
            items.push_back(jstr(c, "name", typeLabel(type)));
        }
    kit::FieldGroup g(id);
    ImGuiStorage* storage = ImGui::GetStateStorage();
    const ImGuiID unlinkedKey = ImGui::GetID("##user_unlinked");
    int idx = 0;
    for (int i = 0; i < (int)ids.size(); ++i)
        if (ids[i] == target) idx = i;
    bool changed = false;
    if (idx == 0 && ids.size() > 1 && (!target.empty() || !storage->GetBool(unlinkedKey, false))) {
        idx = 1;  // stale or never-chosen target: link the circuit's component
        target = ids[idx];
        changed = true;
    }
    if (kit::fieldCombo(id, label, &idx, items,
                        kit::FieldOpts().withTip(
                            "The circuit component this sizing belongs to; Apply writes the result into it."))) {
        target = ids[idx];
        storage->SetBool(unlinkedKey, idx == 0);
        changed = true;
    }
    if (target.empty()) kit::inlineStatus("Not linked: the result is a calculation only.", kit::Tone::Info);
    return changed;
}
// ISO 3320 preferred bores and rods (mm).
const double kIsoBores[] = {25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 320, 400, 500};
const double kIsoRods[] = {12, 14, 18, 22, 28, 36, 45, 56, 70, 90, 110, 140, 180, 220, 280, 360};
kit::FieldOpts ratioOpts(const char* tip) {
    return kit::FieldOpts().withUnit(kit::Unit::Percent).withScale(100.0).withRange(0.01, 1.0).withTip(tip);
}
}  // namespace

void HydraulicPanel::drawCylinderSizing(AppState& st) {
    const char* kWidest = "Working pressure";
    const bool us = SimManager::unitSys(st).us;
    if (kit::cardBegin("cyl_in", "Cylinder", "size_actuator")) {
        if (targetField("target", "Component", circuit_, "cylinder_linear", cylTarget_)) {
            boreNote_.clear();
            const int i = componentIndex(cylTarget_);
            if (i >= 0) {
                const json& p = jobj(jarr(circuit_, "components")[i], "params");
                cylBore_ = jnum(p, "bore_mm", cylBore_);
                cylRod_ = jnum(p, "rod_mm", cylRod_);
                cylStroke_ = jnum(p, "stroke_mm", cylStroke_);
            }
        }
        {
            kit::FieldGroup g("cyl_force", kWidest);
            kit::fieldNumber("force", "Required force", &cylForceKn_,
                             fieldU("kN", us).withMin(0.0).withTip(
                                 "Force the cylinder must push at the working pressure."));
        }
        // Belongs to the force above: full width, its outcome inline under it.
        if (kit::secondaryButton("Suggest bore from force###bore_from_force", true, nullptr, ImVec2(-FLT_MIN, 0))) {
            const double area = cylForceKn_ * 1e3 / (std::max(1e-6, cylPressure_) * 1e5 * std::max(0.05, cylEff_));
            const double bore = std::sqrt(4.0 * area / 3.14159265358979) * 1000.0;
            double pick = kIsoBores[13];
            for (double b : kIsoBores)
                if (b >= bore) {
                    pick = b;
                    break;
                }
            cylBore_ = pick;
            // ISO 3320 rod near 0.57 x bore (area ratio about 1.5).
            double rod = kIsoRods[0];
            for (double r : kIsoRods)
                if (r <= 0.6 * pick) rod = r;
            cylRod_ = rod;
            boreNote_ = "ISO 3320 bore " + fmtU(pick, "mm", us) + " (needs " + fmtU(bore, "mm", us, 3) +
                        "), rod " + fmtU(rod, "mm", us) + ". Size it to confirm.";
        }
        kit::itemTooltip("Pick the smallest ISO 3320 bore that reaches the force at this pressure and efficiency.");
        kit::inlineStatus(boreNote_.c_str(), kit::Tone::Info);
        {
            kit::FieldGroup g("cyl_geom", kWidest);
            const kit::FieldOpts mm = fieldU("mm", us).withMin(0.0);
            if (kit::fieldNumber("bore", "Bore", &cylBore_, mm)) boreNote_.clear();
            if (kit::fieldNumber("rod", "Rod", &cylRod_, kit::FieldOpts(mm).withTip("0 for single-acting / rodless.")))
                boreNote_.clear();
            kit::fieldNumber("pressure", "Working pressure", &cylPressure_,
                             fieldU("bar", us).withMin(0.0));
            kit::fieldNumber("stroke", "Stroke", &cylStroke_, mm);
            kit::fieldNumber("speed", "Extend speed", &cylVelocity_,
                             fieldU("mm/s", us).withMin(0.0));
            kit::fieldNumber("eff", "Efficiency", &cylEff_, ratioOpts("Force efficiency (seal friction), typically 90-95 %."));
        }
    }
    kit::cardEnd();
    // Every sizing tab: inputs first, then the result with its Apply / next-step actions (a fresh
    // result scrolls into view once).
    if (cylRes_.is_object() && !cylRes_.empty()) {
        kit::gapRow();
        revealHere(Reveal::Cylinder);
        const kit::Tone forceTone =
            jnum(cylRes_, "extend_force_kn") + 1e-9 >= cylForceKn_ ? kit::Tone::Ok : kit::Tone::Warn;
        kit::keyValueCard("cyl_res", "Cylinder sizing", "results",
                          {{"Extend force", qtyU(cylRes_, "extend_force_kn", "kN", us), forceTone},
                           {"Retract force", qtyU(cylRes_, "retract_force_kn", "kN", us)},
                           {"Extend flow", qtyU(cylRes_, "flow_extend_lpm", "L/min", us)},
                           {"Retract flow", qtyU(cylRes_, "flow_retract_lpm", "L/min", us)},
                           {"Stroke time", qty(cylRes_, "cycle_time_s", "s")},
                           {"Bore area", qtyU(cylRes_, "area_bore_cm2", "cm2", us)},
                           {"Annulus area", qtyU(cylRes_, "area_annulus_cm2", "cm2", us)}});
        warningsList(cylRes_);
        const bool busy = store_.inflight() > 0;
        if (kit::secondaryButton("Apply to component###cyl_apply", componentIndex(cylTarget_) >= 0 && !busy,
                                 "Link the sizing to a cylinder in the circuit first."))
            applyCylinderSizing(st);
        ImGui::SameLine();
        if (kit::secondaryButton("Size the pump###cyl_to_pump")) {
            pumpFlow_ = jnum(cylRes_, "flow_extend_lpm", pumpFlow_);
            pumpPressure_ = cylPressure_;
            setSizeKind(SizeKind::Pump);
        }
        kit::itemTooltip("Carry this cylinder's extend flow and working pressure to the pump sizing.");
    }
}

void HydraulicPanel::drawPumpSizing(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    if (kit::cardBegin("pump_in", "Pump", "size_pump")) {
        if (targetField("target", "Component", circuit_, "pump", pumpTarget_)) {
            const int i = componentIndex(pumpTarget_);
            if (i >= 0) pumpSpeed_ = jnum(jobj(jarr(circuit_, "components")[i], "params"), "speed_rpm", pumpSpeed_);
        }
        kit::FieldGroup g("pump", "Efficiency");
        kit::fieldNumber("flow", "Flow", &pumpFlow_, fieldU("L/min", us).withMin(0.0));
        kit::fieldNumber("pressure", "Pressure", &pumpPressure_, fieldU("bar", us).withMin(0.0));
        kit::fieldNumber("eff", "Efficiency", &pumpEff_, ratioOpts("Overall pump efficiency at the duty point."));
        kit::fieldNumber("speed", "Speed", &pumpSpeed_, kit::FieldOpts().withUnit(kit::Unit::rpm).withMin(0.0));
    }
    kit::cardEnd();
    if (pumpRes_.is_object() && !pumpRes_.empty()) {
        kit::gapRow();
        revealHere(Reveal::Pump);
        kit::keyValueCard("pump_res", "Pump sizing", "results",
                          {{"Displacement", qty(pumpRes_, "displacement_cc_rev", "cc/rev")},
                           {"Shaft power", powerQty(pumpRes_, "shaft_power_kw", us)},
                           {"Hydraulic power", powerQty(pumpRes_, "hydraulic_power_kw", us)},
                           {"Shaft torque", qtyU(pumpRes_, "torque_nm", "N m", us)}});
        warningsList(pumpRes_);
        const bool busy = store_.inflight() > 0;
        const int i = componentIndex(pumpTarget_);
        if (kit::secondaryButton("Apply to component###pump_apply", i >= 0 && !busy,
                                 "Link the sizing to a pump in the circuit first.")) {
            json& p = circuit_["components"][i]["params"];
            if (!p.is_object()) p = json::object();
            p["displacement_cc_rev"] = std::ceil(jnum(pumpRes_, "displacement_cc_rev") * 10.0) / 10.0;
            p["speed_rpm"] = jnum(pumpRes_, "speed_rpm", pumpSpeed_);
            circuitChanged(st);
            setStatus("Displacement and speed applied to " + componentName(pumpTarget_) + ".", 1);
        }
        ImGui::SameLine();
        if (kit::secondaryButton("Size its drive###pump_to_drive")) {
            driveTarget_ = pumpTarget_;
            drive_.kind = 0;
            drive_.flowLpm = pumpFlow_;
            drive_.pressureBar = pumpPressure_;
            drive_.speedRpm = pumpSpeed_;
            drive_.pumpEff = pumpEff_;
            setSizeKind(SizeKind::Drive);
        }
        kit::itemTooltip("Carry this duty point to the electric drive sizing.");
    }
}

void HydraulicPanel::drawMotorSizing(AppState& st) {
    // Labels short enough for the inline layout the Cylinder and Pump cards use (the card never
    // flips to stacked fields between tabs); the tips carry the full terms.
    const bool us = SimManager::unitSys(st).us;
    if (kit::cardBegin("motor_in", "Hydraulic motor", "size_motor")) {
        targetField("target", "Component", circuit_, "motor_hydraulic", motorTarget_);
        kit::FieldGroup g("motor", "Torque efficiency");
        kit::fieldNumber("torque", "Output torque", &motTorque_, fieldU("N m", us).withMin(0.0));
        kit::fieldNumber("speed", "Output speed", &motSpeed_, kit::FieldOpts().withUnit(kit::Unit::rpm).withMin(0.0));
        kit::fieldNumber("pressure", "Pressure drop", &motPressure_,
                         fieldU("bar", us).withMin(0.0).withTip(
                             "Differential pressure available across the motor ports."));
        kit::fieldNumber("mech", "Torque efficiency", &motMech_, ratioOpts("Mechanical (torque) efficiency of the motor."));
        kit::fieldNumber("vol", "Flow efficiency", &motVol_, ratioOpts("Volumetric (leakage) efficiency of the motor."));
        kit::fieldEnum("type", "Motor type", &motType_, kMotorTypes, countOf(kMotorTypes),
                       kit::FieldOpts().withTip("Selects the catalog displacement series."));
    }
    kit::cardEnd();
    if (motorRes_.is_object() && !motorRes_.empty()) {
        kit::gapRow();
        revealHere(Reveal::Motor);
        kit::keyValueCard("motor_res", "Motor sizing", "results",
                          {{"Catalog displacement", qty(motorRes_, "standard_displacement_cc_rev", "cc/rev"), kit::Tone::Ok},
                           {"Displacement", qty(motorRes_, "displacement_cc_rev", "cc/rev")},
                           {"Flow", qtyU(motorRes_, "flow_lpm", "L/min", us)},
                           {"Output power", powerQty(motorRes_, "output_power_kw", us)},
                           {"Hydraulic input", powerQty(motorRes_, "input_hydraulic_power_kw", us)}});
        warningsList(motorRes_);
        const bool busy = store_.inflight() > 0;
        const int i = componentIndex(motorTarget_);
        if (kit::secondaryButton("Apply to component###motor_apply", i >= 0 && !busy,
                                 "Link the sizing to a motor in the circuit first.")) {
            json& p = circuit_["components"][i]["params"];
            if (!p.is_object()) p = json::object();
            p["displacement_cc_rev"] = jhas(motorRes_, "standard_displacement_cc_rev")
                                           ? jnum(motorRes_, "standard_displacement_cc_rev")
                                           : jnum(motorRes_, "displacement_cc_rev");
            circuitChanged(st);
            setStatus("Catalog displacement applied to " + componentName(motorTarget_) + ".", 1);
        }
    }
}

void HydraulicPanel::drawDriveSizing(AppState& st) {
    // A hydraulic pump's duty point can come from a circuit pump; the shared pump-drive form owns
    // every other input (the same widget as Piping > Solve > Pump drive).
    if (drive_.kind == 0) {
        if (kit::cardBegin("drv_pump", "Circuit pump", "pump_drive")) {
            if (targetField("target", "Pump", circuit_, "pump", driveTarget_) && !driveTarget_.empty())
                setDriveFromPump(driveTarget_);
        }
        kit::cardEnd();
        kit::gapRow();
    }
    // Mains supply follows the pane's units until the user picks one.
    const bool us = SimManager::unitSys(st).us;
    sim::defaultSupplyForUnits(drive_, us);
    sim::drawPumpDriveInputs("drive", drive_, true, us);
    if (driveRes_.is_object() && !driveRes_.empty()) {
        kit::gapRow();
        revealHere(Reveal::Drive);
        sim::drawPumpDriveResult("drive_res", driveRes_, "Drive sizing", us);
        const bool busy = store_.inflight() > 0;
        const int i = componentIndex(driveTarget_);
        if (drive_.kind == 0 &&
            kit::secondaryButton("Apply to pump###drive_apply", i >= 0 && !busy, "Link the drive to a pump in the circuit first.")) {
            json& p = circuit_["components"][i]["params"];
            if (!p.is_object()) p = json::object();
            if (jhas(driveRes_, "motor_rating_kw")) p["motor_rating_kw"] = jnum(driveRes_, "motor_rating_kw");
            if (jhas(driveRes_, "motor_rating_hp")) p["motor_rating_hp"] = jnum(driveRes_, "motor_rating_hp");
            if (jhas(driveRes_, "poles")) p["motor_poles"] = (int)jnum(driveRes_, "poles");
            const std::string frame = jstr(driveRes_, "iec_frame", jstr(driveRes_, "nema_frame"));
            if (!frame.empty()) p["motor_frame"] = frame;
            p["motor_efficiency_class"] = kEffClasses[std::clamp(drive_.effClass, 0, countOf(kEffClasses) - 1)];
            persist(st);  // drive data is sourcing metadata: the simulation is unaffected
            setStatus("Motor rating, poles and frame applied to " + componentName(driveTarget_) + ".", 1);
        }
    }
}

// ---- simulate ----------------------------------------------------------------------------------

void HydraulicPanel::drawSimulateForm(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    const json& comps = jarr(circuit_, "components");
    const std::string blocker = simulateBlocker();
    if (focusSimulationBlocker_) {
        ImGui::SetScrollY(0);
        focusSimulationBlocker_ = false;
    }
    if (!blocker.empty()) {
        kit::inlineStatus(blocker.c_str(), kit::Tone::Warn);
        if (comps.empty() || circuitNextStep() == NextStep::Connect ||
            blocker.find("Add a ") == 0) {
            if (kit::secondaryButton(comps.empty() ? "Choose a circuit###fix_circuit" : "Edit circuit###fix_circuit")) {
                setForm(Form::Circuit);
                if (!comps.empty() && circuitNextStep() == NextStep::Connect) handleCommand(st, "sim_hyd_hose");
            }
        }
    }
    bool setupChanged = false;
    if (kit::cardBegin("run", "Simulation time", "run_study")) {
        kit::FieldGroup g("run", "Output step");
        setupChanged |= kit::fieldNumber("duration", "Duration", &durationS_, kit::FieldOpts().withUnit(kit::Unit::s).withMin(0.001));
        setupChanged |= kit::fieldNumber("dt", "Output step", &outputDtS_,
                         kit::FieldOpts().withUnit(kit::Unit::s).withMin(1e-5).withTip(
                             "Sampling of the returned series; the integrator steps adaptively."));
        bool present = simulationExtra_.contains("max_step_s");
        double maxStep = jnum(simulationExtra_, "max_step_s", 0.01);
        if (kit::fieldNumber("max_step", "Maximum step", &maxStep,
                             kit::FieldOpts().withUnit(kit::Unit::s).withMin(1e-8).withPresent(&present)
                                 .withHint("engine default").withTip("Largest integrator time step, independent of output sampling."))) {
            if (present) simulationExtra_["max_step_s"] = maxStep;
            else simulationExtra_.erase("max_step_s");
            setupChanged = true;
        }
    }
    kit::cardEnd();

    // Valve schedule (rows normalised by syncForDraw: every row names a valve).
    const std::vector<int> valves = componentsOfType("valve_directional");
    kit::sectionHeader("Valve schedule", "flow_arrow", nullptr, (int)schedule_.size());
    if (valves.empty()) {
        kit::dimText("No directional valve in the circuit: the pump runs against the circuit as connected.");
    } else {
        std::vector<std::string> vNames;
        for (int vi : valves) vNames.push_back(componentName(jstr(comps[vi], "id")));
        int removeIdx = -1;
        for (int i = 0; i < (int)schedule_.size(); ++i) {
            ScheduleRow& r = schedule_[i];
            bool rowChanged = false;
            ImGui::PushID(i);
            const std::string title = "Step " + std::to_string(i + 1);
            bool rm = false;
            if (kit::removableCard("step", title.c_str(), "flow_arrow", nullptr, "Remove this step", &rm)) {
                kit::FieldGroup g("step", "Position");
                rowChanged |= kit::fieldNumber("t", "Time", &r.t,
                                 kit::FieldOpts().withUnit(kit::Unit::s).withMin(0.0).withTip(
                                     "When the valve shifts, from the start of the run."));
                int vi = 0;
                for (int k = 0; k < (int)valves.size(); ++k)
                    if (jstr(comps[valves[k]], "id") == r.valve) vi = k;
                if (kit::fieldCombo("valve", "Valve", &vi, vNames)) {
                    r.valve = jstr(comps[valves[vi]], "id");
                    rowChanged = true;
                }
                rowChanged |= kit::fieldEnum("pos", "Position", &r.pos, kPosWords, countOf(kPosWords),
                               kit::FieldOpts().withItemTips(kPosLabels).withTip(
                                   "Side A: P to A, B to T. Side B: P to B, A to T. Center: the valve's center condition."));
                if (r.pos == 3)
                    rowChanged |= kit::fieldSlider("open", "Opening", &r.frac, -1.0, 1.0,
                                     kit::FieldOpts().withScale(100.0).withUnit(kit::Unit::Percent).withTip(
                                         "Proportional spool opening: +100 % = fully to A, -100 % = fully to B."));
            }
            r.edited |= rowChanged;
            setupChanged |= rowChanged;
            kit::cardEnd();
            ImGui::PopID();
            if (rm) removeIdx = i;
            kit::gapRow();
        }
        if (removeIdx >= 0) {
            schedule_.erase(schedule_.begin() + removeIdx);
            setupChanged = true;
        }
        if (kit::secondaryButton("+ Step###add_step")) {
            ScheduleRow r;
            r.t = schedule_.empty() ? 0.0 : schedule_.back().t + 0.5;
            r.valve = schedule_.empty() ? jstr(comps[valves.front()], "id") : schedule_.back().valve;
            r.pos = 2;
            schedule_.push_back(r);
            setupChanged = true;
        }
        kit::itemTooltip("Each step shifts the valve at its time; the spool travels in its response time.");
    }

    // Loads per actuator: one card each, index-scoped.
    kit::sectionHeader("Loads", "load_force");
    bool anyActuator = false;
    for (int i = 0; i < (int)comps.size(); ++i) {
        const json& c = comps[i];
        const std::string type = jstr(c, "type");
        const bool cyl = type == "cylinder_linear";
        const bool rot = type == "motor_hydraulic" || type == "cylinder_rotary";
        if (!cyl && !rot) continue;
        anyActuator = true;
        const std::string id = jstr(c, "id");
        ImGui::PushID(i);
        const std::string title = componentName(id);
        if (kit::cardBegin("load", title.c_str(), cyl ? "cylinder_feature" : "rotate_component")) {
            kit::FieldGroup g("load", "Start position");
            const json& L = jobj(loads_, id.c_str());
            auto loadField = [&](const char* key, const char* label, kit::Unit unit, const char* tip) {
                double v = jnum(L, key, 0.0);
                if (!kit::fieldNumber(key, label, &v, fieldU(kit::unitText(unit), us).withTip(tip))) return;
                setupChanged = true;
                json& dst = loads_[id];
                if (!dst.is_object()) dst = json::object();
                if (v == 0.0)
                    dst.erase(key);
                else
                    dst[key] = v;
            };
            if (cyl) {
                loadField("force_n", "External force", kit::Unit::N, "Load force on the rod; positive opposes extension.");
                loadField("mass_kg", "Moved mass", kit::Unit::kg, "Mass of the driven load.");
                loadField("friction_n", "Load friction", kit::Unit::N, "Coulomb friction of the driven load.");
                double x0 = jnum(jobj(initial_, id.c_str()), "position_mm", 0.0);
                if (kit::fieldNumber("x0", "Start position", &x0,
                                     fieldU("mm", us).withMin(0.0).withTip("Rod extension at t = 0."))) {
                    json& row = initial_[id];
                    if (!row.is_object()) row = json::object();
                    if (x0 == 0.0) row.erase("position_mm");
                    else row["position_mm"] = x0;
                    setupChanged = true;
                }
            } else {
                loadField("torque_nm", "Load torque", kit::Unit::N_m, "Positive opposes A-to-B rotation.");
                loadField("inertia_kgm2", "Load inertia", kit::Unit::kg_m2, "Inertia of the driven load.");
            }
        }
        kit::cardEnd();
        ImGui::PopID();
        kit::gapRow();
    }
    if (!anyActuator) kit::dimText("No actuator in the circuit.");

    if (kit::cardBegin("fluid", "Fluid", "fluid")) {
        kit::FieldGroup g("fluid", "Bulk modulus");
        if (kit::fieldNumber("rho", "Density", &fluidRho_,
                         fieldU("kg/m3", us).withMin(1.0).withTip(us ?
                             "ISO VG32 mineral oil is about 54.3 lb/ft\xc2\xb3." :
                             "ISO VG32 mineral oil is about 870 kg/m\xc2\xb3."))) {
            fluid_["density_kgm3"] = fluidRho_;
            setupChanged = true;
        }
        if (kit::fieldNumber("beta", "Bulk modulus", &fluidBeta_,
                         fieldU("bar", us).withMin(1.0).withTip(
                             "Effective bulk modulus (entrained air lowers it)."))) {
            fluid_["bulk_modulus_bar"] = fluidBeta_;
            setupChanged = true;
        }
        if (kit::fieldNumber("nu", "Viscosity", &fluidCst_,
                             kit::FieldOpts().withUnit(kit::Unit::cSt).withMin(0.01))) {
            fluid_["viscosity_cst"] = fluidCst_;
            setupChanged = true;
        }
        double air = jnum(fluid_, "air_fraction", 0.002);
        if (kit::fieldNumber("air", "Entrained air", &air,
                             kit::FieldOpts().withUnit(kit::Unit::Percent).withScale(100.0).withRange(0.0, 0.199))) {
            fluid_["air_fraction"] = air;
            setupChanged = true;
        }
    }
    kit::cardEnd();
    if (setupChanged) circuitChanged(st);
    if (ImGui::CollapsingHeader("Advanced simulation parameters###hyd_all_sim")) {
        if (simulationJsonDirty_) {
            simulationJsonCache_ = simulationDefinition();
            simulationJsonDirty_ = false;
        }
        json edited;
        if (drawNativeJson("simulation_params", "Advanced simulation parameters", simulationJsonCache_, edited, true)) {
            std::string error;
            if (!configureSimulation(st, edited, error)) jsonDrafts_["simulation_params"].error = error;
        }
    }
}

void HydraulicPanel::drawResultsForm(AppState& st) {
    switch (resultPage_) {
        case ResultPage::Outcome: drawSimResults(st); break;
        case ResultPage::Bom: drawBomForm(st); break;
        case ResultPage::Piping: drawPipingForm(st); break;
    }
}

void HydraulicPanel::drawSimResults(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    if (!sim_.is_object() || sim_.empty()) {
        kit::emptyState("results", "No simulation results yet",
                        "Run the simulation to see pressures, actuator motion and energy");
        return;
    }
    const json& s = jobj(sim_, "summary");
    const double relief = reliefPressureBar();
    const bool overPeak = relief > 0.0 && jnum(s, "peak_pressure_bar") > relief * 1.15;
    const std::size_t nWarn = jarr(sim_, "warnings").size();
    revealHere(Reveal::Simulate);
    // One outcome chip, then the relief verdict; metrics and display controls follow below.
    if (kit::cardBegin("outcome", "Outcome", "plots_view")) {
        kit::chipFlowBegin("facts");
        if (nWarn > 0) {
            // The chip counts the solver's warnings listed further down; it is not a verdict on
            // the relief comparison that follows, so it says where to look.
            const std::string w = sim::countNoun((long long)nWarn, "warning") + " (see below)";
            kit::flowChip(w.c_str(), kit::Tone::Warn, true, "Listed under the result cards below.");
        } else {
            kit::flowChip("Completed", kit::Tone::Ok, true);
        }
        kit::chipFlowEnd();
        kit::Tone tone = kit::Tone::Ok;
        const std::string verdict = simVerdict(s, relief, us, tone);
        kit::inlineStatus(verdict.c_str(), tone);
    }
    kit::cardEnd();
    kit::gapRow();
    kit::keyValueCard("key_metrics", "Key metrics", "performance",
                      {{"Peak pressure", qtyU(s, "peak_pressure_bar", "bar", us, 1.0, 3),
                        overPeak ? kit::Tone::Warn : kit::Tone::Neutral},
                       {"Efficiency", pct(s, "efficiency", 3)},
                       {"Pump energy", qty(s, "pump_energy_kj", "kJ", 1.0, 3)}});
    kit::gapRow();
    // Key metrics: one key/value card per actuator and pump, index-scoped.
    auto resultCards = [&](const char* group, const char* icon, auto&& rows) {
        const json& g = jobj(s, group);
        int i = 0;
        for (auto it = g.begin(); it != g.end(); ++it, ++i) {
            ImGui::PushID(group);
            ImGui::PushID(i);
            kit::keyValueCard("res", componentName(it.key()).c_str(), icon, rows(*it));
            ImGui::PopID();
            ImGui::PopID();
            kit::gapRow();
        }
    };
    resultCards("cylinders", "cylinder_feature", [us](const json& r) {
        return std::vector<kit::KeyValueItem>{
            {"Stroke time", qty(r, "stroke_time_s", "s", 1.0, 3)},
            {"Max speed", qtyU(r, "max_velocity_mms", "mm/s", us, 1.0, 3)},
            {"End reached", jbool(r, "end_reached", false) ? "Yes" : "No",
             jbool(r, "end_reached", false) ? kit::Tone::Ok : kit::Tone::Warn}};
    });
    resultCards("motors", "rotate_component", [us](const json& r) {
        return std::vector<kit::KeyValueItem>{{"Steady speed", qty(r, "steady_speed_rpm", "rpm", 1.0, 3)},
                                              {"Steady torque", qtyU(r, "steady_torque_nm", "N m", us, 1.0, 3)}};
    });
    resultCards("pumps", "size_pump", [us](const json& r) {
        return std::vector<kit::KeyValueItem>{{"Mean pressure", qtyU(r, "mean_pressure_bar", "bar", us, 1.0, 3)},
                                              {"Mean power", powerQty(r, "mean_power_kw", us, 3)}};
    });
    warningsList(sim_);
    if (kit::secondaryButton("Show plots###show_plots", true, nullptr, ImVec2(-FLT_MIN, 0)))
        setCentralView(CentralView::Plots);
    // Details (energy balance, solver, stored samples): collapsed by default.
    std::vector<kit::KeyValueItem> details = {{"Useful work", qty(s, "useful_work_kj", "kJ", 1.0, 3)},
                                              {"Heat", qty(s, "heat_kj", "kJ", 1.0, 3)},
                                              {"Relief open", qty(s, "relief_open_time_s", "s", 1.0, 3)}};
    const json& solver = jobj(sim_, "solver");
    if (!solver.empty()) {
        std::string method = jstr(solver, "method", kDash);
        if (!method.empty() && method[0] >= 'a' && method[0] <= 'z') method[0] = (char)(method[0] - 'a' + 'A');
        details.push_back({"Solver", method});
        details.push_back({"Steps", fmtText(solver, "steps") + " (" + fmtText(solver, "rejected_steps") + " rejected)"});
    }
    if (jhas(sim_, "samples_full"))
        details.push_back({"Stored samples", fmtText(sim_, "samples_stored") + " of " + fmtText(sim_, "samples_full")});
    kit::keyValueCard("details", "Details", "performance", details, &simDetailsOpen_);
}

// ---- BOM ---------------------------------------------------------------------------------------

void HydraulicPanel::drawBomForm(AppState& st) {
    if (jarr(circuit_, "components").empty()) {
        if (kit::emptyState("bom", "Nothing to list yet", "Add components to the circuit first.", "Add components"))
            handleCommand(st, "sim_hyd_add");
        return;
    }
    const bool us = SimManager::unitSys(st).us;
    json& comps = circuit_["components"];  // an array (syncForDraw)
    // The bill of materials first once it is built; the sourcing it came from below.
    if (bom_.is_object() && !bom_.empty()) {
        const json& s = jobj(bom_, "summary");
        const json& lines = jarr(bom_, "bom_lines");
        const bool allSourced = jarr(s, "missing_mpn").empty();
        kit::sectionHeader("Bill of materials", "results", nullptr, (int)lines.size(), true);
        kit::keyValueCard("bom_summary", "Summary", nullptr,
                          {{"Lines", fmtText(s, "total")},
                           {"Sourced", fmtText(s, "sourced") + " of " + fmtText(s, "total"),
                            allSourced ? kit::Tone::Ok : kit::Tone::Warn}});
        kit::gapRow();
        // BOM lines as list rows: name, then type and sourcing ("Not sourced" until a part number
        // is set; the manufacturer only when known), quantity at the right.
        for (int i = 0; i < (int)lines.size(); ++i) {
            const json& l = lines[i];
            const std::string title = jstr(l, "name", typeLabel(jstr(l, "type")));
            const std::string mpn = jstr(l, "mpn");
            const std::string mfr = jstr(l, "manufacturer");
            std::string second = typeLabel(jstr(l, "type")) + " \xc2\xb7 ";
            if (mpn.empty()) {
                second += "Not sourced";
            } else {
                second += "Part no. " + mpn;
                if (!mfr.empty()) second += " \xc2\xb7 " + mfr;
            }
            const std::string q = fmtText(l, "quantity") + " " + jstr(l, "unit", "ea");
            kit::RowStyle rs;
            rs.icon = "cat_hydraulics";
            rs.secondary = second.c_str();
            rs.meta = q.c_str();
            rs.tooltip = second.c_str();
            ImGui::PushID(i);
            kit::row("line", title.c_str(), rs);
            ImGui::PopID();
        }
        // Hoses are line items too: their length, bore and rating.
        const json& hoses = jarr(circuit_, "hoses");
        if (!hoses.empty()) {
            kit::sectionHeader("Hoses", "sch_wire", nullptr, (int)hoses.size());
            for (int i = 0; i < (int)hoses.size(); ++i) {
                const json& h = hoses[i];
                const std::string net = jstr(h, "net_name");
                const std::string title = net.empty() ? "Hose " + std::to_string(i + 1) : net;
                const std::string second = qtyU(h, "length_mm", "mm", us) + " \xc2\xb7 ID " +
                                           qtyU(h, "id_mm", "mm", us) + " \xc2\xb7 " + qtyU(h, "rating_bar", "bar", us);
                kit::RowStyle rs;
                rs.icon = "sch_wire";
                rs.secondary = second.c_str();
                rs.tooltip = second.c_str();
                ImGui::PushID(i);
                kit::row("hose", title.c_str(), rs);
                ImGui::PopID();
            }
        }
        warningsList(bom_);
    }
    kit::sectionHeader("Sourcing", "bom", nullptr, (int)comps.size(), !(bom_.is_object() && !bom_.empty()));
    kit::dimText("A component without a part number is listed as not sourced.");
    bool edited = false;
    // One collapsible card per component (open where the part number is missing): MPN,
    // manufacturer and quantity, index-scoped.
    for (int i = 0; i < (int)comps.size(); ++i) {
        json& c = comps[i];
        const json& src = jobj(c, "sourcing");
        auto setSrc = [&](const char* key, json v) {
            json& dst = c["sourcing"];
            if (!dst.is_object()) dst = json::object();
            dst[key] = std::move(v);
            edited = true;
        };
        ImGui::PushID(i);
        const std::string type = typeLabel(jstr(c, "type"));
        const std::string title = jstr(c, "name", type) + " \xc2\xb7 " + type;
        bool& open = srcOpen_[jstr(c, "id")];
        if (kit::cardBegin("src", title.c_str(), "cat_hydraulics", &open)) {
            kit::FieldGroup g("src", "Manufacturer");
            std::string mpn = jstr(src, "mpn");
            if (kit::fieldText("mpn", "Part number", &mpn,
                               kit::FieldOpts().withHint("Not sourced").withTip("Manufacturer part number (MPN).")))
                setSrc("mpn", mpn.empty() ? json() : json(mpn));
            std::string mfr = jstr(src, "manufacturer");
            if (kit::fieldText("mfr", "Manufacturer", &mfr, kit::FieldOpts().withHint("e.g. Bosch Rexroth")))
                setSrc("manufacturer", mfr.empty() ? json() : json(mfr));
            int q = (int)jnum(src, "quantity", 1.0);
            if (kit::fieldInt("qty", "Quantity", &q, kit::FieldOpts().withMin(1.0))) setSrc("quantity", q);
        }
        kit::cardEnd();
        ImGui::PopID();
        kit::gapRow();
    }
    if (edited) {
        bom_ = json();
        commitCircuitMutation(st, false);
    }
}

// ---- piping handoff ----------------------------------------------------------------------------

void HydraulicPanel::drawPipingForm(AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    const json& hoses = jarr(circuit_, "hoses");
    if (hoses.empty()) {
        if (kit::emptyState("route_pipe", "No hoses to hand off",
                            "Connect components with hoses first; each hose becomes a pipe run.", "Connect ports"))
            handleCommand(st, "sim_hyd_hose");
        return;
    }
    bool edited = false;
    if (kit::cardBegin("handoff", "Hose lines to piping", "route_pipe")) {
        kit::FieldGroup g("handoff", "Verify tolerance");
        if (kit::fieldNumber("supply", "Supply pressure", &hoSupplyBar_,
                         fieldU("bar", us).withMin(0.0).withTip(
                             "Pump outlet / relief setting. Each hose becomes a pipe run sized from its inside "
                             "diameter and length, solved as an independent supply-to-demand case with hydraulic oil."))) {
            handoffParams_["supply_pressure_bar"] = hoSupplyBar_;
            edited = true;
        }
        if (kit::fieldNumber("flow", "Flow", &hoFlowLpm_,
                         fieldU("L/min", us).withMin(0.0).withTip(
                             "Flow each line carries (the pump flow by default)."))) {
            handoffParams_["flow_lpm"] = hoFlowLpm_;
            edited = true;
        }
        if (kit::fieldNumber("load", "Load pressure", &hoLoadBar_,
                             fieldU("bar", us).withMin(0.0).withPresent(&hoLoadPresent_).withHint(
                                 "optional").withTip("Optional: actuator load pressure for the pump margin."))) {
            if (hoLoadPresent_) handoffParams_["load_pressure_bar"] = hoLoadBar_;
            else handoffParams_.erase("load_pressure_bar");
            edited = true;
        }
        if (kit::fieldNumber("tol", "Verify tolerance", &hoTolPct_,
                         kit::FieldOpts().withUnit(kit::Unit::Percent).withMin(0.0).withTip(
                             "Estimate vs solved pressure-drop agreement band."))) {
            handoffParams_["tolerance_pct"] = hoTolPct_;
            edited = true;
        }
    }
    kit::cardEnd();
    if (edited) {
        handoff_ = json();
        pipingOpenReq_ = false;
        persist(st);
    }
    if (ImGui::CollapsingHeader("Advanced piping handoff parameters###hyd_all_handoff")) {
        kit::dimText("Per-hose lines, fluid, defaults and operating point use hydraulic_piping_handoff keys.");
        json next;
        if (drawNativeJson("handoff_params", "Advanced piping handoff parameters", handoffParams_, next, true)) {
            std::string error;
            if (!configureHandoff(st, next, error)) jsonDrafts_["handoff_params"].error = error;
        }
    }
    // The inputs first, then the generated network (a fresh result scrolls into view once): one
    // pipe run per hose.
    const bool haveResult = handoff_.is_object() && !handoff_.empty();
    if (haveResult) {
        const json& verify = jobj(handoff_, "verify");
        revealHere(Reveal::Handoff);
        kit::sectionHeader("Hose runs", "results", nullptr, (int)jarr(jobj(handoff_, "piping_doc"), "runs").size());
        kit::chipFlowBegin("outcome");
        const bool solved = jstr(handoff_, "stage") == "solved";
        kit::flowChip(solved ? "Solved" : "Solve failed", solved ? kit::Tone::Ok : kit::Tone::Warn, true);
        if (!verify.empty()) {
            const json& checks = jarr(verify, "checks");
            int pass = 0;
            for (const auto& c : checks) pass += jbool(c, "pass", false) ? 1 : 0;
            const std::string v = std::to_string(pass) + " of " + std::to_string(checks.size()) + " lines verified";
            const std::string tip = jstr(verify, "verdict");
            kit::flowChip(v.c_str(), jbool(verify, "pass", false) ? kit::Tone::Ok : kit::Tone::Err, true,
                          tip.empty() ? nullptr : tip.c_str());
        }
        kit::chipFlowEnd();
        kit::gapRow();
        const json& estimates = jobj(handoff_, "estimates");
        const json& perLine = jobj(handoff_, "per_line_dp_kpa");
        const json& margins = jobj(jobj(handoff_, "margin"), "per_line");
        // One key/value card per hose line (index-scoped): the hose's net name and geometry.
        const json& lines = jarr(handoff_, "lines");
        for (int i = 0; i < (int)lines.size(); ++i) {
            const json& l = lines[i];
            const std::string rid = jstr(l, "run_id");
            const std::string hid = jstr(l, "hose_id");
            std::string name;
            for (int k = 0; k < (int)hoses.size() && name.empty(); ++k)
                if (jstr(hoses[k], "id") == hid) {
                    name = jstr(hoses[k], "net_name");
                    if (name.empty()) name = "Hose " + std::to_string(k + 1);
                }
            if (name.empty()) name = hid;
            const std::string title = name + " \xc2\xb7 " + qtyU(l, "id_mm", "mm", us, 1.0, 3) + " \xc3\x97 " +
                                      qtyU(l, "length_mm", "mm", us);
            const json& est = jobj(estimates, rid.c_str());
            const json& margin = jobj(margins, rid.c_str());
            const bool shortMargin = jhas(margin, "pump_margin_solved_bar") && jnum(margin, "pump_margin_solved_bar") < 0.0;
            // Drops arrive in kPa; shown like every hydraulic pressure (bar, or psi in US units).
            const std::string estDp = qtyU(est, "dp_kpa", "bar", us, 0.01, 3);
            const std::string solvedDp = jhas(perLine, rid.c_str())
                ? fmtU(std::fabs(jnum(perLine, rid.c_str())) * 0.01, "bar", us, 3) : std::string(kDash);
            ImGui::PushID(i);
            kit::keyValueCard("line", title.c_str(), "route_pipe",
                              {{"Velocity", qtyU(est, "velocity_ms", "m/s", us, 1.0, 3)},
                               {"Drop, estimated", estDp},
                               {"Drop, solved", solvedDp},
                               {"Pump margin", qtyU(margin, "pump_margin_solved_bar", "bar", us, 1.0, 3),
                                shortMargin ? kit::Tone::Warn : kit::Tone::Neutral}});
            ImGui::PopID();
            kit::gapRow();
        }
        warningsList(handoff_);
    }
}

}  // namespace ui
