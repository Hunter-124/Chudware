// Manufacture operator surface plumbing. Nothing here floats over the viewport any more:
// the Offline/Live mode strip is real chrome drawn by app.cpp directly under the ribbon
// (drawManufactureModeBar, whose height app.cpp reserves before the dockspace), and the
// Devices / Run / Attention views are bodies hosted by the pinned left column
// (ui/layout.cpp). This file owns the mode strip, those bodies, and the RPC queue.
//
// The strip is CHROME, not a warning banner. It is one compact frosted row (ui::glassPanel over
// the panel's AdaptiveSurface) carrying a kit::chip for the mode, one dim explanation line and a
// right-aligned action. Offline Simulation is the SAFE state, so it gets the calm neutral glass
// and an Ok chip; LIVE MACHINE is the hazardous one, so it is the only state that tints the
// chrome (glassCardWash in palette.statusErr) and the only one with a danger action. Every
// widget here comes from ui::kit — this file paints no chips, buttons or empty states of its own.
#include "manufacture_panel.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdio>
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
#include "../widgets/kit.h"
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

// The strip's live-machine chrome tint. Strong enough to read as "this is not simulation" at a
// glance, far short of an alarm fill — the danger button and the Err chip carry the emphasis.
constexpr float kLiveWashTopAlpha = 0.22f;

// Geometry of a kit::chip drawn WITH a dot. The kit does not export its chip metrics, so this
// mirrors kit::chipGeom() out of the same ui::metrics tokens (padX U(1.5), padY U(0.5), gap U(1),
// dot radius U(0.75)). The strip needs the height to centre the chip against the frame-height
// action button, and the chrome width to know how much room a device id has left in the pill.
struct DotChipBox {
    float chromeW = 0.0f;   // everything in the pill that is not the label
    float height  = 0.0f;
};

DotChipBox dotChipBox() {
    DotChipBox b;
    b.chromeW = metrics::U(1.5f) * 2.0f + metrics::U(0.75f) * 2.0f + metrics::U(1.0f);
    b.height  = ImGui::GetTextLineHeight() + metrics::U(0.5f) * 2.0f;
    return b;
}

// The strip's single dim explanation line, shortened in fixed tiers so a safety statement is
// never half-painted: full sentence -> compact sentence -> nothing at all. The mode chip beside
// it always states the mode, so dropping the line loses no mode information.
const char* fittingNote(const char* full, const char* compact, float budget) {
    if (budget <= 0.0f) return nullptr;
    if (ImGui::CalcTextSize(full).x <= budget) return full;
    if (ImGui::CalcTextSize(compact).x <= budget) return compact;
    return nullptr;
}

// Global operator notices. Every rail body repeats them: the rail shows one tab at a time, so
// a notice parked on a sibling tab would be invisible exactly when the operator needs it.
void drawOperatorNotices(AppState& st) {
    kit::inlineStatus(st.mfg.lastError.c_str(), kit::Tone::Warn);   // no-op while empty
    if (st.daemonUnreachable || !st.mfgMirror || !st.mfgMirror->gatewayReachable()) {
        const std::string why = st.daemonUnreachable
            ? "Manufacturing gateway unreachable — live values are unknown and cached rows are not shown as current."
            : (st.mfgMirror ? st.mfgMirror->gatewayWhy()
                            : "Manufacturing mirror unavailable.");
        kit::blockedState("Manufacturing gateway unreachable", why.c_str());
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
    // One compact chrome row: the mode chip and its action button, neither taller than a frame.
    // Called before drawManufacturePanel, so it must never read st.mfgMirror (null on the first
    // frame) or anything else produced later in the frame.
    return ImGui::GetFrameHeight() + 2.0f * metrics::camRowPadY();
}

void drawManufactureModeBar(AppState& st, float x, float y, float w, float h) {
    if (w <= 0.0f || h <= 0.0f) return;
    const bool offline = st.mfg.mode == MfgUi::Mode::OfflineSimulation;

    ImGui::SetNextWindowPos(ImVec2(x, y), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(w, h), ImGuiCond_Always);
    // Transparent — the glass painted below is the only background.
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(metrics::camGutter(), metrics::camRowPadY()));
    ImGui::Begin("##ManufactureModeBar", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
                 ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBringToFrontOnFocus);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();

    // Chrome material: the app's frosted glass, square-cornered because the strip is full-bleed
    // between the ribbon and the dockspace. The viewport columns/tabs start one grid unit below
    // that band. Carry the strip's material through that chrome gutter rather than exposing a
    // bright sliver of the studio canvas between two dark surfaces. This is paint only: the
    // reserved mode-row height and the controls' hit rectangles remain unchanged.
    // Only LIVE MACHINE tints it.
    ImDrawList*             dl   = ImGui::GetWindowDrawList();
    const ImRect            rect(ImVec2(x, y), ImVec2(x + w, y + h + metrics::U()));
    const AdaptiveSurface&  surf = adaptivePanel(panelId("mfg_mode_bar"), rect);
    dl->PushClipRect(rect.Min, rect.Max, false);
    glassPanel(dl, rect, GlassLevel::Thin, 0.0f, surf);
    if (!offline)
        glassCardWash(dl, rect.Min, rect.Max, 0.0f, palette.statusErr, kLiveWashTopAlpha);
    dl->PopClipRect();

    const float      gap    = metrics::camBtnGap();
    const float      rowTop = ImGui::GetCursorPosY();
    const float      rowH   = ImGui::GetFrameHeight();
    const DotChipBox box    = dotChipBox();
    // Every element on the row places its own baseline: the chip is shorter than the button, and
    // ImGui aligns a line's items by their tops, not their centres.
    auto centerY = [rowTop, rowH](float itemH) {
        ImGui::SetCursorPosY(rowTop + std::max(0.0f, (rowH - itemH) * 0.5f));
    };

    // Measured before anything is drawn so the explanation line gets the honest leftover width.
    const char* actionLabel = offline ? "Enter Live Machine…" : "Return to Offline Simulation";
    const float actionW     = ImGui::CalcTextSize(actionLabel).x +
                              ImGui::GetStyle().FramePadding.x * 2.0f;
    const float rowW        = ImGui::GetContentRegionAvail().x;

    // ---- mode chip: the one element that always survives, however narrow the window ----
    const kit::Tone   chipTone  = offline ? kit::Tone::Ok : kit::Tone::Err;
    const char* const chipShort = offline ? "Offline simulation" : "LIVE MACHINE";
    const std::string chipFull  =
        offline ? std::string("Offline simulation")
                : (st.mfg.liveDeviceId.empty()
                       ? std::string("LIVE MACHINE — device identity unknown")
                       : "LIVE MACHINE — " + st.mfg.liveDeviceId);
    const float chipBudget = std::max(0.0f, rowW - actionW - gap * 2.0f - box.chromeW);
    std::string chipLabel  = kit::elide(chipFull.c_str(), chipBudget);
    if (chipLabel.empty()) chipLabel = chipShort;   // not even an elided id fits: mode word only
    centerY(box.height);
    kit::chip(chipLabel.c_str(), chipTone, /*dot=*/true,
              chipLabel == chipFull ? nullptr : chipFull.c_str());

    // ---- explanation ----
    ImGui::SameLine(0, gap);
    const float noteBudget = ImGui::GetContentRegionAvail().x - actionW - gap;
    const char* note =
        offline ? fittingNote("Nothing is sent to a machine.", "No machine output.", noteBudget)
                : fittingNote("Opening a panel or observing a run never changes this mode.",
                              "Observing a run never changes this mode.", noteBudget);
    if (note) {
        centerY(ImGui::GetTextLineHeight());
        kit::dimText(note);
        ImGui::SameLine(0, 0);
    }

    // ---- right-aligned action: spacer measured from what the row has left, never an offset ----
    ImGui::Dummy(ImVec2(std::max(0.0f, ImGui::GetContentRegionAvail().x - actionW - gap), 0.0f));
    ImGui::SameLine(0, gap);
    centerY(rowH);
    if (offline) {
        const bool  ready  = !st.mfg.selectedDeviceId.empty() && !st.mfg.requestInFlight;
        const char* reason = st.mfg.selectedDeviceId.empty()
            ? "Live Machine unavailable — select a device, then explicitly claim command ownership."
            : "Live Machine unavailable — another manufacturing request is in flight.";
        if (kit::secondaryButton(actionLabel, ready, ready ? nullptr : reason)) {
            queueManufactureRequest(st, "mfg_device_claim",
                                    {{"device_id", st.mfg.selectedDeviceId},
                                     {"role", "commander"}}, "enter_live");
        }
    } else if (kit::dangerButton(actionLabel)) {
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
        char label[64];
        char full[96];
        std::snprintf(label, sizeof(label), "%zu unacknowledged critical", criticalCount);
        std::snprintf(full, sizeof(full), "%zu unacknowledged critical attention item%s",
                      criticalCount, criticalCount == 1 ? "" : "s");
        kit::chip(label, kit::Tone::Err, /*dot=*/true, full);
        kit::dimText("Acknowledge them before operating the machine.");
    }
    if (!st.mfgMirror) {
        kit::blockedState("Manufacturing mirror unavailable.", nullptr);
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
