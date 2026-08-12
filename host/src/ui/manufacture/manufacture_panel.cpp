// Manufacture operator surface plumbing. Nothing here floats over the viewport any more:
// the Offline/Live mode strip is real chrome drawn by app.cpp directly under the ribbon
// (drawManufactureModeBar, whose height app.cpp reserves before the dockspace), and the
// Devices / Run / Attention views are bodies hosted by the pinned left column
// (ui/layout.cpp). This file owns the mode strip, those bodies, and the RPC queue.
#include "manufacture_panel.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>
#include <utility>

#include "imgui.h"

#include "attention_panel.h"
#include "approval_dialog.h"
#include "device_panel.h"
#include "mfg_mirror.h"
#include "run_panel.h"
#include "../metrics.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

using json = nlohmann::json;
struct LeaseHeartbeatState {
    std::string deviceId;
    std::string leaseId;
    std::uint64_t intervalMs = 0;
    std::chrono::steady_clock::time_point next;

    void clear() {
        deviceId.clear();
        leaseId.clear();
        intervalMs = 0;
        next = {};
    }
};

LeaseHeartbeatState leaseHeartbeat;

std::string resultReason(const core::RpcResult& result) {
    if (!result.err.empty()) return result.err;
    const json& body = result.result;
    if (!body.is_object()) return "Manufacturing request failed.";
    for (const char* key : {"reason", "error", "refusal", "why"}) {
        const auto it = body.find(key);
        if (it == body.end()) continue;
        if (it->is_string()) return it->get<std::string>();
        if (it->is_object()) {
            const std::string code = it->value("code", std::string());
            const std::string message = it->value("message", std::string());
            if (!code.empty() && !message.empty()) return code + " — " + message;
            if (!message.empty()) return message;
            if (!code.empty()) return code;
        }
    }
    return "Manufacturing request was refused.";
}

bool resultAccepted(const core::RpcResult& result) {
    if (!result.ok) return false;
    if (!result.result.is_object()) return true;
    const std::string state = result.result.value("state", std::string());
    if (state == "refused" || state == "unknown") return false;
    return result.result.value("accepted", true);
}

void drawInlineWarning(const std::string& text) {
    if (text.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                palette.statusWarn[2], palette.statusWarn[3]));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
}

// Single-line trailing note for the mode strip. Renders the full text when it fits on the
// current row, otherwise a hoverable marker — a safety statement is never half-painted.
void drawTrailingNote(const char* text, const float* color) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(color[0], color[1], color[2], color[3]));
    if (ImGui::CalcTextSize(text).x <= ImGui::GetContentRegionAvail().x) {
        ImGui::TextUnformatted(text);
        ImGui::PopStyleColor();
        return;
    }
    ImGui::TextUnformatted("(?)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ImGui::TextUnformatted(text);
        ImGui::EndTooltip();
    }
}

// Global operator notices. Every rail body repeats them: the rail shows one tab at a time, so
// a notice parked on a sibling tab would be invisible exactly when the operator needs it.
void drawOperatorNotices(AppState& st) {
    if (!st.mfg.lastError.empty()) drawInlineWarning(st.mfg.lastError);
    if (st.daemonUnreachable || !st.mfgMirror || !st.mfgMirror->gatewayReachable()) {
        const std::string why = st.daemonUnreachable
            ? "Manufacturing gateway unreachable — live values are unknown and cached rows are not shown as current."
            : (st.mfgMirror ? st.mfgMirror->gatewayWhy()
                            : "Manufacturing mirror unavailable.");
        drawInlineWarning(why);
    }
}

const std::string& signedInActor() {
    static const std::string actor = [] {
        const char* value = std::getenv("USER");
        if (!value || !*value) value = std::getenv("USERNAME");
        return value && *value ? std::string(value) : std::string();
    }();
    return actor;
}

void acknowledgeAttention(void* context, std::string_view itemId,
                          std::string_view actor) {
    auto* app = static_cast<AppState*>(context);
    if (!app || itemId.empty() || actor.empty()) return;
    queueManufactureRequest(*app, "mfg_attention_ack",
                            {{"id", std::string(itemId)}, {"by", std::string(actor)}},
                            "attention_ack");
}

std::size_t unacknowledgedCriticalCount(const MfgMirror& mirror) {
    return static_cast<std::size_t>(std::count_if(
        mirror.attention().begin(), mirror.attention().end(),
        [](const MfgAttentionRow& item) {
            return item.severity == "critical" && item.acknowledgedSeq == 0 &&
                   item.resolvedSeq == 0;
        }));
}

void drawAttentionBody(AppState& st) {
    static const MfgAttentionRow emptyRow;
    const std::vector<MfgAttentionRow>& rows = st.mfgMirror->attention();
    MfgAttentionPanelInput input;
    input.itemsData = rows.empty() ? &emptyRow : rows.data();
    input.itemsCount = rows.size();
    input.actor = signedInActor();
    input.requestInFlight = st.mfg.requestInFlight;
    input.acknowledgeContext = &st;
    input.acknowledge = acknowledgeAttention;
    drawManufactureAttentionPanel(input);
}

}  // namespace

float manufactureModeBarHeight(const AppState& st) {
    if (st.activeWorkspace != AppState::Workspace::Manufacture || st.showWeb) return 0.0f;
    // One row: the mode word (h2 font) beside the mode button, so size from the taller of the
    // two. Called before drawManufacturePanel, so it must never read st.mfgMirror (null on
    // the first frame) or anything else produced later in the frame.
    float row = std::max(ImGui::GetFrameHeight(), ImGui::GetTextLineHeight());
    if (st.fonts.h2) row = std::max(row, st.fonts.h2->FontSize);
    return 2.0f * metrics::cpMenuRowPadY() + row;
}

void drawManufactureModeBar(AppState& st, float x, float y, float w, float h) {
    if (w <= 0.0f || h <= 0.0f) return;
    const bool offline = st.mfg.mode == MfgUi::Mode::OfflineSimulation;

    ImGui::SetNextWindowPos(ImVec2(x, y), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(w, h), ImGuiCond_Always);
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
                          offline ? ImVec4(palette.setupWarnBg[0], palette.setupWarnBg[1],
                                           palette.setupWarnBg[2], palette.setupWarnBg[3])
                                  : ImVec4(palette.proseBg[0], palette.proseBg[1],
                                           palette.proseBg[2], palette.proseBg[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(metrics::cpMenuGutter(), metrics::cpMenuRowPadY()));
    ImGui::Begin("##ManufactureModeBar", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
                 ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBringToFrontOnFocus);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();

    // The whole strip is one row: mode word, mode button, then the reason/no-op note.
    const char* button = offline ? "Claim command ownership & enter Live Machine"
                                 : "Return to Offline Simulation";
    const float gap = metrics::U(2);
    const float buttonWidth = ImGui::CalcTextSize(button).x +
                              ImGui::GetStyle().FramePadding.x * 2.0f;
    const float textBudget =
        std::max(0.0f, ImGui::GetContentRegionAvail().x - buttonWidth - gap);

    ImGui::AlignTextToFramePadding();
    const float* semantic = offline ? palette.statusWarn : palette.statusErr;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(semantic[0], semantic[1], semantic[2], semantic[3]));
    if (st.fonts.h2) ImGui::PushFont(st.fonts.h2);
    auto fits = [textBudget](const char* text) {
        return ImGui::CalcTextSize(text).x <= textBudget;
    };

    if (offline) {
        constexpr const char* mode = "OFFLINE SIMULATION";
        constexpr const char* compact = "OFFLINE SIMULATION — no machine output";
        constexpr const char* full = "OFFLINE SIMULATION — nothing is sent to a machine";
        ImGui::TextUnformatted(fits(full) ? full : (fits(compact) ? compact : mode));
    } else if (st.mfg.liveDeviceId.empty()) {
        constexpr const char* mode = "LIVE MACHINE";
        constexpr const char* compact = "LIVE MACHINE — identity unknown";
        constexpr const char* full = "LIVE MACHINE — device identity unknown";
        ImGui::TextUnformatted(fits(full) ? full : (fits(compact) ? compact : mode));
    } else {
        const std::string full = "LIVE MACHINE — " + st.mfg.liveDeviceId;
        ImGui::TextUnformatted(fits(full.c_str()) ? full.c_str() : "LIVE MACHINE");
    }
    if (st.fonts.h2) ImGui::PopFont();
    ImGui::PopStyleColor();

    ImGui::SameLine(0, gap);
    if (offline) {
        const bool disabled = st.mfg.selectedDeviceId.empty() || st.mfg.requestInFlight;
        ImGui::BeginDisabled(disabled);
        if (ImGui::Button(button)) {
            queueManufactureRequest(st, "mfg_device_claim",
                                    {{"device_id", st.mfg.selectedDeviceId},
                                     {"role", "commander"}}, "enter_live");
        }
        ImGui::EndDisabled();
        if (disabled) {
            ImGui::SameLine(0, gap);
            drawTrailingNote(st.mfg.selectedDeviceId.empty()
                ? "Live Machine unavailable — select a device, then explicitly claim command ownership."
                : "Live Machine unavailable — another manufacturing request is in flight.",
                palette.statusWarn);
        }
    } else {
        if (ImGui::Button(button)) {
            const std::string deviceId = st.mfg.liveDeviceId;
            json release{{"device_id", deviceId}};
            const MfgDevice* device =
                st.mfgMirror ? st.mfgMirror->device(deviceId) : nullptr;
            if (device && !device->leaseId.empty())
                release["lease_id"] = device->leaseId;
            else if (leaseHeartbeat.deviceId == deviceId &&
                     !leaseHeartbeat.leaseId.empty())
                release["lease_id"] = leaseHeartbeat.leaseId;
            st.mfg.mode = MfgUi::Mode::OfflineSimulation;
            st.mfg.liveDeviceId.clear();
            leaseHeartbeat.clear();
            if (!deviceId.empty())
                queueManufactureRequest(st, "mfg_device_release", std::move(release),
                                        "leave_live");
        }
        ImGui::SameLine(0, gap);
        drawTrailingNote("Opening a panel or observing a run never changes this mode.",
                         palette.textDim);
    }
    ImGui::End();
}

std::size_t manufactureCriticalCount(const AppState& st) {
    return st.mfgMirror ? unacknowledgedCriticalCount(*st.mfgMirror) : 0;
}

void drawManufactureDevicesBody(AppState& st) {
    drawOperatorNotices(st);
    drawManufactureDevicePanel(st);
}

void drawManufactureRunBody(AppState& st) {
    drawOperatorNotices(st);
    drawManufactureRunPanel(st);
}

void drawManufactureAttentionBody(AppState& st) {
    drawOperatorNotices(st);
    const std::size_t criticalCount = manufactureCriticalCount(st);
    if (criticalCount > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusErr[0], palette.statusErr[1],
                                     palette.statusErr[2], palette.statusErr[3]));
        ImGui::TextWrapped("%zu unacknowledged critical attention item%s",
                           criticalCount, criticalCount == 1 ? "" : "s");
        ImGui::PopStyleColor();
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.textDim[0], palette.textDim[1],
                                     palette.textDim[2], palette.textDim[3]));
        ImGui::TextWrapped("Acknowledge them before operating the machine.");
        ImGui::PopStyleColor();
    }
    if (!st.mfgMirror) {
        ImGui::TextDisabled("Manufacturing mirror unavailable.");
        return;
    }
    drawAttentionBody(st);
}

void queueManufactureRequest(AppState& st, std::string method, json params,
                             std::string context) {
    // Human provenance is authenticated and stamped by hostd. Request payloads
    // never carry approver or initiator identity assertions.
    if (method == "mfg_approval_create") params.erase("approver_auth");
    if (method == "mfg_run_start" || method == "mfg_run_hold" ||
        method == "mfg_run_resume" || method == "mfg_run_abort")
        params.erase("initiator");
    const std::string signature = method + "\n" + params.dump();
    const bool duplicate = std::any_of(st.mfg.requests.begin(), st.mfg.requests.end(),
        [&](const MfgUi::Request& request) {
            return request.method + "\n" + request.params.dump() == signature;
        });
    if (!duplicate)
        st.mfg.requests.push_back(MfgUi::Request{std::move(method), std::move(params),
                                                 std::move(context)});
}

void dispatchManufactureRequests(AppState& st) {
    if (st.mfg.mode == MfgUi::Mode::LiveMachine &&
        !leaseHeartbeat.deviceId.empty() && !leaseHeartbeat.leaseId.empty() &&
        leaseHeartbeat.intervalMs > 0 &&
        std::chrono::steady_clock::now() >= leaseHeartbeat.next) {
        leaseHeartbeat.next = std::chrono::steady_clock::now() +
            std::chrono::milliseconds(leaseHeartbeat.intervalMs);
        queueManufactureRequest(
            st, "mfg_device_heartbeat",
            {{"device_id", leaseHeartbeat.deviceId},
             {"lease_id", leaseHeartbeat.leaseId}},
            "lease_heartbeat");
    }
    if (st.mfg.requestInFlight || st.mfg.requests.empty() || !st.workerStarted || !st.mfgMirror)
        return;

    MfgUi::Request request = std::move(st.mfg.requests.front());
    st.mfg.requests.pop_front();
    st.mfg.requestInFlight = true;
    st.mfg.lastError.clear();
    AppState* app = &st;
    st.rpc.call(request.method, std::move(request.params),
        [app, method = std::move(request.method), context = std::move(request.context)]
        (const core::RpcResult& result) {
            app->mfg.requestInFlight = false;
            const bool accepted = resultAccepted(result);
            if (result.ok) app->mfgMirror->ingestRpc(method, result.result);

            if (!accepted) {
                const std::string state = result.result.is_object()
                    ? result.result.value("state", std::string()) : std::string();
                app->mfg.lastError = state == "unknown"
                    ? "Command outcome unknown — no retry was sent. Resolve it in Attention or the journal."
                    : resultReason(result);
                if (method == "mfg_device_list" && !result.ok)
                    app->mfgMirror->markGatewayError(app->mfg.lastError);
                if (context == "lease_heartbeat") {
                    app->mfg.mode = MfgUi::Mode::OfflineSimulation;
                    app->mfg.liveDeviceId.clear();
                    leaseHeartbeat.clear();
                }
                return;
            }

            if (context == "enter_live") {
                app->mfg.mode = MfgUi::Mode::LiveMachine;
                app->mfg.liveDeviceId = app->mfg.selectedDeviceId;
                const json lease = result.result.value("lease", json::object());
                leaseHeartbeat.deviceId = app->mfg.liveDeviceId;
                leaseHeartbeat.leaseId = lease.value("lease_id", std::string());
                leaseHeartbeat.intervalMs = lease.value("heartbeat_ms", 0ULL);
                leaseHeartbeat.next = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(leaseHeartbeat.intervalMs);
                queueManufactureRequest(*app, "mfg_device_describe",
                                        {{"device_id", app->mfg.liveDeviceId}});
            } else if (context == "approval_propose" || context == "approval_propose_open") {
                app->mfg.openApprovalDialog = true;
            } else if (context == "approval_create") {
                app->toast(ToastLevel::Success, "Manufacturing approval issued by the arbiter.");
                if (!app->mfg.selectedRunId.empty())
                    queueManufactureRequest(*app, "mfg_run_describe",
                                            {{"run_id", app->mfg.selectedRunId}});
            } else if (context == "connect_device" || context == "disconnect_device" ||
                       context == "leave_live") {
                if (!app->mfg.selectedDeviceId.empty())
                    queueManufactureRequest(*app, "mfg_device_describe",
                                            {{"device_id", app->mfg.selectedDeviceId}});
            } else if (context == "preflight_run" || context == "start_run" ||
                       context == "hold_run" || context == "abort_run") {
                if (!app->mfg.selectedRunId.empty())
                    queueManufactureRequest(*app, "mfg_run_describe",
                                            {{"run_id", app->mfg.selectedRunId}});
            } else if (context == "lease_heartbeat") {
                const json lease = result.result.value("lease", json::object());
                leaseHeartbeat.intervalMs =
                    lease.value("heartbeat_ms", leaseHeartbeat.intervalMs);
                leaseHeartbeat.next = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(leaseHeartbeat.intervalMs);
            }
        });
}

void seedManufactureScenario(AppState& st) {
    if (!st.mfgMirror) st.mfgMirror = std::make_shared<MfgMirror>();
    st.mfgMirror->seedManufactureShot();
    st.mfg.mode = MfgUi::Mode::OfflineSimulation;
    st.mfg.selectedDeviceId = "dev:open_frame_router:2f7a91c40e12";
    st.mfg.selectedRunId = "run:shot:0042";
    st.mfg.liveDeviceId.clear();
    st.mfg.openApprovalDialog = false;
    st.mfg.bootstrapQueued = true;
    st.mfg.lastError.clear();
}

void drawManufacturePanel(AppState& st) {
    if (!st.mfgMirror) st.mfgMirror = std::make_shared<MfgMirror>();
    if (!st.mfg.bootstrapQueued && st.workerStarted) {
        st.mfg.bootstrapQueued = true;
        queueManufactureRequest(st, "mfg_device_list", json::object(), "bootstrap_devices");
        queueManufactureRequest(st, "mfg_run_list", json::object(), "bootstrap_runs");
        queueManufactureRequest(st, "mfg_attention_list", json::object(),
                                "bootstrap_attention");
    }

    // The mode strip is chrome drawn by app.cpp; Devices/Run/Attention are rail bodies.
    // Nothing left to float here — only the modal approval dialog.
    drawManufactureApprovalDialog(st);
}

}  // namespace ui
