#include "attention_panel.h"

#include <algorithm>
#include <vector>

#include "imgui.h"

#include "../metrics.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

const float* severityColor(const std::string& severity) {
    if (severity == "critical") return palette.statusErr;
    if (severity == "warning") return palette.statusWarn;
    return palette.textDim;
}

bool deliveryStillFailed(const MfgAttentionRow& item, std::size_t index) {
    const MfgAttentionDelivery& record = item.deliveries[index];
    if (record.status != "failed" && record.status != "rate_limited") return false;
    for (std::size_t later = index + 1; later < item.deliveries.size(); ++later) {
        if (item.deliveries[later].channel == record.channel) return false;
    }
    return true;
}

bool drawAttentionItem(const MfgAttentionRow& item, const MfgAttentionPanelInput& input) {
    ImGui::PushID(item.id.c_str());
    ImGui::Separator();
    ImGui::Spacing();

    const float* severity = severityColor(item.severity);
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(severity[0], severity[1], severity[2], severity[3]));
    ImGui::TextUnformatted(item.severity.empty() ? "unknown" : item.severity.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine(0, metrics::U(2));
    ImGui::TextUnformatted(item.reason.empty() ? "Machine attention" : item.reason.c_str());
    if (item.occurrenceCount > 1) {
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("%u occurrences", item.occurrenceCount);
    }

    ImGui::TextWrapped("%s", item.why.empty() ? "No reason was supplied." : item.why.c_str());
    ImGui::TextDisabled("Device: %s", item.device.empty() ? "—" : item.device.c_str());
    ImGui::SameLine(0, metrics::U(3));
    ImGui::TextDisabled("Run: %s", item.run.empty() ? "—" : item.run.c_str());
    ImGui::TextDisabled("Raised at journal seq %llu",
                        static_cast<unsigned long long>(item.raisedSeq));

    for (std::size_t i = 0; i < item.deliveries.size(); ++i) {
        if (!deliveryStillFailed(item, i)) continue;
        const MfgAttentionDelivery& delivery = item.deliveries[i];
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusErr[0], palette.statusErr[1],
                                     palette.statusErr[2], palette.statusErr[3]));
        ImGui::TextWrapped("Not delivered via %s — %s%s%s",
                           delivery.channel.empty() ? "unnamed channel" : delivery.channel.c_str(),
                           delivery.status.c_str(), delivery.error.empty() ? "" : ": ",
                           delivery.error.c_str());
        ImGui::PopStyleColor();
    }

    const bool actorMissing = input.actor.empty();
    const bool sinkMissing = input.acknowledge == nullptr;
    const bool disabled = input.requestInFlight || actorMissing || sinkMissing;
    ImGui::BeginDisabled(disabled);
    const bool clicked = ImGui::Button("Acknowledge");
    ImGui::EndDisabled();

    if (actorMissing) {
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("Actor identity is required to acknowledge.");
    } else if (sinkMissing) {
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("Acknowledgement service is unavailable.");
    } else if (input.requestInFlight) {
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("Acknowledgement request in flight.");
    } else {
        ImGui::SameLine(0, metrics::U(2));
        ImGui::TextDisabled("Records acknowledgement by %.*s; it does not resolve the cause.",
                            static_cast<int>(input.actor.size()), input.actor.data());
    }

    if (clicked && !disabled) {
        input.acknowledge(input.acknowledgeContext, item.id, input.actor);
        ImGui::PopID();
        return true;
    }
    ImGui::PopID();
    return false;
}

}  // namespace

void drawManufactureAttentionPanel(const MfgAttentionPanelInput& input) {
    ImGui::TextUnformatted("Machine attention");
    ImGui::TextDisabled("Items remain here until the journal records acknowledgement or resolution.");
    if (!input.requestError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.statusErr[0], palette.statusErr[1],
                                     palette.statusErr[2], palette.statusErr[3]));
        ImGui::TextWrapped("Acknowledgement was not recorded: %.*s",
                           static_cast<int>(input.requestError.size()), input.requestError.data());
        ImGui::PopStyleColor();
    }

    std::vector<const MfgAttentionRow*> open;
    open.reserve(input.size());
    for (const MfgAttentionRow& item : input) {
        if (item.acknowledgedSeq == 0 && item.resolvedSeq == 0) open.push_back(&item);
    }
    std::stable_sort(open.begin(), open.end(), [](const auto* left, const auto* right) {
        return left->raisedSeq > right->raisedSeq;
    });

    if (open.empty()) {
        ImGui::Spacing();
        ImGui::TextDisabled("No open attention items.");
        return;
    }
    for (const MfgAttentionRow* item : open) {
        if (drawAttentionItem(*item, input)) return;
    }
}

}  // namespace ui
