#include "manufacture_panel.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>
#include <utility>

#include "imgui.h"
#include "imgui_internal.h"

#include "attention_panel.h"
#include "approval_dialog.h"
#include "device_panel.h"
#include "mfg_mirror.h"
#include "run_panel.h"
#include "../layout.h"
#include "../metrics.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

using json = nlohmann::json;

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

struct ManufactureRegion {
    ImVec2 min;
    ImVec2 max;
    bool valid = false;
};

ManufactureRegion operatorRegion() {
    const OverlayBand& band = overlayBand();
    if (!band.valid) return {};
    const float inset = metrics::U(2);
    float left = overlayColumnEdge(0);
    if (left <= band.minX || left >= band.maxX) left = band.minX;
    float right = overlayColumnEdge(1);
    if (right <= left + inset || right > band.maxX) right = band.maxX;
    ManufactureRegion out;
    out.min = ImVec2(left + inset, band.minY + inset);
    out.max = ImVec2(right - inset, band.maxY - inset);
    out.valid = out.max.x > out.min.x + metrics::U(60) &&
                out.max.y > out.min.y + metrics::U(40);
    return out;
}

void drawModeBanner(AppState& st, const ManufactureRegion& region) {
    const float height = metrics::U(19);
    ImGui::SetNextWindowPos(region.min, ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(region.max.x - region.min.x, height), ImGuiCond_Always);
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
                          st.mfg.mode == MfgUi::Mode::OfflineSimulation
                              ? ImVec4(palette.setupWarnBg[0], palette.setupWarnBg[1],
                                       palette.setupWarnBg[2], palette.setupWarnBg[3])
                              : ImVec4(palette.proseBg[0], palette.proseBg[1],
                                       palette.proseBg[2], palette.proseBg[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(metrics::cpMenuGutter(), metrics::cpMenuRowPadY()));
    ImGui::Begin("##ManufactureModeBanner", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();

    const bool offline = st.mfg.mode == MfgUi::Mode::OfflineSimulation;
    const float* semantic = offline ? palette.statusWarn : palette.statusErr;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(semantic[0], semantic[1], semantic[2], semantic[3]));
    if (st.fonts.h2) ImGui::PushFont(st.fonts.h2);

    // The viewport keeps a compact affordance at this band's leading edge. Reserve its frame and
    // the standard control gap so the safety-critical mode word can never be painted underneath it.
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + metrics::camBtnH() + metrics::camBtnGap());
    const float availableTextWidth = ImGui::GetContentRegionAvail().x;
    auto fits = [availableTextWidth](const char* text) {
        return ImGui::CalcTextSize(text).x <= availableTextWidth;
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

    if (offline) {
        const bool disabled = st.mfg.selectedDeviceId.empty() || st.mfg.requestInFlight;
        ImGui::BeginDisabled(disabled);
        if (ImGui::Button("Claim command ownership & enter Live Machine")) {
            queueManufactureRequest(st, "mfg_device_claim",
                                    {{"device_id", st.mfg.selectedDeviceId},
                                     {"role", "commander"}}, "enter_live");
        }
        ImGui::EndDisabled();
        if (disabled) {
            drawInlineWarning(st.mfg.selectedDeviceId.empty()
                ? "Live Machine unavailable — select a device, then explicitly claim command ownership."
                : "Live Machine unavailable — another manufacturing request is in flight.");
        }
    } else {
        if (ImGui::Button("Return to Offline Simulation")) {
            const std::string deviceId = st.mfg.liveDeviceId;
            st.mfg.mode = MfgUi::Mode::OfflineSimulation;
            st.mfg.liveDeviceId.clear();
            if (!deviceId.empty())
                queueManufactureRequest(st, "mfg_device_release", {{"device_id", deviceId}},
                                        "leave_live");
        }
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextUnformatted("Opening a panel or observing a run never changes this mode.");
    }
    ImGui::End();
}

void beginOperatorChild(const char* id, const ImVec2& size) {
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(palette.panelBg[0], palette.panelBg[1],
                                                   palette.panelBg[2], palette.panelBg[3]));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(metrics::cpMenuGutter(), metrics::cpMenuGutter()));
    ImGui::BeginChild(id, size,
                      ImGuiChildFlags_Border | ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();
}

void endOperatorChild(const char* stableId) {
    const ImVec2 childPos = ImGui::GetWindowPos();
    const ImVec2 childSize = ImGui::GetWindowSize();
    const ImRect rect(childPos, ImVec2(childPos.x + childSize.x, childPos.y + childSize.y));
    const AdaptiveSurface& surf = adaptivePanel(panelId(stableId), rect);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
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

void drawOperatorDrawer(AppState& st, const ManufactureRegion& region) {
    const float bannerHeight = metrics::U(19);
    const float gap = metrics::U(2);
    const float available = region.max.y - region.min.y - bannerHeight - gap;
    const float height = std::min(metrics::U(108), available);
    if (height < metrics::U(36)) return;
    const ImVec2 pos(region.min.x, region.max.y - height);
    const ImVec2 size(region.max.x - region.min.x, height);

    ImGui::SetNextWindowPos(pos, ImGuiCond_Always);
    ImGui::SetNextWindowSize(size, ImGuiCond_Always);
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
                          ImVec4(palette.windowBg[0], palette.windowBg[1],
                                 palette.windowBg[2], 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::Begin("##ManufactureOperatorDrawer", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoBringToFrontOnFocus);
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();

    if (!st.mfg.lastError.empty()) drawInlineWarning(st.mfg.lastError);
    if (st.daemonUnreachable || !st.mfgMirror || !st.mfgMirror->gatewayReachable()) {
        const std::string why = st.daemonUnreachable
            ? "Manufacturing gateway unreachable — live values are unknown and cached rows are not shown as current."
            : (st.mfgMirror ? st.mfgMirror->gatewayWhy()
                            : "Manufacturing mirror unavailable.");
        drawInlineWarning(why);
    }

    const std::size_t criticalCount = st.mfgMirror
                                          ? unacknowledgedCriticalCount(*st.mfgMirror)
                                          : 0;
    if (criticalCount > 0) {
        ImGui::PushStyleColor(
            ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], palette.statusErr[3]));
        ImGui::Text("%zu unacknowledged critical attention item%s",
                    criticalCount, criticalCount == 1 ? "" : "s");
        ImGui::PopStyleColor();
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("Open Attention before operating the machine.");
    }

    if (ImGui::BeginTabBar("##mfg_operator_tabs")) {
        if (ImGui::BeginTabItem("Devices")) {
            beginOperatorChild("##mfg_devices", ImVec2(0, 0));
            drawManufactureDevicePanel(st);
            endOperatorChild("mfg_devices_scroll");
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Run")) {
            beginOperatorChild("##mfg_runs", ImVec2(0, 0));
            drawManufactureRunPanel(st);
            endOperatorChild("mfg_runs_scroll");
            ImGui::EndTabItem();
        }

        const std::string attentionLabel = criticalCount > 0
            ? "Attention (" + std::to_string(criticalCount) + " critical)###mfg_attention"
            : "Attention###mfg_attention";
        ImGui::PushStyleColor(
            ImGuiCol_Text,
            criticalCount > 0
                ? ImVec4(palette.statusErr[0], palette.statusErr[1],
                         palette.statusErr[2], palette.statusErr[3])
                : ImVec4(palette.text[0], palette.text[1],
                         palette.text[2], palette.text[3]));
        const ImGuiTabItemFlags attentionFlags =
            st.shot ? ImGuiTabItemFlags_SetSelected : ImGuiTabItemFlags_None;
        const bool attentionOpen =
            ImGui::BeginTabItem(attentionLabel.c_str(), nullptr, attentionFlags);
        ImGui::PopStyleColor();
        if (attentionOpen) {
            beginOperatorChild("##mfg_attention", ImVec2(0, 0));
            drawAttentionBody(st);
            endOperatorChild("mfg_attention_scroll");
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::End();
}

}  // namespace

void queueManufactureRequest(AppState& st, std::string method, json params,
                             std::string context) {
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
                return;
            }

            if (context == "enter_live") {
                app->mfg.mode = MfgUi::Mode::LiveMachine;
                app->mfg.liveDeviceId = app->mfg.selectedDeviceId;
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

    const ManufactureRegion region = operatorRegion();
    if (!region.valid) return;
    drawModeBanner(st, region);
    drawOperatorDrawer(st, region);
    drawManufactureApprovalDialog(st);
}

}  // namespace ui
