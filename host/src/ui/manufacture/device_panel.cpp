#include "device_panel.h"

#include <algorithm>
#include <cstddef>
#include <string>

#include "imgui.h"

#include "manufacture_panel.h"
#include "mfg_mirror.h"
#include "../metrics.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

const float* capabilityColor(const std::string& truth) {
    if (truth == "verified") return palette.statusOk;
    if (truth == "declared") return palette.accent;
    if (truth == "assumed") return palette.statusWarn;
    if (truth == "unsupported") return palette.statusErr;
    return palette.textDim;
}

void drawCapabilityChip(const MfgCapabilityLevel& capability) {
    const std::string label = capability.reported && !capability.truth.empty()
                                  ? capability.truth : "unreported";
    const float* semantic = capabilityColor(label);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(semantic[0], semantic[1], semantic[2], semantic[3]));
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(palette.frameBg[0], palette.frameBg[1],
                                                  palette.frameBg[2], palette.frameBg[3]));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(palette.frameBgHovered[0],
                                                         palette.frameBgHovered[1],
                                                         palette.frameBgHovered[2],
                                                         palette.frameBgHovered[3]));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(palette.frameBg[0], palette.frameBg[1],
                                                        palette.frameBg[2], palette.frameBg[3]));
    ImGui::SmallButton(label.c_str());
    ImGui::PopStyleColor(4);
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ImGui::Text("level: %s", capability.level.c_str());
        ImGui::Text("truth: %s", label.c_str());
        if (!capability.missingLevel.empty())
            ImGui::Text("missing_level: %s", capability.missingLevel.c_str());
        if (!capability.why.empty()) ImGui::TextWrapped("%s", capability.why.c_str());
        ImGui::EndTooltip();
    }
}

void disabledReason(bool disabled, const std::string& reason) {
    if (!disabled) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                palette.statusWarn[2], palette.statusWarn[3]));
    ImGui::TextWrapped("%s", reason.c_str());
    ImGui::PopStyleColor();
}

// Narrow-rail label/value row: value stays on the label's line when it fits, otherwise it
// drops to a wrapped line so long ids and hashes never run off the pinned column.
void kvRow(const char* label, const std::string& value) {
    const char* text = value.empty() ? "—" : value.c_str();
    const float gap = metrics::U(1);
    const float inlineBudget = ImGui::GetContentRegionAvail().x -
                               ImGui::CalcTextSize(label).x - gap;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                palette.textDim[2], palette.textDim[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    if (ImGui::CalcTextSize(text).x <= inlineBudget) {
        ImGui::SameLine(0, gap);
        ImGui::TextUnformatted(text);
        return;
    }
    ImGui::Indent(metrics::U(2));
    ImGui::TextWrapped("%s", text);
    ImGui::Unindent(metrics::U(2));
}

void enqueueSelectedQueries(AppState& st, const std::string& deviceId) {
    queueManufactureRequest(st, "mfg_device_describe", {{"device_id", deviceId}});
    queueManufactureRequest(st, "mfg_device_capability", {{"device_id", deviceId}});
    queueManufactureRequest(st, "mfg_telemetry_snapshot", {{"device_id", deviceId}});
    queueManufactureRequest(st, "mfg_run_list", {{"device_id", deviceId}});
}

}  // namespace

void drawManufactureDevicePanel(AppState& st) {
    if (!st.mfgMirror) {
        ImGui::TextDisabled("Manufacturing mirror unavailable.");
        return;
    }
    MfgMirror& mirror = *st.mfgMirror;

    if (st.fonts.h2) ImGui::PushFont(st.fonts.h2);
    ImGui::TextUnformatted("Devices");
    if (st.fonts.h2) ImGui::PopFont();
    ImGui::SameLine(0, metrics::U(2));
    if (ImGui::SmallButton("Refresh"))
        queueManufactureRequest(st, "mfg_device_list", nlohmann::json::object(), "refresh_devices");

    if (mirror.devices().empty()) {
        ImGui::Spacing();
        ImGui::TextWrapped("No devices are registered. Add an emulator or commission a machine, then refresh.");
        return;
    }

    ImGui::Spacing();
    for (const MfgDevice& row : mirror.devices()) {
        ImGui::PushID(row.deviceId.c_str());
        const bool selected = st.mfg.selectedDeviceId == row.deviceId;
        // The rail is narrow: the row header carries the machine class only, with the
        // device id and freshness wrapped underneath instead of clipped beside it.
        const std::string title = row.machineClass.empty() ? row.deviceId : row.machineClass;
        if (ImGui::Selectable(title.c_str(), selected, ImGuiSelectableFlags_AllowDoubleClick)) {
            st.mfg.selectedDeviceId = row.deviceId;
            enqueueSelectedQueries(st, row.deviceId);
        }
        ImGui::Indent(metrics::U(2));
        if (!row.machineClass.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                        palette.textDim[2], palette.textDim[3]));
            ImGui::TextWrapped("%s", row.deviceId.c_str());
            ImGui::PopStyleColor();
        }
        const MfgFreshPresentation state = presentMfgFresh(row.state);
        const float* stateColor = state.ok ? palette.statusOk
                                : state.stale ? palette.statusWarn : palette.textDim;
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(stateColor[0], stateColor[1],
                                                    stateColor[2], stateColor[3]));
        ImGui::TextWrapped("%s", state.current.c_str());
        ImGui::PopStyleColor();
        if (!state.detail.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                        palette.textDim[2], palette.textDim[3]));
            ImGui::TextWrapped("%s", state.detail.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::Unindent(metrics::U(2));
        ImGui::PopID();
    }

    const MfgDevice* selected = mirror.device(st.mfg.selectedDeviceId);
    if (!selected) {
        ImGui::Spacing();
        ImGui::TextDisabled("Select a device to inspect capability truth and command ownership.");
        return;
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    kvRow("Controller", selected->controller);
    kvRow("Command owner", selected->commandOwner.empty() ? std::string("unclaimed")
                                                          : selected->commandOwner);
    kvRow("Commissioning ceiling", selected->maxExercisableBy);

    ImGui::Spacing();
    ImGui::TextUnformatted("Capability truth");
    if (selected->capabilities.empty()) {
        ImGui::TextDisabled("No capability rows were returned.");
    }
    // Stacked cards, not a 3-column table: at ~360 logical px the reason column was unreadable.
    for (std::size_t i = 0; i < selected->capabilities.size(); ++i) {
        const MfgCapabilityLevel& capability = selected->capabilities[i];
        ImGui::PushID(static_cast<int>(i));
        ImGui::TextUnformatted(capability.level.empty() ? "unnamed level"
                                                        : capability.level.c_str());
        ImGui::SameLine(0, metrics::U(1));
        drawCapabilityChip(capability);
        ImGui::Indent(metrics::U(2));
        if (!capability.why.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                        palette.textDim[2], palette.textDim[3]));
            ImGui::TextWrapped("%s", capability.why.c_str());
            ImGui::PopStyleColor();
        }
        if (!capability.missingLevel.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                        palette.statusWarn[2], palette.statusWarn[3]));
            ImGui::TextWrapped("missing_level: %s", capability.missingLevel.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::Unindent(metrics::U(2));
        ImGui::PopID();
    }

    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                palette.statusWarn[2], palette.statusWarn[3]));
    ImGui::TextWrapped("No software safety function. Hardwired E-stop, limits and interlocks are the only safety devices.");
    ImGui::PopStyleColor();

    ImGui::Spacing();
    const bool busy = st.mfg.requestInFlight;
    ImGui::BeginDisabled(busy);
    if (ImGui::Button("Connect"))
        queueManufactureRequest(st, "mfg_device_connect", {{"device_id", selected->deviceId}},
                                "connect_device");
    ImGui::SameLine(0, metrics::U(2));
    if (ImGui::Button("Disconnect"))
        queueManufactureRequest(st, "mfg_device_disconnect", {{"device_id", selected->deviceId}},
                                "disconnect_device");
    ImGui::EndDisabled();
    disabledReason(busy, "Device controls unavailable — another manufacturing request is in flight.");
}

}  // namespace ui
