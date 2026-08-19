#include "device_panel.h"

#include <cstddef>
#include <string>

#include "imgui.h"

#include "manufacture_panel.h"
#include "mfg_mirror.h"
#include "../metrics.h"
#include "../widgets/kit.h"
#include "../../core/app_state.h"

namespace ui {
namespace {

// Capability truth -> the app-wide semantic tone. Exactly the mapping the old capabilityColor()
// carried, spelled in kit vocabulary: verified is healthy, declared is the vendor's own claim
// (accent emphasis, deliberately not a green status), assumed is degraded, unsupported blocks,
// and anything unreported stays neutral rather than inventing a judgement.
kit::Tone capabilityTone(const std::string& truth) {
    if (truth == "verified")    return kit::Tone::Ok;
    if (truth == "declared")    return kit::Tone::Accent;
    if (truth == "assumed")     return kit::Tone::Warn;
    if (truth == "unsupported") return kit::Tone::Err;
    return kit::Tone::Neutral;
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
        kit::blockedState("Manufacturing mirror unavailable.", nullptr);
        return;
    }
    MfgMirror& mirror = *st.mfgMirror;

    auto refreshDevices = [&st]() {
        queueManufactureRequest(st, "mfg_device_list", nlohmann::json::object(), "refresh_devices");
    };

    // Section header with its action right-aligned ON the header line: the pinned rail is ~360dp
    // and cannot spend a whole row on a Refresh button. The action is sized to the header's own
    // text line so it never eats into the head gap below it.
    const float  railW   = ImGui::GetContentRegionAvail().x;
    const ImVec2 headTop = ImGui::GetCursorScreenPos();
    kit::sectionHeader("Devices", "ws_manufacture", nullptr,
                       static_cast<int>(mirror.devices().size()), /*first=*/true);
    const ImVec2 afterHead = ImGui::GetCursorScreenPos();
    const float  actionSz  = ImGui::GetTextLineHeight();
    ImGui::SetCursorScreenPos(ImVec2(headTop.x + railW - actionSz, headTop.y));
    if (kit::iconButton("mfg_dev_refresh", "refresh", "Refresh device list", true,
                        kit::Tone::Accent, actionSz)) {
        refreshDevices();
    }
    ImGui::SetCursorScreenPos(afterHead);

    if (mirror.devices().empty()) {
        if (kit::emptyState("ws_manufacture", "No devices",
                            "No devices are registered. Add an emulator or commission a machine, then refresh.",
                            "Refresh")) {
            refreshDevices();
        }
        return;
    }

    for (const MfgDevice& device : mirror.devices()) {
        const MfgFreshPresentation state = presentMfgFresh(device.state);
        // The rail is narrow: the machine class leads, the id and controller ride the dim second
        // line, and the reported state takes the right-hand meta slot.
        const std::string title = device.machineClass.empty() ? device.deviceId
                                                              : device.machineClass;
        std::string secondary;
        if (!device.machineClass.empty()) secondary = device.deviceId;
        if (!device.controller.empty()) {
            secondary = secondary.empty() ? device.controller : secondary + " · " + device.controller;
        }
        // The freshness "why" is never lost — it rides the row tooltip here and is rendered in
        // full on the selected device's card below.
        std::string tooltip = device.deviceId + "\n" + state.current;
        if (!state.detail.empty()) tooltip += "\n" + state.detail;

        kit::RowStyle style;
        style.icon      = "ws_manufacture";
        style.iconTone  = state.ok ? kit::Tone::Ok
                        : state.stale ? kit::Tone::Warn : kit::Tone::Neutral;
        style.secondary = secondary.empty() ? nullptr : secondary.c_str();
        style.meta      = state.current.c_str();
        style.tooltip   = tooltip.c_str();
        style.selected  = st.mfg.selectedDeviceId == device.deviceId;
        if (kit::row(device.deviceId.c_str(), title.c_str(), style)) {
            st.mfg.selectedDeviceId = device.deviceId;
            enqueueSelectedQueries(st, device.deviceId);
        }
        kit::gapRow();
    }

    const MfgDevice* selected = mirror.device(st.mfg.selectedDeviceId);
    if (!selected) {
        kit::gapRow();
        kit::dimText("Select a device to inspect capability truth and command ownership.");
        return;
    }

    const std::string detailTitle = selected->machineClass.empty() ? selected->deviceId
                                                                   : selected->machineClass;
    const MfgFreshPresentation selectedState = presentMfgFresh(selected->state);

    kit::gapSection();
    if (kit::cardBegin("mfg_dev_detail", detailTitle.c_str(), "ws_manufacture")) {
        kit::keyValueRow("Device", selected->deviceId.c_str());
        kit::keyValueRow("Controller",
                         selected->controller.empty() ? "—" : selected->controller.c_str());
        kit::keyValueRow("Command owner",
                         selected->commandOwner.empty() ? "unclaimed"
                                                        : selected->commandOwner.c_str());
        kit::keyValueRow("Commissioning ceiling",
                         selected->maxExercisableBy.empty() ? "—"
                                                            : selected->maxExercisableBy.c_str());
        kit::keyValueRow("State", selectedState.current.c_str(),
                         selectedState.ok ? kit::Tone::Ok
                       : selectedState.stale ? kit::Tone::Warn : kit::Tone::Neutral);
        kit::dimText(selectedState.detail.c_str());
    }
    kit::cardEnd();

    kit::sectionHeader("Capability truth", "health_check", nullptr,
                       static_cast<int>(selected->capabilities.size()));
    if (selected->capabilities.empty()) kit::dimText("No capability rows were returned.");

    // Stacked cards, not a 3-column table: at ~360 logical px the reason column was unreadable.
    for (std::size_t i = 0; i < selected->capabilities.size(); ++i) {
        const MfgCapabilityLevel& capability = selected->capabilities[i];
        const std::string truth = capability.reported && !capability.truth.empty()
                                      ? capability.truth : "unreported";
        const std::string level = capability.level.empty() ? std::string("unnamed level")
                                                           : capability.level;
        std::string chipTip = "level: " + level + "\ntruth: " + truth;
        if (!capability.missingLevel.empty()) chipTip += "\nmissing_level: " + capability.missingLevel;

        ImGui::PushID(static_cast<int>(i));
        if (kit::cardBegin("mfg_dev_cap", level.c_str())) {
            // Chip geometry mirrors kit::chip: padX*2 (U3) + dot (U1.5) + gap (U1).
            kit::cardHeaderRight(ImGui::CalcTextSize(truth.c_str()).x + metrics::U(5.5f));
            kit::chip(truth.c_str(), capabilityTone(truth), /*dot=*/true, chipTip.c_str());
            if (!capability.why.empty()) kit::dimText(capability.why.c_str());
            if (!capability.missingLevel.empty()) {
                kit::inlineStatus(("missing_level: " + capability.missingLevel).c_str(),
                                  kit::Tone::Warn);
            }
        }
        kit::cardEnd();
        ImGui::PopID();
        kit::gapRow();
    }

    kit::gapSection();
    kit::inlineStatus("No software safety function. Hardwired E-stop, limits and interlocks are the only safety devices.",
                      kit::Tone::Warn);

    kit::gapRow();
    const bool        busy = st.mfg.requestInFlight;
    const char* const busyWhy =
        "Device controls unavailable — another manufacturing request is in flight.";
    if (kit::primaryButton("Connect", !busy, busyWhy)) {
        queueManufactureRequest(st, "mfg_device_connect", {{"device_id", selected->deviceId}},
                                "connect_device");
    }
    ImGui::SameLine(0, metrics::camBtnGap());
    if (kit::secondaryButton("Disconnect", !busy, busyWhy)) {
        queueManufactureRequest(st, "mfg_device_disconnect", {{"device_id", selected->deviceId}},
                                "disconnect_device");
    }
}

}  // namespace ui
